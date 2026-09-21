"""Startup recovery: fail rows interrupted by a restart, sweep orphan job dirs.

A row in ``extracting``/``downloading``/``uploading`` cannot resume after the
process died; marking it failed keeps ``archived`` truthful and keeps the row
retryable via resubmission. Orphan ``job-*`` temp dirs are removed so crashes
and killed containers cannot leak disk space.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3

from . import repository

log = logging.getLogger("bookmedia")

INTERRUPTED = ("extracting", "downloading", "uploading")


def recover_interrupted(conn: sqlite3.Connection) -> int:
    """Fail rows stuck mid-pipeline from a previous run. Returns the count."""
    total = 0
    for status in INTERRUPTED:
        for row in repository.find_by_status(conn, status):
            repository.fail_interrupted(
                conn, row["id"], error=f"interrupted by restart while {status}"
            )
            log.warning("recovery: archive #%s failed (was %s)", row["id"], status)
            total += 1
    return total


def sweep_temp_dirs(temp_dir: str) -> int:
    """Remove leftover per-job temp dirs from a previous run. Returns the count."""
    removed = 0
    try:
        entries = sorted(os.listdir(temp_dir))
    except OSError as exc:
        log.warning("recovery: temp sweep skipped: %s", exc.strerror or exc)
        return 0
    for name in entries:
        if not name.startswith("job-"):
            continue
        shutil.rmtree(os.path.join(temp_dir, name), ignore_errors=True)
        log.warning("recovery: removed orphan temp dir %s", name)
        removed += 1
    return removed