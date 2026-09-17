"""Versioned, lossless media preparation facts and policy."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from pixav.shared.exceptions import MediaDependencyError, RemuxError


class MediaStream(BaseModel):
    model_config = {"frozen": True, "extra": "forbid"}
    kind: Literal["video", "audio"]
    codec: str
    width: int = Field(default=0, ge=0)
    height: int = Field(default=0, ge=0)


class MediaFacts(BaseModel):
    model_config = {"frozen": True, "extra": "forbid"}
    container: str = Field(min_length=1)
    size_bytes: int = Field(gt=0)
    duration_seconds: float = Field(gt=0, allow_inf_nan=False)
    streams: tuple[MediaStream, ...] = Field(min_length=2)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class PreparationPolicy(BaseModel):
    model_config = {"frozen": True}
    version: Literal["lossless-mp4-v1"] = "lossless-mp4-v1"
    duration_tolerance: float = Field(default=0.05, ge=0, allow_inf_nan=False)

    def validate_media(self, facts: MediaFacts) -> None:
        video = [s for s in facts.streams if s.kind == "video"]
        audio = [s for s in facts.streams if s.kind == "audio"]
        if len(video) != 1 or not audio:
            raise RemuxError("preparation requires exactly one video and at least one audio stream")
        if video[0].codec not in {"h264", "hevc"} or not video[0].width or not video[0].height:
            raise RemuxError("unsupported video codec or dimensions")
        if any(s.codec not in {"aac", "ac3", "eac3", "alac"} for s in audio):
            raise RemuxError("unsupported audio codec")

    def validate_output(self, source: MediaFacts, output: MediaFacts) -> None:
        self.validate_media(source)
        self.validate_media(output)
        if "mp4" not in output.container.split(","):
            raise RemuxError("prepared output is not MP4")
        if source.streams != output.streams:
            raise RemuxError("remux changed streams, codecs or dimensions")
        if abs(source.duration_seconds - output.duration_seconds) > self.duration_tolerance + 1e-9:
            raise RemuxError("remux duration differs beyond policy tolerance")


class PreparedSegment(BaseModel):
    """One independently uploadable piece of a prepared artifact."""

    model_config = {"frozen": True, "extra": "forbid"}
    index: int = Field(ge=0)
    path: str
    size_bytes: int = Field(gt=0, lt=10_000_000_000)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    start_seconds: float = Field(ge=0, allow_inf_nan=False)
    end_seconds: float = Field(gt=0, allow_inf_nan=False)
    media_info: dict = Field(default_factory=dict)


class PreparedArtifact(BaseModel):
    model_config = {"frozen": True}
    path: str
    input_path: str
    input_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    facts: MediaFacts
    input_facts: MediaFacts | None = None
    policy_version: Literal["lossless-mp4-v1"] = "lossless-mp4-v1"
    # Empty means the artifact is uploaded whole. Splitting is a transport
    # concern of the provider's per-file limit, not a property of the media.
    segments: tuple[PreparedSegment, ...] = ()


def whole_file_segment(artifact: PreparedArtifact) -> PreparedSegment:
    """The single segment standing for an artifact small enough to upload whole."""
    return PreparedSegment(
        index=0,
        path=artifact.path,
        size_bytes=artifact.facts.size_bytes,
        sha256=artifact.facts.sha256,
        start_seconds=0.0,
        end_seconds=artifact.facts.duration_seconds,
        media_info={
            "container": artifact.facts.container,
            "streams": [
                {"codec_type": stream.kind, "codec_name": stream.codec, "width": stream.width, "height": stream.height}
                for stream in artifact.facts.streams
            ],
        },
    )


# Two boundaries meeting closer than this are the same instant; a larger
# difference is a gap or an overlap, whichever way it points.
SEGMENT_JUNCTION_TOLERANCE = 1e-6


def timeline_defect(
    timeline: Sequence[tuple[int, float, float]], duration_seconds: float, *, tolerance: float
) -> str | None:
    """Why an ordered ``(index, start, end)`` timeline fails to reconstruct the
    media, or ``None`` when it covers the whole of it exactly once.

    This is the single definition of "covers the film". It is applied when a
    plan is made and again when the persisted manifest is re-checked before
    durability, so the two can never drift into disagreeing about it.
    """
    spans = sorted(timeline)
    if not spans:
        return "segment plan is empty"
    if [index for index, _, _ in spans] != list(range(len(spans))):
        return "segment plan is not a contiguous zero-based sequence"
    for (_, _, earlier_end), (_, later_start, _) in zip(spans, spans[1:], strict=False):
        if abs(later_start - earlier_end) > SEGMENT_JUNCTION_TOLERANCE:
            return "segment plan leaves a gap or overlap at a boundary"
    if spans[0][1] != 0.0:
        return "segment plan does not start at the beginning of the media"
    # Split segments each carry their own container header, so their sizes do
    # not sum to the source. The timeline is what must reconstruct the film;
    # per-segment bytes are verified individually against their own hashes.
    if abs(spans[-1][2] - duration_seconds) > tolerance:
        return "segment plan does not cover the full media duration"
    return None


def segment_plan(artifact: PreparedArtifact) -> tuple[PreparedSegment, ...]:
    """Segments covering the artifact exactly once, in order, without gaps.

    A rejected plan must not reach the uploader: a manifest that does not
    reconstruct the film cannot be repaired after the bytes are remote.
    """
    if not artifact.segments:
        return (whole_file_segment(artifact),)
    segments = tuple(sorted(artifact.segments, key=lambda s: s.index))
    defect = timeline_defect(
        [(s.index, s.start_seconds, s.end_seconds) for s in segments],
        artifact.facts.duration_seconds,
        tolerance=PreparationPolicy().duration_tolerance,
    )
    if defect:
        raise RemuxError(defect)
    return segments


def _hash_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def inspect_media(path: str) -> MediaFacts:
    from pixav.media_loader.metadata import probe_media

    data = await probe_media(path)
    return MediaFacts(
        container=data["container"],
        size_bytes=data["size_bytes"],
        duration_seconds=data["duration_seconds"],
        streams=data["streams"],
        sha256=data["sha256"],
    )


async def prepare_media(input_path: str, output_path: str, remuxer) -> PreparedArtifact:
    """Reuse verified outputs after crashes; activate only a verified temporary file.

    Callers must serialize the operation and retain both the input and the intent.
    """
    policy = PreparationPolicy()
    input_target = Path(input_path)
    if input_target.is_symlink() or any(parent.is_symlink() for parent in input_target.parents):
        raise RemuxError("symlink in preparation input path")
    source = await inspect_media(input_path)
    policy.validate_media(source)
    if Path(input_path).suffix.lower() == ".mp4" and "mp4" in source.container.split(","):
        return PreparedArtifact(
            path=input_path, input_path=input_path, input_sha256=source.sha256, input_facts=source, facts=source
        )
    output = Path(output_path)
    if output.is_symlink() or any(p.is_symlink() for p in output.parents):
        raise RemuxError("symlink in preparation output path")
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = output.with_suffix(".lock")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise MediaDependencyError("preparation operation already has an active writer") from exc
        _verify_preparation_intent(output, source, policy)
        if output.exists():
            facts = await inspect_media(output_path)
            policy.validate_output(source, facts)
        else:
            temporary = output.with_suffix(".pending.mp4")
            if temporary.is_symlink():
                raise RemuxError("symlink in preparation temporary path")
            if temporary.exists():
                try:
                    facts = await inspect_media(str(temporary))
                    policy.validate_output(source, facts)
                except MediaDependencyError:
                    raise
                except RemuxError:
                    await remuxer.remux(input_path, str(temporary))
                    facts = await inspect_media(str(temporary))
            else:
                await remuxer.remux(input_path, str(temporary))
                facts = await inspect_media(str(temporary))
            policy.validate_output(source, facts)
            os.replace(temporary, output)
        return PreparedArtifact(
            path=output_path, input_path=input_path, input_sha256=source.sha256, input_facts=source, facts=facts
        )
    finally:
        os.close(descriptor)


def _verify_preparation_intent(output: Path, source: MediaFacts, policy: PreparationPolicy) -> None:
    """Bind recoverable output to exact input bytes before invoking FFmpeg.

    Codec/duration equality alone cannot distinguish two different films.
    A torn intent or an output without an intent needs explicit reconciliation.
    """
    receipt = output.with_suffix(".intent.json")
    expected = {"source": source.model_dump(mode="json"), "policy": policy.model_dump(mode="json")}
    if receipt.is_symlink():
        raise RemuxError("symlink in preparation intent")
    if receipt.exists():
        try:
            recorded = json.loads(receipt.read_text())
        except (ValueError, OSError) as exc:
            raise RemuxError("unreadable preparation intent requires reconciliation") from exc
        if recorded != expected:
            raise RemuxError("prepared output belongs to a different input or policy")
        return
    if output.exists() or output.with_suffix(".pending.mp4").exists():
        raise RemuxError("existing output has no preparation intent")
    descriptor = os.open(receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(expected, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())


async def hash_file(path: str) -> str:
    return await asyncio.to_thread(_hash_file, path)
