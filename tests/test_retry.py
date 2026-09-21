"""Tests for the dashboard retry worker (real pipeline, mocked seams)."""

import sqlite3
import threading
import time

import pytest

from bookmedia import repository
from bookmedia.db import init_db
from bookmedia.extract import Post
from bookmedia.retry import AlreadyQueued, InvalidState, RetryQueue


def _db(tmp_path):
    db = str(tmp_path / "retry.db")
    conn = init_db(db)
    conn.close()
    return db


def _failed_row(db_path: str, url: str = "https://x.com/u/status/1",
                chat: str = "123", type: str = "research") -> int:
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        row_id = repository.insert_received(
            conn, platform="x", submitted_url=url, type=type, chat_id=chat
        )
        repository.set_status(conn, row_id, "failed", error="boom")
        return row_id
    finally:
        conn.close()


def _post(url: str = "https://x.com/u/status/1") -> Post:
    return Post(
        platform="x", post_id="1", canonical_url=url, username="u",
        text="t", published_at=None, media=[],
    )


def _wait_status(db_path: str, row_id: int, want: tuple, timeout: float = 8.0) -> str:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        conn = sqlite3.connect(db_path)
        try:
            conn.row_factory = sqlite3.Row
            row = repository.get(conn, row_id)
            last = row["status"] if row else "<missing>"
            if last in want:
                return last
        finally:
            conn.close()
        time.sleep(0.05)
    raise AssertionError(f"row #{row_id} stayed {last}, wanted {want}")


def test_retry_runs_pipeline_and_archives(tmp_path):
    """A failed row is reprocessed end-to-end (fresh conn, own client)."""
    db = _db(tmp_path)
    row_id = _failed_row(db)
    calls = {}

    def fake_extract(url, cookies_path=None):
        calls["extract"] = url
        return _post(url)

    def fake_download(post, workdir):
        calls["download"] = workdir
        return []

    def fake_publish(client, chat_id, post, category, files,
                     on_sent=None, thread_id=None):
        calls["publish"] = (chat_id, category, thread_id)
        on_sent((0, 777, "", "text"))
        return None

    q = RetryQueue(
        db_path=db, temp_dir=str(tmp_path), cookies_path="",
        bot_token="t", extract=fake_extract, download=fake_download,
        publish=fake_publish, client_factory=lambda: object(),
    )
    assert q.retry(row_id) == "queued"
    assert _wait_status(db, row_id, ("archived",)) == "archived"
    assert calls["extract"].endswith("/status/1")
    assert calls["publish"][0] == "123"  # same originating chat
    assert calls["publish"][1] == "research"  # same type
    conn = sqlite3.connect(db)
    try:
        conn.row_factory = sqlite3.Row
        msgs = repository.messages_for(conn, row_id)
        assert [(m["telegram_message_id"], m["media_type"]) for m in msgs] == [(777, "text")]
    finally:
        conn.close()


def test_retry_rejects_archived_and_mid_pipeline(tmp_path):
    db = _db(tmp_path)
    conn = sqlite3.connect(db)
    try:
        conn.row_factory = sqlite3.Row
        done = repository.insert_received(
            conn, platform="x", submitted_url="https://x.com/u/status/9",
            type="unsorted", chat_id="1",
        )
        repository.set_status(conn, done, "archived")
        busy = repository.insert_received(
            conn, platform="x", submitted_url="https://x.com/u/status/8",
            type="unsorted", chat_id="1",
        )
        repository.set_status(conn, busy, "downloading")
    finally:
        conn.close()
    q = RetryQueue(
        db_path=db, temp_dir=str(tmp_path), cookies_path="", bot_token="t",
        extract=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no extract")),
        client_factory=lambda: object(),
    )
    with pytest.raises(InvalidState):
        q.retry(done)
    with pytest.raises(InvalidState):
        q.retry(busy)
    with pytest.raises(InvalidState):
        q.retry(424242)


def test_retry_single_slot_and_idempotent_same_row(tmp_path):
    """Second attempt while the worker is busy gets AlreadyQueued."""
    db = _db(tmp_path)
    first = _failed_row(db, url="https://x.com/u/status/1")
    second = _failed_row(db, url="https://x.com/u/status/2")
    release = threading.Event()

    def slow_extract(url, cookies_path=None):
        release.wait(timeout=10)
        return _post(url)

    q = RetryQueue(
        db_path=db, temp_dir=str(tmp_path), cookies_path="", bot_token="t",
        extract=slow_extract, download=lambda p, w: [],
        publish=lambda *a, **k: None, client_factory=lambda: object(),
    )
    assert q.retry(first) == "queued"
    with pytest.raises(AlreadyQueued):
        q.retry(first)  # same row twice
    with pytest.raises(AlreadyQueued):
        q.retry(second)  # single worker slot
    release.set()
    assert _wait_status(db, first, ("archived", "failed")) in ("archived", "failed")
