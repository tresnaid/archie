"""Deterministic archive filenames: ``{username}__{YYYYMMDD_HHMMSS}.{ext}``."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def sanitize_username(name: str) -> str:
    """Replace filesystem-unsafe characters with ``_``; never return empty."""
    clean = _SAFE.sub("_", name.strip()).strip(" .,_")
    return clean or "unknown"


def format_timestamp(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y%m%d_%H%M%S")


def build_filename(username: str, when: datetime, extension: str,
                   index: int = 0, total: int = 1) -> str:
    ext = extension.lower().lstrip(".")
    stem = f"{sanitize_username(username)}__{format_timestamp(when)}"
    if total > 1:
        stem += f"({index + 1})"
    return f"{stem}.{ext}"


def assign_filenames(paths: list[str], username: str, when: datetime) -> list[str]:
    """Map downloaded files (already in upload order) to convention names."""
    total = len(paths)
    return [
        build_filename(username, when, Path(p).suffix, index=i, total=total)
        for i, p in enumerate(paths)
    ]
