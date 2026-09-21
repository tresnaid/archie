"""Minimal Telegram Bot API client (stdlib only, long polling).

The ``transport`` seam exists so tests can run without network access:
``transport(url, body, timeout, content_type) -> response bytes``. Production
uses urllib with a POST carrying the given content type.
"""

from __future__ import annotations

import json
import logging
import math
import mimetypes
import re
import secrets
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

Transport = Callable[[str, bytes, int, str], bytes]

log = logging.getLogger("bookmedia")

_JSON = "application/json"
_CAPTION_LIMIT = 1024
_TEXT_LIMIT = 4096
_UPLOAD_TIMEOUT = 120
_LARGE_FILE_THRESHOLD = 10 * 1024 * 1024  # 10 MB — scale timeout above this
_UPLOAD_SPEED_BPS = 1_000_000  # conservative estimate: 1 MB/s for timeout scaling


class TelegramError(Exception):
    pass


def _error_detail(exc: BaseException) -> str:
    """One-line, secret-free reason for a failed request.

    HTTP errors surface their status code and response body so server-side
    rejections (e.g. a Bot API upload limit) are diagnosable instead of
    showing up as a bare ``HTTPError``. Everything else falls back to
    ``Type: message``.
    """
    if isinstance(exc, urllib.error.HTTPError):
        try:
            body = exc.read().decode("utf-8", "replace").strip()
        except Exception:  # noqa: BLE001 - body read is best-effort
            body = ""
        reason = f"HTTP {exc.code}"
        if body:
            return f"{reason}: {body[:300]}"
        if exc.reason:
            return f"{reason}: {exc.reason}"
        return reason
    detail = str(exc).strip()
    return f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__


def _default_transport(url: str, body: bytes, timeout: int,
                       content_type: str = _JSON) -> bytes:
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": content_type}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _check(data: Any) -> Any:
    if not isinstance(data, dict) or not data.get("ok"):
        description = data.get("description") if isinstance(data, dict) else None
        raise TelegramError(f"telegram error: {description or 'unknown'}")
    return data["result"]


def _scrub_url(text: str) -> str:
    """Remove http(s) URLs so logs/diagnostics never carry submitted links."""
    return re.sub(r"https?://\S+", "<url>", text)


def _log_send_result(log: logging.Logger, method: str, raw: bytes,
                     content_type: str) -> None:
    """Debug-log what Telegram did with an outgoing message.

    Every send is either accepted or explained: the local Bot API only writes
    ``[pid ..] <bot-id>`` lines (not per-message outcomes), so without this a
    dropped ack/result/final reply leaves no trace at all.
    """
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001 - non-JSON responses still get logged
        log.debug("telegram %s -> non-JSON response (%s, %d bytes)",
                  method, content_type, len(raw))
        return
    if isinstance(payload, dict) and payload.get("ok"):
        log.debug("telegram %s -> ok", method)
        return
    description = payload.get("description") if isinstance(payload, dict) else None
    log.warning("telegram %s rejected: %s",
                method, _scrub_url(str(description or "unknown")))


def _encode_multipart(fields: dict[str, str], file_field: str,
                      filename: str, data: bytes) -> tuple[bytes, str]:
    boundary = secrets.token_hex(16)
    buf = bytearray()
    for key, value in fields.items():
        buf += (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
            f"{value}\r\n"
        ).encode("utf-8")
    guessed = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    buf += (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{file_field}";'
        f' filename="{filename}"\r\n'
        f"Content-Type: {guessed}\r\n\r\n"
    ).encode("utf-8")
    buf += data
    buf += f"\r\n--{boundary}--\r\n".encode("utf-8")
    return bytes(buf), f"multipart/form-data; boundary={boundary}"


def _archive_text(header: str, text: str, footer: str, limit: int) -> str:
    text = (text or "").strip()
    full = f"{header}\n\n{text}\n\n{footer}" if text else f"{header}\n\n{footer}"
    if len(full) <= limit:
        return full
    room = max(0, limit - len(header) - len(footer) - 5)
    return f"{header}\n\n{text[:room]}…\n\n{footer}"


def _header(post, category: str) -> str:
    author = f"@{post.username} · " if post.username else ""
    return f"{author}{post.platform} · {category}"


def build_caption(post, category: str) -> str:
    """Photo/video caption: metadata + truncated text, fits 1024 chars."""
    return _archive_text(_header(post, category), post.text,
                         post.canonical_url, _CAPTION_LIMIT)


def build_text_archive(post, category: str) -> str:
    """Text-only archive message: full text up to the 4096-char limit."""
    return _archive_text(_header(post, category), post.text,
                         post.canonical_url, _TEXT_LIMIT)


class BotClient:
    def __init__(self, token: str, transport: Transport | None = None,
                 local_base_url: str = "") -> None:
        self._token = token
        self._local_base = f"{local_base_url}/bot{token}" if local_base_url else ""
        self._cloud_base = f"https://api.telegram.org/bot{token}"
        self._transport = transport or _default_transport

    @property
    def _base(self) -> str:
        return self._local_base or self._cloud_base

    @property
    def is_local(self) -> bool:
        return bool(self._local_base)

    def _call(self, method: str, params: dict[str, Any], timeout: int) -> Any:
        try:
            raw = self._transport(
                f"{self._base}/{method}", json.dumps(params).encode(), timeout, _JSON
            )
            data = json.loads(raw.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - wrapped; URL/token never included
            raise TelegramError(f"telegram request failed: {_error_detail(exc)}") from exc
        _log_send_result(log, method, raw, _JSON)
        return _check(data)

    def _upload_timeout(self, file_size: int) -> int:
        """Scale timeout for large files; minimum _UPLOAD_TIMEOUT."""
        if file_size <= _LARGE_FILE_THRESHOLD:
            return _UPLOAD_TIMEOUT
        seconds = math.ceil(file_size / _UPLOAD_SPEED_BPS) + 30
        return max(_UPLOAD_TIMEOUT, seconds)

    def _send_file(self, method: str, file_field: str, chat_id: str,
                   path: str, caption: str = "",
                   thread_id: int | None = None) -> int:
        name = Path(path).name
        fields: dict[str, str] = {"chat_id": chat_id}
        if caption:
            fields["caption"] = caption
        if thread_id is not None:
            fields["message_thread_id"] = str(thread_id)

        file_size = Path(path).stat().st_size
        timeout = self._upload_timeout(file_size)

        if self.is_local:
            fields[file_field] = f"file://{Path(path).resolve()}"
            body = json.dumps(fields).encode()
            content_type = _JSON
        else:
            body, content_type = _encode_multipart(
                fields, file_field, name, Path(path).read_bytes()
            )

        try:
            raw = self._transport(
                f"{self._base}/{method}", body, timeout, content_type
            )
            payload = json.loads(raw.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - wrapped; URL/token never included
            raise TelegramError(f"telegram request failed: {_error_detail(exc)}") from exc
        _log_send_result(log, method, raw, content_type)
        result = _check(payload)
        if not isinstance(result, dict) or not isinstance(result.get("message_id"), int):
            raise TelegramError("telegram error: upload returned no message_id")
        return result["message_id"]

    def send_photo(self, chat_id: str, path: str, caption: str = "",
                   thread_id: int | None = None) -> int:
        return self._send_file("sendPhoto", "photo", chat_id, path, caption, thread_id)

    def send_video(self, chat_id: str, path: str, caption: str = "",
                   thread_id: int | None = None) -> int:
        return self._send_file("sendVideo", "video", chat_id, path, caption, thread_id)

    def send_document(self, chat_id: str, path: str, caption: str = "",
                      thread_id: int | None = None) -> int:
        return self._send_file("sendDocument", "document", chat_id, path, caption, thread_id)

    def get_updates(self, offset: int | None = None, timeout: int = 25) -> list[dict]:
        params: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        result = self._call("getUpdates", params, timeout + 15)
        return result if isinstance(result, list) else []

    def send_message(
        self, chat_id: str, text: str, reply_to: int | None = None,
        thread_id: int | None = None,
    ) -> dict:
        params: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_to is not None:
            params["reply_to_message_id"] = reply_to
        if thread_id is not None:
            params["message_thread_id"] = thread_id
        result = self._call("sendMessage", params, 30)
        return result if isinstance(result, dict) else {}
