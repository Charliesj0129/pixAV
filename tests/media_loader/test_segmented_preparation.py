"""BDD-028/035/038: managed splitting preserves sources and fences recovery."""

# ruff: noqa: S603,S607 -- fixed FFmpeg command with a generated local fixture

import asyncio
import fcntl
import hashlib
import os
import subprocess
from pathlib import Path
from threading import Event
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from pixav.media_loader.activity import MediaActivityWorker
from pixav.media_loader.preparation import MediaFacts, PreparedArtifact, segment_plan
from pixav.media_loader.remuxer import FFmpegRemuxer
from pixav.media_loader.segmented_preparation import prepare_segments
from pixav.media_loader.video_parts import IncompatibleMediaError, MediaOperationError, PartMedia
from pixav.shared.exceptions import MediaDependencyError, RemuxError
from pixav.shared.models import VideoPart


@pytest.fixture
def split_setup(tmp_path, monkeypatch):
    source = tmp_path / "synthetic.mp4"
    source.write_bytes(b"original" * 20)
    facts = MediaFacts(
        container="mov,mp4",
        size_bytes=source.stat().st_size,
        duration_seconds=10,
        sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        streams=(
            {"kind": "video", "codec": "h264", "width": 1920, "height": 1080},
            {"kind": "audio", "codec": "aac"},
        ),
    )
    artifact = PreparedArtifact(path=str(source), input_path=str(source), input_sha256=facts.sha256, facts=facts)

    def split(self, source, directory, identity, *, target):
        parts = []
        for index in range(2):
            digest = hashlib.sha256(str(index).encode() * 50).hexdigest()
            path = directory / f"pixav-{identity}-part-{index}-{digest[:16]}.mp4"
            path.write_bytes(str(index).encode() * 50)
            parts.append(
                VideoPart(
                    video_id=identity,
                    filename=path.name,
                    part_index=index,
                    manifest_version=1,
                    size_bytes=50,
                    sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                    start_seconds=index * 5,
                    end_seconds=(index + 1) * 5,
                )
            )
        return parts, {}

    monkeypatch.setattr(PartMedia, "prepare", split)
    return artifact, tmp_path / "segments", uuid4()


async def test_completed_split_recovery_hashes_all_parts_without_reprocessing(split_setup, monkeypatch):
    artifact, directory, identity = split_setup
    result = await prepare_segments(artifact, directory, identity, target_bytes=100)
    assert len(segment_plan(result)) == 2
    assert Path(artifact.path).read_bytes() == b"original" * 20
    monkeypatch.setattr(PartMedia, "prepare", lambda *_args, **_kwargs: pytest.fail("must reuse committed segments"))
    assert await prepare_segments(artifact, directory, identity, target_bytes=100) == result
    assert (directory / "segments.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("damage", ["missing", "hash", "escape", "intent", "receipt", "receipt_without_intent"])
async def test_recovery_rejects_damaged_or_unowned_segments(split_setup, damage):
    artifact, directory, identity = split_setup
    result = await prepare_segments(artifact, directory, identity, target_bytes=100)
    path = Path(result.segments[0].path)
    if damage == "missing":
        path.unlink()
    elif damage == "hash":
        path.write_bytes(b"z" * 50)
    elif damage == "escape":
        path.unlink()
        path.symlink_to(artifact.path)
    elif damage == "intent":
        (directory / "segment.intent.json").write_text("{}")
    elif damage == "receipt_without_intent":
        (directory / "segment.intent.json").unlink()
    else:
        (directory / "segments.json").write_text(artifact.model_dump_json())
    with pytest.raises(MediaDependencyError):
        await prepare_segments(artifact, directory, identity, target_bytes=100)
    assert Path(artifact.path).exists()


async def test_active_writer_and_symlink_root_block_split(split_setup):
    artifact, directory, identity = split_setup
    directory.mkdir()
    with (directory / "segment.lock").open("w") as locked:
        fcntl.flock(locked, fcntl.LOCK_EX)
        with pytest.raises(MediaDependencyError, match="active writer"):
            await prepare_segments(artifact, directory, identity, target_bytes=100)
    link = directory.parent / "link"
    link.symlink_to(directory, target_is_directory=True)
    with pytest.raises(MediaDependencyError, match="symlink"):
        await prepare_segments(artifact, link, identity, target_bytes=100)


@pytest.mark.parametrize(
    "failure,expected",
    [(IncompatibleMediaError("codec"), RemuxError), (MediaOperationError("timeout"), MediaDependencyError)],
)
async def test_media_and_infrastructure_errors_remain_distinct(split_setup, monkeypatch, failure, expected):
    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(PartMedia, "prepare", fail)
    with pytest.raises(expected):
        await prepare_segments(*split_setup, target_bytes=100)


async def test_lease_cancellation_waits_for_writer_to_stop(split_setup, monkeypatch):
    started, stopped = Event(), Event()

    def wait(self, *args, **kwargs):
        started.set()
        try:
            while True:
                self.check()
                stopped.wait(0.01)
        finally:
            stopped.set()

    monkeypatch.setattr(PartMedia, "prepare", wait)
    running = asyncio.create_task(prepare_segments(*split_setup, target_bytes=100))
    await asyncio.to_thread(started.wait, 5)
    assert started.is_set()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert stopped.is_set()
    assert Path(split_setup[0].path).exists()


async def test_small_media_stays_whole_and_invalid_targets_are_rejected(split_setup):
    artifact, directory, identity = split_setup
    assert await prepare_segments(artifact, directory, identity, target_bytes=1000) == artifact
    assert not directory.exists()
    for value in (0, 10_000_000_000):
        with pytest.raises(ValueError):
            await prepare_segments(artifact, directory, identity, target_bytes=value)
        with pytest.raises(ValueError):
            MediaActivityWorker(
                AsyncMock(), AsyncMock(), AsyncMock(), output_dir=str(directory), segment_target_bytes=value
            )


async def test_managed_prepare_reports_real_lossless_segments_bdd_028_035_038(tmp_path, monkeypatch):
    if os.getenv("PIXAV_RUN_MEDIA_TESTS") != "1":
        pytest.skip("set PIXAV_RUN_MEDIA_TESTS=1 for real FFmpeg")
    source = tmp_path / "synthetic.mp4"
    subprocess.run(  # noqa: S603,S607 -- generated fixture, fixed FFmpeg arguments
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=8",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:sample_rate=48000",
            "-t",
            "4",
            "-c:v",
            "libx264",
            "-g",
            "8",
            "-bf",
            "0",
            "-c:a",
            "aac",
            str(source),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    from pixav.shared.workflow import ActivityRequest

    request = ActivityRequest(
        task_id=uuid4(),
        execution_id=uuid4(),
        attempt_id=uuid4(),
        operation_id=uuid4(),
        owner=uuid4(),
        generation=1,
        stage="prepare",
        identity="a" * 40,
        input_path=str(source),
    )
    worker = MediaActivityWorker(
        AsyncMock(),
        AsyncMock(),
        FFmpegRemuxer(),
        output_dir=str(tmp_path / "output"),
        segment_target_bytes=source.stat().st_size - 1,
    )
    result = await worker.execute(request)
    assert result.outcome == "success"
    assert len(segment_plan(result.prepared)) >= 2
    assert source.is_file()
    monkeypatch.setattr(
        PartMedia, "prepare", lambda *_args, **_kwargs: pytest.fail("replayed prepare must reuse parts")
    )
    assert (await worker.execute(request)).prepared == result.prepared
