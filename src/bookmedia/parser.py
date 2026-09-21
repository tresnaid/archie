"""Parse Telegram message text into an archive request.

Accepted input:

    <URL>
    <URL> --type=<type>

Anything else starts no archive job (returns None).
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

DEFAULT_TYPE = "unsorted"
_TYPE_PREFIX = "--type="


@dataclass(frozen=True)
class ParsedMessage:
    url: str
    type: str = DEFAULT_TYPE


def _is_url(token: str) -> bool:
    try:
        parts = urlsplit(token)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.hostname)


def parse_message(text: str | None) -> ParsedMessage | None:
    if not text:
        return None
    tokens = text.split()
    if not tokens or not _is_url(tokens[0]):
        return None
    if len(tokens) == 1:
        return ParsedMessage(url=tokens[0])
    if len(tokens) != 2 or not tokens[1].startswith(_TYPE_PREFIX):
        return None
    kind = tokens[1][len(_TYPE_PREFIX):]
    if not kind:
        return None
    return ParsedMessage(url=tokens[0], type=kind)
