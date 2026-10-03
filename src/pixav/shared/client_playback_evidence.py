"""Operator attestation of actual audiovisual playback, separate from HTTP checks."""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


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
