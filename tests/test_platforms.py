from bookmedia.platforms import SUPPORTED_PLATFORMS, detect_platform


def test_instagram():
    assert detect_platform("https://www.instagram.com/p/ABC/") == "instagram"
    assert detect_platform("https://instagram.com/reel/XYZ") == "instagram"


def test_tiktok_including_short_hosts():
    assert detect_platform("https://www.tiktok.com/@u/video/123") == "tiktok"
    assert detect_platform("https://vm.tiktok.com/AbC/") == "tiktok"
    assert detect_platform("https://vt.tiktok.com/AbC/") == "tiktok"


def test_x_covers_twitter_domain():
    assert detect_platform("https://x.com/u/status/1") == "x"
    assert detect_platform("https://twitter.com/u/status/1") == "x"
    assert detect_platform("https://mobile.twitter.com/u/status/1") == "x"


def test_threads():
    assert detect_platform("https://www.threads.com/@u/post/1") == "threads"
    assert detect_platform("https://www.threads.net/@u/post/1") == "threads"


def test_youtube():
    assert detect_platform("https://www.youtube.com/shorts/ABC123") == "youtube"
    assert detect_platform("https://youtube.com/watch?v=ABC") == "youtube"
    assert detect_platform("https://youtu.be/ABC123") == "youtube"


def test_host_matching_is_case_insensitive():
    assert detect_platform("https://WWW.INSTAGRAM.COM/p/ABC/") == "instagram"


def test_unsupported_returns_none():
    assert detect_platform("https://www.example.com/post") is None
    assert detect_platform("https://facebook.com/post") is None


def test_supported_platforms_lists_five():
    assert SUPPORTED_PLATFORMS == ("instagram", "tiktok", "x", "threads", "youtube")
