"""Startup recovery: interrupted rows and orphan temp dirs."""

import os

from bookmedia import recovery, repository
from bookmedia.db import init_db


def _conn(tmp_path):
    return init_db(str(tmp_path / "t.db"))


def _received(conn, url):
    return repository.insert_received(
        conn, platform="x", submitted_url=url, type="unsorted"
    )


def test_recover_interrupted_fails_each_mid_state(tmp_path):
    conn = _conn(tmp_path)
    extracting = _received(conn, "https://x.com/u/1")
    downloading = _received(conn, "https://x.com/u/2")
    uploading = _received(conn, "https://x.com/u/3")
    archived = _received(conn, "https://x.com/u/4")
    repository.set_status(conn, extracting, "extracting")
    repository.set_status(conn, downloading, "downloading")
    repository.set_status(conn, uploading, "uploading")
    repository.set_status(conn, archived, "archived")

    count = recovery.recover_interrupted(conn)

    assert count == 3
    assert "while extracting" in repository.get(conn, extracting)["error"]
    assert "while downloading" in repository.get(conn, downloading)["error"]
    assert "while uploading" in repository.get(conn, uploading)["error"]
    for row_id in (extracting, downloading, uploading):
        assert repository.get(conn, row_id)["status"] == "failed"
    assert repository.get(conn, archived)["status"] == "archived"


def test_recover_interrupted_leaves_received_and_failed_alone(tmp_path):
    conn = _conn(tmp_path)
    received = _received(conn, "https://x.com/u/1")
    failed = _received(conn, "https://x.com/u/2")
    repository.set_status(conn, failed, "failed", error="boom")

    assert recovery.recover_interrupted(conn) == 0

    assert repository.get(conn, received)["status"] == "received"
    row = repository.get(conn, failed)
    assert (row["status"], row["error"]) == ("failed", "boom")


def test_sweep_temp_dirs_removes_only_job_dirs(tmp_path):
    temp = tmp_path / "tmp"
    (temp / "job-7").mkdir(parents=True)
    (temp / "job-7" / "u__t.mp4").write_text("media")
    (temp / "job-9").mkdir()
    (temp / "keep.me").write_text("x")
    (temp / "notajob").mkdir()

    removed = recovery.sweep_temp_dirs(str(temp))

    assert removed == 2
    assert (temp / "keep.me").exists()
    assert (temp / "notajob").exists()
    assert list(temp.glob("job-*")) == []


def test_sweep_temp_dirs_missing_dir_is_safe(tmp_path):
    assert recovery.sweep_temp_dirs(str(tmp_path / "nope")) == 0


def test_sweep_never_touches_data_dir(tmp_path):
    temp = tmp_path / "tmp"
    temp.mkdir()
    (temp / "job-1").mkdir()
    (temp / "archive.db").write_text("db")

    recovery.sweep_temp_dirs(str(temp))

    assert (temp / "archive.db").exists()
    assert not (temp / "job-1").exists()