"""Startup: validate env, ensure dirs, init DB, then poll Telegram."""

from __future__ import annotations

import logging
import os
import sys
from functools import partial

from . import bot, recovery, service
from .config import load_settings
from .db import init_db
from .downloader import download as download_media
from .extract import extract
from .logging_setup import setup_logging
from .telegram import BotClient

log = logging.getLogger("bookmedia")


def main() -> int:
    try:
        settings = load_settings()
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    setup_logging(settings.log_level)
    try:
        os.makedirs(settings.data_dir, exist_ok=True)
        os.makedirs(settings.temp_dir, exist_ok=True)
        conn = init_db(settings.db_path)
    except OSError as exc:
        log.error("startup failed: %s", exc.strerror or exc)
        return 1
    except Exception as exc:  # noqa: BLE001 - startup must report any DB failure
        log.error("startup failed: %s: %s", type(exc).__name__, exc)
        return 1
    try:
        interrupted = recovery.recover_interrupted(conn)
        orphans = recovery.sweep_temp_dirs(settings.temp_dir)
        if interrupted or orphans:
            log.info(
                "startup recovery: %d interrupted row(s) marked failed,"
                " %d orphan temp dir(s) removed",
                interrupted, orphans,
            )
    except Exception as exc:  # noqa: BLE001 - recovery must never block startup
        log.warning("startup recovery skipped: %s", type(exc).__name__)
    if os.path.isfile(settings.cookies_path):
        # Path only, never contents — cookies are secrets.
        log.info("extractor cookies: using %s", settings.cookies_path)
    else:
        log.info(
            "extractor cookies: none found at %s (public content only)",
            settings.cookies_path,
        )
    log.info("ready; starting telegram polling")
    # The pipeline client MUST use the local Bot API server when configured:
    # cloud uploads read the whole file into RAM (OOM on 1 GB+ media) and cap
    # bot uploads at 50 MB. The "(local Bot API)" log line below comes from
    # run_forever's separate poll client, which hid this for a long time.
    client = BotClient(settings.bot_token,
                       local_base_url=settings.local_bot_api_url)
    extractor = partial(extract, cookies_path=settings.cookies_path)
    downloader = partial(download_media, cookies_path=settings.cookies_path,
                         max_bytes=settings.max_media_bytes)
    process = partial(
        service.process_submission,
        conn=conn,
        client=client,
        temp_dir=settings.temp_dir,
        extract=extractor,
        download=downloader,
    )

    # Optional embedded monitoring dashboard
    dashboard_thread = None
    if settings.dashboard_port > 0:
        from .dashboard import _Handler, run_dashboard
        from .retry import AlreadyQueued, InvalidState, RetryQueue

        retry_queue = RetryQueue(
            db_path=settings.db_path,
            temp_dir=settings.temp_dir,
            cookies_path=settings.cookies_path,
            bot_token=settings.bot_token,
            local_bot_api_url=settings.local_bot_api_url,
            max_media_bytes=settings.max_media_bytes,
        )
        _Handler.retry_error_types = (AlreadyQueued, InvalidState)
        dashboard_thread = run_dashboard(
            port=settings.dashboard_port,
            retry_fn=retry_queue.retry,
            db_path=settings.db_path,
        )
        log.info("dashboard enabled on port %d (localhost only)", settings.dashboard_port)

    bot.run_forever(settings, process)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
