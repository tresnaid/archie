import json
from datetime import datetime, timezone

import pytest

from bookmedia import extract as extract_module
from bookmedia.extract import (
    ExtractionError,
    MediaItem,
    Post,
    UnsupportedPlatformError,
    extract,
    ytdlp_extract,
)


def _info(**overrides):
    info = {
        "id": "abc123",
        "webpage_url": "https://www.tiktok.com/@user/video/abc123",
        "uploader_id": "12345",
        "uploader": "user",
        "channel": "chan",
        "description": "hello world",
        "timestamp": 1789051950,
        "ext": "mp4",
        "url": "https://cdn.example/v.mp4",
    }
    info.update(overrides)
    return info


class _FakeYDL:
    def __init__(self, info=None, error=None):
        self.info = info
        self.error = error
        self.seen = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def extract_info(self, url, download=False):
        self.seen["url"] = url
        self.seen["download"] = download
        if self.error is not None:
            raise self.error
        return self.info


def _factory(captured, ydl):
    def make(opts):
        captured.append(opts)
        return ydl

    return make


def test_instagram_prefers_uploader_over_numeric_uploader_id():
    info = _info(uploader_id="123456789", uploader="myhandle", platform="instagram")
    post = ytdlp_extract("instagram", "https://www.instagram.com/p/x", ydl_factory=_factory([], _FakeYDL(info)))
    assert post.username == "myhandle"


def test_tiktok_prefers_uploader_handle():
    info = _info(uploader_id="12345678", uploader="tiktokuser")
    post = ytdlp_extract("tiktok", "https://vm.tiktok.com/abc", ydl_factory=_factory([], _FakeYDL(info)))
    assert post.username == "tiktokuser"


def test_tiktok_strips_leading_at_from_uploader():
    info = _info(uploader_id="12345678", uploader="@tiktokuser")
    post = ytdlp_extract("tiktok", "https://vm.tiktok.com/abc", ydl_factory=_factory([], _FakeYDL(info)))
    assert post.username == "tiktokuser"


def test_tiktok_falls_back_to_channel_when_no_uploader():
    info = _info(uploader_id="12345678", uploader=None, channel="fallbackuser")
    post = ytdlp_extract("tiktok", "https://vm.tiktok.com/abc", ydl_factory=_factory([], _FakeYDL(info)))
    assert post.username == "fallbackuser"


def test_tiktok_falls_back_to_numeric_when_only_id():
    info = _info(uploader_id="12345678", uploader=None, channel=None)
    post = ytdlp_extract("tiktok", "https://vm.tiktok.com/abc", ydl_factory=_factory([], _FakeYDL(info)))
    assert post.username == "12345678"


def test_x_keeps_uploader_id_as_username():
    info = _info(uploader_id="xhandle")
    post = ytdlp_extract("x", "https://x.com/u/status/1", ydl_factory=_factory([], _FakeYDL(info)))
    assert post.username == "xhandle"


def test_extract_dispatches_by_platform(monkeypatch):
    calls = []

    def fake(platform, url, *, cookies_path=None):
        calls.append((platform, url, cookies_path))
        raise AssertionError("stop")

    monkeypatch.setattr(
        extract_module, "BACKENDS", {"x": fake, "tiktok": fake}
    )
    with pytest.raises(AssertionError):
        extract("https://x.com/u/status/1", cookies_path="/c")
    assert calls == [("x", "https://x.com/u/status/1", "/c")]
    with pytest.raises(AssertionError):
        extract("https://vm.tiktok.com/abc")
    assert calls[-1][0] == "tiktok"


def test_extract_unsupported_host_raises_extraction_error():
    with pytest.raises(UnsupportedPlatformError):
        extract("https://www.facebook.com/watch?v=1")
    assert issubclass(UnsupportedPlatformError, ExtractionError)


def test_extract_tiktok_photo_post_delegates_to_backend(monkeypatch):
    import bookmedia.extract as extract_module

    captured = {}

    def fake_tiktok_extract(platform, url, *, cookies_path=None):
        captured["called"] = True
        return Post(platform="tiktok", post_id="7665708800246172949",
                     canonical_url=url, username="", text="",
                     published_at=None, media=())

    monkeypatch.setattr(extract_module, "BACKENDS", {"tiktok": fake_tiktok_extract})
    extract("https://www.tiktok.com/@user/photo/7665708800246172949")
    assert captured.get("called")


def test_ytdlp_extract_maps_full_info():
    captured = []
    ydl = _FakeYDL(_info())
    post = ytdlp_extract(
        "tiktok", "https://vm.tiktok.com/abc", ydl_factory=_factory(captured, ydl)
    )
    assert post.platform == "tiktok"
    assert post.post_id == "abc123"
    assert post.canonical_url == "https://www.tiktok.com/@user/video/abc123"
    assert post.username == "user"
    assert post.text == "hello world"
    assert post.published_at == datetime(2026, 9, 10, 14, 52, 30, tzinfo=timezone.utc)
    assert len(post.media) == 1
    item = post.media[0]
    assert (item.source_url, item.media_type, item.extension, item.order) == (
        "https://cdn.example/v.mp4",
        "video",
        "mp4",
        0,
    )
    assert ydl.seen == {"url": "https://vm.tiktok.com/abc", "download": False}
    assert captured[0]["skip_download"] is True
    assert captured[0]["retries"] == 3
    assert captured[0]["fragment_retries"] == 3
    assert captured[0]["extractor_retries"] == 3
    assert captured[0]["file_access_retries"] == 3


def test_ytdlp_extract_upload_date_fallback_and_username_chain():
    captured = []
    info = _info(timestamp=None, uploader_id=None, uploader=None, upload_date="20260910")
    post = ytdlp_extract("x", "https://x.com/u/status/1", ydl_factory=_factory(captured, _FakeYDL(info)))
    assert post.published_at == datetime(2026, 9, 10, tzinfo=timezone.utc)
    assert post.username == "chan"


def test_ytdlp_extract_missing_id_falls_back_to_canonical():
    captured = []
    info = _info(id=None, webpage_url="HTTPS://X.COM/u/status/9/?utm_source=x#y")
    post = ytdlp_extract("x", "https://x.com/u/status/9", ydl_factory=_factory(captured, _FakeYDL(info)))
    assert post.post_id == "https://x.com/u/status/9"
    assert post.canonical_url == "https://x.com/u/status/9"


def test_ytdlp_extract_missing_webpage_url_uses_input():
    captured = []
    info = _info(webpage_url=None)
    post = ytdlp_extract("x", "https://x.com/u/status/1", ydl_factory=_factory(captured, _FakeYDL(info)))
    assert post.canonical_url == "https://x.com/u/status/1"


def test_ytdlp_extract_playlist_entries_keep_order_and_photo_type():
    captured = []
    info = _info(
        entries=[
            {"url": "https://cdn.example/1.jpg", "ext": "jpg"},
            {"url": "", "ext": "weird"},
            None,
        ]
    )
    post = ytdlp_extract("instagram", "https://instagram.com/p/x", ydl_factory=_factory(captured, _FakeYDL(info)))
    assert [(m.media_type, m.extension, m.order) for m in post.media] == [
        ("photo", "jpg", 0),
        ("", "weird", 1),
    ]
    assert post.media[1].source_url == ""


def test_ytdlp_extract_wraps_backend_errors():
    captured = []
    ydl = _FakeYDL(error=RuntimeError("login required"))
    with pytest.raises(ExtractionError, match="login required"):
        ytdlp_extract("instagram", "https://instagram.com/p/x", ydl_factory=_factory(captured, ydl))


def test_ytdlp_extract_cookiefile_only_when_file_exists(tmp_path):
    captured = []
    missing = str(tmp_path / "nope.txt")
    ytdlp_extract("x", "https://x.com/1", cookies_path=missing, ydl_factory=_factory(captured, _FakeYDL(_info())))
    assert "cookiefile" not in captured[-1]
    real = tmp_path / "cookies.txt"
    real.write_text("cookies")
    ytdlp_extract("x", "https://x.com/1", cookies_path=str(real), ydl_factory=_factory(captured, _FakeYDL(_info())))
    # cookiefile points to a writable temp copy, not the original
    assert captured[-1]["cookiefile"] != str(real)


_OEMBED = {
    "url": "https://x.com/juza_23/status/2099660276987531332",
    "author_name": "Justin",
    "author_url": "https://x.com/juza_23",
    "html": '<blockquote class="twitter-tweet"><p lang="en" dir="ltr">first line<br><br>second &amp; line</p>&mdash; Justin (@juza_23) <a href="https://x.com/x">September 15, 2026</a></blockquote>',
}


def _oembed_response(payload):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(payload).encode()

    return FakeResponse()


def test_x_text_tweet_falls_back_to_oembed(monkeypatch):
    import bookmedia.extract as extract_module

    def no_video(platform, url, *, cookies_path=None):
        raise ExtractionError("x extraction failed: ERROR: [twitter] 1: No video could be found in this tweet")

    seen = {}
    real_synd = extract_module._x_syndication
    real_text = extract_module.x_text_post

    def spy_syndication(url):
        seen["syndication"] = url
        raise OSError("syndication down")

    def spy_text(url):
        seen["text"] = url
        return real_text(url)

    monkeypatch.setattr(extract_module, "ytdlp_extract", no_video)
    monkeypatch.setattr(extract_module, "_x_syndication", spy_syndication)
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout=30: _oembed_response(_OEMBED),
    )
    post = extract("https://x.com/i/status/2099660276987531332")
    assert post.platform == "x"
    assert post.post_id == "2099660276987531332"
    assert post.canonical_url == "https://x.com/juza_23/status/2099660276987531332"
    assert post.username == "juza_23"
    assert post.text == "first line\n\nsecond & line"
    assert post.published_at is None
    assert post.media == ()
    assert seen["syndication"] == "https://x.com/i/status/2099660276987531332"


def test_x_fallback_uses_tweet_url_not_linked_site(monkeypatch):
    """A tweet linking out (infron.ai, b.ai) must fall back to the tweet.

    Regression: yt-dlp follows the tweet's t.co link to the *linked* site,
    so its error names infron.ai/b.ai. Syndication + oEmbed must still run
    against the /status/<id> URL, never the linked site.
    """
    import bookmedia.extract as extract_module

    seen = {}

    def linked_site_error(platform, url, *, cookies_path=None):
        raise ExtractionError(
            "x extraction failed: ERROR: Unsupported URL:"
            " https://infron.ai/models/qwen/qwen3.8-27b:free"
        )

    def spy_syndication(url):
        seen["syndication"] = url
        raise OSError("syndication down")

    def spy_text(url):
        seen["text"] = url
        raise ExtractionError("x text fallback failed: down")

    monkeypatch.setattr(extract_module, "ytdlp_extract", linked_site_error)
    monkeypatch.setattr(extract_module, "_x_syndication", spy_syndication)
    monkeypatch.setattr(extract_module, "x_text_post", spy_text)
    with pytest.raises(ExtractionError, match="Unsupported URL"):
        extract("https://x.com/InfronAI/status/2100373472354390408")
    assert seen["syndication"] == "https://x.com/i/status/2100373472354390408"
    # the oEmbed fallback uses the *provided* (handle) URL, not the link-out
    assert seen["text"] == "https://x.com/InfronAI/status/2100373472354390408"


def test_x_linked_out_url_pins_canonical_to_submitted_tweet(monkeypatch):
    """When syndication succeeds, canonical_url is the submitted tweet URL."""
    import bookmedia.extract as extract_module

    def linked_site_error(platform, url, *, cookies_path=None):
        raise ExtractionError(
            "x extraction failed: ERROR: Unsupported URL: https://b.ai/"
        )

    monkeypatch.setattr(extract_module, "ytdlp_extract", linked_site_error)
    monkeypatch.setattr(
        extract_module, "_x_syndication",
        lambda url: ("itsjdraven", "some text", (
            MediaItem(source_url="https://pbs.twimg.com/media/x.jpg",
                      media_type="photo", extension="jpg", order=0),
        )),
    )
    post = extract("https://x.com/itsjdraven/status/2100159234990321902?utm_source=share")
    assert post.post_id == "2100159234990321902"
    assert post.canonical_url == "https://x.com/itsjdraven/status/2100159234990321902"
    assert post.username == "itsjdraven"
    assert len(post.media) == 1


def test_x_other_errors_do_not_hit_oembed(monkeypatch):
    import urllib.request

    import bookmedia.extract as extract_module

    def login(platform, url, *, cookies_path=None):
        raise ExtractionError("x extraction failed: login required")

    monkeypatch.setattr(extract_module, "ytdlp_extract", login)
    called = []
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **k: called.append(True) or _oembed_response(_OEMBED),
    )
    with pytest.raises(ExtractionError, match="login required"):
        extract("https://x.com/u/status/1")
    assert called == []


def test_x_oembed_failure_keeps_original_error(monkeypatch):
    import bookmedia.extract as extract_module

    def no_video(platform, url, *, cookies_path=None):
        raise ExtractionError("x extraction failed: No video could be found")

    monkeypatch.setattr(extract_module, "ytdlp_extract", no_video)
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError("net down"))
    )
    with pytest.raises(ExtractionError, match="No video could be found"):
        extract("https://x.com/u/status/1")


def _page_response(html_body):
    class FakeResponse:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self):
            return html_body.encode()
    return FakeResponse()


def _json_response(data):
    class FakeResponse:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self):
            return json.dumps(data).encode()
    return FakeResponse()


def test_x_image_tweet_extracts_via_syndication(monkeypatch):
    import bookmedia.extract as extract_module

    def no_video(platform, url, *, cookies_path=None):
        raise ExtractionError("x extraction failed: No video could be found in this tweet")

    monkeypatch.setattr(extract_module, "ytdlp_extract", no_video)
    syndication_data = {
        "user": {"screen_name": "juza_23"},
        "text": "check this out",
        "photos": [
            {"url": "https://pbs.twimg.com/media/abc.jpg"},
            {"url": "https://pbs.twimg.com/media/def.jpg"},
        ],
    }
    def fake_open(req, timeout=30):
        if "syndication.twimg.com" in req.full_url:
            return _json_response(syndication_data)
        if "publish.twitter.com/oembed" in req.full_url:
            return _oembed_response(_OEMBED)
        return _page_response("<html></html>")

    monkeypatch.setattr("urllib.request.urlopen", fake_open)
    post = extract("https://x.com/i/status/123456")
    assert post.platform == "x"
    assert post.post_id == "123456"
    assert post.username == "juza_23"
    assert post.text == "check this out"
    assert len(post.media) == 2
    assert post.media[0].source_url == "https://pbs.twimg.com/media/abc.jpg"
    assert post.media[0].media_type == "photo"
    assert post.media[1].source_url == "https://pbs.twimg.com/media/def.jpg"


def test_x_no_images_falls_back_to_oembed(monkeypatch):
    import bookmedia.extract as extract_module

    def no_video(platform, url, *, cookies_path=None):
        raise ExtractionError("x extraction failed: No video could be found")

    monkeypatch.setattr(extract_module, "ytdlp_extract", no_video)
    syndication_data = {"user": {"screen_name": "juza_23"}, "text": "just text", "photos": []}
    calls = []
    def fake_open(req, timeout=30):
        calls.append(req.full_url)
        if "syndication.twimg.com" in req.full_url:
            return _json_response(syndication_data)
        if "publish.twitter.com/oembed" in req.full_url:
            return _oembed_response(_OEMBED)
        return _page_response("<html></html>")

    monkeypatch.setattr("urllib.request.urlopen", fake_open)
    post = extract("https://x.com/i/status/2099660276987531332")
    assert post.media == ()
    assert post.username == "juza_23"


def test_instagram_photo_post_extracts_og_images(monkeypatch):
    import bookmedia.extract as extract_module

    def no_video(platform, url, *, cookies_path=None):
        raise ExtractionError("instagram extraction failed: ERROR: No video formats found!")

    monkeypatch.setattr(extract_module, "ytdlp_extract", no_video)
    page_html = (
        '<meta property="og:image" content="https://cdninstagram.com/v/t1/abc.jpg"/>'
        '<meta property="og:image" content="https://cdninstagram.com/v/t1/def.jpg"/>'
    )
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=30: _page_response(page_html))
    post = extract("https://www.instagram.com/p/DWlzA16mpst/")
    assert post.platform == "instagram"
    assert post.post_id == "DWlzA16mpst"
    assert len(post.media) == 2
    assert post.media[0].media_type == "photo"


def test_instagram_photo_no_images_raises(monkeypatch):
    import bookmedia.extract as extract_module

    def no_video(platform, url, *, cookies_path=None):
        raise ExtractionError("instagram extraction failed: ERROR: No video formats found!")

    monkeypatch.setattr(extract_module, "ytdlp_extract", no_video)
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=30: _page_response("<html></html>"))
    with pytest.raises(ExtractionError, match="photo/carousel extraction requires cookies"):
        extract("https://www.instagram.com/p/DWlzA16mpst/")


def test_instagram_carousel_extracts_display_uri_images(monkeypatch):
    """Carousel: display_uri from embedded JSON gives all slides."""
    import bookmedia.extract as extract_module

    def no_video(platform, url, *, cookies_path=None):
        raise ExtractionError("instagram extraction failed: ERROR: No video formats found!")

    monkeypatch.setattr(extract_module, "ytdlp_extract", no_video)
    page_html = (
        '<meta property="og:image" content="https://cdninstagram.com/v/t1/og.jpg"/>'
        '"display_uri":"https:\\/\\/cdninstagram.com\\/v\\/t1\\/slide1.jpg"'
        '"display_uri":"https:\\/\\/cdninstagram.com\\/v\\/t1\\/slide2.jpg"'
        '"display_uri":"https:\\/\\/cdninstagram.com\\/v\\/t1\\/slide3.jpg"'
    )
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=30: _page_response(page_html))
    post = extract("https://www.instagram.com/p/CAROUSEL1/")
    assert post.platform == "instagram"
    assert post.post_id == "CAROUSEL1"
    assert len(post.media) == 3
    assert post.media[0].source_url == "https://cdninstagram.com/v/t1/slide1.jpg"
    assert post.media[1].source_url == "https://cdninstagram.com/v/t1/slide2.jpg"
    assert post.media[2].source_url == "https://cdninstagram.com/v/t1/slide3.jpg"
    assert post.media[0].media_type == "photo"


def test_tiktok_photo_extract_uses_tikwm_api(monkeypatch):
    import bookmedia.extract as extract_module

    api_response = json.dumps({
        "code": 0,
        "data": {
            "author": {"unique_id": "testuser"},
            "images": [
                "https://p16-common-sign.tiktokcdn-us.com/img1.jpg",
                "https://p16-common-sign.tiktokcdn-us.com/img2.jpg",
            ],
        },
    }).encode()
    fake_resp = type("R", (), {"read": lambda s: api_response,
                                "__enter__": lambda s: s,
                                "__exit__": lambda s, *a: None})()

    def fake_open(req, timeout=30):
        return fake_resp

    monkeypatch.setattr("urllib.request.urlopen", fake_open)
    post = extract("https://www.tiktok.com/@user/photo/7665708800246172949")
    assert post.platform == "tiktok"
    assert post.post_id == "7665708800246172949"
    assert post.username == "testuser"
    assert len(post.media) == 2
    assert post.media[0].media_type == "photo"
    assert post.media[0].extension == "jpg"


def test_tiktok_video_extract_uses_ytdlp(monkeypatch):
    import bookmedia.extract as extract_module

    captured = {}
    def fake_ytdlp(platform, url, *, cookies_path=None):
        captured["called"] = True
        return Post(platform="tiktok", post_id="123", canonical_url=url,
                     username="testuser", text="hi", published_at=None, media=())

    monkeypatch.setattr(extract_module, "ytdlp_extract", fake_ytdlp)
    post = extract("https://www.tiktok.com/@user/video/123")
    assert captured.get("called")
    assert post.username == "testuser"


def test_tiktok_photo_api_no_images_raises(monkeypatch):
    import bookmedia.extract as extract_module

    api_response = json.dumps({"code": 0, "data": {"images": []}}).encode()
    fake_resp = type("R", (), {"read": lambda s: api_response,
                                "__enter__": lambda s: s,
                                "__exit__": lambda s, *a: None})()
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=30: fake_resp)
    with pytest.raises(ExtractionError, match="tiktok photo extraction failed"):
        extract("https://www.tiktok.com/@user/photo/7665708800246172949")


def _tikwm_payload(**data):
    return json.dumps({"code": 0, "data": data}).encode()


def _tikwm_response(**data):
    body = _tikwm_payload(**data)
    return type("R", (), {"read": lambda s: body,
                          "__enter__": lambda s: s,
                          "__exit__": lambda s, *a: None})()


def test_tiktok_audio_only_rescues_real_video_from_tikwm(monkeypatch):
    import bookmedia.extract as extract_module

    def fake_ytdlp(platform, url, *, cookies_path=None):
        # Web extraction: only the soundtrack survives.
        return Post(platform="tiktok", post_id="7682753151031725332",
                    canonical_url=url, username="pumarclla", text="hi",
                    published_at=None,
                    media=(MediaItem(source_url="https://cdn/mp3?x=1",
                                     media_type="", extension="mp3", order=0),))

    def fake_open(req, timeout=30):
        assert "tikwm.com" in req.full_url
        return _tikwm_response(
            author={"unique_id": "pumarclla"},
            play="https://v16m.tiktokcdn-us.com/video/tos.mp4?_nc_sid=x",
        )

    monkeypatch.setattr(extract_module, "ytdlp_extract", fake_ytdlp)
    monkeypatch.setattr("urllib.request.urlopen", fake_open)
    post = extract("https://www.tiktok.com/@pumarclla/video/7682753151031725332")
    assert post.platform == "tiktok"
    assert post.post_id == "7682753151031725332"
    assert post.username == "pumarclla"
    assert len(post.media) == 1
    assert post.media[0].media_type == "video"
    assert post.media[0].extension == "mp4"


def test_tiktok_audio_only_rescues_slideshow_images_from_tikwm(monkeypatch):
    import bookmedia.extract as extract_module

    def fake_ytdlp(platform, url, *, cookies_path=None):
        return Post(platform="tiktok", post_id="111", canonical_url=url,
                    username="pumarclla", text="",
                    published_at=None,
                    media=(MediaItem(source_url="https://cdn/a.m4a",
                                     media_type="", extension="m4a", order=0),))

    def fake_open(req, timeout=30):
        return _tikwm_response(
            author={"unique_id": "pumarclla"},
            images=[
                "https://p16-sign.tiktokcdn.com/img1.jpg",
                "https://p16-sign.tiktokcdn.com/img2.jpg",
            ],
        )

    monkeypatch.setattr(extract_module, "ytdlp_extract", fake_ytdlp)
    monkeypatch.setattr("urllib.request.urlopen", fake_open)
    post = extract("https://www.tiktok.com/@pumarclla/video/111")
    assert [m.media_type for m in post.media] == ["photo", "photo"]
    assert post.media[0].extension == "jpg"


def test_tiktok_audio_only_kept_when_tikwm_has_nothing(monkeypatch):
    import bookmedia.extract as extract_module

    audio_post = Post(platform="tiktok", post_id="222", canonical_url="u",
                      username="pumarclla", text="",
                      published_at=None,
                      media=(MediaItem(source_url="https://cdn/a.mp3",
                                       media_type="", extension="mp3", order=0),))

    def fake_ytdlp(platform, url, *, cookies_path=None):
        return audio_post

    def fake_open(req, timeout=30):
        return _tikwm_response()

    monkeypatch.setattr(extract_module, "ytdlp_extract", fake_ytdlp)
    monkeypatch.setattr("urllib.request.urlopen", fake_open)
    assert extract("https://www.tiktok.com/@pumarclla/video/222") is audio_post


_THREADS_EMBED_IMAGE = (
    '<img class="img" src="https://scontent.cdninstagram.com/v/t51.2885-15/abc.jpg'
    '?stp=jpg&amp;oe=123" height="300" draggable="false" alt="" />'
)
_THREADS_EMBED_AVATAR = (
    '<img class="img" src="https://scontent.cdninstagram.com/v/t51.2885-19/av.jpg'
    '?oe=123" alt="nasa" height="36" width="36" />'
)
_THREADS_EMBED_VIDEO = (
    '<video controls="1" loop="1" class=""><source '
    'src="https://scontent.cdninstagram.com/o1/v/t16/f2/m84/AQO.mp4?_nc_sid=1d576d" />'
    '</video>'
)


def _threads_page(title="Alvin Foo (@alvinfoo) on Threads",
                  description="A mother with her baby",
                  url="https://www.threads.com/@alvinfoo/post/DSJbc6ciVdv"):
    return (
        "<!DOCTYPE html><html><head>"
        f'<meta property="og:title" content="{title}" />'
        f'<meta property="og:description" content="{description}" />'
        f'<meta property="og:url" content="{url}" />'
        "</head><body></body></html>"
    )


def test_threads_extracts_media_from_embed_page(monkeypatch):
    def fake_open(req, timeout=30):
        if req.full_url.endswith("/embed"):
            assert req.headers.get("User-agent") == extract_module._DESKTOP_USER_AGENT
            return _page_response(_threads_page() + _THREADS_EMBED_IMAGE + _THREADS_EMBED_AVATAR)
        return _page_response(_threads_page())

    monkeypatch.setattr("urllib.request.urlopen", fake_open)
    post = extract("https://www.threads.com/@alvinfoo/post/DSJbc6ciVdv?xmt=AQG0tracking")
    assert post.platform == "threads"
    assert post.post_id == "DSJbc6ciVdv"
    assert post.username == "alvinfoo"
    assert post.text == "A mother with her baby"
    assert post.canonical_url == "https://www.threads.com/@alvinfoo/post/DSJbc6ciVdv"
    assert post.published_at is None
    assert [m.source_url for m in post.media] == [
        "https://scontent.cdninstagram.com/v/t51.2885-15/abc.jpg?stp=jpg&oe=123",
    ]
    assert post.media[0].media_type == "photo"
    assert post.media[0].extension == "jpg"
    assert post.media[0].order == 0


def test_threads_extracts_carousel_and_video_ordering(monkeypatch):
    def fake_open(req, timeout=30):
        if req.full_url.endswith("/embed"):
            return _page_response(
                _threads_page() + _THREADS_EMBED_VIDEO + _THREADS_EMBED_IMAGE
                + _THREADS_EMBED_IMAGE.replace("abc.jpg", "def.jpg")
            )
        return _page_response(_threads_page())

    monkeypatch.setattr("urllib.request.urlopen", fake_open)
    post = extract("https://www.threads.com/@alvinfoo/post/DSJbc6ciVdv")
    assert [m.media_type for m in post.media] == ["video", "photo", "photo"]
    assert [m.order for m in post.media] == [0, 1, 2]
    assert post.media[0].extension == "mp4"


def test_threads_text_post_has_no_media(monkeypatch):
    def fake_open(req, timeout=30):
        return _page_response(_threads_page(description="just a thought"))

    monkeypatch.setattr("urllib.request.urlopen", fake_open)
    post = extract("https://www.threads.com/t/DSJbc6ciVdv")
    assert post.platform == "threads"
    assert post.post_id == "DSJbc6ciVdv"  # /t/ share format
    assert post.text == "just a thought"
    assert post.media == ()


def test_threads_embed_failure_archives_text_only(monkeypatch):
    def fake_open(req, timeout=30):
        if req.full_url.endswith("/embed"):
            raise OSError("embed down")
        return _page_response(_threads_page(title="Zuck (@zuck) on Threads"))

    monkeypatch.setattr("urllib.request.urlopen", fake_open)
    post = extract("https://www.threads.com/@zuck/post/C8rOe_Hr4Sm")
    assert post.username == "zuck"
    assert post.media == ()


def test_threads_embed_url_strips_query_params(monkeypatch):
    embed_urls = []

    def fake_open(req, timeout=30):
        if req.full_url.endswith("/embed"):
            embed_urls.append(req.full_url)
            return _page_response(_threads_page() + _THREADS_EMBED_IMAGE)
        return _page_response(_threads_page())

    monkeypatch.setattr("urllib.request.urlopen", fake_open)
    extract("https://www.threads.com/@alvinfoo/post/DSJbc6ciVdv?xmt=AQG0tracking")
    assert embed_urls == [
        "https://www.threads.com/@alvinfoo/post/DSJbc6ciVdv/embed"
    ]


def test_threads_login_shell_suppresses_placeholder_text(monkeypatch):
    placeholder = "Join Threads to share ideas, ask questions, and follow your favorites."

    def fake_open(req, timeout=30):
        return _page_response(_threads_page(description=placeholder))

    monkeypatch.setattr("urllib.request.urlopen", fake_open)
    post = extract("https://www.threads.com/@alvinfoo/post/DSJbc6ciVdv")
    assert post.text == ""
    assert post.media == ()


def test_threads_no_shortcode_is_a_clean_extraction_error(monkeypatch):
    def fake_open(req, timeout=30):
        raise AssertionError("network should not be touched without a shortcode")

    monkeypatch.setattr("urllib.request.urlopen", fake_open)
    with pytest.raises(ExtractionError, match="no shortcode"):
        extract("https://www.threads.com/@alvinfoo")


def test_threads_unreachable_page_raises_extraction_error(monkeypatch):
    def fake_open(req, timeout=30):
        raise OSError("net down")

    monkeypatch.setattr("urllib.request.urlopen", fake_open)
    with pytest.raises(ExtractionError, match="threads page fetch failed"):
        extract("https://www.threads.com/@alvinfoo/post/DSJbc6ciVdv")

