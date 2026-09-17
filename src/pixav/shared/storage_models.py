"""Frozen remote-storage domain models and the versioned verification policy.

A provider reference is a clue for re-acquiring media, never proof that the
media is durable. Durability is a committed transition backed by an independent
cold read-back, and this module owns the rules that transition must satisfy.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from types import MappingProxyType
from typing import Literal, Protocol

from pydantic import BaseModel, Field, ValidationError, model_validator

from pixav.media_loader.preparation import MediaFacts, timeline_defect
from pixav.shared.models import utc_now

# Only a Photos sharing location can stand for a Photos asset. Any other host is
# a different provider with a different, separately proven integrity policy.
SHARE_URL_PREFIXES = ("https://photos.app.goo.gl/", "https://photos.google.com/")

# The only provenance that proves the bytes came back from the provider rather
# than from a local staging file or an upload temporary.
COLD_READBACK_METHOD = "photos-original-browser"

# New assets are bound to this version at creation. Older versions stay
# available in POLICIES below so their evidence keeps its original meaning.
CURRENT_POLICY_VERSION = "photos-original-v2"

# Every version that has ever been bound to an asset. A version is never
# removed from here: an asset recorded under it still has to be interpretable.
PolicyVersion = Literal["photos-original-v1", "photos-original-v2"]

RemoteAssetState = Literal["REQUESTED", "CREATED", "VERIFIED", "DURABLE", "INVALID"]
SegmentState = Literal[
    "prepared",
    "upload_intent",
    "reconcile",
    "user_action_required",
    "quota_wait",
    "backed_up",
    "verified",
    "failed",
]


class RemoteAssetSegment(BaseModel):
    """One uploadable piece of a remote asset; effects live in PostgreSQL."""

    model_config = {"frozen": True}

    asset_id: uuid.UUID
    segment_index: int = Field(ge=0)
    start_seconds: float = Field(ge=0, allow_inf_nan=False)
    end_seconds: float = Field(gt=0, allow_inf_nan=False)
    size_bytes: int = Field(gt=0, lt=10_000_000_000)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    local_path: str
    media_info: dict = Field(default_factory=dict)
    share_url: str | None = None
    account_id: uuid.UUID | None = None
    state: SegmentState = "prepared"
    recovery: dict = Field(default_factory=dict)
    verification: dict = Field(default_factory=dict)
    uploaded_at: datetime | None = None
    usage_counted_at: datetime | None = None
    retry_not_before: datetime | None = None
    updated_at: datetime | None = None

    @property
    def filename(self) -> str:
        """Provider-side name carrying this segment's own identity.

        The hash prefix means a segment from a different manifest version can
        never be mistaken for this one inside the guest's media store.
        """
        return f"pixav-{self.asset_id}-part-{self.segment_index}-{self.sha256[:16]}.mp4"


class TransferableFile(Protocol):
    """What an upload or read-back adapter needs to know about a file.

    Both the isolated per-part flow and the managed remote asset satisfy this,
    so the proven Photos automation is shared rather than duplicated.
    """

    @property
    def filename(self) -> str: ...

    @property
    def size_bytes(self) -> int: ...

    @property
    def sha256(self) -> str: ...

    @property
    def share_url(self) -> str | None: ...

    @property
    def media_info(self) -> dict: ...


class RemoteAsset(BaseModel):
    """The provider-side copy of one prepared artifact."""

    model_config = {"frozen": True}

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    video_id: uuid.UUID
    artifact_id: uuid.UUID
    provider: Literal["google_photos"] = "google_photos"
    state: RemoteAssetState = "REQUESTED"
    policy_version: str = CURRENT_POLICY_VERSION
    expected: dict = Field(default_factory=dict)
    evidence: dict = Field(default_factory=dict)
    segment_count: int = Field(gt=0)
    durable_at: datetime | None = None
    invalidated_reason: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime | None = None


class IntegrityError(ValueError):
    """A read-back did not satisfy the configured verification policy."""


class VerificationPolicy(BaseModel):
    """``photos-original-v1``: per-segment byte identity, independently fetched.

    The policy version is recorded with every asset so a later change cannot
    retroactively re-interpret evidence that was collected under older rules.
    Full-original verification means the complete byte count and SHA-256 match;
    a hash failure is never relaxed into a weaker check at verification time.

    This version proves each segment came back from the provider with exactly
    the expected bytes. It cannot say anything about the media those bytes
    contain, because a v1 read-back receipt carried no observation of it --
    which is why :class:`PhotosOriginalV2` exists rather than this class being
    tightened in place.
    """

    model_config = {"frozen": True}

    version: PolicyVersion = "photos-original-v1"
    duration_tolerance: float = Field(default=0.05, ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def _version_names_these_rules(self) -> VerificationPolicy:
        """A policy cannot be labelled with a version it does not implement.

        The declared type covers every known version so that a subclass can
        pin its own, which also makes it possible to construct a v1 object
        calling itself v2. That would make the recorded version a lie, so it
        is refused here; ``policy_for`` is the only way to reach a policy.
        """
        if self.version != type(self).model_fields["version"].default:
            raise ValueError("a verification policy version names the rules that implement it")
        return self

    def validate_share_location(self, share_url: str) -> None:
        if not share_url.startswith(SHARE_URL_PREFIXES):
            raise IntegrityError("invalid Photos share location")

    def validate_backup_evidence(self, evidence: dict) -> None:
        """Upload-side evidence. It records creation, never durability."""
        if not evidence.get("backed_up") or not evidence.get("original_quality"):
            raise IntegrityError("backup evidence missing")

    def validate_segment_readback(self, segment: RemoteAssetSegment, evidence: dict) -> None:
        """Cold read-back of one segment: full bytes and hash, independent session."""
        if evidence.get("method") != COLD_READBACK_METHOD:
            raise IntegrityError("independent cloud provenance required")
        if evidence.get("size") != segment.size_bytes:
            raise IntegrityError("remote byte count does not match the expected artifact")
        if evidence.get("sha256") != segment.sha256:
            raise IntegrityError("remote content hash does not match the expected artifact")

    def validate_asset_readback(self, expected: MediaFacts, observed: MediaFacts) -> None:
        """Whole-asset media consistency once every segment has been read back."""
        if observed.size_bytes != expected.size_bytes:
            raise IntegrityError("remote byte count does not match the expected artifact")
        if observed.sha256 != expected.sha256:
            raise IntegrityError("remote content hash does not match the expected artifact")
        if {s.codec for s in observed.streams} != {s.codec for s in expected.streams}:
            raise IntegrityError("remote codec expectations do not match")
        if abs(observed.duration_seconds - expected.duration_seconds) > self.duration_tolerance + 1e-9:
            raise IntegrityError("remote duration differs beyond policy tolerance")

    def validate_segment_media(self, segment: RemoteAssetSegment, expected: dict, evidence: dict) -> None:
        """No media observation was collected under v1, so there is none to check.

        Reading a v1 receipt under a stricter rule is precisely the retroactive
        reinterpretation the recorded version exists to prevent.
        """
        return None

    def validate_manifest_timeline(self, expected: dict, segments: Sequence[RemoteAssetSegment]) -> None:
        """v1 relied on the plan-time junction check and recorded nothing more."""
        return None


class PhotosOriginalV2(VerificationPolicy):
    """``photos-original-v2``: the v1 byte rules plus whole-asset consistency.

    A manifest of individually correct segments can still fail to be the film:
    the pieces may not meet, or the bytes that hash correctly may not decode to
    the media that was prepared. v2 requires the read-back to report what it
    actually retrieved, and re-checks the persisted timeline against the same
    rule the plan was accepted under, before anything becomes durable.
    """

    version: PolicyVersion = "photos-original-v2"

    def _facts(self, payload: object, missing: str) -> MediaFacts:
        try:
            return MediaFacts.model_validate(payload)
        except (TypeError, ValidationError) as exc:
            raise IntegrityError(missing) from exc

    def validate_segment_media(self, segment: RemoteAssetSegment, expected: dict, evidence: dict) -> None:
        """What the read-back observed must be the media this segment stands for."""
        observed = self._facts(evidence.get("observed"), "read-back receipt carries no observed media facts")
        whole = self._facts(expected, "the asset records no media expectation to verify against")
        span = segment.end_seconds - segment.start_seconds
        if segment.start_seconds == 0.0 and abs(span - whole.duration_seconds) <= self.duration_tolerance + 1e-9:
            # One segment standing for the whole artifact, so the strongest
            # available check applies: complete hash, byte count and duration.
            self.validate_asset_readback(whole, observed)
            return
        if observed.size_bytes != segment.size_bytes or observed.sha256 != segment.sha256:
            # The probe has to describe the very bytes that satisfied the hash,
            # or it is an observation of something else entirely.
            raise IntegrityError("observed media facts describe different bytes than the read-back")
        if {s.codec for s in observed.streams} != {s.codec for s in whole.streams}:
            raise IntegrityError("remote codec expectations do not match")
        if abs(observed.duration_seconds - span) > self.duration_tolerance + 1e-9:
            raise IntegrityError("remote segment duration differs beyond policy tolerance")

    def validate_manifest_timeline(self, expected: dict, segments: Sequence[RemoteAssetSegment]) -> None:
        """The persisted manifest must still cover the whole film exactly once."""
        whole = self._facts(expected, "the asset records no media expectation to verify against")
        defect = timeline_defect(
            [(s.segment_index, s.start_seconds, s.end_seconds) for s in segments],
            whole.duration_seconds,
            tolerance=self.duration_tolerance,
        )
        if defect:
            raise IntegrityError(defect)


# Every version that has ever been recorded against an asset stays here and
# stays frozen: an asset is promoted under the rules its evidence was collected
# under, never under whichever rules happen to be current.
POLICIES: Mapping[str, VerificationPolicy] = MappingProxyType(
    {
        "photos-original-v1": VerificationPolicy(),
        CURRENT_POLICY_VERSION: PhotosOriginalV2(),
    }
)


def policy_for(version: str) -> VerificationPolicy:
    """The frozen rules an asset's evidence was collected under."""
    policy = POLICIES.get(version)
    if policy is None:
        raise IntegrityError("evidence was recorded under an unknown verification policy")
    return policy


def current_policy() -> VerificationPolicy:
    """The rules a newly created asset is bound to."""
    return POLICIES[CURRENT_POLICY_VERSION]
