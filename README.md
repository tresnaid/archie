# ARCHIE

A self-hosted Telegram bot for archiving and preserving social media posts, videos, and threads directly into Telegram storage with SQLite indexing.

[![Python](https://img.shields.io/badge/Python-3.12+-3776AB?style=flat-square&logo=python&logoColor=white)](https://python.org/)
[![Telegram Bot API](https://img.shields.io/badge/Telegram-Bot_API-26A5E4?style=flat-square&logo=telegram&logoColor=white)](https://core.telegram.org/bots/api)
[![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?style=flat-square&logo=docker&logoColor=white)](https://www.docker.com/)
[![SQLite](https://img.shields.io/badge/Database-SQLite-003B57?style=flat-square&logo=sqlite&logoColor=white)](https://www.sqlite.org/)
[![yt--dlp](https://img.shields.io/badge/Extractor-yt--dlp-red?style=flat-square)](https://github.com/yt-dlp/yt-dlp)
[![Tests: pytest](https://img.shields.io/badge/Tests-pytest-0A9EDC?style=flat-square&logo=pytest&logoColor=white)](https://docs.pytest.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green?style=flat-square)](LICENSE)

---

## Overview

Social platforms are volatile. Research indicates that over 25% of webpages from the past decade are no longer accessible, and social media posts are frequently deleted, suspended, or altered. Platform-native bookmarks only store links, which break when the source post disappears.

**ARCHIE** stores the actual media and post content:
- Share a link into your private Telegram group or topic.
- Archie extracts the media, re-uploads it into Telegram, and records full metadata and original publication timestamps in a local SQLite index.
- No external cloud storage subscriptions, third-party accounts, or proprietary databases required.

---

## Supported Sources

- **Instagram** (Posts, Reels, Carousels)
- **TikTok** (Videos, Slideshows)
- **X / Twitter** (Videos, Images, Thread Text)
- **Threads** (Media and Text posts)

*Platform extraction leverages yt-dlp and modular extractors with optional authenticated session cookie support.*

---

## Usage

### Simple Archiving
Paste any supported link directly into your Telegram group:

```text
https://x.com/user/status/1234567890
```

### Custom Categories
Append an optional category flag to tag and organize entries:

```text
https://x.com/user/status/1234567890 --type=research
https://x.com/user/status/1234567890 --type=design
https://x.com/user/status/1234567890 --type=dev
```

When no type is specified, the entry defaults to `type = unsorted`.

### How Media is Stored
- **Single Media:** `username__YYYYMMDD_HHMMSS.mp4`
- **Carousels / Multiple Media:**
  - `username__YYYYMMDD_HHMMSS(1).jpg`
  - `username__YYYYMMDD_HHMMSS(2).jpg`
  - `username__YYYYMMDD_HHMMSS(3).mp4`

The filename preserves the original post's publication timestamp whenever provided by the upstream platform.

---

## Reliability & Fault Tolerance

- **Atomic Message Tracking:** Every sent Telegram message is recorded immediately. Partially uploaded carousels retain their uploaded message IDs and are marked `failed` rather than `archived` until all items succeed.
- **Safe Retries:** Failed jobs store a bounded error description. Sending the same link again automatically re-triggers processing without duplicating database records.
- **Crash Recovery:** If the container restarts mid-pipeline, interrupted rows are marked as `failed` on startup and orphaned temporary files are safely swept.
- **Health Heartbeat:** The container maintains a live heartbeat poll loop and reports health status through `docker compose ps`.

---

## Architecture & Requirements

### Tech Stack
- **Runtime:** Python 3.12+
- **Containerization:** Docker & Docker Compose
- **Database:** Embedded SQLite
- **Extractor:** `yt-dlp` (pinned via `pyproject.toml`)
- **Testing:** `pytest` (unit and integration test suites)

```text
archie/
├── src/
│   └── bookmedia/      # Bot core, polling engine, extractors, DB models
├── tests/              # Unit & integration test suites
├── cookies/            # Optional platform authentication cookies
├── docker-compose.yml  # Multi-service container orchestration
├── Dockerfile          # Production container build
├── pyproject.toml      # Python build configuration and dependencies
└── requirements.txt    # Frozen pip dependencies
```

---

## Setup & Deployment

### 1. Prerequisites
- Docker & Docker Compose
- Telegram Bot Token (from [@BotFather](https://t.me/BotFather))

### 2. Configuration
Copy the environment template:

```bash
cp .env.example .env
```

Configure your `.env` variables:

```dotenv
TELEGRAM_BOT_TOKEN=123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ
TELEGRAM_ALLOWED_USER_IDS=12345678,87654321
DATA_DIR=/app/data
TEMP_DIR=/app/tmp
COOKIES_PATH=/app/cookies/cookies.txt
LOG_LEVEL=INFO
```

### 3. Initialize Directories & Start Service

Create host mount points:

```bash
mkdir -p data tmp cookies
```

Start the container in the background:

```bash
docker compose up -d
```

Check container health:

```bash
docker compose ps
docker compose logs -f
```

---

## Status & Operations

### Health & Pipeline Inspection

Inspect archive counts, in-flight jobs, and pipeline status:

```bash
PYTHONPATH=src python3 -m bookmedia.status --db ./data/archive.db
```

Useful inspection flags:
- `--status uploading`: View jobs currently in-flight
- `--limit 20`: Show recent activity
- `--stale-minutes 10`: Adjust stuck job detection threshold

### Safe Database Backup

Safely back up the active SQLite database without stopping the container:

```bash
sqlite3 ./data/archive.db ".backup './data/archive-backup.db'"
```

---

## Monitoring Dashboard

Archie includes an optional local web dashboard for monitoring archive status, reviewing logs, and retrying failed links.

To enable, set `DASHBOARD_PORT=8080` in `.env` and restart. Access the dashboard at:  
`http://127.0.0.1:8080/`

- **Live Auto-Refresh:** Updates archive status every 5 seconds.
- **Search & Filter:** Search by URL, platform, username, or category tag.
- **Operator Actions:** One-click retry for failed rows or cancel in-flight jobs.
- **Health Endpoint:** `GET /api/health` machine-readable health check.

---

## Running Tests

Run the test suite with pytest:

```bash
pip install -r requirements.txt
pytest -q
```

---

## License

This project is open-source software licensed under the [MIT License](LICENSE).
