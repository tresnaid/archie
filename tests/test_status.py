import json
import sqlite3
from datetime import datetime, timedelta, timezone

from bookmedia import repository
from bookmedia.db import init_db
from bookmedia.status import main


def _db(tmp_path, heartbeat_age_seconds=None):
    db = str(tmp_path / "archive.db")
    conn = init_db(db)
    row_id = repository.insert_received(
        conn, platform="x", submitted_url="https://x.com/u/1", type="research"
    )
    repository.set_status(conn, row_id, "archived")
    conn.close()
    if heartbeat_age_seconds is not None:
        ts = (datetime.now(timezone.utc) - timedelta(seconds=heartbeat_age_seconds)).isoformat()
        (tmp_path / "heartbeat.json").write_text(json.dumps({"ts": ts, "offset": 9, "pid": 1}))
    return db


def test_status_reports_counts_and_fresh_heartbeat(tmp_path, capsys):
    assert main(["--db", _db(tmp_path, heartbeat_age_seconds=10)]) == 0
    out = capsys.readouterr().out
    assert "archived: 1" in out and "heartbeat" in out and "STALE" not in out


def test_status_missing_heartbeat_is_unhealthy(tmp_path, capsys):
    assert main(["--db", _db(tmp_path)]) == 1
    assert "no heartbeat" in capsys.readouterr().out


def test_status_stale_heartbeat_is_unhealthy(tmp_path, capsys):
    assert main(["--db", _db(tmp_path, heartbeat_age_seconds=999)]) == 1
    assert "STALE" in capsys.readouterr().out


def _backdate(db_path, row_id, minutes_ago):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE archives SET updated_at ="
        " strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?) WHERE id = ?",
        (f"-{minutes_ago} minutes", row_id),
    )
    conn.commit()
    conn.close()


def test_status_reports_stuck_rows_and_in_flight_counts(tmp_path, capsys):
    import sqlite3

    db = str(tmp_path / "archive.db")
    conn = init_db(db)
    stuck_id = repository.insert_received(
        conn, platform="x", submitted_url="https://x.com/u/stuck", type="research"
    )
    fresh_id = repository.insert_received(
        conn, platform="x", submitted_url="https://x.com/u/fresh", type="research"
    )
    repository.set_status(conn, stuck_id, "uploading")
    repository.set_status(conn, fresh_id, "uploading")
    conn.close()
    _backdate(db, stuck_id, 30)

    assert main(["--db", db, "--stale-minutes", "5"]) == 1  # no heartbeat file
    out = capsys.readouterr().out
    assert "in-flight (received/extracting/downloading/uploading): 2" in out
    assert "STUCK (5m+ in a non-terminal state): 1" in out
    # Per-row visibility: the stale in-flight row shows its age.
    stuck_line = next(line for line in out.splitlines() if f"#{stuck_id} [uploading]" in line)
    assert "(last update " in stuck_line
    assert f"#{fresh_id} [uploading]" in out


def test_status_filters_rows_by_status(tmp_path, capsys):
    db = _db(tmp_path, heartbeat_age_seconds=10)
    assert main(["--db", db, "--status", "archived", "--limit", "10"]) == 0
    out = capsys.readouterr().out
    assert "[archived]" in out


def test_status_idle_detection_reports_last_activity(tmp_path, capsys):
    import sqlite3

    db = str(tmp_path / "archive.db")
    conn = init_db(db)
    row_id = repository.insert_received(
        conn, platform="x", submitted_url="https://x.com/u/1", type="research"
    )
    conn.close()
    _backdate(db, row_id, 10)

    assert main(["--db", db]) == 1  # no heartbeat file
    out = capsys.readouterr().out
    assert "last archive activity: " in out
    assert "(IDLE)" in out
    assert "STUCK (5m+ in a non-terminal state): 1" in out


def test_status_empty_database_reports_no_activity(tmp_path, capsys):
    db = str(tmp_path / "archive.db")
    init_db(db).close()
    assert main(["--db", db]) == 1  # no heartbeat file
    out = capsys.readouterr().out
    assert "last archive activity: none" in out
    assert "STUCK" not in out
