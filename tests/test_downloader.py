import os
import re
from datetime import datetime, timezone

import pytest

from bookmedia.downloader import (
    DEFAULT_MAX_MEDIA_BYTES,
    Downloaded,
    DownloadError,
    MediaTooLargeError,
    download,
    estimated_media_bytes,
)
from bookmedia.extract import Post


def _post(**overrides):
    base = {
        "platform": "tiktok",
        "post_id": "123",
        "canonical_url": "https://www.tiktok.com/@user/video/123",
        "username": "user",
        "text": "caption",
        "published_at": datetime(2026, 9, 10, 14, 52, 30, tzinfo=timezone.utc),
        "media": [],
    }
    base.update(overrides)
    return Post(**base)


class FakeYDL:
    seen_opts = None

    def __init__(self, opts):
        FakeYDL.seen_opts = opts

    def download(self, urls):
        assert urls == ["https://www.tiktok.com/@user/video/123"]
        outtmpl = FakeYDL.seen_opts["outtmpl"]
        workdir = os.path.dirname(outtmpl)
        for name in ("raw_1.mp4", "raw_2.jpg"):
            with open(os.path.join(workdir, name), "wb") as fh:
                fh.write(b"data")


def test_download_renames_to_convention_in_order(tmp_path):
    files = download(_post(), str(tmp_path), ydl_factory=FakeYDL)
    assert files == [
        Downloaded(path=os.path.join(str(tmp_path), "user__20260910_145230(1).mp4"),
                   filename="user__20260910_145230(1).mp4"),
        Downloaded(path=os.path.join(str(tmp_path), "user__20260910_145230(2).jpg"),
                   filename="user__20260910_145230(2).jpg"),
    ]
    assert sorted(os.listdir(str(tmp_path))) == [
        "user__20260910_145230(1).mp4",
        "user__20260910_145230(2).jpg",
    ]
    opts = FakeYDL.seen_opts
    assert opts["retries"] == 3
    assert opts["fragment_retries"] == 3
    assert opts["extractor_retries"] == 3
    assert opts["file_access_retries"] == 3


def test_download_single_file_has_no_number(tmp_path):
    class Single(FakeYDL):
        def download(self, urls):
            workdir = os.path.dirname(FakeYDL.seen_opts["outtmpl"])
            with open(os.path.join(workdir, "raw_1.mp4"), "wb") as fh:
                fh.write(b"data")

    files = download(_post(), str(tmp_path), ydl_factory=Single)
    assert [f.filename for f in files] == ["user__20260910_145230.mp4"]


def test_download_unknown_user_and_archive_time_fallback(tmp_path):
    files = download(_post(username="", published_at=None), str(tmp_path), ydl_factory=FakeYDL)
    assert len(files) == 2
    assert re.fullmatch(r"unknown__\d{8}_\d{6}\(1\)\.mp4", files[0].filename)


def test_download_no_files_produced_is_an_error(tmp_path):
    class Empty:
        def __init__(self, opts):
            pass

        def download(self, urls):
            pass

    with pytest.raises(DownloadError):
        download(_post(), str(tmp_path), ydl_factory=Empty)


def test_download_backend_failure_is_wrapped(tmp_path):
    class Boom:
        def __init__(self, opts):
            pass

        def download(self, urls):
            raise RuntimeError("boom")

    with pytest.raises(DownloadError, match="boom"):
        download(_post(), str(tmp_path), ydl_factory=Boom)


def test_download_cookiefile_only_when_path_exists(tmp_path):
    captured = {}

    class Spy(FakeYDL):
        def __init__(self, opts):
            FakeYDL.seen_opts = opts
            captured.update(opts)

    download(_post(), str(tmp_path), ydl_factory=Spy)
    assert "cookiefile" not in captured
    cookie = tmp_path / "cookies.txt"
    cookie.write_text("cookies")
    download(_post(), str(tmp_path), ydl_factory=Spy, cookies_path=str(cookie))
    # cookiefile points to a writable temp copy, not the original read-only path
    assert "cookiefile" in captured
    assert captured["cookiefile"] != str(cookie)
    assert not os.path.exists(captured["cookiefile"])


def test_download_direct_urls_bypasses_ytdlp(tmp_path, monkeypatch):
    from bookmedia.extract import MediaItem

    media = (
        MediaItem(source_url="https://cdn.example.com/a.jpg", media_type="photo", extension="jpg", order=0),
        MediaItem(source_url="https://cdn.example.com/b.jpg", media_type="photo", extension="jpg", order=1),
    )
    post = _post(media=media)

    class NoYDL:
        def __init__(self, opts):
            raise AssertionError("yt-dlp should not be called for direct URLs")

    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b"fake image data"

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=30: FakeResp())
    files = download(post, str(tmp_path), ydl_factory=NoYDL)
    assert len(files) == 2
    assert files[0].filename.startswith("user__20260910_145230(1).jpg")
    assert files[1].filename.startswith("user__20260910_145230(2).jpg")


class ProbeYDL(FakeYDL):
    """FakeYDL plus a metadata-only size probe."""
    probe_info: dict = {}
    probe_calls = 0

    def extract_info(self, url, download=True):
        assert download is False  # metadata only, never downloads
        ProbeYDL.probe_calls += 1
        return ProbeYDL.probe_info


def test_estimated_media_bytes_sums_requested_formats():
    info = {
        "requested_formats": [
            {"filesize": 1000, "ext": "mp4"},
            {"filesize_approx": 500, "ext": "m4a"},
        ],
    }
    assert estimated_media_bytes(_post(), ydl_factory=_probe_factory(info)) == 1500


def _probe_factory(info):
    class Probe:
        def __init__(self, opts):
            pass

        def extract_info(self, url, download=True):
            assert download is False
            return info

    return Probe


def test_download_oversized_media_fails_before_download(tmp_path):
    """A post over the ceiling raises before yt-dlp.download() runs."""
    info = {"requested_formats": [{"filesize": 3 * 1024 * 1024 * 1024}]}
    called = {"download": False}

    class Big(ProbeYDL):
        def download(self, urls):
            called["download"] = True
            raise AssertionError("must not download")

    ProbeYDL.probe_info = info
    with pytest.raises(MediaTooLargeError, match="3072 MB estimated"):
        download(_post(), str(tmp_path), ydl_factory=Big,
                 max_bytes=2000 * 1024 * 1024)
    assert called["download"] is False
    assert os.listdir(str(tmp_path)) == []  # nothing written


def test_download_under_limit_proceeds(tmp_path):
    ProbeYDL.probe_info = {"requested_formats": [{"filesize": 1024}]}
    files = download(_post(), str(tmp_path), ydl_factory=ProbeYDL,
                     max_bytes=2000 * 1024 * 1024)
    assert len(files) == 2


def test_download_unknown_size_never_blocks(tmp_path):
    """Missing filesize metadata must not gate the download (probe returns 0)."""
    ProbeYDL.probe_info = {"formats": [{"format_id": "18", "filesize": None}]}
    files = download(_post(), str(tmp_path), ydl_factory=ProbeYDL,
                     max_bytes=1024)
    assert len(files) == 2


def test_download_gate_skipped_for_direct_photo_urls(tmp_path, monkeypatch):
    from bookmedia.extract import MediaItem

    media = (MediaItem(source_url="https://cdn.example.com/a.jpg",
                       media_type="photo", extension="jpg", order=0),)

    class NoProbe:
        def __init__(self, opts):
            raise AssertionError("no yt-dlp probe for direct photo URLs")

    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b"data"

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=30: FakeResp())
    files = download(_post(media=media), str(tmp_path), ydl_factory=NoProbe,
                     max_bytes=1)  # tiny limit: photos bypass the gate
    assert len(files) == 1


def test_download_gate_disabled_when_max_bytes_none_or_zero(tmp_path):
    ProbeYDL.probe_calls = 0
    files = download(_post(), str(tmp_path), ydl_factory=ProbeYDL)
    assert files and ProbeYDL.probe_calls == 0  # no probe without a limit
    files = download(_post(), str(tmp_path), ydl_factory=ProbeYDL, max_bytes=0)
    assert files and ProbeYDL.probe_calls == 0


def test_default_max_media_bytes_matches_local_api_cap():
    assert DEFAULT_MAX_MEDIA_BYTES == 2000 * 1024 * 1024


def test_size_guard_ignores_non_downloading_and_below_floor_statuses():
    from bookmedia.downloader import _make_size_guard

    guard = _make_size_guard(1000)
    guard({"status": "finished"})  # non-downloading statuses never abort
    # below the 64 MB confidence floor: misleading early estimates ignored
    guard({"status": "downloading", "downloaded_bytes": 500,
           "total_bytes_estimate": 10**12})
    with pytest.raises(MediaTooLargeError, match="exceeded the 0 MB limit"):
        guard({"status": "downloading", "downloaded_bytes": 2000})  # hard cap


def test_download_installs_progress_hook_only_when_limited(tmp_path):
    ProbeYDL.probe_info = {"requested_formats": [{"filesize": 10}]}
    download(_post(), str(tmp_path), ydl_factory=ProbeYDL,
             max_bytes=2000 * 1024 * 1024)
    assert FakeYDL.seen_opts.get("progress_hooks")
    download(_post(), str(tmp_path), ydl_factory=ProbeYDL)
    assert "progress_hooks" not in FakeYDL.seen_opts


def test_download_aborts_midway_when_estimate_crosses_limit(tmp_path):
    """HLS size estimates grow during download; the guard aborts when over."""
    # Pre-download probe underestimates (manifest was still incomplete):
    ProbeYDL.probe_info = {"requested_formats": [{"filesize": 100 * 1024 * 1024}]}
    statuses = [
        {"status": "downloading", "downloaded_bytes": 70 * 1024 * 1024,
         "total_bytes_estimate": 1500 * 1024 * 1024},  # under limit → continue
        {"status": "downloading", "downloaded_bytes": 80 * 1024 * 1024,
         "total_bytes_estimate": 2500 * 1024 * 1024},  # over limit → abort
    ]

    class Guarded(ProbeYDL):
        def download(self, urls):
            (hook,) = FakeYDL.seen_opts["progress_hooks"]
            for status in statuses:
                hook(status)  # raises inside yt-dlp's download loop
            raise AssertionError("guard must have aborted the download")

    with pytest.raises(MediaTooLargeError,
                       match="2500 MB while downloading, over the 2000 MB limit"):
        download(_post(), str(tmp_path), ydl_factory=Guarded,
                 max_bytes=2000 * 1024 * 1024)
    assert os.listdir(str(tmp_path)) == []  # nothing renamed into place


def test_download_aborts_on_downloaded_bytes_without_any_estimate(tmp_path):
    """Even with no total estimate, downloaded bytes past the ceiling abort."""
    ProbeYDL.probe_info = {"requested_formats": [{"filesize": None}]}

    class HardCase(ProbeYDL):
        def download(self, urls):
            (hook,) = FakeYDL.seen_opts["progress_hooks"]
            hook({"status": "downloading",
                  "downloaded_bytes": 2049 * 1024 * 1024})

    with pytest.raises(MediaTooLargeError, match="2049 MB downloaded"):
        download(_post(), str(tmp_path), ydl_factory=HardCase,
                 max_bytes=2000 * 1024 * 1024)
