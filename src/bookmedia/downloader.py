"""Media download via yt-dlp into a per-job temp dir, renamed to convention."""

from __future__ import annotations

import glob
import os
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

import yt_dlp

from .extract import _writable_cookies
from .filenames import assign_filenames

_FORMAT = "bv*[ext=mp4]+ba[ext=m4a]/bv+ba/b[ext=mp4]/bv/b"

# Default per-post media ceiling. The upload ceilings are 50 MB (cloud) and
# 2000 MB (local Bot API); refusing oversized media before download avoids
# wasting an hour of downloading on a post that can never be uploaded.
# Override with MAX_MEDIA_BYTES in the environment; 0 disables the check.
DEFAULT_MAX_MEDIA_BYTES = 2000 * 1024 * 1024

# Running size estimates from HLS manifests are noisy early on (they grow
# fragment by fragment), so the mid-download guard only acts on estimates
# once this much has actually been downloaded.
_SIZE_GUARD_FLOOR = 64 * 1024 * 1024


class MediaTooLargeError(Exception):
    """Estimated media size exceeds the configured ceiling (before download)."""


@dataclass
class Downloaded:
    path: str
    filename: str


class DownloadError(Exception):
    pass


def _info_size_bytes(info: dict[str, Any]) -> int:
    """Best-effort total media size from yt-dlp metadata (0 when unknown).

    Sums filesize/filesize_approx across the selected formats of each entry;
    falls back to the request format's video+audio pair for playlists.
    """
    total = 0

    def entry_size(entry: dict[str, Any]) -> int:
        best = 0
        for fmt in entry.get("requested_formats") or []:
            best += fmt.get("filesize") or fmt.get("filesize_approx") or 0
        if best:
            return best
        fmt_id = entry.get("format_id") or ""
        for fmt in entry.get("formats") or []:
            if fmt.get("format_id") == fmt_id:
                return fmt.get("filesize") or fmt.get("filesize_approx") or 0
        return 0

    entries = info.get("entries") if isinstance(info, dict) else None
    if entries is None and isinstance(info, dict):
        total = entry_size(info)
    else:
        for entry in entries or []:
            if isinstance(entry, dict):
                total += entry_size(entry)
    return int(total)


def estimated_media_bytes(post, *, cookies_path: str | None = None,
                           ydl_factory: Callable[[dict[str, Any]], Any] = yt_dlp.YoutubeDL) -> int:
    """Metadata-only size probe for a post's canonical URL (0 when unknown).

    Never downloads media. The download gate in :func:`download` compares the
    returned estimate against its ``max_bytes`` ceiling.
    """
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "socket_timeout": 30,
        "retries": 3,
        "extractor_retries": 3,
    }
    tmp_cookies = _writable_cookies(cookies_path)
    if tmp_cookies:
        opts["cookiefile"] = tmp_cookies
    try:
        info = ydl_factory(opts).extract_info(post.canonical_url, download=False)
    finally:
        if tmp_cookies:
            try:
                os.unlink(tmp_cookies)
            except OSError:
                pass
    return _info_size_bytes(info or {})


def _make_size_guard(max_bytes: int) -> Callable[[dict[str, Any]], None]:
    """Progress hook that aborts a download crossing the size ceiling.

    Two checks: hard abort when downloaded bytes themselves exceed the
    ceiling, and estimate-based abort (after ``_SIZE_GUARD_FLOOR`` downloaded)
    when yt-dlp's running total estimate crosses it — fragmented HLS manifests
    only reveal the true size progressively, so the pre-download probe can
    underestimate.
    """

    def guard(status: dict[str, Any]) -> None:
        if status.get("status") != "downloading":
            return
        done = status.get("downloaded_bytes") or 0
        if done > max_bytes:
            raise MediaTooLargeError(
                f"media exceeded the {max_bytes / (1 << 20):.0f} MB limit "
                f"while downloading ({done / (1 << 20):.0f} MB downloaded) — "
                f"aborted mid-download"
            )
        if done < _SIZE_GUARD_FLOOR:
            return
        estimate = status.get("total_bytes") or status.get("total_bytes_estimate") or 0
        if estimate > max_bytes:
            raise MediaTooLargeError(
                f"media estimated at {estimate / (1 << 20):.0f} MB while "
                f"downloading, over the {max_bytes / (1 << 20):.0f} MB limit — "
                f"aborted mid-download"
            )

    return guard


def download(post, work_dir: str, *,
             ydl_factory: Callable[[dict[str, Any]], Any] = yt_dlp.YoutubeDL,
             cookies_path: str | None = None,
             max_bytes: int | None = None) -> list[Downloaded]:
    os.makedirs(work_dir, exist_ok=True)
    if max_bytes and max_bytes > 0 and _has_video_urls(post):
        estimated = estimated_media_bytes(
            post, cookies_path=cookies_path, ydl_factory=ydl_factory
        )
        if estimated > max_bytes:
            raise MediaTooLargeError(
                f"media too large: {estimated / (1 << 20):.0f} MB estimated, "
                f"over the {max_bytes / (1 << 20):.0f} MB limit — post not "
                f"downloaded."
            )
    opts: dict[str, Any] = {
        "outtmpl": os.path.join(work_dir, "raw_%(autonumber)s.%(ext)s"),
        "format": _FORMAT,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 3,
        "file_access_retries": 3,
    }
    if max_bytes and max_bytes > 0:
        opts["progress_hooks"] = [_make_size_guard(max_bytes)]
    tmp_cookies = _writable_cookies(cookies_path)
    if tmp_cookies:
        opts["cookiefile"] = tmp_cookies
    try:
        ydl_factory(opts).download([post.canonical_url])
    except MediaTooLargeError:
        raise
    except Exception as exc:
        if _has_photo_urls(post):
            return _download_urls(post, work_dir)
        raise DownloadError(f"media download failed: {exc}") from exc
    finally:
        if tmp_cookies:
            try:
                os.unlink(tmp_cookies)
            except OSError:
                pass
    raws = sorted(glob.glob(os.path.join(work_dir, "raw_*")))
    if not raws:
        if _has_photo_urls(post):
            return _download_urls(post, work_dir)
        raise DownloadError("media download produced no files")
    return _rename_raws(raws, post, work_dir)


def _has_photo_urls(post) -> bool:
    return bool(post.media and all(
        m.source_url.startswith("http") for m in post.media
    ))


def _has_video_urls(post) -> bool:
    """True when the download routes through yt-dlp (not pure direct URLs)."""
    return not _has_photo_urls(post)


def _download_urls(post, work_dir: str) -> list[Downloaded]:
    """Download media directly from source URLs with browser headers."""
    raws = []
    for item in sorted(post.media, key=lambda m: m.order):
        if not item.source_url:
            continue
        ext = item.extension or "jpg"
        raw_path = os.path.join(work_dir, f"raw_{item.order + 1}.{ext}")
        req = urllib.request.Request(item.source_url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://www.google.com/",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as resp, open(raw_path, "wb") as f:
                f.write(resp.read())
            raws.append(raw_path)
        except Exception as exc:
            raise DownloadError(f"direct download failed: {exc}") from exc
    if not raws:
        raise DownloadError("direct download produced no files")
    return _rename_raws(raws, post, work_dir)


def _rename_raws(raws: list[str], post, work_dir: str) -> list[Downloaded]:
    when = post.published_at or datetime.now(timezone.utc)
    names = assign_filenames(raws, post.username or "unknown", when)
    files = []
    for raw, name in zip(raws, names):
        dest = os.path.join(work_dir, name)
        os.rename(raw, dest)
        files.append(Downloaded(path=dest, filename=name))
    return files
