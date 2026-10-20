"""Presenting a prepared segment to the upload guest without endangering it."""

from __future__ import annotations

import asyncio
import errno
import fcntl
import hashlib
import os
import threading
import uuid
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from pixav.pixel_injector.segment_staging import release_segment, segment_directory, stage_segment
from pixav.shared.exceptions import UploadError
from pixav.shared.media_file import open_media_file, run_media_work
from pixav.shared.storage_models import RemoteAssetSegment

PAYLOAD = b"managed segment bytes"
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()


def artifact(tmp_path, payload: bytes = PAYLOAD):
    path = tmp_path / "prepared" / "movie.mp4"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def segment(path, **changes) -> RemoteAssetSegment:
    fields = {
        "asset_id": uuid.UUID("11111111-1111-4111-8111-111111111111"),
        "segment_index": 0,
        "start_seconds": 0.0,
        "end_seconds": 12.0,
        "size_bytes": len(PAYLOAD),
        "sha256": DIGEST,
        "local_path": str(path),
    }
    return RemoteAssetSegment(**{**fields, **changes})


def test_staging_links_the_artifact_under_its_canonical_name(tmp_path):
    source = artifact(tmp_path)
    item = segment(source)

    directory = stage_segment(item, root=tmp_path / "staging")

    staged = directory / item.filename
    assert staged.read_bytes() == PAYLOAD
    assert staged.stat().st_ino == source.stat().st_ino, "a link, not a second copy of the film"
    assert directory == segment_directory(item, root=tmp_path / "staging")


def test_staging_is_idempotent_across_a_retried_attempt(tmp_path):
    source = artifact(tmp_path)
    item = segment(source)
    root = tmp_path / "staging"

    first = stage_segment(item, root=root)
    second = stage_segment(item, root=root)

    assert first == second
    assert list(first.iterdir()) == [first / item.filename]


def test_a_missing_artifact_is_refused_before_any_container_is_asked_for(tmp_path):
    item = segment(tmp_path / "prepared" / "gone.mp4")
    with pytest.raises(UploadError):
        stage_segment(item, root=tmp_path / "staging")


def test_an_artifact_whose_size_contradicts_the_record_is_refused(tmp_path):
    """Uploading these bytes would attach a false size to a durable asset."""
    source = artifact(tmp_path, payload=PAYLOAD + b"more")
    with pytest.raises(UploadError):
        stage_segment(segment(source), root=tmp_path / "staging")


def test_a_symlinked_artifact_is_refused(tmp_path):
    real = artifact(tmp_path)
    link = tmp_path / "prepared" / "link.mp4"
    link.symlink_to(real)
    with pytest.raises(UploadError):
        stage_segment(segment(link), root=tmp_path / "staging")


def test_a_symlinked_staging_root_is_refused(tmp_path):
    source = artifact(tmp_path)
    (tmp_path / "elsewhere").mkdir()
    root = tmp_path / "staging"
    root.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(UploadError):
        stage_segment(segment(source), root=root)


def test_a_foreign_file_under_the_staged_name_is_never_overwritten(tmp_path):
    source = artifact(tmp_path)
    item = segment(source)
    root = tmp_path / "staging"
    directory = segment_directory(item, root=root)
    directory.mkdir(parents=True)
    (directory / item.filename).write_bytes(b"someone else")

    with pytest.raises(UploadError):
        stage_segment(item, root=root)


def test_release_requires_committed_usage(tmp_path):
    """An unconfirmed upload still needs its presentation to reconcile against."""
    source = artifact(tmp_path)
    item = segment(source)
    stage_segment(item, root=tmp_path / "staging")

    with pytest.raises(UploadError):
        release_segment(item, root=tmp_path / "staging")


def test_release_drops_the_link_and_leaves_the_artifact_bdd_004(tmp_path):
    """Only the cleanup gate may decide that local media can go."""
    source = artifact(tmp_path)
    item = segment(source)
    root = tmp_path / "staging"
    directory = stage_segment(item, root=root)
    confirmed = item.model_copy(update={"usage_counted_at": datetime.now(timezone.utc)})

    assert release_segment(confirmed, root=root) is True
    assert not (directory / item.filename).exists()
    assert source.read_bytes() == PAYLOAD
    assert not directory.exists(), "the asset's directory goes once its last segment does"


def test_release_never_unlinks_the_artifact_itself(tmp_path):
    """If the artifact already sits at the staged path, releasing must do nothing."""
    item = segment(tmp_path / "x")
    root = tmp_path / "staging"
    directory = segment_directory(item, root=root)
    directory.mkdir(parents=True)
    staged = directory / item.filename
    staged.write_bytes(PAYLOAD)
    confirmed = segment(staged).model_copy(update={"usage_counted_at": datetime.now(timezone.utc)})

    assert release_segment(confirmed, root=root) is False
    assert staged.exists()


def test_staging_refuses_same_size_foreign_bytes_instead_of_reusing_them(tmp_path):
    source = artifact(tmp_path)
    item = segment(source)
    root = tmp_path / "staging"
    directory = segment_directory(item, root=root)
    directory.mkdir(parents=True)
    target = directory / item.filename
    target.write_bytes(b"x" * len(PAYLOAD))
    with pytest.raises(UploadError):
        stage_segment(item, root=root)
    assert target.read_bytes() == b"x" * len(PAYLOAD)


def test_staging_refuses_an_artifact_held_by_a_writer(tmp_path):
    source = artifact(tmp_path)
    with open_media_file(source, exclusive=True), pytest.raises(UploadError):
        stage_segment(segment(source), root=tmp_path / "staging")


def test_release_waits_for_a_reader_of_the_shared_inode(tmp_path):
    source = artifact(tmp_path)
    item = segment(source, usage_counted_at=datetime.now(timezone.utc))
    root = tmp_path / "staging"
    target = stage_segment(item, root=root) / item.filename
    with open_media_file(target):
        assert release_segment(item, root=root) is False
        assert target.exists() and source.exists()
    assert release_segment(item, root=root) is True


def test_release_preserves_the_last_copy_when_the_prepared_name_is_missing(tmp_path):
    source = artifact(tmp_path)
    item = segment(source, usage_counted_at=datetime.now(timezone.utc))
    root = tmp_path / "staging"
    target = stage_segment(item, root=root) / item.filename
    source.unlink()
    assert release_segment(item, root=root) is False
    assert target.read_bytes() == PAYLOAD


def test_release_refuses_foreign_same_size_bytes(tmp_path):
    source = artifact(tmp_path)
    item = segment(source, usage_counted_at=datetime.now(timezone.utc))
    root = tmp_path / "staging"
    target = stage_segment(item, root=root) / item.filename
    target.unlink()
    target.write_bytes(b"x" * len(PAYLOAD))
    assert release_segment(item, root=root) is False
    assert target.read_bytes() == b"x" * len(PAYLOAD)


def cross_device_copy(monkeypatch, source):
    from pixav.pixel_injector import segment_staging as module

    original = os.link

    def link(src, dst, **kwargs):
        if os.fspath(src) in (str(source), source.name):
            raise OSError(errno.EXDEV, "synthetic cross-device link")
        return original(src, dst, **kwargs)

    monkeypatch.setattr(module.os, "link", link)
    return module


def test_cross_device_pending_work_is_never_overwritten(tmp_path, monkeypatch):
    source = artifact(tmp_path)
    item = segment(source)
    root = tmp_path / "staging"
    directory = segment_directory(item, root=root)
    directory.mkdir(parents=True)
    pending = directory / (item.filename + ".pending")
    pending.write_bytes(b"retained unfinished work")
    cross_device_copy(monkeypatch, source)
    with pytest.raises(UploadError):
        stage_segment(item, root=root)
    assert pending.read_bytes() == b"retained unfinished work"
    assert not (directory / item.filename).exists()


def test_cross_device_copy_holds_source_and_output_locks_until_verified(tmp_path, monkeypatch):
    source = artifact(tmp_path)
    item = segment(source)
    module = cross_device_copy(monkeypatch, source)
    copy = module.shutil.copyfileobj

    def inspect(reader, writer):
        with pytest.raises(BlockingIOError), open_media_file(source, exclusive=True):
            pytest.fail("prepared source was unlocked during copy")
        descriptor = os.open(f"/proc/self/fd/{writer.fileno()}", os.O_RDONLY)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        finally:
            os.close(descriptor)
        assert os.fstat(writer.fileno()).st_mode & 0o777 == 0o600
        copy(reader, writer)

    monkeypatch.setattr(module.shutil, "copyfileobj", inspect)
    target = stage_segment(item, root=tmp_path / "staging") / item.filename
    assert target.read_bytes() == PAYLOAD and target.stat().st_ino != source.stat().st_ino


@pytest.mark.parametrize("fault", ["source-change", "foreign-target"])
def test_copy_conflict_preserves_pending_work_and_never_replaces_a_new_name(tmp_path, monkeypatch, fault):
    source = artifact(tmp_path)
    item = segment(source)
    root = tmp_path / "staging"
    directory = segment_directory(item, root=root)
    target = directory / item.filename
    module = cross_device_copy(monkeypatch, source)
    copy = module.shutil.copyfileobj

    def conflict(reader, writer):
        copy(reader, writer)
        if fault == "source-change":
            source.write_bytes(b"x" * len(PAYLOAD))
        else:
            target.write_bytes(b"foreign bytes must survive")

    monkeypatch.setattr(module.shutil, "copyfileobj", conflict)
    with pytest.raises(UploadError):
        stage_segment(item, root=root)
    assert (directory / (item.filename + ".pending")).read_bytes() == PAYLOAD
    if fault == "foreign-target":
        assert target.read_bytes() == b"foreign bytes must survive"
    else:
        assert not target.exists()


def test_real_cross_filesystem_copy_and_release_preserve_the_prepared_artifact(tmp_path):
    if not Path("/dev/shm").is_dir() or Path("/dev/shm").stat().st_dev == tmp_path.stat().st_dev:
        pytest.skip("requires a separate local tmpfs")
    source = artifact(tmp_path)
    item = segment(source, usage_counted_at=datetime.now(timezone.utc))
    with TemporaryDirectory(prefix="pixav-segment-contract-", dir="/dev/shm") as name:
        root = Path(name)
        target = stage_segment(item, root=root) / item.filename
        assert target.stat().st_dev != source.stat().st_dev
        assert target.read_bytes() == PAYLOAD
        with open_media_file(target):
            assert release_segment(item, root=root) is False
        assert release_segment(item, root=root) is True
        assert not target.exists() and source.read_bytes() == PAYLOAD


async def test_cancelled_copy_drains_the_thread_and_retains_private_pending_work(tmp_path, monkeypatch):
    source = artifact(tmp_path)
    item = segment(source)
    root = tmp_path / "staging"
    module = cross_device_copy(monkeypatch, source)
    copy = module.shutil.copyfileobj
    entered, released = threading.Event(), threading.Event()

    def held(reader, writer):
        entered.set()
        assert released.wait(5)
        return copy(reader, writer)

    monkeypatch.setattr(module.shutil, "copyfileobj", held)
    running = asyncio.create_task(run_media_work(stage_segment, item, root=root))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        running.cancel()
        await asyncio.sleep(0.02)
        assert not running.done()
        running.cancel()
        await asyncio.sleep(0.02)
        assert not running.done()
    finally:
        released.set()
        with suppress(asyncio.CancelledError):
            await asyncio.wait_for(running, 5)
    assert running.cancelled()
    directory = segment_directory(item, root=root)
    pending = directory / (item.filename + ".pending")
    assert pending.read_bytes() == PAYLOAD and pending.stat().st_mode & 0o777 == 0o600
    assert not (directory / item.filename).exists()
    with open_media_file(source, exclusive=True):
        pass


async def test_cancelled_release_stops_before_unlink_and_drains_its_hash(tmp_path, monkeypatch):
    source = artifact(tmp_path)
    item = segment(source, usage_counted_at=datetime.now(timezone.utc))
    root = tmp_path / "staging"
    target = stage_segment(item, root=root) / item.filename
    from pixav.pixel_injector import segment_staging as module

    expected = module._expected
    entered, released = threading.Event(), threading.Event()

    def held(locked, segment):
        entered.set()
        assert released.wait(5)
        return expected(locked, segment)

    monkeypatch.setattr(module, "_expected", held)
    running = asyncio.create_task(run_media_work(release_segment, item, root=root))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        running.cancel()
        await asyncio.sleep(0.02)
        assert not running.done() and target.exists()
    finally:
        released.set()
        with suppress(asyncio.CancelledError):
            await asyncio.wait_for(running, 5)
    assert running.cancelled() and target.read_bytes() == PAYLOAD and source.read_bytes() == PAYLOAD
