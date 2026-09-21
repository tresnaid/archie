import re
import sqlite3
from datetime import datetime, timezone

import pytest

from bookmedia import repository
from bookmedia.db import init_db
from bookmedia.extract import Post


def _conn(tmp_path):
    return init_db(str(tmp_path / "t.db"))


def _post(**overrides):
    post = {
        "platform": "x",
        "post_id": "p1",
        "canonical_url": "https://x.com/u/status/1",
        "username": "user",
        "text": "caption",
        "published_at": datetime(2026, 9, 10, 14, 52, 30, tzinfo=timezone.utc),
        "media": (),
    }
    post.update(overrides)
    return Post(**post)


def test_insert_received_defaults(tmp_path):
    conn = _conn(tmp_path)
    row_id = repository.insert_received(
        conn, platform="x", submitted_url="https://x.com/u/1", type="research"
    )
    row = repository.get(conn, row_id)
    assert row["status"] == "received"
    assert row["type"] == "research"
    assert row["error"] == ""
    assert row["filename_timestamp_source"] == "archive_time"


def test_find_by_submitted_url_hit_and_miss(tmp_path):
    conn = _conn(tmp_path)
    assert repository.find_by_submitted_url(conn, "https://x.com/u/1") is None
    row_id = repository.insert_received(
        conn, platform="x", submitted_url="https://x.com/u/1", type="unsorted"
    )
    assert repository.find_by_submitted_url(conn, "https://x.com/u/1")["id"] == row_id


def test_find_by_identity_hit_and_miss(tmp_path):
    conn = _conn(tmp_path)
    assert repository.find_by_identity(conn, "123", "x", "p1") is None
    row_id = repository.insert_received(
        conn, platform="x", submitted_url="https://x.com/u/1", type="unsorted",
        chat_id="123",
    )
    repository.save_extracted(conn, row_id, post=_post(), chat_id="123", type="unsorted")
    assert repository.find_by_identity(conn, "123", "x", "p1")["id"] == row_id
    assert repository.find_by_identity(conn, "123", "x", "other") is None
    # Same post_id, different chat — not a duplicate for this chat
    assert repository.find_by_identity(conn, "456", "x", "p1") is None


def test_identity_uniqueness_enforced(tmp_path):
    conn = _conn(tmp_path)
    first = repository.insert_received(
        conn, platform="x", submitted_url="https://x.com/u/1", type="unsorted",
        chat_id="123",
    )
    repository.save_extracted(conn, first, post=_post(), chat_id="123", type="unsorted")
    second = repository.insert_received(
        conn, platform="x", submitted_url="https://x.com/u/2", type="unsorted",
        chat_id="123",
    )
    with pytest.raises(sqlite3.IntegrityError):
        repository.save_extracted(conn, second, post=_post(), chat_id="123", type="unsorted")
    # Same post_id in a different chat is fine
    third = repository.insert_received(
        conn, platform="x", submitted_url="https://x.com/u/3", type="unsorted",
        chat_id="456",
    )
    repository.save_extracted(conn, third, post=_post(), chat_id="456", type="unsorted")
    assert repository.get(conn, third) is not None


def test_set_status_transitions_and_rejects_unknown(tmp_path):
    conn = _conn(tmp_path)
    row_id = repository.insert_received(
        conn, platform="x", submitted_url="https://x.com/u/1", type="unsorted"
    )
    before = repository.get(conn, row_id)["updated_at"]
    repository.set_status(conn, row_id, "extracting")
    row = repository.get(conn, row_id)
    assert row["status"] == "extracting"
    assert row["updated_at"] >= before
    repository.set_status(conn, row_id, "failed", error="boom")
    row = repository.get(conn, row_id)
    assert (row["status"], row["error"]) == ("failed", "boom")
    with pytest.raises(ValueError):
        repository.set_status(conn, row_id, "nope")


def test_save_extracted_records_post_time_source(tmp_path):
    conn = _conn(tmp_path)
    row_id = repository.insert_received(
        conn, platform="x", submitted_url="https://x.com/u/1", type="unsorted"
    )
    repository.save_extracted(conn, row_id, post=_post(), type="research", chat_id="123")
    row = repository.get(conn, row_id)
    assert row["post_id"] == "p1"
    assert row["canonical_url"] == "https://x.com/u/status/1"
    assert row["username"] == "user"
    assert row["text"] == "caption"
    assert row["published_at"] == "2026-09-10T14:52:30Z"
    assert row["filename_timestamp_source"] == "post_time"
    assert row["type"] == "research"


def test_save_extracted_without_timestamp_keeps_archive_time(tmp_path):
    conn = _conn(tmp_path)
    row_id = repository.insert_received(
        conn, platform="x", submitted_url="https://x.com/u/1", type="unsorted"
    )
    repository.save_extracted(conn, row_id, post=_post(published_at=None), type="unsorted", chat_id="123")
    row = repository.get(conn, row_id)
    assert row["published_at"] is None
    assert row["filename_timestamp_source"] == "archive_time"


def test_delete_removes_row(tmp_path):
    conn = _conn(tmp_path)
    row_id = repository.insert_received(
        conn, platform="x", submitted_url="https://x.com/u/1", type="unsorted"
    )
    repository.delete(conn, row_id)
    assert repository.get(conn, row_id) is None


def test_delete_also_removes_message_references(tmp_path):
    """Deleting an archive must not leave orphaned archive_messages rows."""
    conn = _conn(tmp_path)
    row_id = repository.insert_received(
        conn, platform="x", submitted_url="https://x.com/u/1", type="unsorted"
    )
    repository.add_message(conn, row_id, chat_id="123", message_id=7,
                           media_index=1, filename="u__t(1).jpg", media_type="photo")
    assert repository.messages_for(conn, row_id) != []

    repository.delete(conn, row_id)

    assert repository.get(conn, row_id) is None
    assert conn.execute(
        "SELECT COUNT(*) FROM archive_messages WHERE archive_id = ?", (row_id,)
    ).fetchone()[0] == 0
    # The engine agrees there is nothing dangling.
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_mark_archived_sets_status_and_timestamp(tmp_path):
    conn = _conn(tmp_path)
    row_id = repository.insert_received(
        conn, platform="x", submitted_url="https://x.com/u/1", type="unsorted"
    )
    repository.set_status(conn, row_id, "uploading")
    repository.mark_archived(conn, row_id)
    row = repository.get(conn, row_id)
    assert row["status"] == "archived"
    assert row["error"] == ""
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", row["archive_timestamp"])


def test_add_message_records_uploads_in_index_order(tmp_path):
    conn = _conn(tmp_path)
    row_id = repository.insert_received(
        conn, platform="x", submitted_url="https://x.com/u/1", type="unsorted"
    )
    repository.add_message(conn, row_id, chat_id="123", message_id=42,
                           media_index=2, filename="u__t(2).jpg", media_type="photo")
    repository.add_message(conn, row_id, chat_id="123", message_id=41,
                           media_index=1, filename="u__t(1).mp4", media_type="video")
    rows = repository.messages_for(conn, row_id)
    assert [(m["telegram_message_id"], m["media_index"], m["filename"], m["media_type"])
            for m in rows] == [(41, 1, "u__t(1).mp4", "video"),
                               (42, 2, "u__t(2).jpg", "photo")]
    assert all(m["telegram_chat_id"] == "123" for m in rows)


def test_add_message_text_archive_uses_empty_filename(tmp_path):
    conn = _conn(tmp_path)
    row_id = repository.insert_received(
        conn, platform="x", submitted_url="https://x.com/u/1", type="unsorted"
    )
    repository.add_message(conn, row_id, chat_id="123", message_id=7,
                           media_index=0, filename="", media_type="text")
    assert repository.messages_for(conn, row_id)[0]["media_type"] == "text"


def test_find_by_any_url_miss(tmp_path):
    conn = _conn(tmp_path)
    assert repository.find_by_any_url(conn, "https://x.com/u/1") is None


def test_find_by_any_url_matches_canonical_url(tmp_path):
    conn = _conn(tmp_path)
    row_id = repository.insert_received(
        conn, platform="x", submitted_url="https://x.com/u/status/1", type="research",
        chat_id="123",
    )
    repository.save_extracted(conn, row_id, post=_post(), chat_id="123", type="research")
    # Different share form (tracking param) of the same canonical post.
    hit = repository.find_by_any_url(conn, "https://x.com/u/status/1/?utm_source=share", chat_id="123")
    assert hit["id"] == row_id


def test_find_by_any_url_matches_url_shaped_identity(tmp_path):
    conn = _conn(tmp_path)
    row_id = repository.insert_received(
        conn, platform="x", submitted_url="https://x.com/a", type="unsorted",
        chat_id="123",
    )
    repository.save_extracted(
        conn, row_id,
        post=_post(post_id="https://x.com/u/status/9",
                   canonical_url="https://x.com/other"),
        chat_id="123",
        type="unsorted",
    )
    hit = repository.find_by_any_url(conn, "https://x.com/u/status/9", chat_id="123")
    assert hit["id"] == row_id


def test_find_by_status_returns_rows_oldest_first(tmp_path):
    conn = _conn(tmp_path)
    first = repository.insert_received(
        conn, platform="x", submitted_url="https://x.com/u/1", type="unsorted"
    )
    second = repository.insert_received(
        conn, platform="x", submitted_url="https://x.com/u/2", type="unsorted"
    )
    repository.set_status(conn, first, "uploading")
    repository.set_status(conn, second, "uploading")
    rows = repository.find_by_status(conn, "uploading")
    assert [r["id"] for r in rows] == [first, second]
    assert repository.find_by_status(conn, "archived") == []
    with pytest.raises(ValueError):
        repository.find_by_status(conn, "nope")


def test_set_status_truncates_oversized_error(tmp_path):
    conn = _conn(tmp_path)
    row_id = repository.insert_received(
        conn, platform="x", submitted_url="https://x.com/u/1", type="unsorted"
    )
    repository.set_status(conn, row_id, "failed", error="x" * 5000)
    stored = repository.get(conn, row_id)["error"]
    assert len(stored) == 500 and set(stored) == {"x"}
