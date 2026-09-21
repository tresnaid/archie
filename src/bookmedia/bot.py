"""Polling loop and message routing.

Valid supported links are handed to ``process`` (the archive pipeline);
gating, parsing, and platform detection stay here. Everything else is
ignored, with the reason logged so silence is always explainable.

The bot no longer requires a dedicated configured chat. Any group the bot
has been added to can archive links; the originating chat ID travels with
the archive job throughout processing so Group A content never leaks to
Group B.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import Settings
from .parser import ParsedMessage, parse_message
from .platforms import SUPPORTED_PLATFORMS, detect_platform
from .telegram import BotClient, _error_detail

log = logging.getLogger("bookmedia")


def _ack_failure_detail(exc: BaseException) -> str:
    """Short, secret-free reason used in the ack-skipped warning.

    ``_error_detail`` may include a truncated server body (which can echo the
    ack text); keep the update-skipped warning to the exception type plus a
    compact scrubbed reason instead.
    """
    detail = _error_detail(exc)
    return re.sub(r"https?://\S+", "<url>", detail)[:160]


@dataclass(frozen=True)
class Outcome:
    action: str  # "ignore" | "ack" | "error"
    reply: str | None = None
    platform: str | None = None
    parsed: ParsedMessage | None = None
    chat_id: str | None = None
    thread_id: int | None = None  # forum topic the link was posted in


def handle_message(settings: Settings, message: dict) -> Outcome:
    """Validate and gate the message. Does NOT call process — that stays in poll_once."""
    chat_id = str(message.get("chat", {}).get("id"))
    # Telegram sends this only for messages inside a forum topic; it is absent
    # in ordinary groups, so None simply means "post at the group's default".
    thread_id = message.get("message_thread_id")
    sender = message.get("from", {})
    if settings.allowed_user_ids and str(sender.get("id")) not in settings.allowed_user_ids:
        log.info("ignoring update: user %s is not allowed", sender.get("id"))
        return Outcome("ignore")
    parsed = parse_message(message.get("text") or "")
    if parsed is None:
        log.info("ignoring update: no URL in message text")
        return Outcome("ignore")
    platform = detect_platform(parsed.url)
    if platform is None:
        log.info("unsupported host in %s", parsed.url)
        names = ", ".join(p.capitalize() for p in SUPPORTED_PLATFORMS)
        return Outcome(
            "error",
            reply=f"Unsupported link: no archiver for this host yet. Supported: {names}.",
        )
    log.info("archiving %s link from chat %s topic %s as type '%s'",
             platform, chat_id, thread_id, parsed.type)
    return Outcome("ack", platform=platform, parsed=parsed, chat_id=chat_id,
                   thread_id=thread_id)


def poll_once(client: BotClient, settings: Settings, offset: int, process) -> int:
    for update in client.get_updates(offset=offset):
        offset = max(offset, update.get("update_id", offset) + 1)
        message = update.get("message") or update.get("channel_post")
        if not message:
            continue
        try:
            outcome = handle_message(settings, message)
            msg_id = message.get("message_id")
            chat = outcome.chat_id or str(message.get("chat", {}).get("id"))
            thread = outcome.thread_id
            if outcome.action == "ack":
                # Immediate feedback before the pipeline starts (can take
                # minutes). An exception here — typically a flood-control 429
                # from back-to-back bursts — skips the WHOLE update below
                # (process is never called, no row is created), so log the
                # send outcome before it: without this the link silently
                # vanishes ("archiving … link" log with no row afterwards).
                try:
                    client.send_message(
                        chat,
                        f"⏳ {outcome.platform.capitalize()} link received, archiving…",
                        reply_to=msg_id,
                        thread_id=thread,
                    )
                except Exception as exc:  # noqa: BLE001 - ack is feedback only
                    log.warning("update %s: ack send failed (%s); update skipped",
                                update.get("update_id"),
                                getattr(exc, "http_code", None)
                                or _ack_failure_detail(exc))
                try:
                    result = process(outcome.parsed, outcome.platform,
                                     chat_id=chat, thread_id=thread)
                except Exception as exc:  # noqa: BLE001 - dispatcher bugs must not stop polling
                    # Platform only — SECURITY.md: avoid logging complete URLs.
                    log.exception("archiving %s link crashed", outcome.platform)
                    result = (
                        "Couldn't archive this link: internal error"
                        f" ({type(exc).__name__}). Please resubmit."
                    )
                client.send_message(chat, result, reply_to=msg_id, thread_id=thread)
                continue
            elif outcome.reply is not None:
                client.send_message(chat, outcome.reply, reply_to=msg_id,
                                    thread_id=thread)
        except Exception as exc:  # noqa: BLE001 - one bad update must not stop polling
            log.warning("update %s failed: %s", update.get("update_id"), type(exc).__name__)
    return offset


def write_heartbeat(data_dir: str, offset: int):
    """Record a successful poll so the status command can prove liveness."""
    path = Path(data_dir) / "heartbeat.json"
    try:
        path.write_text(json.dumps({
            "ts": datetime.now(timezone.utc).isoformat(),
            "offset": offset,
            "pid": os.getpid(),
        }))
    except OSError as exc:
        log.warning("heartbeat write failed: %s", exc)
    return path


def run_forever(settings: Settings, process, client: BotClient | None = None) -> None:
    client = client or BotClient(settings.bot_token,
                                 local_base_url=settings.local_bot_api_url)
    log.info("telegram polling started%s",
             " (local Bot API)" if client.is_local else "")
    offset = 0
    while True:
        try:
            offset = poll_once(client, settings, offset, process)
            write_heartbeat(settings.data_dir, offset)
        except Exception as exc:  # noqa: BLE001 - network blips must not stop polling
            log.warning("poll failed: %s", type(exc).__name__)
            time.sleep(5)
