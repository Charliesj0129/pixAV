"""Playback facts are separate from remote durability and live client acceptance."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class PlayableAsset(BaseModel):
    model_config = {"frozen": True}

    video_id: UUID
    remote_asset_id: UUID
    state: Literal["PREPARING", "READY", "STALE", "INVALID"]
    manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    cache_path: str | None = None
    size_bytes: int | None = Field(default=None, gt=0)
    sha256: str | None = None
    evidence: dict = Field(default_factory=dict)
    playback_verified_at: datetime | None = None


class LibraryPublication(BaseModel):
    model_config = {"frozen": True}

    video_id: UUID
    state: Literal["PENDING", "PUBLISHED", "STALE", "FAILED"]
    manifest_sha256: str | None = None
    revision: str | None = None
    metadata: dict = Field(default_factory=dict)
    poster_path: str | None = None
    poster_sha256: str | None = None
