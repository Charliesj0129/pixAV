"""Frozen Pydantic domain models shared by all modules."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from pixav.shared.enums import AccountStatus, StorageHealth, TaskState, VideoStatus


def utc_now() -> datetime:
    """Return timezone-aware UTC timestamps for model defaults."""
    return datetime.now(timezone.utc)


class Account(BaseModel):
    """A Google account used for Google Photos uploads."""

    model_config = {"frozen": True}

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    email: str
    status: AccountStatus = AccountStatus.ACTIVE
    storage_instance_id: uuid.UUID | None = None
    last_used_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)


class Video(BaseModel):
    """A media item tracked through the pipeline."""

    model_config = {"frozen": True}

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    title: str
    magnet_uri: str | None = None
    local_path: str | None = None
    share_url: str | None = None
    cdn_url: str | None = None
    status: VideoStatus = VideoStatus.DISCOVERED
    metadata_json: str | None = None
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
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime | None = None


class StorageInstance(BaseModel):
    """A Google Photos storage bucket tied to an account."""

    model_config = {"frozen": True}

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    account_id: uuid.UUID
    capacity_bytes: int = 0
    used_bytes: int = 0
    health: StorageHealth = StorageHealth.HEALTHY
    created_at: datetime = Field(default_factory=utc_now)
