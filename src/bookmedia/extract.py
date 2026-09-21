"""Platform router + yt-dlp extraction backend.

Conceptual interface: ``extract(url) -> Post``. Per-platform backends live in
``BACKENDS`` so they stay replaceable; Telegram behavior stays out of here.
"""

from __future__ import annotations

import html
import json
import os
import re
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import quote, urlencode, urlparse, urlsplit

from .platforms import SUPPORTED_PLATFORMS, detect_platform
from .urls import normalize_url

_PHOTO_EXTENSIONS = frozenset({"jpg", "jpeg", "png", "webp", "gif"})
_VIDEO_EXTENSIONS = frozenset({"mp4", "mov", "m4v", "webm", "mkv"})


class ExtractionError(Exception):
    """Any extraction failure (unsupported host, backend error, unparseable data)."""


class UnsupportedPlatformError(ExtractionError):
    """No extractor registered for the URL's host."""


@dataclass(frozen=True)
class MediaItem:
    source_url: str  # may be empty when only a page URL was resolved (Phase 3 re-resolves)
    media_type: str  # "photo" | "video" | "" when unknown
    extension: str
    order: int


@dataclass(frozen=True)
class Post:
    platform: str
    post_id: str  # stable source id, else normalized canonical URL (never empty)
    canonical_url: str
    username: str
    text: str
    published_at: datetime | None
    media: tuple[MediaItem, ...] = field(default_factory=tuple)


def _media_type(extension: str) -> str:
    ext = extension.lower()
    if ext in _PHOTO_EXTENSIONS:
        return "photo"
    if ext in _VIDEO_EXTENSIONS:
        return "video"
    return ""


def _published_at(info: dict) -> datetime | None:
    stamp = info.get("timestamp")
    if isinstance(stamp, (int, float)) and stamp > 0:
        return datetime.fromtimestamp(stamp, timezone.utc)
    upload_date = info.get("upload_date") or ""
    if len(upload_date) == 8 and upload_date.isdigit():
        return datetime(
            int(upload_date[:4]), int(upload_date[4:6]), int(upload_date[6:8]),
            tzinfo=timezone.utc,
        )
    return None


def _username(platform: str, info: dict) -> str:
    if platform == "instagram":
        return info.get("uploader") or info.get("uploader_id") or info.get("channel") or ""
    if platform == "tiktok":
        raw = info.get("uploader") or info.get("channel") or info.get("uploader_id") or ""
        return raw.lstrip("@") if raw else ""
    return info.get("uploader_id") or info.get("uploader") or info.get("channel") or ""


def post_from_info(platform: str, url: str, info: dict) -> Post:
    canonical = normalize_url(info.get("webpage_url") or url)
    raw_id = info.get("id")
    post_id = str(raw_id) if raw_id else canonical
    entries = info.get("entries") or [info]
    media = tuple(
        MediaItem(
            source_url=entry.get("url") or "",
            media_type=_media_type(entry.get("ext") or ""),
            extension=entry.get("ext") or "",
            order=n,
        )
        for n, entry in enumerate(e for e in entries if isinstance(e, dict))
    )
    return Post(
        platform=platform,
        post_id=post_id,
        canonical_url=canonical,
        username=_username(platform, info),
        text=info.get("description") or "",
        published_at=_published_at(info),
        media=media,
    )


def _writable_cookies(cookies_path: str | None) -> str | None:
    """Copy cookies to a writable temp file so yt-dlp can't fail on read-only mounts."""
    if not cookies_path or not os.path.isfile(cookies_path):
        return None
    import shutil, tempfile
    fd, tmp = tempfile.mkstemp(suffix=".txt", prefix="cookies_")
    os.close(fd)
    shutil.copy2(cookies_path, tmp)
    return tmp


def ytdlp_extract(platform: str, url: str, *, cookies_path: str | None = None,
                  ydl_factory=None) -> Post:
    """Metadata-only extraction (``--skip-download``); media download is Phase 3."""
    from yt_dlp import YoutubeDL

    factory = ydl_factory or YoutubeDL
    options: dict = {"quiet": True, "no_warnings": True, "skip_download": True,
                     "socket_timeout": 30, "retries": 3, "fragment_retries": 3,
                     "extractor_retries": 3, "file_access_retries": 3}
    tmp_cookies = _writable_cookies(cookies_path)
    if tmp_cookies:
        options["cookiefile"] = tmp_cookies
    try:
        with factory(options) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        raise ExtractionError(f"{platform} extraction failed: {exc}") from exc
    finally:
        if tmp_cookies:
            try:
                os.unlink(tmp_cookies)
            except OSError:
                pass
    return post_from_info(platform, url, info)


BACKENDS = {platform: ytdlp_extract for platform in SUPPORTED_PLATFORMS}


_TIKWM_API = "https://www.tikwm.com/api/"


def _tikwm_info(url: str) -> dict:
    """Return the tikwm.com API response ``data`` dict, or ``{}``."""
    payload = urlencode({"url": url, "hd": "1"}).encode()
    request = urllib.request.Request(
        _TIKWM_API, data=payload,
        headers={"User-Agent": "Mozilla/5.0",
                 "Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception:
        return {}
    if data.get("code") != 0:
        return {}
    return data.get("data") or {}


_TIKTOK_PHOTO = re.compile(r"/photo/(\d+)")


def _is_audio_only(post: Post) -> bool:
    """True when yt-dlp's web extraction saw only a soundtrack (mp3/m4a).

    Some TikTok video posts expose only the music track to the anonymous
    web extraction; the pictures are gone but the audio remains.
    """
    return bool(post.media) and all(
        item.extension.lower() in ("mp3", "m4a", "aac", "opus", "wav")
        for item in post.media
    )


def _tikwm_rescue(url: str) -> Post | None:
    """Re-extract via tikwm.com: slideshow images, else the real video."""
    info = _tikwm_info(url)
    if not info:
        return None
    raw_id = url.rstrip("/").split("/")[-1]
    username = (info.get("author") or {}).get("unique_id") or ""
    raw_images = info.get("images") or []
    if raw_images:
        media = tuple(
            MediaItem(source_url=u, media_type="photo",
                      extension=_ext_from_url(u), order=i)
            for i, u in enumerate(raw_images)
        )
    else:
        video_url = info.get("hdplay") or info.get("play") or ""
        if not video_url:
            return None
        ext = _ext_from_url(video_url)
        if ext == "jpg":
            ext = "mp4"
        media = (MediaItem(
            source_url=video_url, media_type="video",
            extension=ext, order=0,
        ),)
    return Post(
        platform="tiktok", post_id=raw_id, canonical_url=url,
        username=username, text="", published_at=None, media=media,
    )


def tiktok_extract(platform: str, url: str, *, cookies_path: str | None = None) -> Post:
    """yt-dlp for video posts; tikwm.com API fallback for photo slides."""
    if _TIKTOK_PHOTO.search(urlparse(url).path):
        post = _tikwm_rescue(url)
        if post is None or not any(m.media_type == "photo" for m in post.media):
            raise ExtractionError("tiktok photo extraction failed: no images returned")
        return post
    try:
        post = ytdlp_extract(platform, url, cookies_path=cookies_path)
    except ExtractionError as exc:
        rescued = _tikwm_rescue(url)
        if rescued is not None:
            return rescued
        raise
    if _is_audio_only(post):
        rescued = _tikwm_rescue(url)
        if rescued is not None:
            return rescued
    return post


BACKENDS["tiktok"] = tiktok_extract


_IG_NO_MEDIA = ("No video formats found", "There is no video in this post",
                "empty media response")


def ig_extract(platform: str, url: str, *, cookies_path: str | None = None) -> Post:
    try:
        return ytdlp_extract(platform, url, cookies_path=cookies_path)
    except ExtractionError as exc:
        msg = str(exc)
        if not any(pat in msg for pat in _IG_NO_MEDIA):
            raise
    images = _ig_images(url)
    if images:
        match = _INSTAGRAM_shortcode.search(url)
        shortcode = match.group(1) if match else url
        return Post(
            platform="instagram", post_id=shortcode or url, canonical_url=url,
            username="", text="", published_at=None, media=tuple(images),
        )
    raise ExtractionError(
        "instagram photo/carousel extraction requires cookies or is otherwise unsupported"
    )


_INSTAGRAM_shortcode = re.compile(r"/p/([A-Za-z0-9_-]+)")


_IG_DISPLAY_URI = re.compile(r'"display_uri"\s*:\s*"([^"]+)"', re.IGNORECASE)


def _ig_images(url: str) -> list[MediaItem]:
    """Scrape media URLs from the Instagram post page HTML.

    Extracts ``display_uri`` values from the embedded JSON data (carousel
    slides + single images).  Falls back to ``og:image`` if no JSON data
    is found.
    """
    request = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X)"
                       " AppleWebKit/605.1.15 (KHTML, like Gecko)"
                       " Version/16.0 Mobile/15E148 Safari/604.1",
        "Accept": "text/html,application/xhtml+xml",
    })
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8", errors="replace")
    except Exception:
        return []
    seen: list[str] = []
    seen_set: set[str] = set()
    for raw in _IG_DISPLAY_URI.findall(body):
        u = html.unescape(raw.replace("\\/", "/"))
        if u not in seen_set:
            seen.append(u)
            seen_set.add(u)
    if not seen:
        for raw in _OG_IMAGE.findall(body):
            u = html.unescape(raw)
            if u not in seen_set:
                seen.append(u)
                seen_set.add(u)
    return [
        MediaItem(source_url=u, media_type="photo", extension=_ext_from_url(u), order=i)
        for i, u in enumerate(seen)
    ]


BACKENDS["instagram"] = ig_extract


_TWEET_ID = re.compile(r"/status/(\d+)")
_NO_VIDEO = "No video could be found"


def url_has_tweet_id(url: str) -> bool:
    """True when ``url`` points at an X/Twitter post (``/status/<id>``).

    Used to keep the fallbacks honest: an "Unsupported URL" naming a
    different site means yt-dlp followed the tweet's link-out, which is
    still a tweet we can archive from the tweet URL itself.
    """
    return _TWEET_ID.search(urlparse(url).path) is not None


def canonical_for(submitted: str, tweet_url: str) -> str:
    """Prefer the submitted (human) URL; fall back to the ``/i/status`` form."""
    if url_has_tweet_id(submitted):
        try:
            return normalize_url(submitted)
        except ValueError:
            return submitted
    return tweet_url

_OG_IMAGE = re.compile(r'<meta\s+property="og:image"\s+content="([^"]+)"', re.IGNORECASE)


def _ext_from_url(url: str) -> str:
    path = urlparse(url).path.lower()
    for ext in ("jpg", "jpeg", "png", "gif", "webp", "mp4", "webm"):
        if path.endswith(f".{ext}"):
            return ext
    return "jpg"


def _x_syndication(url: str) -> tuple[str, str, list[MediaItem]]:
    """Fetch tweet data from the public syndication API.

    Returns (username, text, media_items). Empty media means text-only.
    """
    match = _TWEET_ID.search(urlparse(url).path)
    if match is None:
        raise ExtractionError(f"x extraction failed: no tweet id in {url}")
    tweet_id = match.group(1)
    api_url = f"https://cdn.syndication.twimg.com/tweet-result?id={tweet_id}&token=x"
    request = urllib.request.Request(api_url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        data = json.loads(response.read().decode("utf-8"))
    username = data.get("user", {}).get("screen_name") or ""
    text = data.get("text") or ""
    published_at = data.get("created_at") or ""
    photos = data.get("photos") or []
    media = tuple(
        MediaItem(source_url=p.get("url", ""), media_type="photo",
                  extension=_ext_from_url(p.get("url", "")), order=i)
        for i, p in enumerate(photos)
    )
    return username, text, media


def x_extract(platform: str, url: str, *, cookies_path: str | None = None) -> Post:
    # yt-dlp follows the tweet's t.co short link to whatever site is linked
    # *inside* the tweet (e.g. infron.ai, b.ai) and reports THAT as
    # webpage_url. Fallbacks must run against the tweet URL itself, so pin it
    # from the /status/<id> before touching yt-dlp.
    match = _TWEET_ID.search(urlparse(url).path)
    tweet_url = (
        f"https://x.com/i/status/{match.group(1)}" if match else url
    )
    try:
        return ytdlp_extract(platform, url, cookies_path=cookies_path)
    except ExtractionError as exc:
        # yt-dlp follows the tweet's t.co short link to whatever site is
        # linked *inside* the tweet (e.g. infron.ai, b.ai) and reports THAT
        # as "Unsupported URL". That is not a login/rate-limit failure —
        # fall through to the tweet-level fallbacks below.
        if "No video could be found" not in str(exc):
            msg = str(exc)
            if "Unsupported URL: https://" in msg:
                linked = msg.split("Unsupported URL:", 1)[1].strip()
                if not url_has_tweet_id(linked):
                    original = exc
                else:
                    raise
            else:
                raise
        else:
            original = exc
    try:
        username, text, media = _x_syndication(tweet_url)
        tweet_id = _TWEET_ID.search(urlparse(tweet_url).path)
        tweet_id = tweet_id.group(1) if tweet_id else tweet_url
        if media:
            return Post(
                platform="x", post_id=tweet_id, canonical_url=canonical_for(url, tweet_url),
                username=username, text=text, published_at=None, media=media,
            )
    except Exception:
        pass
    try:
        return x_text_post(canonical_for(url, tweet_url))
    except ExtractionError as fallback_exc:
        raise ExtractionError(f"{original}; {fallback_exc}") from fallback_exc


BACKENDS["x"] = x_extract


def _oembed_text(embed_html: str) -> str:
    paragraph = re.search(r"<p\b[^>]*>(.*?)</p>", embed_html, re.DOTALL | re.IGNORECASE)
    body = paragraph.group(1) if paragraph else embed_html
    body = re.sub(r"(?i)<br\s*/?>", "\n", body)
    return html.unescape(re.sub(r"<[^>]+>", "", body)).strip()


def x_text_post(url: str) -> Post:
    """Text-only fallback for tweets without video, via X oEmbed.

    Only day-precision dates are available, so ``published_at`` stays unknown
    and the archive-time fallback applies honestly.
    """
    match = _TWEET_ID.search(urlparse(url).path)
    if match is None:
        raise ExtractionError(f"x extraction failed: no tweet id in {url}")
    oembed_url = (
        "https://publish.twitter.com/oembed?url="
        + quote(f"https://x.com/i/status/{match.group(1)}")
    )
    request = urllib.request.Request(oembed_url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
        username = urlparse(data["author_url"]).path.strip("/")
        text = _oembed_text(data["html"])
    except Exception as exc:
        raise ExtractionError(f"x text fallback failed: {exc}") from exc
    canonical = data.get("url") or url
    post_id = _TWEET_ID.search(urlparse(canonical).path)
    return Post(
        platform="x",
        post_id=post_id.group(1) if post_id else match.group(1),
        canonical_url=canonical,
        username=username,
        text=text,
        published_at=None,
        media=(),
    )

_THREADS_SHORTCODE = re.compile(r"/(?:post|t)/([A-Za-z0-9_-]+)")
_DESKTOP_USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                       "Version/17.0 Safari/605.1.15")
_EMBED_MEDIA = re.compile(
    r'<img class="img" src="([^"]+)" height="300"|<source src="([^"]+)"'
)
_OG_CONTENT = re.compile(
    r'<meta[^>]+property="og:([a-z:]+)"[^>]+content="([^"]*)"',
    re.IGNORECASE,
)
_OG_TITLE_USER = re.compile(r"\(@([A-Za-z0-9_.]+)\)")


def _fetch_page(url: str, user_agent: str) -> str:
    request = urllib.request.Request(url, headers={
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml",
    })
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read().decode("utf-8", errors="replace")
    except Exception as exc:
        raise ExtractionError(f"threads page fetch failed: {exc}") from exc


def _og_tags(body: str) -> dict:
    return {prop: html.unescape(value) for prop, value in _OG_CONTENT.findall(body)}


def _threads_embed_media(url: str) -> list[MediaItem]:
    """Scrape the SSR embed page; it serves media URLs without auth.

    Post photos (and carousel slides) render as ``<img class="img" ...
    height="300">``; avatars use other heights, so they are excluded.
    Video posts render one ``<video>`` with a direct ``<source>`` URL.
    """
    parts = urlsplit(url)._replace(query="", fragment="")
    embed_url = parts.geturl() + "/embed"
    body = _fetch_page(embed_url, _DESKTOP_USER_AGENT)
    media: list[MediaItem] = []
    seen: set[str] = set()
    for found in _EMBED_MEDIA.finditer(body):
        raw, kind = (found.group(1), "photo") if found.group(1) else (found.group(2), "video")
        source = html.unescape(raw)
        if source in seen:
            continue
        seen.add(source)
        media.append(MediaItem(
            source_url=source, media_type=kind,
            extension=_ext_from_url(source), order=len(media),
        ))
    return media


def threads_extract(platform: str, url: str, *, cookies_path: str | None = None) -> Post:
    """Public-post scraper: og tags for metadata, embed page for media.

    yt-dlp has no Threads extractor and the GraphQL API requires an
    authenticated session, but the post page's og tags and its server-side
    rendered /embed page are readable unauthenticated.  Publication time is
    not exposed, so ``published_at`` stays unknown and the archive-time
    filename fallback applies honestly.  Cookies are accepted for interface
    consistency but not needed.
    """
    match = _THREADS_SHORTCODE.search(urlparse(url).path)
    if match is None:
        raise ExtractionError(f"threads extraction failed: no shortcode in {url}")
    post_id = match.group(1)

    body = _fetch_page(url, _DESKTOP_USER_AGENT)
    tags = _og_tags(body)
    title_match = _OG_TITLE_USER.search(tags.get("title", ""))
    username = title_match.group(1) if title_match else ""

    try:
        media = _threads_embed_media(url)
    except ExtractionError:
        media = []  # embed page unavailable: archive the text instead
    canonical = normalize_url(tags.get("url") or url)
    text = tags.get("description", "")
    if "Join Threads to share ideas" in text:
        # Login shell: the post is private, deleted, or otherwise unavailable.
        text = ""
    return Post(
        platform=platform,
        post_id=post_id,
        canonical_url=canonical,
        username=username,
        text=text,
        published_at=None,
        media=tuple(media),
    )


BACKENDS["threads"] = threads_extract


def extract(url: str, *, cookies_path: str | None = None) -> Post:
    platform = detect_platform(url)
    if platform is None:
        raise UnsupportedPlatformError(
            f"no extractor for host {urlparse(url).hostname}"
        )
    backend = BACKENDS.get(platform)
    if backend is None:
        raise UnsupportedPlatformError(f"no extractor for platform {platform}")
    return backend(platform, url, cookies_path=cookies_path)