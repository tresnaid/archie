"""SQLite archive repository. State transitions only; no extraction logic."""

from __future__ import annotations

import sqlite3

from .extract import Post
from .urls import normalize_url

STATUSES = ("received", "extracting", "downloading", "uploading", "archived", "failed")

_ERROR_LIMIT = 500  # keep stored errors diagnosable but bounded


def insert_received(conn: sqlite3.Connection, *, platform: str, submitted_url: str,
                    type: str, chat_id: str = "") -> int:
    """New submission. Provisional post_id is the normalized submitted URL."""
    cur = conn.execute(
        "INSERT INTO archives (platform, post_id, submitted_url, type, telegram_chat_id)"
        " VALUES (?, ?, ?, ?, ?)",
        (platform, normalize_url(submitted_url), submitted_url, type, chat_id),
    )
    conn.commit()
    return cur.lastrowid


def get(conn: sqlite3.Connection, row_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM archives WHERE id = ?", (row_id,)).fetchone()


def find_by_submitted_url(conn: sqlite3.Connection, submitted_url: str) -> sqlite3.Row | None:
    return (
        conn
        .execute("SELECT * FROM archives WHERE submitted_url = ?", (submitted_url,))
        .fetchone()
    )


def find_by_identity(conn: sqlite3.Connection, chat_id: str, platform: str,
                     post_id: str) -> sqlite3.Row | None:
    return (
        conn
        .execute(
            "SELECT * FROM archives WHERE telegram_chat_id = ? AND platform = ? AND post_id = ?",
            (chat_id, platform, post_id),
        )
        .fetchone()
    )


def set_status(conn: sqlite3.Connection, row_id: int, status: str, *, error: str = "") -> None:
    if status not in STATUSES:
        raise ValueError(f"unknown status: {status}")
    conn.execute(
        "UPDATE archives SET status = ?, error = ?,"
        " updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?",
        (status, error[:_ERROR_LIMIT], row_id),
    )
    conn.commit()


def save_extracted(conn: sqlite3.Connection, row_id: int, *, post: Post, chat_id: str, type: str) -> None:
    published = post.published_at.strftime("%Y-%m-%dT%H:%M:%SZ") if post.published_at else None
    conn.execute(
        "UPDATE archives SET platform = ?, post_id = ?, canonical_url = ?, username = ?,"
        " text = ?, published_at = ?, filename_timestamp_source = ?, type = ?,"
        " telegram_chat_id = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?",
        (
            post.platform, post.post_id, post.canonical_url, post.username, post.text,
            published, "post_time" if published else "archive_time", type,
            chat_id, row_id,
        ),
    )
    conn.commit()


def delete(conn: sqlite3.Connection, row_id: int) -> None:
    """Remove a redundant duplicate row and its Telegram message references.

    Children go first so removing a row can never leave an orphaned
    ``archive_messages`` entry pointing at an archive that no longer exists.
    """
    conn.execute("DELETE FROM archive_messages WHERE archive_id = ?", (row_id,))
    conn.execute("DELETE FROM archives WHERE id = ?", (row_id,))
    conn.commit()


def mark_archived(conn: sqlite3.Connection, row_id: int) -> None:
    conn.execute(
        "UPDATE archives SET status = 'archived', error = '',"
        " archive_timestamp = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),"
        " updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?",
        (row_id,),
    )
    conn.commit()


def add_message(conn: sqlite3.Connection, archive_id: int, *, chat_id: str,
                message_id: int, media_index: int, filename: str,
                media_type: str) -> None:
    conn.execute(
        "INSERT INTO archive_messages (archive_id, telegram_chat_id,"
        " telegram_message_id, media_index, filename, media_type)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (archive_id, chat_id, message_id, media_index, filename, media_type),
    )
    conn.commit()


def messages_for(conn: sqlite3.Connection, archive_id: int) -> list[sqlite3.Row]:
    return (
        conn
        .execute(
            "SELECT * FROM archive_messages WHERE archive_id = ? ORDER BY media_index",
            (archive_id,),
        )
        .fetchall()
    )


def find_by_any_url(conn: sqlite3.Connection, submitted_url: str,
                    chat_id: str = "") -> sqlite3.Row | None:
    """Pre-extraction dedup: match submitted URL, canonical URL, or URL identity.

    When ``chat_id`` is provided, results are scoped to that originating chat
    so duplicate detection respects per-chat isolation. When empty (legacy
    or pre-chat records), the search falls back to global URL/identity match.
    """
    normalized = normalize_url(submitted_url)
    row = find_by_submitted_url(conn, submitted_url)
    if row is None:
        if chat_id:
            row = conn.execute(
                "SELECT * FROM archives"
                " WHERE telegram_chat_id = ? AND (canonical_url = ? OR post_id = ?)"
                " LIMIT 1",
                (chat_id, normalized, normalized),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM archives"
                " WHERE canonical_url = ? OR post_id = ? LIMIT 1",
                (normalized, normalized),
            ).fetchone()
    elif chat_id and row["telegram_chat_id"] and row["telegram_chat_id"] != chat_id:
        row = None
    return row


def find_by_status(conn: sqlite3.Connection, status: str) -> list[sqlite3.Row]:
    """Rows currently in ``status``, oldest first (startup recovery order)."""
    if status not in STATUSES:
        raise ValueError(f"unknown status: {status}")
    return conn.execute(
        "SELECT * FROM archives WHERE status = ? ORDER BY id", (status,)
    ).fetchall()


def fail_interrupted(conn: sqlite3.Connection, row_id: int, *, error: str) -> None:
    """Mark a mid-pipeline row failed after a crash; keeps the error bounded."""
    set_status(conn, row_id, "failed", error=error)
