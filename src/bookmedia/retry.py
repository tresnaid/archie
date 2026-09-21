"""Dashboard retry: re-run a row through the real archive pipeline.

Only ``failed``/``received`` rows are retryable. ``archived`` rows are
rejected; ``extracting``/``downloading``/``uploading`` rows are owned by a
live pipeline. One retry runs at a time (single worker slot). Each run uses
a fresh SQLite connection and fresh BotClient (no cross-thread sharing).

Limitation: the ``archives`` table stores the originating chat but not the
forum ``thread_id``, so a dashboard retry posts to the group's default
location, not the original topic. Chat isolation and dedup are unchanged:
``service.process_submission`` still owns routing.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from functools import partial
from typing import Callable

from . import repository
from .downloader import download as download_media
from .extract import extract
from .parser import ParsedMessage
from .platforms import detect_platform
from .service import process_submission
from .telegram import BotClient
from .upload import publish as publish_archive

log = logging.getLogger("bookmedia")

_RETRYABLE = ("failed", "received")


class RetryError(Exception):
    """Base class for dashboard retry rejections."""


class AlreadyQueued(RetryError):
    """A retry is already running (same row, or the worker slot is busy)."""

    http_code = 409


class InvalidState(RetryError):
    """The row cannot be retried (missing, archived, or mid-pipeline)."""

    http_code = 400


class RetryQueue:
    """Single-slot background worker reusing the archive pipeline."""

    def __init__(
        self,
        *,
        db_path: str,
        temp_dir: str,
        cookies_path: str,
        bot_token: str,
        local_bot_api_url: str = "",
        max_media_bytes: int = 2000 * 1024 * 1024,
        extract=extract,
        download=download_media,
        publish=publish_archive,
        client_factory: Callable[..., object] | None = None,
    ) -> None:
        self._db_path = db_path
        self._temp_dir = temp_dir
        self._cookies_path = cookies_path
        self._bot_token = bot_token
        self._local_bot_api_url = local_bot_api_url
        self._max_media_bytes = max_media_bytes
        self._extract = extract
        self._download = download
        self._publish = publish
        self._client_factory = client_factory or (
            lambda: BotClient(bot_token, local_base_url=local_bot_api_url)
        )
        self._lock = threading.Lock()
        self._in_flight: set[int] = set()

    def retry(self, row_id: int) -> str:
        """Queue a real pipeline run for ``row_id``; returns ``"queued"``.

        Raises ``AlreadyQueued`` when this row (or the single worker slot)
        is busy, ``InvalidState`` when the row is missing, archived, or
        owned by a live pipeline stage.
        """
        row_id = int(row_id)
        conn = sqlite3.connect(self._db_path)
        try:
            conn.row_factory = sqlite3.Row
            row = repository.get(conn, row_id)
            if row is None:
                raise InvalidState(f"archive #{row_id} does not exist")
            status = row["status"]
            if status == "archived":
                raise InvalidState(f"archive #{row_id} is already archived")
            if status not in _RETRYABLE:
                raise InvalidState(
                    f"archive #{row_id} is {status}, owned by a live pipeline"
                )
            with self._lock:
                if row_id in self._in_flight:
                    raise AlreadyQueued(f"retry for #{row_id} is already running")
                if self._in_flight:
                    raise AlreadyQueued("another dashboard retry is already running")
                self._in_flight.add(row_id)
        finally:
            conn.close()
        worker = threading.Thread(
            target=self._run, args=(row_id,), daemon=True,
            name=f"dashboard-retry-{row_id}",
        )
        worker.start()
        return "queued"

    def _run(self, row_id: int) -> None:
        conn = sqlite3.connect(self._db_path)
        try:
            conn.row_factory = sqlite3.Row
            row = repository.get(conn, row_id)
            if row is None:
                log.warning("dashboard retry #%s: row vanished, skipping", row_id)
                return
            if row["status"] not in _RETRYABLE:
                log.info(
                    "dashboard retry #%s: now %s, skipping", row_id, row["status"]
                )
                return
            submitted_url = row["submitted_url"] or row["canonical_url"]
            chat_id = row["telegram_chat_id"]
            if not submitted_url or not chat_id:
                repository.set_status(
                    conn, row_id, "failed",
                    error="retry unavailable: original URL or chat not recorded",
                )
                return
            parsed = ParsedMessage(url=submitted_url, type=row["type"] or "unsorted")
            platform = detect_platform(parsed.url)
            if platform is None:
                repository.set_status(
                    conn, row_id, "failed",
                    error="retry unavailable: platform no longer supported",
                )
                return
            extractor = partial(self._extract, cookies_path=self._cookies_path)
            downloader = partial(self._download, cookies_path=self._cookies_path,
                                 max_bytes=self._max_media_bytes)
            result = process_submission(
                parsed, platform,
                conn=conn,
                client=self._client_factory(),
                chat_id=chat_id,
                temp_dir=self._temp_dir,
                extract=extractor,
                download=downloader,
                publish=self._publish,
            )
            log.info("dashboard retry #%s finished: %s", row_id, result)
        except Exception as exc:  # noqa: BLE001 - worker must never kill server
            log.warning("dashboard retry #%s crashed: %s", row_id, type(exc).__name__)
            try:
                repository.set_status(
                    conn, row_id, "failed",
                    error=f"dashboard retry crashed ({type(exc).__name__})",
                )
            except Exception:  # noqa: BLE001 - best effort on a dying worker
                pass
        finally:
            try:
                conn.close()
            finally:
                with self._lock:
                    self._in_flight.discard(row_id)
