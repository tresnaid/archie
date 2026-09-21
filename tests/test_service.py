import os
from datetime import datetime, timezone
from datetime import datetime, timezone

import pytest

from bookmedia import repository
from bookmedia.db import init_db
from bookmedia.downloader import Downloaded, DownloadError
from bookmedia.extract import ExtractionError, MediaItem, Post
from bookmedia.parser import ParsedMessage
from bookmedia.service import process_submission
from bookmedia.telegram import TelegramError


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


def _parsed(url="https://x.com/u/status/1", type="research"):
    return ParsedMessage(url=url, type=type)


def _count(conn):
    return conn.execute("SELECT COUNT(*) FROM archives").fetchone()[0]


def _photo(order=0):
    return MediaItem(source_url="", media_type="photo", extension="jpg", order=order)


def _context(tmp_path):
    return {"client": object(), "chat_id": "123", "temp_dir": str(tmp_path / "tmp")}


def _download_two(post, workdir):
    return [
        Downloaded(path="/tmp/u__t(1).jpg", filename="u__t(1).jpg"),
        Downloaded(path="/tmp/u__t(2).mp4", filename="u__t(2).mp4"),
    ]


class FakePublish:
    def __init__(self, fail_after=None, error=None):
        self.calls = []
        self.fail_after = fail_after
        self.error = error

    def __call__(self, client, chat_id, post, category, files, *,
                 on_sent, thread_id=None):
        self.calls.append((chat_id, category, files, thread_id))
        if not files:
            on_sent((0, 100, "", "text"))
            return
        kinds = {"jpg": "photo", "mp4": "video"}
        for position, item in enumerate(files, start=1):
            if self.fail_after is not None and position > self.fail_after:
                raise self.error
            ext = item.filename.rsplit(".", 1)[-1]
            on_sent((position, 100 + position, item.filename, kinds[ext]))


def test_success_archives_media_end_to_end(tmp_path):
    conn = _conn(tmp_path)
    publish = FakePublish()
    reply = process_submission(
        _parsed(), "x", conn=conn, extract=lambda url: _post(media=(_photo(),)),
        download=_download_two, publish=publish, **_context(tmp_path),
    )
    assert "Archived @user" in reply and "research" in reply and "2 media" in reply
    row = repository.find_by_identity(conn, "123", "x", "p1")
    assert row["status"] == "archived"
    assert row["username"] == "user"
    assert row["submitted_url"] == "https://x.com/u/status/1"
    assert row["published_at"] == "2026-09-10T14:52:30Z"
    assert row["filename_timestamp_source"] == "post_time"
    assert row["archive_timestamp"] is not None
    assert publish.calls[0][0] == "123" and publish.calls[0][1] == "research"
    messages = repository.messages_for(conn, row["id"])
    assert [(m["telegram_message_id"], m["media_index"], m["filename"], m["media_type"])
            for m in messages] == [(101, 1, "u__t(1).jpg", "photo"),
                                   (102, 2, "u__t(2).mp4", "video")]
    import os
    assert not os.path.exists(os.path.join(_context(tmp_path)["temp_dir"], f"job-{row['id']}"))


def test_text_only_post_skips_download_and_sends_text_archive(tmp_path):
    conn = _conn(tmp_path)

    def no_download(post, workdir):
        raise AssertionError("text-only posts must skip download")

    publish = FakePublish()
    reply = process_submission(
        _parsed(), "x", conn=conn, extract=lambda url: _post(published_at=None),
        download=no_download, publish=publish, **_context(tmp_path),
    )
    assert "Archived" in reply and "text-only" in reply and "archive time" in reply
    row = repository.find_by_identity(conn, "123", "x", "p1")
    assert row["status"] == "archived"
    assert publish.calls[0][2] == []
    messages = repository.messages_for(conn, row["id"])
    assert [(m["media_index"], m["filename"], m["media_type"]) for m in messages] == [
        (0, "", "text")
    ]


def test_exact_url_duplicate_reports_already_archived(tmp_path):
    conn = _conn(tmp_path)
    row_id = repository.insert_received(
        conn, platform="x", submitted_url="https://x.com/u/status/1", type="research"
    )
    repository.save_extracted(conn, row_id, post=_post(), type="research", chat_id="123")
    repository.set_status(conn, row_id, "archived")

    def fail(url):
        raise AssertionError("must not extract again")

    reply = process_submission(_parsed(), "x", conn=conn, extract=fail,
                               **_context(tmp_path))
    assert "Already archived" in reply and str(row_id) in reply
    assert _count(conn) == 1


def test_same_post_new_url_reports_duplicate_without_new_row(tmp_path):
    conn = _conn(tmp_path)
    row_id = repository.insert_received(
        conn, platform="x", submitted_url="https://x.com/u/status/1", type="research"
    )
    repository.save_extracted(conn, row_id, post=_post(), type="research", chat_id="123")
    repository.set_status(conn, row_id, "archived")
    reply = process_submission(
        _parsed(url="https://x.com/u/status/1?utm_source=share"),
        "x",
        conn=conn,
        extract=lambda url: _post(),
        **_context(tmp_path),
    )
    assert "Already archived" in reply
    assert _count(conn) == 1


def test_failed_submission_retries_on_same_row(tmp_path):
    conn = _conn(tmp_path)
    row_id = repository.insert_received(
        conn, platform="x", submitted_url="https://x.com/u/status/1", type="research"
    )
    repository.set_status(conn, row_id, "failed", error="boom")
    reply = process_submission(
        _parsed(), "x", conn=conn, extract=lambda url: _post(),
        download=lambda post, workdir: [], publish=FakePublish(),
        **_context(tmp_path),
    )
    assert "Archived @user" in reply
    assert _count(conn) == 1
    row = repository.get(conn, row_id)
    assert (row["status"], row["username"]) == ("archived", "user")


def test_non_archived_duplicate_row_is_adopted(tmp_path):
    conn = _conn(tmp_path)
    old_id = repository.insert_received(
        conn, platform="x", submitted_url="https://x.com/u/status/1", type="research"
    )
    repository.save_extracted(conn, old_id, post=_post(), type="research", chat_id="123")
    repository.set_status(conn, old_id, "failed", error="boom")
    reply = process_submission(
        _parsed(url="https://mobile.x.com/u/status/1"),
        "x",
        conn=conn,
        extract=lambda url: _post(),
        download=lambda post, workdir: [],
        publish=FakePublish(),
        **_context(tmp_path),
    )
    assert "Archived @user" in reply
    assert _count(conn) == 1
    row = repository.get(conn, old_id)
    assert (row["status"], row["username"]) == ("archived", "user")


def test_extractor_error_leaves_failed_row_with_error(tmp_path):
    conn = _conn(tmp_path)

    def broken(url):
        raise ExtractionError("x extraction failed: gone")

    reply = process_submission(_parsed(), "x", conn=conn, extract=broken,
                               **_context(tmp_path))
    assert "Couldn't extract" in reply
    row = repository.find_by_submitted_url(conn, "https://x.com/u/status/1")
    assert (row["status"], row["error"]) == ("failed", "x extraction failed: gone")


def test_unexpected_extractor_bug_propagates(tmp_path):
    conn = _conn(tmp_path)

    def buggy(url):
        raise RuntimeError("bug")

    with pytest.raises(RuntimeError):
        process_submission(_parsed(), "x", conn=conn, extract=buggy,
                           **_context(tmp_path))


def test_download_failure_leaves_failed_row_and_cleans_up(tmp_path):
    import os

    conn = _conn(tmp_path)

    def broken(post, workdir):
        os.makedirs(workdir, exist_ok=True)
        raise DownloadError("media download failed: gone")

    reply = process_submission(
        _parsed(), "x", conn=conn, extract=lambda url: _post(media=(_photo(),)),
        download=broken, publish=FakePublish(), **_context(tmp_path),
    )
    assert "Couldn't archive" in reply
    row = repository.find_by_identity(conn, "123", "x", "p1")
    assert row["status"] == "failed" and "gone" in row["error"]
    assert repository.messages_for(conn, row["id"]) == []
    assert not os.path.exists(
        os.path.join(_context(tmp_path)["temp_dir"], f"job-{row['id']}")
    )


def test_partial_upload_keeps_sent_messages_and_marks_failed(tmp_path):
    conn = _conn(tmp_path)
    publish = FakePublish(fail_after=1, error=TelegramError("telegram error: flood"))
    reply = process_submission(
        _parsed(), "x", conn=conn, extract=lambda url: _post(media=(_photo(),)),
        download=_download_two, publish=publish, **_context(tmp_path),
    )
    assert "Couldn't archive" in reply
    row = repository.find_by_identity(conn, "123", "x", "p1")
    assert row["status"] == "failed" and "flood" in row["error"]
    messages = repository.messages_for(conn, row["id"])
    assert [(m["media_index"], m["filename"]) for m in messages] == [(1, "u__t(1).jpg")]


def test_archived_duplicate_via_canonical_url_skips_extraction(tmp_path):
    conn = _conn(tmp_path)
    original = repository.insert_received(
        conn, platform="x", submitted_url="https://x.com/u/status/1", type="research"
    )
    repository.save_extracted(conn, original, post=_post(), type="research", chat_id="123")
    repository.set_status(conn, original, "uploading")
    repository.mark_archived(conn, original)
    calls = []

    def spy_extract(url):
        calls.append(url)
        return _post()

    # Different share form (tracking param) of the already-archived post.
    reply = process_submission(
        _parsed(url="https://x.com/u/status/1/?utm_source=share"), "x",
        conn=conn, extract=spy_extract, download=_download_two,
        publish=FakePublish(), **_context(tmp_path),
    )
    assert calls == []  # dedup happened before extraction
    assert "Already archived" in reply and f"#{original}" in reply
    assert _count(conn) == 1


def test_non_archived_row_found_via_canonical_is_adopted_and_retried(tmp_path):
    conn = _conn(tmp_path)
    old_id = repository.insert_received(
        conn, platform="x", submitted_url="https://x.com/a", type="research"
    )
    repository.save_extracted(conn, old_id, post=_post(), type="research", chat_id="123")
    repository.set_status(conn, old_id, "failed", error="boom")

    reply = process_submission(
        _parsed(url="https://x.com/u/status/1/?utm_source=share"), "x", conn=conn,
        extract=lambda url: _post(), download=lambda post, workdir: [],
        publish=FakePublish(), **_context(tmp_path),
    )

    assert "Archived @user" in reply
    assert _count(conn) == 1
    assert repository.get(conn, old_id)["status"] == "archived"


def test_unexpected_download_error_fails_row_and_cleans_up(tmp_path):
    conn = _conn(tmp_path)

    def haunted(post, workdir):
        os.makedirs(workdir, exist_ok=True)
        raise RuntimeError("ghost")

    reply = process_submission(
        _parsed(), "x", conn=conn, extract=lambda url: _post(media=(_photo(),)),
        download=haunted, publish=FakePublish(), **_context(tmp_path),
    )
    assert "ghost" in reply
    row = repository.find_by_identity(conn, "123", "x", "p1")
    assert row["status"] == "failed" and "ghost" in row["error"]
    assert not os.path.exists(
        os.path.join(_context(tmp_path)["temp_dir"], f"job-{row['id']}")
    )


def test_unexpected_upload_error_fails_row_and_cleans_up(tmp_path):
    conn = _conn(tmp_path)
    publish = FakePublish(fail_after=1, error=RuntimeError("upload ghost"))
    reply = process_submission(
        _parsed(), "x", conn=conn, extract=lambda url: _post(media=(_photo(),)),
        download=_download_two, publish=publish, **_context(tmp_path),
    )
    assert "upload ghost" in reply
    row = repository.find_by_identity(conn, "123", "x", "p1")
    assert row["status"] == "failed"
    assert not os.path.exists(
        os.path.join(_context(tmp_path)["temp_dir"], f"job-{row['id']}")
    )
