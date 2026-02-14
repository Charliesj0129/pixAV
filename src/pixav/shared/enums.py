"""Domain enumerations used across all modules."""

from __future__ import annotations

from enum import Enum, unique


@unique
class TaskState(str, Enum):
    """Lifecycle states for a pipeline task."""

    PENDING = "pending"
    DOWNLOADING = "downloading"
    REMUXING = "remuxing"
    UPLOADING = "uploading"
    VERIFYING = "verifying"
    COMPLETE = "complete"
    FAILED = "failed"


@unique
class AccountStatus(str, Enum):
    """Google account health states."""

    ACTIVE = "active"
    COOLDOWN = "cooldown"
    BANNED = "banned"
    UNVERIFIED = "unverified"


@unique
class VideoStatus(str, Enum):
    """Video availability states."""

    DISCOVERED = "discovered"
    DOWNLOADING = "downloading"
    DOWNLOADED = "downloaded"
    UPLOADING = "uploading"
    AVAILABLE = "available"
    EXPIRED = "expired"
    FAILED = "failed"


@unique
class StorageHealth(str, Enum):
    """Health states for a Google Photos storage instance."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    FULL = "full"
    OFFLINE = "offline"
