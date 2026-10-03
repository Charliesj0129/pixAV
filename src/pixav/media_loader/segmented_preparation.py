"""Connect managed preparation to the existing lossless FFmpeg segmenter."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
from contextlib import suppress
from pathlib import Path
from threading import Event
from uuid import UUID

from pixav.media_loader.preparation import PreparedArtifact, PreparedSegment, segment_plan
from pixav.media_loader.video_parts import (
    LIMIT_BYTES,
    IncompatibleMediaError,
    MediaOperationError,
    PartMedia,
    contained_file,
    sha256,
)
from pixav.shared.exceptions import MediaDependencyError, RemuxError


def _write_json(path: Path, data: dict, *, exclusive: bool = False) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | (os.O_EXCL if exclusive else os.O_TRUNC)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(data, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())


def _sync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _recover(artifact: PreparedArtifact, directory: Path, identity: UUID, target: int) -> PreparedArtifact | None:
    intent = {"artifact": artifact.model_dump(mode="json"), "identity": str(identity), "target_bytes": target}
    intent_path, receipt = directory / "segment.intent.json", directory / "segments.json"
    if intent_path.exists() or intent_path.is_symlink():
        if json.loads(contained_file(directory, intent_path).read_text()) != intent:
            raise MediaDependencyError("segmentation intent requires reconciliation")
    else:
        if receipt.exists() or receipt.is_symlink():
            raise MediaDependencyError("segmentation receipt has no intent")
        _write_json(intent_path, intent, exclusive=True)
        _sync_directory(directory)
    if not receipt.exists() and not receipt.is_symlink():
        return None
    result = PreparedArtifact.model_validate_json(contained_file(directory, receipt).read_text())
    if result.model_copy(update={"segments": ()}) != artifact or len(result.segments) < 2:
        raise MediaDependencyError("segmentation receipt differs from intent")
    for segment in segment_plan(result):
        path = contained_file(directory, Path(segment.path))
        if path.stat().st_size != segment.size_bytes or sha256(path) != segment.sha256:
            raise MediaDependencyError("prepared segment changed; preserve source")
    return result


def _prepare(
    artifact: PreparedArtifact, directory: Path, identity: UUID, target: int, cancelled: Event
) -> PreparedArtifact:
    if any(path.is_symlink() for path in (directory, *directory.parents)):
        raise MediaDependencyError("symlink in segment output path")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(directory / "segment.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise MediaDependencyError("segmentation already has an active writer") from exc
        recovered = _recover(artifact, directory, identity, target)
        if recovered is not None:
            return recovered

        def check() -> None:
            if cancelled.is_set():
                raise MediaOperationError("segment writer cancelled", category="cancelled")

        # Media eligibility has already been checked by PreparationPolicy;
        # the legacy first-4k CLI retains the segmenter's 4K default.
        media = PartMedia(minimum_dimensions=(1, 1))
        media.check = check
        parts, _ = media.prepare(Path(artifact.path), directory, identity, target=target)
        segments = tuple(
            PreparedSegment(
                index=part.part_index,
                path=str(directory / part.filename),
                size_bytes=part.size_bytes,
                sha256=part.sha256,
                start_seconds=part.start_seconds,
                end_seconds=part.end_seconds,
                media_info=part.media_info,
            )
            for part in parts
        )
        result = artifact.model_copy(update={"segments": segments})
        segment_plan(result)
        check()
        for segment in segments:
            Path(segment.path).chmod(0o600)
        temporary = directory / "segments.pending.json"
        _write_json(temporary, result.model_dump(mode="json"))
        os.replace(temporary, directory / "segments.json")
        _sync_directory(directory)
        return result
    except IncompatibleMediaError as exc:
        raise RemuxError("source cannot be segmented losslessly") from exc
    except (MediaOperationError, OSError, ValueError) as exc:
        raise MediaDependencyError("segmentation requires recovery") from exc
    finally:
        os.close(descriptor)


async def prepare_segments(
    artifact: PreparedArtifact, directory: Path, identity: UUID, *, target_bytes: int
) -> PreparedArtifact:
    """Keep small files whole; fence and recover larger files under one intent.

    Cancellation waits for the bounded tools process to stop before releasing
    its writer lock. A repeated completed operation hashes existing segments
    instead of running FFmpeg again. Neither path removes the original file.
    """
    if not 0 < target_bytes < LIMIT_BYTES:
        raise ValueError("invalid segment target")
    if artifact.facts.size_bytes <= target_bytes:
        segment_plan(artifact)
        return artifact
    cancelled = Event()
    work = asyncio.create_task(asyncio.to_thread(_prepare, artifact, directory, identity, target_bytes, cancelled))
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        cancelled.set()
        with suppress(Exception):
            await work
        raise
