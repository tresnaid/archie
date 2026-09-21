import os
import sqlite3

from bookmedia import __main__ as entry
from bookmedia import bot as bot_module


def _env(tmp_path, **overrides):
    env = {
        "TELEGRAM_BOT_TOKEN": "secret-token",
        "DATA_DIR": str(tmp_path / "data"),
        "TEMP_DIR": str(tmp_path / "tmp"),
        # Prevent the developer shell's DASHBOARD_PORT (e.g. 8080 in .env)
        # from starting a dashboard thread during unit tests.
        "DASHBOARD_PORT": "0",
    }
    env.update(overrides)
    return env


def test_startup_initializes_db_then_polls(tmp_path, monkeypatch, capsys):
    for k, v in _env(tmp_path).items():
        monkeypatch.setenv(k, v)
    calls = []
    monkeypatch.setattr(bot_module, "run_forever", lambda settings, process: calls.append((settings, process)))
    assert entry.main() == 0
    db = os.path.join(str(tmp_path / "data"), "archive.db")
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM archives").fetchone()[0] == 0
    assert calls and callable(calls[0][1])
    assert callable(calls[0][1])
    assert "secret-token" not in capsys.readouterr().out


def test_startup_starts_dashboard_retry_queue(tmp_path, monkeypatch):
    """Dashboard startup wires a real retry worker, rejecting archived rows."""
    for k, v in _env(tmp_path, DASHBOARD_PORT="18099").items():
        monkeypatch.setenv(k, v)
    from bookmedia import dashboard
    from bookmedia.retry import InvalidState
    started = {}

    real_run = dashboard.run_dashboard

    def spy(*, port, retry_fn, db_path):
        started["retry_fn"] = retry_fn
        started["port"] = port
        return real_run(port=port, retry_fn=retry_fn, db_path=db_path)

    monkeypatch.setattr(dashboard, "run_dashboard", spy)
    monkeypatch.setattr(bot_module, "run_forever", lambda settings, process: None)
    assert entry.main() == 0
    assert started.get("port") == 18099
    assert "retry_fn" in started
    # The DB row exists only after init_db; an archived row must be rejected.
    import sqlite3 as lite
    from bookmedia import repository
    db = os.path.join(str(tmp_path / "data"), "archive.db")
    conn = lite.connect(db)
    row_id = repository.insert_received(
        conn, platform="x", submitted_url="https://x.com/u/status/1",
        type="unsorted", chat_id="1",
    )
    repository.set_status(conn, row_id, "archived")
    conn.close()
    try:
        started["retry_fn"](row_id)
    except InvalidState:
        pass  # expected: already archived, never re-upload
    else:
        raise AssertionError("retry of an archived row must be rejected")


def test_startup_pipeline_client_targets_local_bot_api(tmp_path, monkeypatch):
    """The pipeline upload client must honor LOCAL_BOT_API_HOST.

    Regression: constructing BotClient without local_base_url sent uploads to
    the cloud API, which reads the whole file into RAM (OOM kills on 1 GB+
    media) and caps uploads at 50 MB.
    """
    for k, v in _env(tmp_path, LOCAL_BOT_API_HOST="localhost").items():
        monkeypatch.setenv(k, v)
    captured = {}
    real_client = entry.BotClient

    def spy(*args, **kwargs):
        client = real_client(*args, **kwargs)
        captured["is_local"] = client.is_local
        return client

    monkeypatch.setattr(entry, "BotClient", spy)
    monkeypatch.setattr(bot_module, "run_forever", lambda settings, process: None)
    assert entry.main() == 0
    assert captured["is_local"] is True


def test_startup_pipeline_client_targets_cloud_without_local_host(
    tmp_path, monkeypatch
):
    """Without LOCAL_BOT_API_HOST the pipeline client stays on the cloud API."""
    for k, v in _env(tmp_path).items():
        monkeypatch.setenv(k, v)
    # The dev shell may export LOCAL_BOT_API_HOST; the cloud test needs it gone.
    monkeypatch.delenv("LOCAL_BOT_API_HOST", raising=False)
    captured = {}
    real_client = entry.BotClient

    def spy(*args, **kwargs):
        client = real_client(*args, **kwargs)
        captured["is_local"] = client.is_local
        return client

    monkeypatch.setattr(entry, "BotClient", spy)
    monkeypatch.setattr(bot_module, "run_forever", lambda settings, process: None)
    assert entry.main() == 0
    assert captured["is_local"] is False


def test_startup_missing_var_reports_name_only(tmp_path, monkeypatch, capsys):
    env = _env(tmp_path)
    del env["TELEGRAM_BOT_TOKEN"]
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert entry.main() == 1
    err = capsys.readouterr().err
    assert "TELEGRAM_BOT_TOKEN" in err
