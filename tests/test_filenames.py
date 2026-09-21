from datetime import datetime, timezone

import pytest

from bookmedia.filenames import assign_filenames, build_filename, sanitize_username


def _when():
    return datetime(2026, 9, 10, 14, 52, 30, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("user", "user"),
        ("user.name-1_2", "user.name-1_2"),
        ("us/er:x?y", "us_er_x_y"),
        ("  spaced  ", "spaced"),
        ("trailing.", "trailing"),
        ("", "unknown"),
        ("///", "unknown"),
    ],
)
def test_sanitize_username(raw, clean):
    assert sanitize_username(raw) == clean


def test_build_single_filename():
    assert build_filename("user", _when(), "mp4") == "user__20260910_145230.mp4"


def test_build_multi_filename_numbering_starts_at_one():
    assert build_filename("user", _when(), "jpg", index=0, total=3) == "user__20260910_145230(1).jpg"
    assert build_filename("user", _when(), "mp4", index=2, total=3) == "user__20260910_145230(3).mp4"


def test_build_filename_sanitizes_and_normalizes_extension():
    assert build_filename("us/er", _when(), ".JPG") == "us_er__20260910_145230.jpg"


def test_assign_single_path_uses_plain_name():
    assert assign_filenames(["/tmp/raw_1.MP4"], "user", _when()) == ["user__20260910_145230.mp4"]


def test_assign_multiple_paths_numbers_in_order():
    assert assign_filenames(["/tmp/raw_1.jpg", "/tmp/raw_2.mp4"], "user", _when()) == [
        "user__20260910_145230(1).jpg",
        "user__20260910_145230(2).mp4",
    ]
