"""Publish an archived post to Telegram, reporting each sent message."""

from __future__ import annotations

import os

from .downloader import Downloaded
from .telegram import build_caption, build_text_archive

_PHOTOS = {".jpg", ".jpeg", ".png"}
_VIDEOS = {".mp4", ".mov", ".webm", ".m4v"}
# Upload ceilings: the cloud Bot API rejects anything over 50 MB outright,
# while a self-hosted local Bot API server (--local) accepts up to 2 GB.
# Oversized files are refused before any send so the failure is immediate and
# diagnosable instead of a raw server-side HTTP error after a long upload.
_CLOUD_MAX_BYTES = 50 * 1024 * 1024
_LOCAL_MAX_BYTES = 2000 * 1024 * 1024  # matches telegram-bot-api --local cap


def max_upload_bytes(is_local: bool) -> int:
    """Per-request byte ceiling for the target Bot API (cloud or local)."""
    return _LOCAL_MAX_BYTES if is_local else _CLOUD_MAX_BYTES


def check_upload_sizes(files: list[Downloaded], *, is_local: bool) -> None:
    """Raise UploadError before sending when a file exceeds the API ceiling."""
    limit = max_upload_bytes(is_local)
    for item in files:
        # A missing file fails on send with its own error; nothing to size here.
        size = os.path.getsize(item.path) if os.path.exists(item.path) else 0
        if size > limit:
            raise UploadError(
                f"{item.filename} is {size / (1 << 20):.0f} MB, over the "
                f"{'local' if is_local else 'cloud'} Bot API limit of "
                f"{limit // (1 << 20)} MB"
            )


class UploadError(Exception):
    pass


def media_kind(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    if ext in _PHOTOS:
        return "photo"
    if ext in _VIDEOS:
        return "video"
    return "document"


def publish(client, chat_id: str, post, category: str,
            files: list[Downloaded], *, on_sent, thread_id: int | None = None) -> None:
    """Send media (or a text archive) then call ``on_sent`` per message.

    ``thread_id`` targets a forum topic; the archive lands in the same topic
    the link was posted in. ``on_sent`` receives
    ``(media_index, message_id, filename, media_type)`` immediately after each
    send, so partial uploads stay recorded on failure.
    """
    if not files:
        result = client.send_message(chat_id, build_text_archive(post, category),
                                     thread_id=thread_id)
        on_sent((0, result["message_id"], "", "text"))
        return
    # Fail fast (before any send) when a file exceeds the target API ceiling.
    check_upload_sizes(files, is_local=bool(getattr(client, "is_local", False)))
    senders = {
        "photo": client.send_photo,
        "video": client.send_video,
        "document": client.send_document,
    }
    caption = build_caption(post, category)
    for position, item in enumerate(files, start=1):
        kind = media_kind(item.filename)
        message_id = senders[kind](
            chat_id, item.path, caption=caption if position == 1 else "",
            thread_id=thread_id,
        )
        on_sent((position, message_id, item.filename, kind))
