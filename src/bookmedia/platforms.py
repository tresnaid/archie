"""Minimal platform detection for Phase 1 input validation.

Full per-platform extraction arrives in Phase 2; this module only answers
\"is this URL from a supported platform?\" so the bot can reject the rest
with a short useful error.
"""

from __future__ import annotations

from urllib.parse import urlsplit

SUPPORTED_PLATFORMS = ("instagram", "tiktok", "x", "threads", "youtube")

# base host suffix -> platform name
_HOST_TABLE = (
    ("instagram.com", "instagram"),
    ("tiktok.com", "tiktok"),
    ("x.com", "x"),
    ("twitter.com", "x"),
    ("threads.com", "threads"),
    ("threads.net", "threads"),
    ("youtube.com", "youtube"),
    ("youtu.be", "youtube"),
)


def detect_platform(url: str) -> str | None:
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return None
    for base, platform in _HOST_TABLE:
        if host == base or host.endswith("." + base):
            return platform
    return None
