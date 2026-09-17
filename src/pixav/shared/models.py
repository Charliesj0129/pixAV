"""Frozen Pydantic domain models shared by all modules."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from pixav.shared.enums import AccountStatus, StorageHealth, TaskState, VideoStatus


def utc_now() -> datetime:
    """Return timezone-aware UTC timestamps for model defaults."""
    return datetime.now(timezone.utc)


def _new_trace_id() -> str:
    return str(uuid.uuid4())


class Account(BaseModel):
    """A Google account used for Google Photos uploads."""

    model_config = {"frozen": True}

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    email: str
    password: str | None = None
    status: AccountStatus = AccountStatus.ACTIVE
    storage_instance_id: uuid.UUID | None = None
    last_used_at: datetime | None = None
    cooldown_until: datetime | None = None
    daily_uploaded_bytes: int = 0
    daily_quota_bytes: int = 20 * 1024 * 1024 * 1024
    quota_reset_at: datetime | None = None
    lease_expires_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)


class Video(BaseModel):
    """A media item tracked through the pipeline."""

    model_config = {"frozen": True}

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    title: str
    magnet_uri: str | None = None
    local_path: str | None = None
    share_url: str | None = None
    # No cdn_url: a Google Photos CDN URL is signed for about an hour and has no
    # expiry of its own, so persisting it defeats the Redis TTL that bounds it.
    # share_url is the durable fact; the CDN URL lives only in CdnCache.
    status: VideoStatus = VideoStatus.DISCOVERED
    metadata_json: str | None = None
    info_hash: str | None = None
    quality_score: int = 0
    tags: list[str] = Field(default_factory=list)
    embedding: list[float] | None = Field(default=None, exclude=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime | None = None
    local_cleanup_after: datetime | None = None
    manifest_version: int | None = None
    expected_part_count: int | None = None
    playback_manifest_version: int | None = None
    source_provenance: str | dict | None = None


class VideoPart(BaseModel):
    """Immutable part identity; mutable effects live in PostgreSQL."""

    model_config = {"frozen": True}
    video_id: uuid.UUID
    part_index: int = Field(ge=0)
    manifest_version: int = Field(gt=0)
    start_seconds: float = Field(ge=0, allow_inf_nan=False)
    end_seconds: float = Field(gt=0, allow_inf_nan=False)
    size_bytes: int = Field(gt=0, lt=10_000_000_000)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    filename: str = Field(pattern=r"^pixav-[a-f0-9-]+-part-[0-9]+-[a-f0-9]{16}\.mp4$")
    media_info: dict = Field(default_factory=dict)
    share_url: str | None = None
    account_id: uuid.UUID | None = None
    state: str = "prepared"
    recovery: dict = Field(default_factory=dict)
    verification: dict = Field(default_factory=dict)
    uploaded_at: datetime | None = None
    usage_counted_at: datetime | None = None
    retry_not_before: datetime | None = None
    updated_at: datetime | None = None


class SourceCandidate(BaseModel):
    """One obtainable source for a media item.

    A media item may have several. A dead swarm invalidates the candidate, not
    the media item, so the pipeline cools this row down and tries the next one
    instead of failing the film.
    """

    model_config = {"frozen": True}

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    video_id: uuid.UUID
    magnet_uri: str
    info_hash: str | None = None
    origin: str = "sehuatang"
    quality_score: int = 0
    state: str = "pending"
    unavailable_until: datetime | None = None
    attempts: int = 0
    last_error: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime | None = None


class Task(BaseModel):
    """A unit of work flowing through Redis queues."""

    model_config = {"frozen": True}

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    video_id: uuid.UUID
    account_id: uuid.UUID | None = None
    state: TaskState = TaskState.PENDING
    queue_name: str = ""
    local_path: str | None = None
    share_url: str | None = None
    retries: int = 0
    max_retries: int = 3
    error_message: str | None = None
    trace_id: str = Field(default_factory=_new_trace_id)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime | None = None
    retry_not_before: datetime | None = None


class StorageInstance(BaseModel):
    """A Google Photos storage bucket tied to an account."""

    model_config = {"frozen": True}

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    account_id: uuid.UUID
    capacity_bytes: int = 0
    used_bytes: int = 0
    health: StorageHealth = StorageHealth.HEALTHY
    created_at: datetime = Field(default_factory=utc_now)
