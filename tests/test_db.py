import sqlite3

import pytest

from bookmedia.db import init_db


def test_init_creates_tables(tmp_path):
    conn = init_db(str(tmp_path / "archive.db"))
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {"archives", "archive_messages"} <= tables
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    conn.close()


def test_init_idempotent_and_preserves_rows(tmp_path):
    path = str(tmp_path / "archive.db")
    conn = init_db(path)
    conn.execute(
        "INSERT INTO archives (platform, post_id, submitted_url) VALUES (?, ?, ?)",
        ("x", "1", "https://x.com/u/status/1"),
    )
    conn.commit()
    conn.close()
    conn = init_db(path)
    assert conn.execute("SELECT COUNT(*) FROM archives").fetchone()[0] == 1
    conn.close()


def test_column_defaults(tmp_path):
    conn = init_db(str(tmp_path / "archive.db"))
    conn.execute(
        "INSERT INTO archives (platform, post_id) VALUES (?, ?)", ("x", "2")
    )
    row = conn.execute(
        "SELECT type, status, filename_timestamp_source FROM archives WHERE post_id='2'"
    ).fetchone()
    assert tuple(row) == ("unsorted", "received", "archive_time")
    conn.close()


def test_duplicate_identity_enforced(tmp_path):
    conn = init_db(str(tmp_path / "archive.db"))
    conn.execute("INSERT INTO archives (platform, post_id, telegram_chat_id) VALUES ('x', '3', '')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO archives (platform, post_id, telegram_chat_id) VALUES ('x', '3', '')")
    conn.close()


def test_status_check_constraint(tmp_path):
    conn = init_db(str(tmp_path / "archive.db"))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO archives (platform, post_id, status) VALUES ('x', '4', 'done')")
    conn.close()


def test_archive_message_child_row(tmp_path):
    conn = init_db(str(tmp_path / "archive.db"))
    cur = conn.execute("INSERT INTO archives (platform, post_id, telegram_chat_id) VALUES ('x', '5', '')")
    conn.execute(
        "INSERT INTO archive_messages (archive_id, telegram_chat_id, telegram_message_id)"
        " VALUES (?, ?, ?)",
        (cur.lastrowid, "123", 456),
    )
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM archive_messages").fetchone()[0] == 1
    conn.close()


def test_init_db_sets_row_factory(tmp_path):
    """init_db must set row_factory so repository queries return sqlite3.Row objects."""
    conn = init_db(str(tmp_path / "archive.db"))
    conn.execute("INSERT INTO archives (platform, post_id) VALUES ('x', '6')")
    conn.commit()
    row = conn.execute("SELECT platform FROM archives WHERE post_id='6'").fetchone()
    assert row["platform"] == "x"
    conn.close()


def _legacy_db(path, *, include_chat_column=False, user_version=1):
    """Recreate a pre-multi-group database.

    ``include_chat_column`` reproduces the half-migrated state that an earlier
    build left behind: the chat column exists but the old global
    ``UNIQUE (platform, post_id)`` constraint is still enforced.
    """
    chat_column = (
        "            telegram_chat_id TEXT NOT NULL DEFAULT '',\n"
        if include_chat_column else ""
    )
    conn = sqlite3.connect(path)
    conn.executescript(
        f"""
        CREATE TABLE archives (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT NOT NULL DEFAULT '',
            post_id TEXT NOT NULL DEFAULT '',
            canonical_url TEXT NOT NULL DEFAULT '',
            submitted_url TEXT NOT NULL DEFAULT '',
            username TEXT NOT NULL DEFAULT '',
            type TEXT NOT NULL DEFAULT 'unsorted',
            published_at TEXT,
            archive_timestamp TEXT,
            filename_timestamp_source TEXT NOT NULL DEFAULT 'archive_time'
                CHECK (filename_timestamp_source IN ('post_time', 'archive_time')),
            text TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'received'
                CHECK (status IN ('received', 'extracting', 'downloading', 'uploading', 'archived', 'failed')),
            error TEXT NOT NULL DEFAULT '',
{chat_column}            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            UNIQUE (platform, post_id)
        );
        CREATE TABLE archive_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            archive_id INTEGER NOT NULL REFERENCES archives (id),
            telegram_chat_id TEXT NOT NULL DEFAULT '',
            telegram_message_id INTEGER NOT NULL,
            media_index INTEGER NOT NULL DEFAULT 0,
            filename TEXT NOT NULL DEFAULT '',
            media_type TEXT NOT NULL DEFAULT ''
        );
        PRAGMA user_version = {user_version};
        """
    )
    return conn


def test_migration_drops_legacy_global_unique(tmp_path):
    """One post must become archivable by a second group after migrating."""
    path = str(tmp_path / "archive.db")
    conn = _legacy_db(path)
    row_id = conn.execute(
        "INSERT INTO archives (platform, post_id, submitted_url, username)"
        " VALUES ('x', '9', 'https://x.com/u/9', 'user')"
    ).lastrowid
    conn.execute(
        "INSERT INTO archive_messages (archive_id, telegram_chat_id, telegram_message_id)"
        " VALUES (?, '-100', 5)",
        (row_id,),
    )
    conn.commit()
    conn.close()

    conn = init_db(path)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    # Chat id is recovered from the Telegram message already recorded.
    assert conn.execute(
        "SELECT telegram_chat_id FROM archives WHERE id = ?", (row_id,)
    ).fetchone()[0] == "-100"
    # A second group may archive the same post...
    conn.execute(
        "INSERT INTO archives (platform, post_id, telegram_chat_id) VALUES ('x', '9', '-200')"
    )
    # ...but the same group still may not archive it twice.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO archives (platform, post_id, telegram_chat_id) VALUES ('x', '9', '-200')"
        )
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM archives").fetchone()[0] == 2
    conn.close()


def test_legacy_row_without_messages_keeps_empty_chat(tmp_path):
    """History we cannot attribute to a chat is not silently assigned one."""
    path = str(tmp_path / "archive.db")
    conn = _legacy_db(path)
    row_id = conn.execute(
        "INSERT INTO archives (platform, post_id) VALUES ('x', '11')"
    ).lastrowid
    conn.commit()
    conn.close()

    conn = init_db(path)
    assert conn.execute(
        "SELECT telegram_chat_id FROM archives WHERE id = ?", (row_id,)
    ).fetchone()[0] == ""
    conn.close()


def test_init_repairs_half_migrated_database(tmp_path):
    """The exact state behind 'UNIQUE constraint failed: archives.platform, archives.post_id'."""
    path = str(tmp_path / "archive.db")
    conn = _legacy_db(path, include_chat_column=True, user_version=2)
    conn.execute(
        "INSERT INTO archives (platform, post_id, telegram_chat_id)"
        " VALUES ('tiktok', '7', '-100')"
    )
    conn.commit()
    conn.close()

    conn = init_db(path)

    conn.execute(
        "INSERT INTO archives (platform, post_id, telegram_chat_id)"
        " VALUES ('tiktok', '7', '-200')"
    )
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM archives").fetchone()[0] == 2
    conn.close()


def test_reinit_keeps_schema_version(tmp_path):
    """schema.sql must not reset the migration stamp on every startup."""
    path = str(tmp_path / "archive.db")
    init_db(path).close()
    conn = init_db(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    conn.close()
