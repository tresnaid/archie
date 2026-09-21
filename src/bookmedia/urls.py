"""Canonical URL normalization for identity and duplicate checks."""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_DEFAULT_PORTS = {"http": 80, "https": 443}


def normalize_url(url: str) -> str:
    """Lowercase scheme/host, drop default ports/fragments/utm params/trailing slash."""
    parts = urlsplit(url)
    if not parts.scheme or not parts.hostname:
        raise ValueError(f"not a URL: {url}")
    scheme = parts.scheme.lower()
    host = parts.hostname.lower()
    port = parts.port
    netloc = host if port is None or port == _DEFAULT_PORTS.get(scheme) else f"{host}:{port}"
    query = urlencode(
        sorted(
            (k, v) for k, v in parse_qsl(parts.query) if not k.lower().startswith("utm_")
        )
    )
    return urlunsplit((scheme, netloc, parts.path.rstrip("/") or "", query, ""))
