"""Submission pipeline: dedup -> extract -> download -> upload -> archived.

Failed rows persist with their error and are retried on resubmission.
Temporary job dirs are always removed; diagnostics live in the database.
"""

from __future__ import annotations

import os
import shutil
import sqlite3

from . import repository
from .downloader import DownloadError, download as download_media
from .extract import ExtractionError, extract
from .parser import ParsedMessage
from .telegram import TelegramError
from .upload import publish as publish_archive


def process_submission(parsed: ParsedMessage, platform: str, *,
                       conn: sqlite3.Connection, client, chat_id: str,
                       temp_dir: str, thread_id: int | None = None,
                       extract=extract,
                       download=download_media,
                       publish=publish_archive) -> str:
    existing = repository.find_by_any_url(conn, parsed.url, chat_id=chat_id)
    if existing is not None and existing["status"] == "archived":
        return f"Already archived as #{existing['id']} (type '{existing['type']}')."
    row_id = (
        existing["id"] if existing is not None
        else repository.insert_received(
            conn, platform=platform, submitted_url=parsed.url, type=parsed.type,
            chat_id=chat_id,
        )
    )
    repository.set_status(conn, row_id, "extracting")
    try:
        post = extract(parsed.url)
    except ExtractionError as exc:
        repository.set_status(conn, row_id, "failed", error=str(exc))
        return f"Couldn't extract this link: {exc}"
    duplicate = repository.find_by_identity(conn, chat_id, post.platform, post.post_id)
    if duplicate is not None and duplicate["id"] != row_id:
        if duplicate["status"] == "archived":
            repository.delete(conn, row_id)
            return f"Already archived as #{duplicate['id']} (type '{duplicate['type']}')."
        repository.delete(conn, row_id)  # adopt the earlier row, retry it
        row_id = duplicate["id"]
        repository.set_status(conn, row_id, "extracting")
    repository.save_extracted(conn, row_id, post=post, chat_id=chat_id, type=parsed.type)

    workdir = os.path.join(temp_dir, f"job-{row_id}")
    shutil.rmtree(workdir, ignore_errors=True)
    try:
        repository.set_status(conn, row_id, "downloading")
        files = download(post, workdir) if post.media else []
        repository.set_status(conn, row_id, "uploading")

        def record(ref) -> None:
            index, message_id, filename, media_type = ref
            repository.add_message(
                conn, row_id, chat_id=chat_id, message_id=message_id,
                media_index=index, filename=filename, media_type=media_type,
            )

        publish(client, chat_id, post, parsed.type, files,
                on_sent=record, thread_id=thread_id)
        repository.mark_archived(conn, row_id)
    except Exception as exc:  # noqa: BLE001 - any failure must stay retryable, never silent
        detail = str(exc) or type(exc).__name__
        repository.set_status(conn, row_id, "failed", error=detail)
        shutil.rmtree(workdir, ignore_errors=True)
        return f"Couldn't archive this link: {detail}"
    shutil.rmtree(workdir, ignore_errors=True)

    author = f"@{post.username} " if post.username else ""
    fallback = " Original timestamp unknown; archive time was used." if not post.published_at else ""
    if files:
        detail = f"{len(files)} media"
    else:
        detail = "text-only"
    return (
        f"Archived {author}({post.platform}, #{row_id}, {detail},"
        f" type '{parsed.type}').{fallback}"
    )
