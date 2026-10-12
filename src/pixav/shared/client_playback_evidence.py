"""Operator attestation of actual audiovisual playback, separate from HTTP checks."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class ClientPlaybackEvidence(BaseModel):
    """A private recording digest and explicit observations bound to one object.

    This validates the operator's report, not the truth of a recording. Live
    acceptance still requires a human to watch and listen on the named client.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    method: Literal["jellyfin-client-v1"]
    video_id: uuid.UUID
    playable_asset_id: uuid.UUID
    remote_asset_id: uuid.UUID
    preparation_version: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(gt=0)
    observed_at: AwareDatetime
    client_version: str = Field(min_length=1)
    server_version: str = Field(min_length=1)
    operator: str = Field(min_length=1)
    recording_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    beginning_audio_video: Literal["PASS"]
    middle_audio_video: Literal["PASS"]
    end_audio_video: Literal["PASS"]
    seek: Literal["PASS"]
    resume: Literal["PASS"]

    def matches(self, row: dict) -> bool:
        return all(
            str(getattr(self, field)) == str(row[column])
            for field, column in (
                ("video_id", "video_id"),
                ("playable_asset_id", "id"),
                ("remote_asset_id", "remote_asset_id"),
                ("preparation_version", "preparation_version"),
                ("sha256", "sha256"),
                ("size", "size_bytes"),
            )
        )


class PlaybackWindow(BaseModel):
    """A watched/listened window, with content position measured offline."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal["beginning", "middle", "end", "junction_crossing", "seek", "resume", "supplemental"]
    expected_seconds: float = Field(ge=0, allow_inf_nan=False)
    content_seconds: float = Field(ge=0, allow_inf_nan=False)
    recording_seconds: float = Field(ge=0, allow_inf_nan=False)
    duration_seconds: float = Field(ge=10, allow_inf_nan=False)
    uncertainty_seconds: float = Field(ge=0, le=2, allow_inf_nan=False)
    boundary_seconds: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    visual_confirmation: Literal["PASS"]
    audio_confirmation: Literal["PASS", "source-silent"]
    identifiable: bool
    source_static: bool = False
    continuous: Literal[True]

    @model_validator(mode="after")
    def position_bound(self) -> PlaybackWindow:
        if not self.identifiable and (not self.source_static or self.kind in ("seek", "resume")):
            raise ValueError("unidentifiable position requires a documented static source and supplemental review")
        if abs(self.content_seconds - self.expected_seconds) + self.uncertainty_seconds > 2 + 1e-9:
            raise ValueError("content position plus sampling uncertainty exceeds two seconds")
        if self.kind == "junction_crossing":
            if self.boundary_seconds is None or not (
                self.content_seconds <= self.boundary_seconds - 5
                and self.content_seconds + self.duration_seconds >= self.boundary_seconds + 5
            ):
                raise ValueError("recording must continuously cover five seconds on both sides of the junction")
        elif self.boundary_seconds is not None:
            raise ValueError("only a junction window can name a boundary")
        return self


class ClientPlaybackEvidenceV2(ClientPlaybackEvidence):
    """No v1 upgrade: complete recording/reference bindings and human review."""

    method: Literal["jellyfin-client-v2"]  # type: ignore[assignment]
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reference_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reference_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    trace_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    analysis_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    item_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    device_id: str = Field(min_length=1)
    duration_seconds: float = Field(ge=30, allow_inf_nan=False)
    junction_seconds: tuple[float, ...]
    saved_resume_seconds: float = Field(ge=0, allow_inf_nan=False)
    operator_confirmed_at: AwareDatetime
    windows: tuple[PlaybackWindow, ...] = Field(min_length=5)

    @model_validator(mode="after")
    def complete_review(self) -> ClientPlaybackEvidenceV2:
        if self.operator_confirmed_at < self.observed_at:
            raise ValueError("human review cannot precede capture")
        if any(not 5 < x < self.duration_seconds - 5 for x in self.junction_seconds):
            raise ValueError("junction is outside the playable timeline")
        if tuple(sorted(set(self.junction_seconds))) != self.junction_seconds:
            raise ValueError("junction set must be unique and ordered")
        for kind in ("beginning", "middle", "end", "seek", "resume"):
            if sum(w.kind == kind for w in self.windows) != 1:
                raise ValueError("each required sampling window must occur exactly once")
        boundaries = tuple(w.boundary_seconds for w in self.windows if w.kind == "junction_crossing")
        if tuple(sorted(boundaries)) != self.junction_seconds:  # type: ignore[type-var, arg-type]
            raise ValueError("complete junction set was not reviewed")
        by_kind: dict[str, PlaybackWindow] = {w.kind: w for w in self.windows if w.kind != "junction_crossing"}
        for kind, target in (
            ("beginning", 0),
            ("middle", self.duration_seconds / 2 - 5),
            ("end", self.duration_seconds - 10),
        ):
            if abs(by_kind[kind].expected_seconds - target) > 1e-6:
                raise ValueError("required window moved away from its reference position")
        if abs(by_kind["resume"].expected_seconds - self.saved_resume_seconds) > 1e-6:
            raise ValueError("resume does not match the Jellyfin bookmark")
        self._require_supplement()
        return self

    def _require_supplement(self) -> None:
        if any(not w.identifiable or w.audio_confirmation == "source-silent" for w in self.windows) and not any(
            w.kind == "supplemental" and w.identifiable and w.audio_confirmation == "PASS" for w in self.windows
        ):
            raise ValueError("static/silent source windows need an identifiable audiovisual supplement")

    def matches_manifest(self, segments: list[dict]) -> bool:
        ordered = sorted(segments, key=lambda s: s["segment_index"])
        if not ordered or [s["segment_index"] for s in ordered] != list(range(len(ordered))):
            return False
        return (
            abs(float(ordered[-1]["end_seconds"]) - self.duration_seconds) <= 1e-6
            and self.manifest_sha256 == playback_manifest_digest(segments)
            and self.junction_seconds
            == tuple(float(s["start_seconds"]) for s in sorted(segments, key=lambda s: s["segment_index"])[1:])
        )


def playback_manifest_digest(segments: list[dict]) -> str:
    """Hash immutable segment identity/timeline, without bearer URLs or paths."""
    fields = ("asset_id", "segment_index", "sha256", "size_bytes", "start_seconds", "end_seconds", "media_info")
    rows = []
    for segment in sorted(segments, key=lambda s: s["segment_index"]):
        row = {key: segment[key] for key in fields}
        row["asset_id"] = str(row["asset_id"])
        if isinstance(row["media_info"], str):
            row["media_info"] = json.loads(row["media_info"])
        rows.append(row)
    return hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def parse_client_playback_evidence(value: object) -> ClientPlaybackEvidence | ClientPlaybackEvidenceV2:
    if isinstance(value, dict) and value.get("method") == "jellyfin-client-v2":
        return ClientPlaybackEvidenceV2.model_validate(value)
    return ClientPlaybackEvidence.model_validate(value)


async def client_matches_current_manifest(client: ClientPlaybackEvidence, row: dict, conn) -> bool:
    if not client.matches(row) or not row["ready_at"] <= client.observed_at <= row["checked_at"]:
        return False
    if isinstance(client, ClientPlaybackEvidenceV2):
        if client.operator_confirmed_at > row["checked_at"]:
            return False
        segments = await conn.fetch(
            "SELECT * FROM remote_asset_segments WHERE asset_id=$1 ORDER BY segment_index", row["remote_asset_id"]
        )
        source = await conn.fetchval(
            "SELECT w.facts FROM remote_assets r JOIN workflow_artifacts w ON w.id=r.artifact_id WHERE r.id=$1",
            row["remote_asset_id"],
        )
        source = json.loads(source) if isinstance(source, str) else source
        return (
            bool(segments)
            and client.matches_manifest([dict(s) for s in segments])
            and (source or {}).get("facts", {}).get("sha256") == client.reference_source_sha256
        )
    return True
