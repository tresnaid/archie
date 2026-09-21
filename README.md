# Telegram Social Archive

A small self-hosted Telegram bot for preserving social-media posts.

Share a link into your private Telegram group and the bot archives the underlying media or post content into Telegram.

No AI, LLM, automatic classification, or external storage service is required.

## Why This Exists

Social platforms are not archives.

- Pew Research Center found that a quarter of all webpages that existed between 2013 and 2023 were no longer accessible by October 2023, and 38% of webpages from 2013 are gone a decade later (https://www.pewresearch.org/data-labs/2024/05/17/when-online-content-disappears/).
- On X/Twitter, half of the posts that are eventually removed become unavailable within six days of being posted, and 90% within 46 days.
- Platform-native saves (favorites, collections, bookmarks) disappear with the post, the account, or the platform itself.

A bookmark stores a pointer, not the content. This bot stores the content: media is re-uploaded into a Telegram group you already use, with the original publication timestamp preserved in the filename and a SQLite index recording what was archived, when, and from where.

The design goal is minimal friction: see a post, paste the link into a chat, done. No new app, account, or storage service.

## Supported Sources

Initial target platforms:

- Instagram
- TikTok
- X / Twitter
- Threads

Platform support depends on the available extractors and may require authenticated cookies for some content.

## Usage

Send:

`https://x.com/user/status/123`

Or optionally categorize it:

`https://x.com/user/status/123 --type=research`

The bot downloads the post media, re-uploads it into the chat, and records it
in the local index, then replies with a short summary (author, platform,
media count, category).

Posts with no downloadable media are archived as a text message instead.

## Categories

Categories are optional and user-defined.

Examples:

`--type=research`

`--type=github`

`--type=idea`

When no type is supplied:

`type = unsorted`

There is intentionally no predefined category list.

## Filenames

Single media:

`username__YYYYMMDD_HHMMSS.mp4`

Example:

`user__20260910_142530.mp4`

Multiple media:

`user__20260910_142530(1).jpg`

`user__20260910_142530(2).jpg`

`user__20260910_142530(3).mp4`

The timestamp represents the original post publication time when available.

## Storage

Telegram stores archived media.

SQLite stores the archive index and metadata.

Temporary media exists locally only while a post is being processed and should be deleted after successful upload.

## Reliability

- Every sent Telegram message is recorded immediately, so a partially
  uploaded post keeps its uploaded message IDs and is marked `failed`
  instead of `archived`.
- Any failure leaves a stored, bounded error and the row stays retryable:
  resend the same link to retry it.
- After a restart, rows the dead process left mid-pipeline are marked
  `failed` (`interrupted by restart while …`) and orphan temp files are
  swept; resend to retry.
- The container reports `healthy` while the poll loop is alive
  (`docker compose ps`).

## Requirements

Recommended deployment:

- Docker
- Docker Compose

Application stack:

- Python
- Telegram Bot API
- SQLite
- yt-dlp for metadata extraction (pinned in `pyproject.toml`)

## Configuration

Create `.env` from `.env.example`.

Example variables (see `.env.example` for the full list):

`TELEGRAM_BOT_TOKEN=`

`TELEGRAM_ALLOWED_USER_IDS=` (optional)

`DATA_DIR=/app/data`

`TEMP_DIR=/app/tmp`

`COOKIES_PATH=/app/cookies/cookies.txt` (optional; used by the extractor when the file exists)

`LOG_LEVEL=INFO`

Never commit real credentials.

## Running

Copy `.env.example` to `.env` and fill in your values:

`cp .env.example .env`

Create the host mount points as your own user (Docker would otherwise
create them as root and the container could not write to them):

`mkdir -p data tmp cookies`

Start (the bot polls Telegram for new messages from any group it has been added to):

`docker compose up -d`

The bot must be a member of any group you want to use it in, with permission to read
messages (disable privacy mode via @BotFather or make it an admin); otherwise it
hears nothing and stays silent.

In groups with topics enabled, the bot posts the archive and its replies into the
same topic the link was shared in.

Logs:

`docker compose logs`

Every ignored message logs its reason, so silence is always explainable.

Container health (healthy while the poll loop's heartbeat is fresh):

`docker compose ps`

Status (liveness plus archive counts, in-flight/STUCK rows, idle detection,
per-row last-update ages; exit 1 when polling is stale):

`PYTHONPATH=src python3 -m bookmedia.status --db ./data/archive.db`

Useful flags: `--status uploading` lists only rows in that state,
`--limit 20` shows more rows, `--stale-minutes 10` widens the stuck
detection window.

Backup (safe to run while the bot is up; never copy `archive.db` directly):

`sqlite3 ./data/archive.db ".backup './data/archive-backup.db'"`

Treat the backup as private — it contains your archive history.

## Monitoring Dashboard

Optional, disabled by default. Set `DASHBOARD_PORT=8080` in `.env`, restart,
and open `http://127.0.0.1:8080/` on the same machine (localhost only):

- archive table with live auto-refresh (every 5 s)
- filter by status, platform, type, or free-text search; sort by any column
  (click a header) — the selection is kept in the URL, so it survives
  refresh and can be bookmarked
- per-row Retry (`failed`/`received` rows re-run the real pipeline) and
  Cancel (mark an in-flight row `failed`)
- `GET /api/health` — machine-readable heartbeat for scripts

The dashboard never exposes the bot token and is an operator tool, not a
public web application.

Stop (database in `./data` is preserved):

`docker compose down`

Run tests on the host:

`pip install -r requirements.txt`

`pytest -q`

## Documentation

This project's operational documentation lives in the maintainer's private repository.
