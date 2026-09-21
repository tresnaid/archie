"""Opt-in integration tests against real platforms.

Skipped unless ``BOOKMEDIA_INTEGRATION=1`` — these hit live external
services whose behavior changes independently of application correctness.
Run explicitly:

    BOOKMEDIA_INTEGRATION=1 pytest -q tests/test_integration.py
"""

import os

import pytest

from bookmedia.extract import extract

pytestmark = pytest.mark.integration


def _enabled():
    return os.environ.get("BOOKMEDIA_INTEGRATION") == "1"


@pytest.mark.skipif(not _enabled(), reason="set BOOKMEDIA_INTEGRATION=1")
def test_x_text_tweet_extract_returns_common_post_model():
    post = extract("https://x.com/Interior/status/463440424141459456")
    assert post.platform == "x"
    assert post.post_id == "463440424141459456"
    assert post.canonical_url.startswith("https://")
    assert post.text  # the famous tweet definitely has text


@pytest.mark.skipif(not _enabled(), reason="set BOOKMEDIA_INTEGRATION=1")
def test_unsupported_platform_fails_cleanly():
    from bookmedia.extract import UnsupportedPlatformError

    with pytest.raises(UnsupportedPlatformError):
        extract("https://www.youtube.com/watch?v=dQw4w9WgXcQ")