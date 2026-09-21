import json
import logging

import pytest

from bookmedia.telegram import (
    BotClient,
    TelegramError,
    build_caption,
    build_text_archive,
)


def _transport_factory(calls, payload):
    def transport(url, body, timeout, content_type):
        assert content_type == "application/json"
        calls.append({"url": url, "body": json.loads(body), "timeout": timeout})
        return json.dumps(payload).encode()

    return transport


def test_get_updates_returns_results_and_passes_params():
    calls = []
    client = BotClient(
        "secret",
        transport=_transport_factory(calls, {"ok": True, "result": [{"update_id": 5}]}),
    )
    assert client.get_updates(offset=4, timeout=25) == [{"update_id": 5}]
    assert calls[0]["url"] == "https://api.telegram.org/botsecret/getUpdates"
    assert calls[0]["body"] == {"offset": 4, "timeout": 25}


def test_send_message_posts_chat_text_and_reply():
    calls = []
    client = BotClient(
        "secret",
        transport=_transport_factory(calls, {"ok": True, "result": {"message_id": 9}}),
    )
    assert client.send_message("123", "hi", reply_to=1) == {"message_id": 9}
    body = calls[0]["body"]
    assert body == {"chat_id": "123", "text": "hi", "reply_to_message_id": 1}


def test_send_message_targets_forum_topic():
    calls = []
    client = BotClient(
        "secret",
        transport=_transport_factory(calls, {"ok": True, "result": {"message_id": 9}}),
    )
    client.send_message("123", "hi", thread_id=55)
    assert calls[0]["body"]["message_thread_id"] == 55


def test_file_send_targets_forum_topic(tmp_path):
    """message_thread_id rides along in the multipart fields."""
    calls = []
    client = BotClient("secret", transport=_upload_transport(calls))
    path = tmp_path / "user__20260910_145230.jpg"
    path.write_bytes(b"x")
    client.send_photo("123", str(path), caption="cap", thread_id=55)
    body = calls[0]["body"]
    assert b'name="chat_id"\r\n\r\n123\r\n' in body
    assert b'name="caption"\r\n\r\ncap\r\n' in body
    assert b'name="message_thread_id"\r\n\r\n55\r\n' in body


def test_api_error_raises_without_token():
    client = BotClient(
        "secret",
        transport=_transport_factory([], {"ok": False, "description": "bad request"}),
    )
    with pytest.raises(TelegramError) as excinfo:
        client.get_updates()
    assert "bad request" in str(excinfo.value)
    assert "secret" not in str(excinfo.value)


def test_malformed_response_raises():
    def transport(url, body, timeout, content_type):
        return b"not json"

    with pytest.raises(TelegramError):
        BotClient("secret", transport=transport).get_updates()


def _upload_transport(calls, message_id=42):
    def transport(url, body, timeout, content_type):
        calls.append(
            {"url": url, "body": body, "timeout": timeout, "content_type": content_type}
        )
        return json.dumps({"ok": True, "result": {"message_id": message_id}}).encode()

    return transport


def _post():
    from datetime import datetime, timezone

    from bookmedia.extract import Post

    return Post(
        platform="tiktok",
        post_id="123",
        canonical_url="https://www.tiktok.com/@user/video/123",
        username="user",
        text="hello world",
        published_at=datetime(2026, 9, 10, 14, 52, 30, tzinfo=timezone.utc),
        media=[],
    )


@pytest.mark.parametrize(
    ("sender", "method", "field"),
    [
        ("send_photo", "sendPhoto", b'name="photo"'),
        ("send_video", "sendVideo", b'name="video"'),
        ("send_document", "sendDocument", b'name="document"'),
    ],
)
def test_send_file_uses_multipart_with_filename_and_caption(tmp_path, sender, method, field):
    calls = []
    client = BotClient("secret", transport=_upload_transport(calls))
    path = tmp_path / "user__20260910_145230.mp4"
    path.write_bytes(b"videobytes")
    message_id = getattr(client, sender)("123", str(path), caption="cap")
    assert message_id == 42
    assert calls[0]["url"] == f"https://api.telegram.org/botsecret/{method}"
    assert calls[0]["content_type"].startswith("multipart/form-data; boundary=")
    body = calls[0]["body"]
    assert b'filename="user__20260910_145230.mp4"' in body
    assert field in body
    assert b"videobytes" in body
    assert b"cap" in body


def test_send_file_error_raises_without_token(tmp_path):
    def transport(url, body, timeout, content_type):
        return json.dumps({"ok": False, "description": "bad file"}).encode()

    path = tmp_path / "f.jpg"
    path.write_bytes(b"x")
    with pytest.raises(TelegramError) as excinfo:
        BotClient("secret", transport=transport).send_photo("123", str(path))
    assert "bad file" in str(excinfo.value)
    assert "secret" not in str(excinfo.value)


def test_build_caption_contains_metadata_and_fits_limit():
    caption = build_caption(_post(), "research")
    assert "@user" in caption and "tiktok" in caption
    assert "research" in caption and "hello world" in caption
    assert "https://www.tiktok.com/@user/video/123" in caption
    assert len(caption) <= 1024


def test_build_caption_truncates_long_text():
    from dataclasses import replace

    caption = build_caption(replace(_post(), text="x" * 5000), "research")
    assert len(caption) <= 1024
    assert caption.rstrip().endswith("…\n\nhttps://www.tiktok.com/@user/video/123")


def test_build_text_archive_fits_message_limit():
    from dataclasses import replace

    text = build_text_archive(replace(_post(), text="y" * 9000), "research")
    assert len(text) <= 4096
    assert "https://www.tiktok.com/@user/video/123" in text


def test_local_client_uses_local_base_url():
    client = BotClient("secret", local_base_url="http://localhost:8081")
    assert client.is_local
    assert client._base == "http://localhost:8081/botsecret"


def test_cloud_client_uses_cloud_base_url():
    client = BotClient("secret")
    assert not client.is_local
    assert client._base == "https://api.telegram.org/botsecret"


def test_local_client_send_file_uses_json_not_multipart(tmp_path):
    calls = []
    client = BotClient("secret", transport=_upload_transport(calls),
                       local_base_url="http://localhost:8081")
    path = tmp_path / "user__20260910_145230.mp4"
    path.write_bytes(b"videobytes")
    client.send_photo("123", str(path), caption="cap")
    assert calls[0]["url"] == "http://localhost:8081/botsecret/sendPhoto"
    assert calls[0]["content_type"] == "application/json"
    body = json.loads(calls[0]["body"])
    assert body["chat_id"] == "123"
    assert body["caption"] == "cap"
    assert body["photo"].startswith("file://")
    assert body["photo"].endswith("user__20260910_145230.mp4")


def test_local_client_send_file_error_raises_without_token(tmp_path):
    def transport(url, body, timeout, content_type):
        return json.dumps({"ok": False, "description": "bad file"}).encode()

    path = tmp_path / "f.jpg"
    path.write_bytes(b"x")
    with pytest.raises(TelegramError) as excinfo:
        BotClient("secret", transport=transport,
                  local_base_url="http://localhost:8081").send_photo("123", str(path))
    assert "bad file" in str(excinfo.value)
    assert "secret" not in str(excinfo.value)


def test_upload_timeout_scales_with_large_file(tmp_path):
    from bookmedia.telegram import _LARGE_FILE_THRESHOLD, _UPLOAD_TIMEOUT, _UPLOAD_SPEED_BPS
    import math

    client = BotClient("secret")
    # Small file: base timeout
    small = tmp_path / "small.mp4"
    small.write_bytes(b"x" * 1000)
    assert client._upload_timeout(1000) == _UPLOAD_TIMEOUT

    # Large file: scaled timeout
    large_size = _LARGE_FILE_THRESHOLD + 10 * 1024 * 1024  # 20 MB
    expected = max(_UPLOAD_TIMEOUT, math.ceil(large_size / _UPLOAD_SPEED_BPS) + 30)
    assert client._upload_timeout(large_size) == expected


def test_send_result_logging_warns_on_rejection(caplog):
    def transport(url, body, timeout, content_type):
        return json.dumps({"ok": False, "description": "Too Many Requests: retry after 5"}).encode()

    client = BotClient("secret", transport=transport)
    with caplog.at_level(logging.WARNING, logger="bookmedia.telegram"):
        with pytest.raises(TelegramError):
            client.send_message("123", "hi")
    assert "telegram sendMessage rejected" in caplog.text
    assert "Too Many Requests" in caplog.text
    assert "secret" not in caplog.text


def test_send_result_logging_reports_ok(caplog):
    """A successful send leaves a trace (local Bot API logs nothing per send)."""
    calls = []
    client = BotClient(
        "secret",
        transport=_transport_factory(calls, {"ok": True, "result": {}}),
    )
    with caplog.at_level(logging.DEBUG, logger="bookmedia"):
        client.send_message("123", "hi")
    assert "telegram sendMessage -> ok" in caplog.text


def test_send_result_logging_scrubs_urls_from_rejections(caplog):
    def transport(url, body, timeout, content_type):
        return json.dumps({
            "ok": False,
            "description": "Bad Request: https://x.com/u/status/1 is bad",
        }).encode()

    client = BotClient("secret", transport=transport)
    with caplog.at_level(logging.WARNING, logger="bookmedia"):
        with pytest.raises(TelegramError):
            client.send_message("123", "hi")
    assert "https://x.com/u/status/1" not in caplog.text
    assert "<url>" in caplog.text
    """Server rejections show status + body instead of a bare HTTPError."""
    import io
    import urllib.error

    def transport(url, body, timeout, content_type):
        raise urllib.error.HTTPError(
            url, 413, "Request Entity Too Large", {},
            io.BytesIO(b'{"ok":false,"description":"Request Entity Too Large: file too big"}'),
        )

    client = BotClient("secret", transport=transport)
    with pytest.raises(TelegramError) as excinfo:
        client.get_updates()
    msg = str(excinfo.value)
    assert "HTTP 413" in msg and "file too big" in msg
    assert "secret" not in msg


def test_non_http_error_detail_includes_exception_message():
    def transport(url, body, timeout, content_type):
        raise TimeoutError("connect timed out")

    client = BotClient("secret", transport=transport)
    with pytest.raises(TelegramError) as excinfo:
        client.get_updates()
    assert "TimeoutError: connect timed out" in str(excinfo.value)
    assert "secret" not in str(excinfo.value)
