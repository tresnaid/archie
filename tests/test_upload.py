from datetime import datetime, timezone

import pytest

from bookmedia.downloader import Downloaded
from bookmedia.extract import Post
from bookmedia.upload import (
    UploadError,
    check_upload_sizes,
    max_upload_bytes,
    media_kind,
    publish,
)


def _post(**overrides):
    base = {
        "platform": "tiktok",
        "post_id": "123",
        "canonical_url": "https://www.tiktok.com/@user/video/123",
        "username": "user",
        "text": "caption",
        "published_at": datetime(2026, 9, 10, 14, 52, 30, tzinfo=timezone.utc),
        "media": [],
    }
    base.update(overrides)
    return Post(**base)


class FakeClient:
    def __init__(self):
        self.calls = []
        self._next_id = 40

    def _id(self):
        self._next_id += 1
        return self._next_id

    def send_photo(self, chat_id, path, caption="", thread_id=None):
        self.calls.append(("photo", chat_id, path, caption, thread_id))
        return self._id()

    def send_video(self, chat_id, path, caption="", thread_id=None):
        self.calls.append(("video", chat_id, path, caption, thread_id))
        return self._id()

    def send_document(self, chat_id, path, caption="", thread_id=None):
        self.calls.append(("document", chat_id, path, caption, thread_id))
        return self._id()

    def send_message(self, chat_id, text, thread_id=None):
        self.calls.append(("message", chat_id, text, thread_id))
        return {"message_id": self._id()}


def test_media_kind_routes_by_extension():
    assert media_kind("a.jpg") == "photo"
    assert media_kind("a.PNG") == "photo"
    assert media_kind("a.mp4") == "video"
    assert media_kind("a.webp") == "document"
    assert media_kind("a") == "document"


def test_publish_media_routes_and_captions_first_only():
    client = FakeClient()
    sent = []
    files = [
        Downloaded(path="/tmp/u__t(1).jpg", filename="u__t(1).jpg"),
        Downloaded(path="/tmp/u__t(2).mp4", filename="u__t(2).mp4"),
        Downloaded(path="/tmp/u__t(3).webp", filename="u__t(3).webp"),
    ]
    publish(client, "123", _post(), "research", files, on_sent=sent.append)
    kinds = [c[0] for c in client.calls]
    assert kinds == ["photo", "video", "document"]
    assert all(c[1] == "123" for c in client.calls)
    assert client.calls[0][3] != ""  # caption on first
    assert client.calls[1][3] == "" and client.calls[2][3] == ""
    assert "@user" in client.calls[0][3] and "caption" in client.calls[0][3]
    assert [s[0] for s in sent] == [1, 2, 3]  # 1-based media index
    assert sent[0][1:] == (41, "u__t(1).jpg", "photo")
    assert sent[1][1:] == (42, "u__t(2).mp4", "video")
    assert sent[2][1:] == (43, "u__t(3).webp", "document")
    assert all(c[4] is None for c in client.calls)  # no topic by default


def test_publish_targets_forum_topic():
    """Media and text archives land in the topic the link came from."""
    client = FakeClient()
    sent = []
    files = [Downloaded(path="/tmp/u__t(1).jpg", filename="u__t(1).jpg")]
    publish(client, "123", _post(), "research", files,
            on_sent=sent.append, thread_id=55)
    assert client.calls[0][4] == 55
    publish(client, "123", _post(), "research", [], on_sent=sent.append,
            thread_id=55)
    assert client.calls[1][0] == "message" and client.calls[1][3] == 55


def test_publish_without_files_sends_text_archive():
    client = FakeClient()
    sent = []
    publish(client, "123", _post(), "research", [], on_sent=sent.append)
    assert len(client.calls) == 1 and client.calls[0][0] == "message"
    assert "caption" in client.calls[0][2]
    assert "https://www.tiktok.com/@user/video/123" in client.calls[0][2]
    assert sent == [(0, 41, "", "text")]


def test_publish_partial_progress_reaches_callback_before_failure():
    class Flaky(FakeClient):
        def send_video(self, chat_id, path, caption="", thread_id=None):
            raise RuntimeError("upload boom")

    sent = []
    files = [
        Downloaded(path="/tmp/u__t(1).jpg", filename="u__t(1).jpg"),
        Downloaded(path="/tmp/u__t(2).mp4", filename="u__t(2).mp4"),
    ]
    try:
        publish(Flaky(), "123", _post(), "research", files, on_sent=sent.append)
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected upload boom")
    assert [s[0] for s in sent] == [1]  # first file recorded before failure


def test_max_upload_bytes_depends_on_target_api():
    assert max_upload_bytes(False) == 50 * 1024 * 1024
    assert max_upload_bytes(True) == 2000 * 1024 * 1024


def test_publish_oversized_file_fails_before_any_send(tmp_path):
    """Oversized media is rejected up front, not by a raw server error."""
    client = FakeClient()
    sent = []
    big = tmp_path / "u__t.mp4"
    big.write_bytes(b"x" * (50 * 1024 * 1024 + 1))
    files = [Downloaded(path=str(big), filename="u__t.mp4")]
    with pytest.raises(UploadError) as excinfo:
        publish(client, "123", _post(), "research", files, on_sent=sent.append)
    assert client.calls == [] and sent == []  # nothing was sent
    assert "over the cloud Bot API limit of 50 MB" in str(excinfo.value)


def test_check_upload_sizes_allows_51mb_on_local(tmp_path):
    """51 MB is over the cloud cap but fine for a local Bot API server."""
    path = tmp_path / "u__t.mp4"
    path.write_bytes(b"x" * (51 * 1024 * 1024))
    files = [Downloaded(path=str(path), filename="u__t.mp4")]
    with pytest.raises(UploadError):
        check_upload_sizes(files, is_local=False)
    check_upload_sizes(files, is_local=True)  # under the 2000 MB local cap
