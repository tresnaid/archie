"""SQLite initialization and migration. The database is an archive index, not the media store."""

from __future__ import annotations

import importlib.resources
import re
import sqlite3

SCHEMA_VERSION = 2

# Matches only the pre-multi-group table constraint, never the per-chat one
# (``UNIQUE (telegram_chat_id, platform, post_id)`` starts with a different column).
_LEGACY_GLOBAL_UNIQUE = re.compile(
    r"\s*UNIQUE\s*\(\s*platform\s*,\s*post_id\s*\)\s*,?", re.IGNORECASE
)


def _schema_sql() -> str:
    return importlib.resources.files("bookmedia").joinpath("schema.sql").read_text()


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


# Uniqueness scopes for archives: the pre-multi-group global scope, and the
# per-chat scope that replaced it.
_GLOBAL_IDENTITY = ["platform", "post_id"]
_CHAT_IDENTITY = ["telegram_chat_id", "platform", "post_id"]


def _has_unique_index(conn: sqlite3.Connection, columns: list[str]) -> bool:
    """True when ``archives`` has a unique index over exactly ``columns``.

    SQLite backs table-level UNIQUE constraints with protected autoindexes that
    ``DROP INDEX`` refuses to remove, so enforcement is detected rather than
    assumed from ``user_version``.
    """
    for index in conn.execute("PRAGMA index_list('archives')").fetchall():
        if not index[2]:  # not unique
            continue
        cols = [
            row[2]
            for row in conn.execute(f"PRAGMA index_info('{index[1]}')").fetchall()
        ]
        if cols == columns:
            return True
    return False


def _ensure_chat_identity_unique(conn: sqlite3.Connection) -> None:
    """Enforce one archive per (chat, platform, post).

    Fresh databases get this from the schema's table constraint; migrated ones
    lost it when the legacy global constraint was removed.
    """
    if _has_unique_index(conn, _CHAT_IDENTITY):
        return
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_archives_chat_identity"
        " ON archives (telegram_chat_id, platform, post_id)"
    )


def _drop_global_platform_post_unique(conn: sqlite3.Connection) -> None:
    """Rewrite the archives definition without the legacy global unique constraint.

    The constraint is part of the CREATE TABLE text, so the stored definition is
    edited directly (``writable_schema``) and the file rebuilt with ``VACUUM``.
    Data is preserved: only schema text changes.
    """
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='archives'"
    ).fetchone()[0]
    if _LEGACY_GLOBAL_UNIQUE.search(sql) is None:
        return
    new_sql = _LEGACY_GLOBAL_UNIQUE.sub("", sql)
    new_sql = re.sub(r",(\s*)\)", r"\1)", new_sql)  # drop any dangling comma
    conn.execute("PRAGMA writable_schema=ON")
    conn.execute(
        "UPDATE sqlite_master SET sql = ? WHERE type='table' AND name='archives'",
        (new_sql,),
    )
    conn.execute("PRAGMA writable_schema=OFF")
    conn.commit()  # VACUUM cannot run inside a transaction
    conn.execute("VACUUM")


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """Add telegram_chat_id to archives, backfill it, and drop legacy uniqueness.

    The chat id is recovered from any Telegram message already recorded for the
    row. Rows with no messages keep an empty chat_id rather than being assigned a
    chat we cannot prove, so historical records are never silently mislabelled.
    """
    if "telegram_chat_id" not in _columns(conn, "archives"):
        conn.execute(
            "ALTER TABLE archives ADD COLUMN telegram_chat_id TEXT NOT NULL DEFAULT ''"
        )
        conn.execute(
            "UPDATE archives SET telegram_chat_id = COALESCE(("
            "  SELECT telegram_chat_id FROM archive_messages"
            "  WHERE archive_messages.archive_id = archives.id"
            "  LIMIT 1"
            "), '')"
        )
    _drop_global_platform_post_unique(conn)


def init_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        # Read the stamp before running the schema: the schema creates missing
        # tables but must not decide what migration the database still needs.
        current = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.executescript(_schema_sql())
        if current < SCHEMA_VERSION:
            _migrate_v1_to_v2(conn)
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        else:
            # Repair what earlier builds could leave behind. Both calls are
            # no-ops on a database that is already correct, so the invariant
            # holds on every boot without trusting the stamp alone.
            _drop_global_platform_post_unique(conn)
        _ensure_chat_identity_unique(conn)
        conn.commit()
    except Exception:
        conn.close()
        raise
    return conn
