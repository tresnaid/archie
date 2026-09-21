"""Embedded monitoring dashboard for bookmedia archive jobs.

Runs a lightweight HTTP server (stdlib http.server, no new deps) on
localhost only. Provides:

  GET /                      — HTML dashboard table
  GET /api/archives          — JSON list of archives
  POST /api/retry/{id}       — run a real retry through the archive pipeline
  POST /api/cancel/{id}      — mark an in-flight archive failed
  GET /api/health            — heartbeat + poll loop status

The ``retry_fn`` callable runs the real pipeline in a background worker and
returns ``"queued"``. It raises ``AlreadyQueued`` (mapped to HTTP 409) when
the single worker slot is busy, or ``InvalidState`` (HTTP 400) for missing,
archived, or mid-pipeline rows.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Callable

from . import repository

log = logging.getLogger("bookmedia")

# ------------------------------------------------------------------
# HTML template (regular string, port substituted via .replace())
# ------------------------------------------------------------------

_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>bookmedia Dashboard</title>
  <style>
    body{font-family:system-ui,Arial,sans-serif;margin:2rem;background:#f5f5f5;color:#111}
    .container{max-width:960px;margin:0 auto}
    h1{font-size:1.5rem;margin-bottom:1rem}
    .status-badge{display:inline-block;padding:2px 6px;border-radius:3px;font-size:0.75rem;font-weight:bold;margin-right:2px}
    .status-received{background:#e3f2fd;color:#1565c0}
    .status-extracting{background:#fff3e0;color:#f57f17}
    .status-downloading{background:#fff3e0;color:#f57f17}
    .status-uploading{background:#fff3e0;color:#f57f17}
    .status-archived{background:#e8f5e9;color:#2e7d32}
    .status-failed{background:#ffebee;color:#c62828}
    table{width:100%;border-collapse:collapse;margin-top:1rem}
    th,td{border:1px solid #ddd;padding:6px 8px;text-align:left;font-size:0.875rem}
    th{background:#fafafa}
    tr:nth-child(even){background:#fafafa}
    .no-data{padding:2rem;color:#777}
    .controls{margin-top:1rem;display:flex;gap:0.5rem;flex-wrap:wrap}
    .btn{padding:4px 8px;font-size:0.75rem;cursor:pointer;background:#1976d2;color:#fff;border:none;border-radius:3px}
    .btn:hover{background:#1565c0}
    .btn-danger{background:#c62828}
    .btn-danger:hover{background:#b71c1c}
    .endpoint{font-family:monospace;font-size:0.75rem;background:#e3e3e3;padding:2px 4px;border-radius:3px;margin:2px 0}
    .filters{margin-top:1rem;display:flex;gap:0.5rem;flex-wrap:wrap;align-items:center;
             background:#fff;border:1px solid #ddd;border-radius:4px;padding:0.5rem 0.75rem}
    .filters label{font-size:0.75rem;color:#555;display:flex;flex-direction:column;gap:2px}
    .filters select,.filters input{font-size:0.8rem;padding:3px 4px;border:1px solid #bbb;border-radius:3px;background:#fff;color:#111}
    .filters input{min-width:10rem}
    .count{font-size:0.75rem;color:#555;margin-left:auto}
    th.sortable{cursor:pointer;user-select:none;white-space:nowrap}
    th.sortable:hover{background:#f0f0f0}
    th.sorted{background:#e8e8e8}
  </style>
</head>
<body>
<div class="container">
  <h1>bookmedia Archive Dashboard</h1>
  <p>Dashboard active on <span id="port">localhost:{port}</span></p>
  <div class="controls">
    <button class="btn" onclick="window.location='/api/archives'">Refresh JSON</button>
    <button class="btn" onclick="resetControls()">Reset filters</button>
  </div>
  <div class="filters">
    <label>Status
      <select id="filter-status" onchange="onControlChange()">
        <option value="">all</option>
        <option value="received">received</option>
        <option value="extracting">extracting</option>
        <option value="downloading">downloading</option>
        <option value="uploading">uploading</option>
        <option value="archived">archived</option>
        <option value="failed">failed</option>
      </select>
    </label>
    <label>Platform
      <select id="filter-platform" onchange="onControlChange()">
        <option value="">all</option>
      </select>
    </label>
    <label>Type
      <select id="filter-type" onchange="onControlChange()">
        <option value="">all</option>
      </select>
    </label>
    <label>Search
      <input id="filter-q" type="search" placeholder="#id, post id, error…"
             oninput="onControlChange()">
    </label>
    <label>Sort
      <select id="sort-column" onchange="onControlChange()">
        <option value="id">#</option>
        <option value="platform">Platform</option>
        <option value="post_id">Post ID</option>
        <option value="status">Status</option>
        <option value="type">Type</option>
        <option value="updated_at">Updated</option>
      </select>
    </label>
    <label>Order
      <select id="sort-order" onchange="onControlChange()">
        <option value="desc">descending</option>
        <option value="asc">ascending</option>
      </select>
    </label>
    <span class="count" id="count"></span>
  </div>
  <table id="archives">
    <thead>
      <tr>
        <th class="sortable" data-sort="id">#</th>
        <th class="sortable" data-sort="platform">Platform</th>
        <th class="sortable" data-sort="post_id">Post ID</th>
        <th class="sortable" data-sort="status">Status</th>
        <th class="sortable" data-sort="type">Type</th>
        <th>Error</th>
        <th class="sortable" data-sort="updated_at">Updated</th>
        <th></th>
      </tr>
    </thead>
    <tbody></tbody>
  </table>
</div>

<script>
let refreshInterval = null;
let allRows = [];

// Filter + sort state. Defaults match the API order (id ascending), so an
// untouched dashboard shows the same rows and order as the API returns.
const state = {
  status: '', platform: '', type: '', q: '',
  sort: 'id', order: 'asc'
};

function esc(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function formatStatus(s) {
  const map = {
    'received': 'status-received',
    'extracting': 'status-extracting',
    'downloading': 'status-downloading',
    'uploading': 'status-uploading',
    'archived': 'status-archived',
    'failed': 'status-failed'
  };
  return map[s] || '';
}

// ------------------------------------------------------------------
// URL <-> state: filters and sort survive refresh, F5, and bookmarks
// ------------------------------------------------------------------

function readStateFromUrl() {
  const p = new URLSearchParams(window.location.search);
  ['status', 'platform', 'type', 'q', 'sort', 'order'].forEach(k => {
    const v = p.get(k);
    if (v !== null) state[k] = v;
  });
  if (!SORTABLE.includes(state.sort)) state.sort = 'id';
  if (state.order !== 'asc' && state.order !== 'desc') state.order = 'asc';
}

function syncStateToUrl() {
  const p = new URLSearchParams();
  ['status', 'platform', 'type', 'q'].forEach(k => { if (state[k]) p.set(k, state[k]); });
  if (state.sort !== 'id') p.set('sort', state.sort);
  if (state.order !== 'asc') p.set('order', state.order);
  const qs = p.toString();
  history.replaceState(null, '', qs ? '?' + qs : window.location.pathname);
}

function applyStateToControls() {
  document.querySelector('#filter-status').value = state.status;
  document.querySelector('#filter-platform').value = state.platform;
  document.querySelector('#filter-type').value = state.type;
  document.querySelector('#filter-q').value = state.q;
  document.querySelector('#sort-column').value = state.sort;
  document.querySelector('#sort-order').value = state.order;
}

function readControlsIntoState() {
  state.status = document.querySelector('#filter-status').value;
  state.platform = document.querySelector('#filter-platform').value;
  state.type = document.querySelector('#filter-type').value;
  state.q = document.querySelector('#filter-q').value.trim();
  state.sort = document.querySelector('#sort-column').value;
  state.order = document.querySelector('#sort-order').value;
}

function onControlChange() {
  readControlsIntoState();
  syncStateToUrl();
  renderRows(allRows);
}

function resetControls() {
  state.status = ''; state.platform = ''; state.type = ''; state.q = '';
  state.sort = 'id'; state.order = 'asc';
  syncStateToUrl();
  applyStateToControls();
  renderRows(allRows);
}

// ------------------------------------------------------------------
// Filtering + sorting (pure functions over the fetched rows)
// ------------------------------------------------------------------

const SORTABLE = ['id', 'platform', 'post_id', 'status', 'type', 'updated_at'];

function uniqSorted(values) {
  return Array.from(new Set(values.filter(v => v !== '' && v != null))).sort();
}

function buildOptions(selectId, values, keep) {
  const select = document.querySelector(selectId);
  select.innerHTML = '<option value="">all</option>' +
    values.map(v => '<option value="' + esc(v) + '">' + esc(v) + '</option>').join('');
  select.value = values.includes(keep) ? keep : '';
  return select.value;
}

function refreshOptions(rows) {
  // A choice that disappears from the data (e.g. a type filter cleared by
  // re-typing) is dropped rather than silently matching nothing.
  state.platform = buildOptions('#filter-platform',
    uniqSorted(rows.map(r => r.platform)), state.platform);
  state.type = buildOptions('#filter-type',
    uniqSorted(rows.map(r => r.type)), state.type);
}

function matches(r) {
  if (state.status && r.status !== state.status) return false;
  if (state.platform && r.platform !== state.platform) return false;
  if (state.type && r.type !== state.type) return false;
  if (state.q) {
    const needle = state.q.toLowerCase();
    const haystack = [r.id, r.platform, r.post_id, r.status, r.type, r.error]
      .join(' ').toLowerCase();
    if (haystack.indexOf(needle) === -1) return false;
  }
  return true;
}

function compare(a, b) {
  let result;
  if (state.sort === 'id') {
    result = (a.id || 0) - (b.id || 0);
  } else {
    // ISO timestamps and text order correctly as strings; post ids may be
    // numeric, so compare numerically when both sides are all digits.
    const x = a[state.sort] == null ? '' : String(a[state.sort]);
    const y = b[state.sort] == null ? '' : String(b[state.sort]);
    if (/^[0-9]+$/.test(x) && /^[0-9]+$/.test(y)) {
      result = Number(x) - Number(y);
    } else {
      result = x.localeCompare(y);
    }
  }
  return state.order === 'asc' ? result : -result;
}

function visibleRows(rows) {
  return rows.filter(matches).sort(compare);
}

function markSortedHeader() {
  document.querySelectorAll('th.sortable').forEach(th => {
    const arrow = th.dataset.sort === 'id' ? '#' : th.textContent.replace(/ [▲▼]$/, '');
    th.classList.toggle('sorted', th.dataset.sort === state.sort);
    th.textContent = arrow + (th.dataset.sort === state.sort
      ? (state.order === 'asc' ? ' ▲' : ' ▼') : '');
  });
}

function renderRows(rows) {
  allRows = rows || [];
  refreshOptions(allRows);
  const tbody = document.querySelector('#archives tbody');
  const shown = visibleRows(allRows);
  document.querySelector('#count').textContent =
    'showing ' + shown.length + ' of ' + allRows.length;
  markSortedHeader();
  if (shown.length === 0) {
    const message = allRows.length === 0
      ? 'No archive rows found'
      : 'No rows match the current filters';
    tbody.innerHTML = '<tr><td colspan="8" class="no-data">' + message + '</td></tr>';
    return;
  }
  tbody.innerHTML = shown.map(r => {
    const statusCls = formatStatus(r.status);
    const ageSec = Math.floor((Date.now() - new Date(r.updated_at).getTime()) / 1000);
    const age = ageSec < 60 ? ageSec + 's ago' :
                ageSec < 3600 ? Math.floor(ageSec/60) + 'm ago' :
                Math.floor(ageSec/3600) + 'h ago';
    const errorDisplay = r.error
      ? '<div style="color:#c62828;font-size:0.75rem;margin-top:2px;">error: '
        + esc(r.error) + '</div>'
      : '';
    const retryBtn = r.status === 'failed' || r.status === 'received'
      ? '<button class="btn btn-danger" onclick="retry(' + r.id + ')">retry</button> '
      : '';
    const cancelBtn = r.status !== 'failed' && r.status !== 'archived'
      ? '<button class="btn btn-danger" onclick="cancel(' + r.id + ')">cancel</button>'
      : '';
    return '<tr>' +
      '<td>' + r.id + '</td>' +
      '<td>' + (esc(r.platform) || '-') + '</td>' +
      '<td>' + (esc(r.post_id) || '-') + '</td>' +
      '<td><span class="status-badge ' + statusCls + '">' + esc(r.status) + '</span></td>' +
      '<td>' + (esc(r.type) || '-') + '</td>' +
      '<td>' + errorDisplay + '</td>' +
      '<td title="' + esc(r.updated_at) + '">' + age + '</td>' +
      '<td>' + retryBtn + cancelBtn + '</td>' +
    '</tr>';
  }).join('');
}

function retry(id) {
  if (!confirm('Retry archive #' + id + ' now? This downloads and uploads it again.')) return;
  fetch('/api/retry/' + id, {method: 'POST'})
    .then(r => r.json().then(d => ({ok: r.ok, d: d})))
    .then(res => { alert(res.d.msg || res.d.error || 'Retry sent'); loadArchives(); })
    .catch(e => alert('Error: ' + e));
}

function cancel(id) {
  if (!confirm('Cancel archive #' + id + '?')) return;
  fetch('/api/cancel/' + id, {method: 'POST'})
    .then(r => r.json().then(d => ({ok: r.ok, d: d})))
    .then(res => { alert(res.d.msg || res.d.error || 'Cancel sent'); loadArchives(); })
    .catch(e => alert('Error: ' + e));
}

function loadArchives() {
  // Re-rendering keeps the current filters: only the data is replaced.
  fetch('/api/archives')
    .then(r => r.json())
    .then(renderRows)
    .catch(e => console.error('load error:', e));
}

function startRefresh() {
  refreshInterval = setInterval(loadArchives, 5000);
  loadArchives();
}

function stopRefresh() {
  if (refreshInterval) clearInterval(refreshInterval);
  refreshInterval = null;
}

// Clicking a column header sorts by it; clicking again flips the direction.
document.querySelectorAll('th.sortable').forEach(th => {
  th.addEventListener('click', () => {
    const key = th.dataset.sort;
    if (state.sort === key) {
      state.order = state.order === 'asc' ? 'desc' : 'asc';
    } else {
      state.sort = key;
      state.order = key === 'id' ? 'desc' : 'asc';
    }
    document.querySelector('#sort-column').value = state.sort;
    document.querySelector('#sort-order').value = state.order;
    syncStateToUrl();
    renderRows(allRows);
  });
});

window.addEventListener('beforeunload', stopRefresh);
readStateFromUrl();
applyStateToControls();
startRefresh();
</script>
</body>
</html>"""


# ------------------------------------------------------------------
# Request handler
# ------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    """Minimal request handler for the bookmedia dashboard."""

    dashboard_port: int = 0
    retry_fn: Callable[[int], None] | None = None
    db_path: str = ""
    retry_error_types: tuple = ()

    def log_message(self, format, *args):  # silence default stderr logging
        pass

    def _send_json(self, code: int, obj: object) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode())

    def _send_html(self, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(self._html_render().encode())

    def _html_render(self) -> str:
        """Render the HTML template with the port substituted."""
        return _HTML.replace("{port}", str(self.dashboard_port))

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        path = self.path.split("?")[0]

        if path == "/":
            self._send_html()
        elif path == "/api/health":
            self._send_json(200, {
                "status": "ok",
                "timestamp": time.time(),
                "dashboard_port": self.dashboard_port,
            })
        elif path == "/api/archives":
            self._send_json(200, self._get_archives())
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        path = self.path.split("?")[0]

        # Extract id from /api/retry/{id} or /api/cancel/{id}
        if path.startswith("/api/retry/"):
            try:
                row_id = int(path[len("/api/retry/"):])
            except ValueError:
                self._send_json(400, {"error": "invalid id"})
                return
            if self.retry_fn is None:
                self._send_json(503, {"error": "retry not configured"})
                return
            try:
                # Use type(self).retry_fn to avoid bound method behavior:
                # self.retry_fn creates a bound method that passes self as first arg
                result = type(self).retry_fn(row_id)
                self._send_json(200, {"msg": f"retry queued for #{row_id}",
                                      "result": result})
            except Exception as exc:  # noqa: BLE001
                code = 500
                for cls in type(self).retry_error_types:
                    if isinstance(exc, cls):
                        code = int(getattr(exc, "http_code", 400) or 400)
                        break
                self._send_json(code, {"error": str(exc)})
            return

        if path.startswith("/api/cancel/"):
            try:
                row_id = int(path[len("/api/cancel/"):])
            except ValueError:
                self._send_json(400, {"error": "invalid id"})
                return
            # Cancel = mark as failed via repository
            try:
                conn = sqlite3.connect(self.db_path)
                repository.set_status(conn, row_id, "failed", error="cancelled via dashboard")
                conn.close()
                self._send_json(200, {"msg": "cancelled #" + str(row_id)})
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"error": str(exc)})
            return

        self._send_json(404, {"error": "not found"})


# ------------------------------------------------------------------
# Archive listing
# ------------------------------------------------------------------

    def _get_archives(self) -> list[dict]:
        """Query all archives ordered by id desc."""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                "SELECT id, platform, post_id, status, type, error, updated_at"
                " FROM archives ORDER BY id ASC"
            )
            rows = cur.fetchall()
            conn.close()
            return [
                {
                    "id": row["id"],
                    "platform": row["platform"] or "",
                    "post_id": row["post_id"] or "",
                    "status": row["status"],
                    "type": row["type"] or "",
                    "error": row["error"] or "",
                    "updated_at": row["updated_at"] or "",
                }
                for row in rows
            ]
        except Exception as exc:  # noqa: BLE001
            return [{"error": str(exc)}]


# ------------------------------------------------------------------
# Server lifecycle
# ------------------------------------------------------------------

def _run_server(
    port: int,
    *,
    retry_fn: Callable[[int], None] | None,
    db_path: str,
) -> None:
    """Run the dashboard HTTP server in a daemon thread."""

    _Handler.dashboard_port = port
    _Handler.retry_fn = retry_fn
    _Handler.db_path = db_path

    try:
        server = HTTPServer(("127.0.0.1", port), _Handler)
    except OSError as exc:
        log.warning("dashboard failed to bind port %d: %s", port, exc)
        return
    try:
        server.serve_forever()
    except OSError:
        pass
    finally:
        server.server_close()


def run_dashboard(
    *,
    port: int = 0,
    retry_fn: Callable[[int], None] | None = None,
    db_path: str | None = None,
) -> threading.Thread | None:
    """Start the dashboard server.

    Returns a Thread running the HTTP server, or None if disabled
    (port <= 0 or db_path is None).
    """

    if port <= 0 or db_path is None:
        return None

    thread = threading.Thread(
        target=_run_server,
        args=(port,),
        kwargs={"retry_fn": retry_fn, "db_path": db_path},
        daemon=True,
    )
    thread.start()
    return thread