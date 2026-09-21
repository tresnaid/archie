"""Tests for the bookmedia embedded monitoring dashboard."""

import json
import socket
import sqlite3
import threading
import time
import urllib.request
import urllib.error

import pytest

from bookmedia import dashboard


def _free_port() -> int:
    """Return a currently-free loopback port for a dashboard test server."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_ready(port: int, timeout: float = 5.0) -> None:
    """Block until the dashboard accepts connections or the timeout expires."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.2)
            try:
                s.connect(("127.0.0.1", port))
                return
            except OSError:
                time.sleep(0.05)
    raise AssertionError(f"dashboard on port {port} did not start in time")


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _make_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS archives "
        "(id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "platform TEXT NOT NULL DEFAULT '', post_id TEXT NOT NULL DEFAULT '', "
        "status TEXT NOT NULL DEFAULT 'received', type TEXT NOT NULL DEFAULT 'unsorted', "
        "error TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT '')"
    )
    conn.execute(
        "INSERT INTO archives (platform, post_id, status, type) VALUES (?, ?, ?, ?)",
        ("instagram", "post123", "failed", "unsorted"),
    )
    conn.execute(
        "INSERT INTO archives (platform, post_id, status, type) VALUES (?, ?, ?, ?)",
        ("twitter", "tweet456", "received", "research"),
    )
    conn.commit()
    return conn


def _post(url: str, body: bytes = b"") -> urllib.request.HTTPResponse:
    """Send a POST request and return the response."""
    req = urllib.request.Request(url, data=body, method="POST")
    return urllib.request.urlopen(req)


# ------------------------------------------------------------------
# run_dashboard / _run_server
# ------------------------------------------------------------------

def test_run_dashboard_disabled_when_port_zero():
    """Port <= 0 must return None (dashboard not started)."""
    thread = dashboard.run_dashboard(port=0, db_path="/nonexistent.db")
    assert thread is None


def test_run_dashboard_disabled_when_no_db():
    """Missing db_path must return None."""
    thread = dashboard.run_dashboard(port=8080, db_path=None)
    assert thread is None


def test_run_dashboard_starts_thread():
    """Enabled port + db_path starts a daemon thread."""
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
        port = _free_port()
    try:
        conn = _make_db(db_path)
        conn.close()
        thread = dashboard.run_dashboard(port=port, db_path=db_path)
        _wait_ready(port)
        assert thread is not None
        assert thread.daemon is True
        thread.join(timeout=3); time.sleep(0.5)
    finally:
        import os
        os.unlink(db_path)


# ------------------------------------------------------------------
# API: /api/archives
# ------------------------------------------------------------------

def test_list_archives_returns_json():
    """GET /api/archives returns all rows as JSON."""
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
        port = _free_port()
    try:
        conn = _make_db(db_path)
        conn.close()

        thread = dashboard.run_dashboard(port=port, db_path=db_path)
        _wait_ready(port)

        import urllib.request
        try:
            resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/api/archives", timeout=5)
            body = json.loads(resp.read().decode())
            assert isinstance(body, list)
            assert len(body) == 2
            assert body[0]["platform"] == "instagram"
            assert body[0]["status"] == "failed"
            assert body[1]["platform"] == "twitter"
            assert body[1]["status"] == "received"
        finally:
            thread.join(timeout=3); time.sleep(0.5)
    finally:
        import os
        os.unlink(db_path)


def test_list_archives_empty_db():
    """Empty database returns an empty list."""
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
        port = _free_port()
    try:
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS archives "
            "(id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "platform TEXT NOT NULL DEFAULT '', post_id TEXT NOT NULL DEFAULT '', "
            "status TEXT NOT NULL DEFAULT 'received', type TEXT NOT NULL DEFAULT 'unsorted', "
            "error TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT '')"
        )
        conn.commit()
        conn.close()

        thread = dashboard.run_dashboard(port=port, db_path=db_path)
        _wait_ready(port)

        import urllib.request
        try:
            resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/api/archives", timeout=5)
            body = json.loads(resp.read().decode())
            assert body == []
        finally:
            thread.join(timeout=3); time.sleep(0.5)
    finally:
        import os
        os.unlink(db_path)


# ------------------------------------------------------------------
# API: /api/health
# ------------------------------------------------------------------

def test_health_endpoint():
    """GET /api/health returns ok + timestamp + port."""
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
        port = _free_port()
    try:
        conn = _make_db(db_path)
        conn.close()

        thread = dashboard.run_dashboard(port=port, db_path=db_path)
        _wait_ready(port)

        import urllib.request
        try:
            resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=5)
            body = json.loads(resp.read().decode())
            assert body["status"] == "ok"
            assert "timestamp" in body
            assert body["dashboard_port"] == port
        finally:
            thread.join(timeout=3); time.sleep(0.5)
    finally:
        import os
        os.unlink(db_path)


# ------------------------------------------------------------------
# API: /api/retry/{id}
# ------------------------------------------------------------------

def test_retry_calls_retry_fn():
    """POST /api/retry/{id} calls the provided retry_fn and reports queued."""
    called_with = {}

    def retry_fn(row_id: int) -> None:
        called_with["id"] = row_id
        return "queued"

    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
        port = _free_port()
    try:
        conn = _make_db(db_path)
        conn.close()

        thread = dashboard.run_dashboard(
            port=port, db_path=db_path, retry_fn=retry_fn
        )
        _wait_ready(port)

        # Use POST to hit the retry endpoint
        try:
            resp = _post(f"http://127.0.0.1:{port}/api/retry/42")
            body = json.loads(resp.read().decode())
            assert called_with == {"id": 42}
            assert body["result"] == "queued"
        finally:
            thread.join(timeout=3); time.sleep(0.5)
    finally:
        import os
        os.unlink(db_path)


def test_retry_rejection_maps_to_http_code():
    """AlreadyQueued -> 409, InvalidState -> 400 (no silent 500s)."""
    from bookmedia.retry import AlreadyQueued, InvalidState

    def busy(row_id: int):
        raise AlreadyQueued("another dashboard retry is already running")

    def bad(row_id: int):
        raise InvalidState("archive #7 is already archived")

    import tempfile
    for fn, code in ((busy, 409), (bad, 400)):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
            port = _free_port()
        try:
            from bookmedia.dashboard import _Handler
            _Handler.retry_error_types = (AlreadyQueued, InvalidState)
            conn = _make_db(db_path)
            conn.close()

            thread = dashboard.run_dashboard(
                port=port, db_path=db_path, retry_fn=fn
            )
            _wait_ready(port)

            try:
                _post(f"http://127.0.0.1:{port}/api/retry/7")
                raise AssertionError(f"expected HTTP {code}")
            except urllib.error.HTTPError as e:
                assert e.code == code, f"got {e.code}, wanted {code}"
            finally:
                thread.join(timeout=3); time.sleep(0.5)
        finally:
            import os
            os.unlink(db_path)


def test_retry_without_fn_returns_503():
    """POST /api/retry/{id} without retry_fn returns 503."""
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
        port = _free_port()
    try:
        conn = _make_db(db_path)
        conn.close()

        thread = dashboard.run_dashboard(port=port, db_path=db_path)
        _wait_ready(port)

        try:
            _post(f"http://127.0.0.1:{port}/api/retry/1")
        except urllib.error.HTTPError as e:
            # 503 is expected
            assert e.code == 503
        finally:
            thread.join(timeout=3); time.sleep(0.5)
    finally:
        import os
        os.unlink(db_path)


# ------------------------------------------------------------------
# API: /api/cancel/{id}
# ------------------------------------------------------------------

def test_cancel_marks_as_failed():
    """POST /api/cancel/{id} marks the row as failed."""
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
        port = _free_port()
    try:
        conn = _make_db(db_path)
        # Set one row to in-flight so we can test cancel
        conn.execute(
            "UPDATE archives SET status = 'downloading' WHERE id = 1"
        )
        conn.commit()
        conn.close()

        thread = dashboard.run_dashboard(
            port=port, db_path=db_path,
            retry_fn=lambda rid: None,
        )
        _wait_ready(port)

        # Use POST to hit the cancel endpoint
        try:
            _post(f"http://127.0.0.1:{port}/api/cancel/1")
            # Verify the row is now 'failed'
            verify_conn = sqlite3.connect(db_path)
            row = verify_conn.execute(
                "SELECT status FROM archives WHERE id = 1"
            ).fetchone()
            verify_conn.close()
            assert row[0] == "failed"
        finally:
            thread.join(timeout=3); time.sleep(0.5)
    finally:
        import os
        os.unlink(db_path)


# ------------------------------------------------------------------
# HTML endpoint
# ------------------------------------------------------------------

def test_html_endpoint_returns_200():
    """GET / returns 200 with HTML containing the dashboard port."""
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
        port = _free_port()
    try:
        conn = _make_db(db_path)
        conn.close()

        thread = dashboard.run_dashboard(port=port, db_path=db_path)
        _wait_ready(port)

        import urllib.request
        try:
            resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5)
            assert resp.status == 200
            body = resp.read().decode()
            assert "bookmedia Dashboard" in body
            assert f"localhost:{port}" in body
        finally:
            thread.join(timeout=3); time.sleep(0.5)
    finally:
        import os
        os.unlink(db_path)


# ------------------------------------------------------------------
# Filter + sort controls (client-side; asserted in the served markup)
# ------------------------------------------------------------------

def test_html_has_filter_and_sort_controls():
    """The dashboard markup exposes filter and sort controls."""
    body = dashboard._HTML.replace("{port}", "8080")
    for control in (
        'id="filter-status"',
        'id="filter-platform"',
        'id="filter-type"',
        'id="filter-q"',
        'id="sort-column"',
        'id="sort-order"',
        'id="count"',
    ):
        assert control in body, control
    # every archive status is selectable
    for status in ("received", "extracting", "downloading", "uploading",
                   "archived", "failed"):
        assert f'value="{status}"' in body, status
    # every sortable column is offered in both the dropdown and the headers
    for column in ("id", "platform", "post_id", "status", "type", "updated_at"):
        assert f'<option value="{column}">' in body, column
        assert f'data-sort="{column}"' in body, column


def test_html_filter_sort_wiring_is_present():
    """Handlers, URL persistence, and escaping are wired in the markup."""
    body = dashboard._HTML
    for hook in (
        "onControlChange()", "resetControls()", "readStateFromUrl()",
        "syncStateToUrl()", "applyStateToControls()", "visibleRows(",
        "function matches(", "function compare(", "function esc(",
    ):
        assert hook in body, hook
    # filters survive refresh/bookmarks via the URL query string
    assert "history.replaceState" in body
    assert "URLSearchParams" in body