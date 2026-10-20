"""Playback materialization worker (BDD-066, 069, 078, 081, 083, 084).

The isolated container is replaced by a fake that writes into the same fresh
work directory the real one would mount, so every host-side guarantee -- what
is offered as input, what is recomputed, what is published -- runs for real.
"""

from __future__ import annotations

import asyncio
import errno
import fcntl
import hashlib
import json
import os
import threading
import uuid
from contextlib import suppress
from pathlib import Path

import pytest

from pixav.pixel_injector.photos_storage import ColdProcessError
from pixav.pixel_injector.playback_activity import PlaybackActivityWorker
from pixav.shared.playback_models import MATERIALIZATION_METHOD, cache_object_path
from pixav.shared.workflow import ActivityRequest

CONTENT = b"synthetic playable object bytes"
DIGEST = hashlib.sha256(CONTENT).hexdigest()
STREAMS = [
    {"kind": "video", "codec": "h264", "width": 1920, "height": 1080},
    {"kind": "audio", "codec": "aac", "width": 0, "height": 0},
]


@pytest.mark.parametrize("phase", ["offer", "verify", "publish"])
async def test_cancellation_drains_file_work_before_return_and_retains_evidence(tmp_path, monkeypatch, phase):
    from pixav.pixel_injector import playback_activity
    from pixav.shared.media_file import open_media_file

    materializer = FakeMaterializer()
    worker, playable, row = build(tmp_path, materializer)
    verified_readback(tmp_path, row)
    request = request_for(playable, "prepare")
    work = worker.cache_root / ".work" / str(playable["id"]) / str(request.attempt_id)
    started, released, finished = threading.Event(), threading.Event(), threading.Event()
    function = {
        "offer": playback_activity._link_verified_readback,
        "verify": worker._verify_output,
        "publish": worker._publish,
    }[phase]

    def held(*args, **kwargs):
        marker = work / "active-file-worker"
        marker.write_bytes(b"owned evidence while file work runs")
        try:
            with open_media_file(marker):
                started.set()
                assert released.wait(5)
                return function(*args, **kwargs)
        finally:
            finished.set()

    if phase == "offer":
        monkeypatch.setattr(playback_activity, "_link_verified_readback", held)
    else:
        monkeypatch.setattr(worker, "_verify_output" if phase == "verify" else "_publish", held)
    running = asyncio.create_task(worker.execute(request))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        running.cancel()
        await asyncio.sleep(0.03)
        assert not running.done(), "cancellation returned while the file worker still held its inode"
        assert (work / "active-file-worker").exists()
        running.cancel()
        await asyncio.sleep(0.03)
        assert not running.done(), "a second cancellation bypassed the drain"
    finally:
        released.set()
        with suppress(asyncio.CancelledError):
            await asyncio.wait_for(running, 5)
        assert await asyncio.to_thread(finished.wait, 2)
    assert running.cancelled()
    assert (work / "active-file-worker").read_bytes() == b"owned evidence while file work runs"
    assert not cache_object_path(worker.cache_root, playable["id"]).exists()
    if phase == "offer":
        assert materializer.seen_cloud == []


async def test_cancellation_after_atomic_publication_retains_reconciliation_inputs(tmp_path, monkeypatch):
    worker, playable, _ = build(tmp_path, FakeMaterializer())
    request = request_for(playable, "prepare")
    work = worker.cache_root / ".work" / str(playable["id"]) / str(request.attempt_id)
    target = cache_object_path(worker.cache_root, playable["id"])
    published, released = threading.Event(), threading.Event()
    replace = os.replace

    def held_commit(source, destination, **kwargs):
        result = replace(source, destination, **kwargs)
        if destination == target.name and "dst_dir_fd" in kwargs:
            published.set()
            assert released.wait(5)
        return result

    monkeypatch.setattr(os, "replace", held_commit)
    running = asyncio.create_task(worker.execute(request))
    try:
        assert await asyncio.to_thread(published.wait, 2)
        running.cancel()
        await asyncio.sleep(0.03)
        assert not running.done() and work.exists()
    finally:
        released.set()
        with suppress(asyncio.CancelledError):
            await asyncio.wait_for(running, 5)
    assert running.cancelled()
    assert target.read_bytes() == CONTENT and (work / target.name).read_bytes() == CONTENT
    assert worker.pool.playable["state"] == "PREPARING", "cache visibility cannot promote domain readiness"


def test_publication_waits_for_a_reader_of_the_existing_object(tmp_path):
    from pixav.strm_resolver.playback import PlaybackCache

    worker, playable, _ = build(tmp_path, FakeMaterializer())
    old = cache_object_path(worker.cache_root, playable["id"])
    old.parent.mkdir(parents=True)
    old.write_bytes(CONTENT)
    replacement = tmp_path / "new-publication.mp4"
    replacement.write_bytes(CONTENT)
    reader = PlaybackCache(worker.cache_root).open_reader(playable["id"], len(CONTENT))
    assert reader is not None
    before = old.stat().st_ino
    try:
        with pytest.raises(BlockingIOError):
            worker._publish(replacement, playable["id"], uuid.uuid4())
        assert old.stat().st_ino == before
        assert reader.read() == CONTENT
    finally:
        reader.close()
    worker._publish(replacement, playable["id"], uuid.uuid4())
    assert old.read_bytes() == CONTENT


def facts(size: int = len(CONTENT), digest: str = DIGEST) -> dict:
    return {"container": "mov,mp4", "size_bytes": size, "duration_seconds": 10.0, "sha256": digest, "streams": STREAMS}


class FakePool:
    def __init__(self, playable: dict, asset: dict, segments: list[dict]) -> None:
        self.playable, self.asset, self.segments = playable, asset, segments

    async def fetchrow(self, sql: str, *args):
        if "FROM playable_assets" in sql:
            return dict(self.playable)
        if "FROM remote_assets" in sql:
            return dict(self.asset)
        raise AssertionError(sql)

    async def fetch(self, sql: str, *args):
        assert "FROM remote_asset_segments" in sql
        return [dict(segment) for segment in self.segments]


class FakeMaterializer:
    """Stands in for the container: sees only the work directory it is given."""

    def __init__(self, *, content: bytes = CONTENT, report: dict | None = None, error: str | None = None) -> None:
        self.content, self.report, self.error = content, report, error
        self.seen_cloud: list[str] = []
        self.linked_inode: int | None = None

    async def materialize(self, manifest: dict, work: Path, *, timeout_seconds: int) -> dict:
        cloud = work / "cloud"
        self.seen_cloud = sorted(p.name for p in cloud.iterdir())
        segment = manifest["segments"][0]
        name = f"pixav-{segment['asset_id']}-part-0-{segment['sha256'][:16]}.mp4"
        if (cloud / name).exists():
            self.linked_inode = (cloud / name).stat().st_ino
        assert segment["local_path"] == "" and segment["account_id"] is None
        if self.error:
            raise ColdProcessError(self.error)
        (work / manifest["output"]).write_bytes(self.content)
        return self.report or {
            "method": MATERIALIZATION_METHOD,
            "segments": [{"index": 0, "size": len(CONTENT), "sha256": DIGEST, "method": "photos-original-browser"}],
            "junctions": "single-segment",
            "observed": facts(),
            "size": len(CONTENT),
            "sha256": DIGEST,
        }


def build(tmp_path: Path, materializer: FakeMaterializer, *, state: str = "PREPARING", sha: str | None = None):
    asset_id, playable_id = uuid.uuid4(), uuid.uuid4()
    playable = {
        "id": playable_id,
        "video_id": uuid.uuid4(),
        "remote_asset_id": asset_id,
        "state": state,
        "size_bytes": len(CONTENT) if sha else None,
        "sha256": sha,
        "ready_at": "2026-01-01T00:00:00+00:00" if sha else None,
        "evidence": "{}",
        "playback_evidence": "{}",
    }
    segment = {
        "asset_id": asset_id,
        "segment_index": 0,
        "start_seconds": 0.0,
        "end_seconds": 10.0,
        "size_bytes": len(CONTENT),
        "sha256": DIGEST,
        "local_path": "/staging/private/prepared.mp4",
        "media_info": "{}",
        "share_url": "https://photos.app.goo.gl/synthetic",
        "account_id": uuid.uuid4(),
        "state": "verified",
        "recovery": "{}",
        "verification": "{}",
    }
    pool = FakePool(playable, {"expected": json.dumps(facts())}, [segment])
    worker = PlaybackActivityWorker(
        pool, materializer, cache_root=tmp_path / "cache", readback_root=tmp_path / "readback"
    )
    return worker, playable, segment


def request_for(playable: dict, purpose: str) -> ActivityRequest:
    return ActivityRequest(
        task_id=uuid.uuid4(),
        execution_id=uuid.uuid4(),
        attempt_id=uuid.uuid4(),
        operation_id=uuid.uuid4(),
        owner=uuid.uuid4(),
        generation=1,
        stage="playback",
        identity=f"playback:{playable['id']}:{purpose}",
        playable_asset_id=playable["id"],
        purpose=purpose,
        asset_id=playable["remote_asset_id"],
    )


def verified_readback(tmp_path: Path, segment: dict, *, digest: str = DIGEST) -> Path:
    folder = tmp_path / "readback" / str(segment["asset_id"]) / "0"
    folder.mkdir(parents=True)
    name = f"pixav-{segment['asset_id']}-part-0-{DIGEST[:16]}.mp4"
    (folder / name).write_bytes(CONTENT)
    (folder / (name + ".json")).write_text(
        json.dumps({"method": "photos-original-browser", "size": len(CONTENT), "sha256": digest})
    )
    return folder / name


@pytest.mark.parametrize("existing", ["media", "receipt"])
def test_offering_readback_preserves_unowned_work_files(tmp_path, existing):
    from pixav.pixel_injector.playback_activity import PlaybackWorkConflictError, _offer_readback
    from pixav.shared.remote_assets import segment_from_row

    worker, _, row = build(tmp_path, FakeMaterializer())
    source = verified_readback(tmp_path, row)
    cloud = tmp_path / "cloud"
    cloud.mkdir()
    foreign = cloud / (source.name if existing == "media" else source.name + ".json")
    foreign.write_bytes(b"retained work from another owner")
    before = foreign.stat().st_ino

    with pytest.raises(PlaybackWorkConflictError, match="RECONCILIATION"):
        _offer_readback(segment_from_row(row), worker.readback_root, source.parent, cloud)

    assert foreign.stat().st_ino == before
    assert foreign.read_bytes() == b"retained work from another owner"


async def test_work_conflict_is_infrastructure_and_retains_the_work_directory(tmp_path, monkeypatch):
    from pixav.pixel_injector import playback_activity

    materializer = FakeMaterializer()
    worker, playable, row = build(tmp_path, materializer)
    verified_readback(tmp_path, row)
    original = playback_activity._link_verified_readback
    retained = []

    def conflict(segment, root, cloud, **kwargs):
        foreign = cloud / segment.filename
        foreign.write_bytes(b"work requiring reconciliation")
        retained.append(foreign)
        return original(segment, root, cloud, **kwargs)

    monkeypatch.setattr(playback_activity, "_link_verified_readback", conflict)
    result = await worker.execute(request_for(playable, "prepare"))

    assert result.outcome == "infrastructure"
    assert result.error_code == "PLAYBACK_WORK_REQUIRES_RECONCILIATION"
    assert len(retained) == 1 and retained[0].read_bytes() == b"work requiring reconciliation"
    assert materializer.seen_cloud == []


async def test_unconfirmed_child_retirement_retains_work_without_invalidating_media(tmp_path, monkeypatch):
    from pixav.pixel_injector.photos_storage import ColdChildRetirementError

    materializer = FakeMaterializer()
    worker, playable, _ = build(tmp_path, materializer)
    retained = []

    async def unknown_child(manifest, work, *, timeout_seconds):
        marker = work / "child-reconciliation-evidence"
        marker.write_bytes(b"retain until exact child retirement is proven")
        retained.append(marker)
        raise ColdChildRetirementError("synthetic-child")

    monkeypatch.setattr(materializer, "materialize", unknown_child)
    result = await worker.execute(request_for(playable, "rebuild"))
    assert result.outcome == "infrastructure"
    assert result.error_code == "PLAYBACK_CHILD_REQUIRES_RECONCILIATION"
    assert len(retained) == 1 and retained[0].exists()


async def test_unconfirmed_retirement_does_not_swallow_worker_cancellation(tmp_path, monkeypatch):
    materializer = FakeMaterializer()
    worker, playable, _ = build(tmp_path, materializer)
    started = asyncio.Event()
    retained = []

    async def unconfirmed(manifest, work, *, timeout_seconds):
        marker = work / "unconfirmed-child"
        marker.write_bytes(b"retain stopped-attempt evidence")
        retained.append(marker)
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            raise asyncio.CancelledError("COLD_CHILD_REQUIRES_RECONCILIATION") from None

    monkeypatch.setattr(materializer, "materialize", unconfirmed)
    running = asyncio.create_task(worker.execute(request_for(playable, "prepare")))
    await asyncio.wait_for(started.wait(), 2)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert running.cancelled()
    assert len(retained) == 1 and retained[0].exists()


def test_a_file_created_after_the_work_check_is_preserved(tmp_path, monkeypatch):
    from pixav.pixel_injector.playback_activity import PlaybackWorkConflictError, _offer_readback
    from pixav.shared.remote_assets import segment_from_row

    worker, _, row = build(tmp_path, FakeMaterializer())
    source = verified_readback(tmp_path, row)
    cloud = tmp_path / "cloud"
    cloud.mkdir()
    foreign = cloud / source.name

    def raced_link(*args, **kwargs):
        foreign.write_bytes(b"created after the empty-work check")
        raise FileExistsError(errno.EEXIST, "work appeared")

    monkeypatch.setattr(os, "link", raced_link)
    with pytest.raises(PlaybackWorkConflictError, match="RECONCILIATION"):
        _offer_readback(segment_from_row(row), worker.readback_root, source.parent, cloud)
    assert foreign.read_bytes() == b"created after the empty-work check"


async def test_cross_mount_copy_protects_both_active_input_and_output(tmp_path, monkeypatch):
    import shutil

    from pixav.pixel_injector import playback_activity

    materializer = FakeMaterializer()
    worker, playable, row = build(tmp_path, materializer)
    verified_readback(tmp_path, row)
    real_link, real_copy = os.link, shutil.copyfileobj
    checked = []

    def separate_mounts(original, destination, **kwargs):
        if "cloud" in Path(destination).parts:
            raise OSError(errno.EXDEV, "separate mounts")
        return real_link(original, destination, **kwargs)

    def copy(reader, writer):
        for handle, mode in ((reader, fcntl.LOCK_EX), (writer, fcntl.LOCK_SH)):
            descriptor = os.open(f"/proc/self/fd/{handle.fileno()}", os.O_RDONLY)
            try:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(descriptor, mode | fcntl.LOCK_NB)
            finally:
                os.close(descriptor)
        checked.append(True)
        real_copy(reader, writer)

    monkeypatch.setattr(os, "link", separate_mounts)
    monkeypatch.setattr(playback_activity.shutil, "copyfileobj", copy)
    result = await worker.execute(request_for(playable, "prepare"))
    assert result.outcome == "success" and result.evidence["inputs"] == "verified-readback"
    assert checked == [True, True]


async def test_prepare_offers_the_verified_readback_by_hard_link_bdd_066(tmp_path):
    materializer = FakeMaterializer()
    worker, playable, segment = build(tmp_path, materializer)
    source = verified_readback(tmp_path, segment)

    result = await worker.execute(request_for(playable, "prepare"))

    assert result.outcome == "success" and result.evidence["inputs"] == "verified-readback"
    assert materializer.linked_inode == source.stat().st_ino
    published = cache_object_path(tmp_path / "cache", playable["id"])
    assert published.read_bytes() == CONTENT
    assert not (tmp_path / "cache" / ".work" / str(playable["id"])).joinpath(str(result.attempt_id)).exists()
    assert result.playable_asset_id == playable["id"] and result.purpose == "prepare"


async def test_prepare_reuses_a_fresh_cold_session_instead_of_downloading_again(tmp_path):
    materializer = FakeMaterializer()
    worker, playable, segment = build(tmp_path, materializer)
    source = verified_readback(tmp_path, segment)
    session = source.parent / "session-fresh"
    session.mkdir()
    for path in list(source.parent.glob("*.mp4*")):
        path.rename(session / path.name)
    result = await worker.execute(request_for(playable, "prepare"))
    assert result.outcome == "success" and result.evidence["inputs"] == "verified-readback"
    assert materializer.linked_inode == (session / source.name).stat().st_ino


async def test_separate_mounts_offer_a_verified_private_copy(tmp_path, monkeypatch):
    materializer = FakeMaterializer()
    worker, playable, segment = build(tmp_path, materializer)
    source = verified_readback(tmp_path, segment)
    real_link = os.link

    def separate_mounts(original, destination, **kwargs):
        if "cloud" in Path(destination).parts:
            raise OSError(errno.EXDEV, "separate mounts")
        return real_link(original, destination, **kwargs)

    monkeypatch.setattr(os, "link", separate_mounts)
    result = await worker.execute(request_for(playable, "prepare"))
    assert result.outcome == "success" and result.evidence["inputs"] == "verified-readback"
    assert materializer.linked_inode != source.stat().st_ino
    assert source.read_bytes() == CONTENT


async def test_corrupted_cross_mount_copy_and_symlink_session_are_not_offered(tmp_path, monkeypatch):
    materializer = FakeMaterializer()
    worker, playable, segment = build(tmp_path, materializer)
    source = verified_readback(tmp_path, segment)
    original = tmp_path / "outside"
    original.mkdir()
    source.parent.joinpath("session-symlink").symlink_to(original, target_is_directory=True)
    real_link = os.link

    def separate_mounts(original, destination, **kwargs):
        if "cloud" in Path(destination).parts:
            raise OSError(errno.EXDEV, "separate mounts")
        return real_link(original, destination, **kwargs)

    def bad_copy(reader, writer):
        writer.write(b"untrusted copy")

    monkeypatch.setattr(os, "link", separate_mounts)
    monkeypatch.setattr("pixav.pixel_injector.playback_activity.shutil.copyfileobj", bad_copy)
    result = await worker.execute(request_for(playable, "prepare"))
    assert result.outcome == "success" and result.evidence["inputs"] == "photos"
    assert materializer.seen_cloud == [] and source.read_bytes() == CONTENT


async def test_prepare_falls_back_to_photos_when_readback_is_gone_bdd_078(tmp_path):
    materializer = FakeMaterializer()
    worker, playable, _ = build(tmp_path, materializer)

    result = await worker.execute(request_for(playable, "prepare"))

    assert result.outcome == "success" and result.evidence["inputs"] == "photos"
    assert materializer.seen_cloud == []


async def test_readback_whose_receipt_disagrees_is_not_offered(tmp_path):
    materializer = FakeMaterializer()
    worker, playable, segment = build(tmp_path, materializer)
    verified_readback(tmp_path, segment, digest="0" * 64)

    result = await worker.execute(request_for(playable, "prepare"))

    assert result.evidence["inputs"] == "photos" and materializer.seen_cloud == []


async def test_rebuild_starts_from_an_empty_directory_bdd_081(tmp_path):
    """A rebuild must come from Photos even while read-back files still exist."""
    materializer = FakeMaterializer()
    worker, playable, segment = build(tmp_path, materializer, state="READY", sha=DIGEST)
    verified_readback(tmp_path, segment)

    result = await worker.execute(request_for(playable, "rebuild"))

    assert result.outcome == "success" and result.evidence["inputs"] == "photos"
    assert materializer.seen_cloud == []
    assert cache_object_path(tmp_path / "cache", playable["id"]).read_bytes() == CONTENT


async def test_rebuild_producing_other_bytes_is_never_published_bdd_069(tmp_path):
    other = b"different bytes entirely"
    report = {
        "method": MATERIALIZATION_METHOD,
        "segments": [{"index": 0, "size": len(CONTENT), "sha256": DIGEST, "method": "photos-original-browser"}],
        "junctions": "single-segment",
        "observed": facts(),
        "size": len(CONTENT),
        "sha256": DIGEST,
    }
    worker, playable, _ = build(tmp_path, FakeMaterializer(content=other, report=report), state="READY", sha=DIGEST)

    result = await worker.execute(request_for(playable, "rebuild"))

    assert result.outcome == "verification_failed" and result.error_code == "PLAYBACK_INTEGRITY_MISMATCH"
    assert not cache_object_path(tmp_path / "cache", playable["id"]).exists()


async def test_rebuild_that_differs_from_the_ready_record_is_refused(tmp_path):
    worker, playable, _ = build(tmp_path, FakeMaterializer(), state="READY", sha="e" * 64)

    result = await worker.execute(request_for(playable, "rebuild"))

    assert result.outcome == "verification_failed" and result.error_code == "PLAYBACK_REBUILD_MISMATCH"
    assert not cache_object_path(tmp_path / "cache", playable["id"]).exists()


@pytest.mark.parametrize(
    ("error", "outcome", "code"),
    [
        ("OriginalMismatchError", "verification_failed", "PLAYBACK_SEGMENT_MISMATCH"),
        ("IncompatibleMediaError", "verification_failed", "PLAYBACK_INTEGRITY_MISMATCH"),
        ("TimeoutError", "infrastructure", "PLAYBACK_MATERIALIZATION_FAILED"),
    ],
)
async def test_container_failures_are_classified_without_details_bdd_084(tmp_path, error, outcome, code):
    worker, playable, _ = build(tmp_path, FakeMaterializer(error=error))

    result = await worker.execute(request_for(playable, "prepare"))

    assert result.outcome == outcome and result.error_code == code
    assert not cache_object_path(tmp_path / "cache", playable["id"]).exists()


async def test_infrastructure_failure_releases_the_work_directory(tmp_path):
    worker, playable, _ = build(tmp_path, FakeMaterializer(error="TimeoutError"))
    request = request_for(playable, "prepare")

    await worker.execute(request)

    assert not (tmp_path / "cache" / ".work" / str(playable["id"]) / str(request.attempt_id)).exists()


async def test_cold_original_work_conflict_retains_files_and_does_not_invalidate_remote(tmp_path):
    class Conflict(FakeMaterializer):
        async def materialize(self, manifest, work, *, timeout_seconds):
            (work / "retained-original.part").write_bytes(b"foreign or unfinished work")
            return await super().materialize(manifest, work, timeout_seconds=timeout_seconds)

    worker, playable, _ = build(tmp_path, Conflict(error="OriginalWorkConflictError"))
    request = request_for(playable, "prepare")
    result = await worker.execute(request)
    assert result.outcome == "infrastructure" and result.error_code == "PLAYBACK_WORK_REQUIRES_RECONCILIATION"
    work = tmp_path / "cache" / ".work" / str(playable["id"]) / str(request.attempt_id)
    assert (work / "retained-original.part").read_bytes() == b"foreign or unfinished work"
    assert not cache_object_path(tmp_path / "cache", playable["id"]).exists()


async def test_republishing_does_not_disturb_an_open_reader_bdd_080(tmp_path):
    worker, playable, _ = build(tmp_path, FakeMaterializer(), state="READY", sha=DIGEST)
    published = cache_object_path(tmp_path / "cache", playable["id"])
    published.parent.mkdir(parents=True)
    published.write_bytes(b"an earlier object")
    reader = published.open("rb")

    result = await worker.execute(request_for(playable, "rebuild"))

    assert result.outcome == "success"
    assert reader.read() == b"an earlier object"
    reader.close()
    assert published.read_bytes() == CONTENT
    assert [name for name in os.listdir(published.parent) if name.startswith(".")] == []
