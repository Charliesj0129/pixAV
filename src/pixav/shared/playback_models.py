"""Frozen PlayableAsset model and the rules a playback materialization must meet.

A durable remote copy proves the provider holds the bytes. It does not prove a
player can be handed a complete film: the segments still have to be brought
back, joined and checked against what they stand for. PlayableAsset is that
second fact, and this module owns the rules it is committed under.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from pixav.media_loader.preparation import MediaFacts, PreparationPolicy, timeline_defect
from pixav.shared.cold_receipts import AccountColdBinding, validate_account_cold_receipt
from pixav.shared.models import utc_now
from pixav.shared.storage_models import COLD_READBACK_METHOD, IntegrityError, RemoteAssetSegment

# How a playable object is assembled from a remote manifest. A single segment
# is served as the provider's original bytes; several are joined by a stream
# copy in the pinned tools image. Changing either is a new version.
PREPARATION_VERSION = "photos-playback-v1"

# What the materialization container writes into its receipt. Anything else
# is a report from some other process and proves nothing about this one.
MATERIALIZATION_METHOD = "photos-playback-materialize"

PlayableState = Literal["PREPARING", "READY", "STALE", "INVALID"]
PlaybackPurpose = Literal["prepare", "rebuild"]
# Where the segment bytes came from. ``verified-readback`` means the files the
# storage read-back already fetched cold from Photos and hash-checked.
PlaybackInputs = Literal["verified-readback", "photos", "mixed"]


class PlayableAsset(BaseModel):
    """The committed fact that a remote asset can be served as one object."""

    model_config = {"frozen": True}

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    video_id: uuid.UUID
    remote_asset_id: uuid.UUID
    preparation_version: str = PREPARATION_VERSION
    state: PlayableState = "PREPARING"
    size_bytes: int | None = Field(default=None, gt=0)
    sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    evidence: dict = Field(default_factory=dict)
    ready_at: datetime | None = None
    materialized_at: datetime | None = None
    playback_verified_at: datetime | None = None
    playback_evidence: dict = Field(default_factory=dict)
    invalidated_reason: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime | None = None

    @property
    def cache_name(self) -> str:
        """The playback cache object name: the asset's identity and nothing else."""
        return cache_object_name(self.id)


def cache_object_name(playable_asset_id: uuid.UUID) -> str:
    return f"{playable_asset_id}.mp4"


# Playback cache layout, shared by the worker that fills it and the resolver
# that serves and evicts it. Published objects live under ``objects/`` and are
# named by identity only; each attempt builds in its own fresh directory under
# ``.work/`` so a rebuild can never be satisfied by what an earlier one left.
CACHE_OBJECTS = "objects"
CACHE_WORK = ".work"


def cache_object_path(root: Path, playable_asset_id: uuid.UUID) -> Path:
    return root / CACHE_OBJECTS / cache_object_name(playable_asset_id)


class SegmentReceipt(BaseModel):
    """One segment as the materialization actually obtained it."""

    model_config = {"frozen": True, "extra": "ignore"}

    index: int = Field(ge=0)
    size: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    method: str
    receipt_version: str | None = None
    authentication: AccountColdBinding | None = Field(default=None, repr=False)
    cold_inputs: str | None = None
    cold_mounts: tuple[str, ...] | None = None
    decode: str | None = None


class MaterializationReceipt(BaseModel):
    """What the isolated materialization process reports about its output."""

    model_config = {"frozen": True, "extra": "ignore"}

    method: str
    segments: tuple[SegmentReceipt, ...] = Field(min_length=1)
    junctions: Literal["PASS", "single-segment"]
    observed: MediaFacts
    size: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


def parse_receipt(payload: object) -> MaterializationReceipt:
    try:
        return MaterializationReceipt.model_validate(payload)
    except (TypeError, ValidationError) as exc:
        raise IntegrityError("materialization receipt is malformed") from exc


def _check_segments(ordered: Sequence[RemoteAssetSegment], receipt: MaterializationReceipt) -> None:
    """Every segment came back as exactly its recorded bytes, from the provider."""
    if not ordered or [s.index for s in receipt.segments] != [s.segment_index for s in ordered]:
        raise IntegrityError("materialization does not account for every segment exactly once")
    for segment, obtained in zip(ordered, receipt.segments, strict=True):
        if obtained.method != COLD_READBACK_METHOD:
            raise IntegrityError("a segment was not obtained from the provider original")
        if obtained.size != segment.size_bytes or obtained.sha256 != segment.sha256:
            raise IntegrityError("a segment's bytes do not match the durable manifest")
        try:
            validate_account_cold_receipt(segment, obtained.model_dump(mode="json"))
        except ValueError:
            raise IntegrityError("playback cold account receipt invalid") from None


def _check_join(expected: dict, ordered: Sequence[RemoteAssetSegment], receipt: MaterializationReceipt) -> None:
    """Joined segments must still be the prepared film, if not its exact bytes."""
    try:
        whole = MediaFacts.model_validate(expected)
    except (TypeError, ValidationError) as exc:
        raise IntegrityError("the asset records no media expectation to join against") from exc
    tolerance = PreparationPolicy().duration_tolerance
    defect = timeline_defect(
        [(s.segment_index, s.start_seconds, s.end_seconds) for s in ordered],
        whole.duration_seconds,
        tolerance=tolerance,
    )
    if defect or receipt.junctions != "PASS":
        raise IntegrityError(defect or "segment junctions were not validated")
    if "mp4" not in receipt.observed.container.split(","):
        raise IntegrityError("joined object is not MP4")
    if receipt.observed.streams != whole.streams:
        raise IntegrityError("joined object changed streams, codecs or dimensions")
    if abs(receipt.observed.duration_seconds - whole.duration_seconds) > tolerance + 1e-9:
        raise IntegrityError("joined object duration differs beyond policy tolerance")


def validate_materialization(
    expected: dict, segments: Sequence[RemoteAssetSegment], payload: object
) -> MaterializationReceipt:
    """Accept a materialized object only if it is the film the remote asset holds.

    Every segment must have come back as the exact bytes recorded for it, by the
    one provenance that proves a provider download, and the manifest must still
    cover the whole film exactly once. A single segment is the original file,
    so its bytes are the playable object. Joined segments cannot reproduce the
    prepared file byte for byte, so the result must instead be the same media:
    same streams, same container family, same duration within policy.
    """
    receipt = parse_receipt(payload)
    if receipt.method != MATERIALIZATION_METHOD:
        raise IntegrityError("materialization provenance is not the playback process")
    ordered = sorted(segments, key=lambda segment: segment.segment_index)
    _check_segments(ordered, receipt)
    if receipt.observed.size_bytes != receipt.size or receipt.observed.sha256 != receipt.sha256:
        raise IntegrityError("observed media facts describe different bytes than the object")
    if len(ordered) > 1:
        _check_join(expected, ordered, receipt)
        return receipt
    if receipt.junctions != "single-segment":
        raise IntegrityError("a single-segment object reports a join")
    if receipt.size != ordered[0].size_bytes or receipt.sha256 != ordered[0].sha256:
        raise IntegrityError("a single-segment object must be the provider original")
    return receipt
