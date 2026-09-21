-- Baseline schema. Migrations that change existing data live in db.py and bump
-- PRAGMA user_version; this file is safe to run on every startup because every
-- statement is guarded by IF NOT EXISTS. It must not set user_version itself,
-- or it would erase the migration stamp and re-trigger migrations forever.

CREATE TABLE IF NOT EXISTS archives (
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
    telegram_chat_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (telegram_chat_id, platform, post_id)
);

CREATE TABLE IF NOT EXISTS archive_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    archive_id INTEGER NOT NULL REFERENCES archives (id),
    telegram_chat_id TEXT NOT NULL DEFAULT '',
    telegram_message_id INTEGER NOT NULL,
    media_index INTEGER NOT NULL DEFAULT 0,
    filename TEXT NOT NULL DEFAULT '',
    media_type TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_archives_status ON archives (status);
CREATE INDEX IF NOT EXISTS idx_archives_post_id ON archives (post_id);
CREATE INDEX IF NOT EXISTS idx_archive_messages_archive ON archive_messages (archive_id);
