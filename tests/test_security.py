"""Security regression tests: secrets never reach replies, logs, or files.

Bot tokens, cookies, and credential-bearing URLs must not
leak through the Telegram surface or the log stream.
"""

import json
import logging

from bookmedia.bot import handle_message, poll_once, write_heartbeat
from bookmedia.config import Settings
from bookmedia.telegram import BotClient


def _settings(**overrides):
    base = {"bot_token": "SECRET-TOKEN"}
    base.update(overrides)
    return Settings(**base)


def _message(text, msg_id=1):
    return {
        "message_id": msg_id,
        "chat": {"id": 123, "type": "group"},
        "from": {"id": 7, "is_bot": False},
        "text": text,
    }


class _EchoClient:
    def __init__(self, updates):
        self.updates = updates
        self.sent = []

    def get_updates(self, offset=None, timeout=25):
        return self.updates

    def send_message(self, chat_id, text, reply_to=None):
        self.sent.append(text)
        return {"message_id": 1}


def test_replies_never_echo_submitted_url():
    for text in (
        "https://x.com/u/status/1?auth=TOPSECRET",
        "https://unsupported.example/x?token=TOPSECRET",
        "not a url",
    ):
        outcome = handle_message(_settings(), _message(text))
        if outcome.reply is not None:
            assert "TOPSECRET" not in outcome.reply
            if outcome.action != "error":
                assert "https://" not in outcome.reply


def test_crash_reply_and_log_exclude_url_and_exception_detail(caplog):
    client = _EchoClient(
        [{"update_id": 1, "message": _message("https://x.com/u?auth=TOPSECRET")}]
    )

    def broken(parsed, platform):
        raise RuntimeError("TRACETOKEN")

    with caplog.at_level(logging.ERROR, logger="bookmedia"):
        poll_once(client, _settings(), 1, broken)
    sent = "\n".join(client.sent)
    assert "TOPSECRET" not in sent and "TRACETOKEN" not in sent
    assert "TOPSECRET" not in caplog.text  # URL stays out of logs too


def test_telegram_errors_carry_neither_url_nor_token():
    def transport(url, body, timeout, content_type):
        raise OSError("net down")

    client = BotClient("SECRET-TOKEN", transport=transport)
    try:
        client.get_updates(offset=1)
    except Exception as exc:
        message = str(exc)
        assert "SECRET-TOKEN" not in message and "api.telegram.org" not in message
    else:
        raise AssertionError("expected TelegramError")


def test_heartbeat_contains_no_secrets(tmp_path):
    path = write_heartbeat(str(tmp_path), offset=5)
    data = json.loads(path.read_text())
    assert set(data) == {"ts", "offset", "pid"}