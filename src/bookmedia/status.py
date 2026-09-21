"""Status CLI: bot liveness plus archive summary.

Usage: ``PYTHONPATH=src python3 -m bookmedia.status --db ./data/archive.db``.
Exit 0 when the heartbeat is fresh, 1 when it is missing or stale.

In-flight rows (received/extracting/downloading/uploading) older than
``--stale-minutes`` are reported as stuck so a wedged pipeline is visible.
Idle detection reports how long since the last archive row change.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

_STALE_AFTER_SECONDS = 180
_TERMINAL_STATUSES = ("archived", "failed")
_DEFAULT_STALE_MINUTES = 5


def _heartbeat(db_path: str) -> tuple[str, int]:
    try:
        data = json.loads((Path(db_path).parent / "heartbeat.json").read_text())
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(data["ts"])).total_seconds()
        return f"heartbeat: {data['ts']} ({int(age)}s ago, offset {data.get('offset')})", age
    except (OSError, ValueError, KeyError) as exc:
        return f"no heartbeat ({type(exc).__name__})", -1


def _row_age_seconds(db_path: str) -> int | None:
    """Seconds since the newest ``updated_at`` across archive rows."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT MAX(updated_at) FROM archives"
        ).fetchone()
        if row is None or row[0] is None:
            return None
        latest = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
        return int((datetime.now(timezone.utc) - latest).total_seconds())
    except (sqlite3.Error, ValueError):
        return None
    finally:
        conn.close()


def _summary(db_path: str, *, stale_minutes: float, status: str | None,
             limit: int) -> list[str]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        counts = dict(conn.execute("SELECT status, COUNT(*) FROM archives GROUP BY status"))
        total = sum(counts.values())
        in_flight = sum(n for s, n in counts.items() if s not in _TERMINAL_STATUSES)
        lines = [f"archives: {total} total"] + [
            f"  {status_}: {n}" for status_, n in sorted(counts.items())
        ]
        lines.append(f"  in-flight (received/extracting/downloading/uploading): {in_flight}")

        stale_cutoff = (
            f"strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-{float(stale_minutes)} minutes')"
        )
        stuck = conn.execute(
            "SELECT COUNT(*) FROM archives"
            f" WHERE status NOT IN ('archived', 'failed')"
            f" AND updated_at < {stale_cutoff}"
        ).fetchone()[0]
        if stuck:
            lines.append(f"  STUCK ({int(stale_minutes)}m+ in a non-terminal state): {stuck}")

        idle = _row_age_seconds(db_path)
        if idle is None:
            lines.append("last archive activity: none")
        else:
            flag = " (IDLE)" if idle > _STALE_AFTER_SECONDS else ""
            lines.append(f"last archive activity: {idle}s ago{flag}")

        query = (
            "SELECT id, status, type, platform, substr(error, 1, 80),"
            " submitted_url, updated_at FROM archives"
        )
        params: list = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        recent = conn.execute(query, params).fetchall()
        for row in recent:
            age = _age_suffix(row[6])
            lines.append(
                f"  #{row[0]} [{row[1]}] ({row[3]}, type '{row[2]}'){age} {row[5]}"
                + (f" error: {row[4]}" if row[4] else "")
            )
        messages = conn.execute("SELECT COUNT(*) FROM archive_messages").fetchone()[0]
        lines.append(f"telegram messages recorded: {messages}")
        return lines
    finally:
        conn.close()


def _age_suffix(updated_at: str) -> str:
    try:
        age = (datetime.now(timezone.utc)
               - datetime.fromisoformat(updated_at.replace("Z", "+00:00"))).total_seconds()
        return f" (last update {int(age)}s ago)"
    except ValueError:
        return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="bookmedia archive status")
    parser.add_argument(
        "--db",
        default=os.path.join(os.environ.get("DATA_DIR", "/app/data"), "archive.db"),
    )
    parser.add_argument(
        "--status",
        choices=["received", "extracting", "downloading", "uploading",
                 "archived", "failed"],
        default=None,
        help="list only rows in this state",
    )
    parser.add_argument(
        "--limit", type=int, default=5, help="rows to list (default 5)"
    )
    parser.add_argument(
        "--stale-minutes", type=float, default=_DEFAULT_STALE_MINUTES,
        help="non-terminal rows unchanged this long are STUCK (default 5)",
    )
    args = parser.parse_args(argv)
    report, age = _heartbeat(args.db)
    print(report)
    if age < 0 or age > _STALE_AFTER_SECONDS:
        print("bot: STALE (not polling)" if age >= 0 else "bot: no heartbeat yet")
        healthy = False
    else:
        print("bot: polling")
        healthy = True
    try:
        print("\n".join(_summary(
            args.db,
            stale_minutes=args.stale_minutes,
            status=args.status,
            limit=args.limit,
        )))
    except sqlite3.Error as exc:
        print(f"database unreadable: {exc}")
        return 1
    return 0 if healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
