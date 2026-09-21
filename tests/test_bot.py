import json
import logging

from bookmedia.bot import write_heartbeat, handle_message, poll_once
from bookmedia.config import Settings


def _settings(**overrides):
    base = {"bot_token": "t"}
    base.update(overrides)
    return Settings(**base)


def _message(text, chat="123", user="7", is_bot=False, msg_id=1, thread_id=None):
    message = {
        "message_id": msg_id,
        "chat": {"id": 123 if chat == "123" else 999, "type": "group"},
        "from": {"id": int(user), "is_bot": is_bot},
        "text": text,
    }
    if thread_id is not None:
        message["message_thread_id"] = thread_id
    return message


class _FakeClient:
    def __init__(self, updates):
        self.updates = updates
        self.sent = []
        self.fail_once = False

    def get_updates(self, offset=None, timeout=25):
        return self.updates

    def send_message(self, chat_id, text, reply_to=None, thread_id=None):
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("boom")
        self.sent.append({"chat_id": chat_id, "text": text, "reply_to": reply_to,
                          "thread_id": thread_id})
        return {"message_id": 1}


def _ok(parsed, platform, *, chat_id="123", thread_id=None):
    return f"done {platform} {parsed.type} {chat_id}"


def test_wrong_chat_ignored():
    # With per-chat architecture, messages from any chat are accepted
    # (no global chat_id gating). Wrong chat no longer means ignore.
    outcome = handle_message(_settings(), _message("https://x.com/a", chat="other"))
    assert outcome.action == "ack"
    assert outcome.chat_id == "999"
    assert outcome.reply is None


def test_disallowed_user_ignored():
    settings = _settings(allowed_user_ids=("7",))
    assert handle_message(settings, _message("https://x.com/a", user="8")).action == "ignore"
    assert handle_message(settings, _message("https://x.com/a", user="7")).action == "ack"


def test_non_url_ignored():
    outcome = handle_message(_settings(), _message("good morning"))
    assert outcome.action == "ignore"


def test_unsupported_platform_error_names_supported():
    outcome = handle_message(_settings(), _message("https://www.facebook.com/watch?v=1"))
    assert outcome.action == "error"
    assert "Instagram" in outcome.reply


def test_valid_link_returns_ack_with_platform_and_parsed():
    outcome = handle_message(
        _settings(), _message("https://x.com/u/status/1 --type=research")
    )
    assert outcome.action == "ack"
    assert outcome.platform == "x"
    assert outcome.parsed.type == "research"
    assert outcome.chat_id == "123"
    assert outcome.thread_id is None  # ordinary group: no topic
    # reply is None — process is called later in poll_once
    assert outcome.reply is None


def test_link_in_topic_carries_thread_id():
    """The topic a link is posted in travels with the archive job."""
    outcome = handle_message(
        _settings(), _message("https://x.com/u/status/1", thread_id=42)
    )
    assert outcome.action == "ack"
    assert outcome.thread_id == 42


def test_poll_once_targets_topic_for_ack_result_and_process():
    client = _FakeClient(
        [{"update_id": 30,
          "message": _message("https://x.com/a", msg_id=31, thread_id=42)}]
    )
    seen = []

    def process(parsed, platform, *, chat_id, thread_id=None):
        seen.append({"chat_id": chat_id, "thread_id": thread_id})
        return "done"

    poll_once(client, _settings(), 30, process)
    assert seen == [{"chat_id": "123", "thread_id": 42}]
    assert client.sent[0]["thread_id"] == 42  # ack
    assert client.sent[1]["thread_id"] == 42  # final result


def test_poll_once_omits_thread_outside_topics():
    client = _FakeClient(
        [{"update_id": 40, "message": _message("https://x.com/a", msg_id=41)}]
    )
    poll_once(client, _settings(), 40, _ok)
    assert client.sent[0]["thread_id"] is None
    assert client.sent[1]["thread_id"] is None


def test_poll_once_sends_ack_then_result_and_advances_offset():
    client = _FakeClient(
        [
            {"update_id": 10, "message": _message("https://x.com/a", msg_id=11)},
            {"update_id": 12, "message": _message("hello", msg_id=13)},
            {"update_id": 14, "message": _message("https://x.com/a", chat="other")},
        ]
    )
    assert poll_once(client, _settings(), 10, _ok) == 15
    # One valid link from chat 123 → 2 messages: immediate ack + final result
    # The other-group message also gets processed (per-chat architecture)
    assert len(client.sent) == 4
    assert "archiving" in client.sent[0]["text"].lower() or "received" in client.sent[0]["text"].lower()
    assert client.sent[0]["reply_to"] == 11
    assert "done x" in client.sent[1]["text"]
    assert client.sent[1]["reply_to"] == 11
    # Both go to chat 123 (the origin of the first valid message)
    assert client.sent[0]["chat_id"] == "123"
    assert client.sent[1]["chat_id"] == "123"
    # The other-group message was also processed and sent to chat 999
    assert client.sent[2]["chat_id"] == "999"
    assert client.sent[3]["chat_id"] == "999"
    # The hello message was ignored (no URL) -- no sent messages for it


def test_poll_once_skips_update_when_ack_send_fails(caplog):
    """A failed ack must not stop polling — and must skip loudly, not silently.

    The ack is best-effort feedback, but process() always follows a sent ack,
    so a failed ack skips the whole update (no row is created). The skip is
    logged with the update id so a vanished link is always explainable.
    """
    client = _FakeClient(
        [
            {"update_id": 20, "message": _message("https://x.com/a", msg_id=21)},
            {"update_id": 22, "message": _message("https://x.com/b", msg_id=23)},
        ]
    )
    client.fail_once = True  # ack for the first message fails
    with caplog.at_level(logging.WARNING, logger="bookmedia"):
        assert poll_once(client, _settings(), 20, _ok) == 23
    # First update skipped after its ack failed: no result reply, no process
    # call; second update processed normally (ack + result).
    assert [m["reply_to"] for m in client.sent] == [21, 23, 23]
    assert "update 20" in caplog.text and "ack send failed" in caplog.text


def test_poll_once_replies_internal_error_when_process_crashes(caplog):
    client = _FakeClient(
        [{"update_id": 30, "message": _message("https://x.com/a", msg_id=31)}]
    )

    def broken(parsed, platform, *, chat_id="123", thread_id=None):
        raise RuntimeError("secret bug detail")

    with caplog.at_level(logging.ERROR, logger="bookmedia"):
        assert poll_once(client, _settings(), 30, broken) == 31
    assert len(client.sent) == 2
    assert "internal error" in client.sent[1]["text"]
    assert client.sent[1]["reply_to"] == 31
    # Exception details stay in the log, never in the chat.
    assert "secret bug detail" not in client.sent[1]["text"]
    assert "RuntimeError" in client.sent[1]["text"]
    # SECURITY.md: the log names the platform, not the full URL.
    assert "archiving x link crashed" in caplog.text
    assert "https://x.com/a" not in caplog.text


def test_wrong_chat_ignore_is_logged(caplog):
    # In the per-chat architecture, messages from any chat are accepted.
    # There is no "not the archive chat" log message anymore.
    outcome = handle_message(_settings(), _message("https://x.com/a", chat="other"))
    assert outcome.action == "ack"
    assert outcome.chat_id == "999"
    assert "not the archive chat" not in caplog.text


def test_disallowed_user_ignore_is_logged(caplog):
    settings = _settings(allowed_user_ids=("7",))
    with caplog.at_level(logging.INFO, logger="bookmedia"):
        assert handle_message(settings, _message("https://x.com/a", user="8")).action == "ignore"
    assert "user 8 is not allowed" in caplog.text


def test_unparsable_ignore_is_logged(caplog):
    with caplog.at_level(logging.INFO, logger="bookmedia"):
        assert handle_message(_settings(), _message("hello")).action == "ignore"
    assert "no URL" in caplog.text


def test_ack_and_error_outcomes_are_logged(caplog):
    with caplog.at_level(logging.INFO, logger="bookmedia"):
        handle_message(_settings(), _message("https://x.com/a/1"))
        handle_message(_settings(), _message("https://unsupported.example/x"))
    assert "archiving x link" in caplog.text
    assert "unsupported host" in caplog.text


def test_write_heartbeat_records_offset_and_pid(tmp_path):
    path = write_heartbeat(str(tmp_path), offset=42)
    data = json.loads(path.read_text())
    assert data["offset"] == 42 and data["pid"] > 0 and "ts" in data
