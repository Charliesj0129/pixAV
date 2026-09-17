"""Immutable versioned Maxwell activity envelopes; Redis is only a transport."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from pixav.media_loader.preparation import PreparedArtifact


class ExecutionState(str, Enum):
    READY = "READY"
    RUNNING = "RUNNING"
    WAITING_RETRY = "WAITING_RETRY"
    WAITING_QUOTA = "WAITING_QUOTA"
    USER_ACTION_REQUIRED = "USER_ACTION_REQUIRED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ExecutionAttempt(BaseModel):
    """Operator-visible attempt history without raw provider responses."""

    model_config = {"frozen": True, "extra": "ignore"}
    id: UUID
    operation_id: UUID
    generation: int
    stage: str
    started_at: datetime
    completed_at: datetime | None = None
    outcome: str | None = None
    error_code: str | None = None


class Execution(BaseModel):
    model_config = {"frozen": True, "extra": "ignore"}
    id: UUID
    task_id: UUID
    state: ExecutionState
    stage: str
    generation: int
    due_at: datetime
    lease_until: datetime | None = None
    infrastructure_retries: int
    max_retries: int
    recovery_count: int
    blocked_reason: str | None = None
    failure_class: str | None = None
    error_code: str | None = None
    replay_of: UUID | None = None
    attempts: tuple[ExecutionAttempt, ...] = ()


class ActivityRequest(BaseModel):
    model_config = {"frozen": True, "extra": "forbid"}
    version: Literal[1] = 1
    task_id: UUID
    execution_id: UUID
    attempt_id: UUID
    operation_id: UUID
    owner: UUID
    generation: int = Field(gt=0)
    stage: Literal["download", "prepare", "upload", "verify"]
    identity: str
    input_path: str | None = None
    # Storage addressing. The authority resolves which segment an activity acts
    # on; a worker never picks one, so it cannot upload past a quota decision.
    asset_id: UUID | None = None
    segment_index: int | None = None
    account_id: UUID | None = None
    share_url: str | None = None


class ActivityResult(BaseModel):
    model_config = {"frozen": True, "extra": "forbid"}
    version: Literal[1] = 1
    task_id: UUID
    execution_id: UUID
    attempt_id: UUID
    operation_id: UUID
    owner: UUID
    generation: int = Field(gt=0)
    outcome: Literal[
        "success",
        "infrastructure",
        "source_unavailable",
        "invalid_media",
        "unknown_effect",
        "quota_exhausted",
        "user_action",
        "verification_failed",
    ]
    artifact_path: str | None = None
    prepared: PreparedArtifact | None = None
    asset_id: UUID | None = None
    segment_index: int | None = None
    account_id: UUID | None = None
    share_url: str | None = None
    # Integrity receipts only. Never credentials, cookies or provider payloads.
    evidence: dict = Field(default_factory=dict)
    # A bounded code, never an exception message or raw external payload.
    error_code: str | None = Field(default=None, pattern=r"^[A-Z_]{1,80}$")


MANAGED_QUEUE = "pixav:media-activity:v1"
# Storage activities ride their own transport so a media worker can never claim
# an upload, and an upload worker can never claim a download.
STORAGE_QUEUE = "pixav:storage-activity:v1"


async def require_workflow_role(pool, role: Literal["pixav_execution_authority", "pixav_activity_worker"]) -> None:
    """Managed runtime refuses shared privileged logins; roles are provisioned separately."""
    valid = await pool.fetchval(
        """SELECT NOT rolsuper AND pg_has_role(current_user,$1,'member')
        AND ($1 <> 'pixav_activity_worker' OR NOT pg_has_role(current_user,'pixav_execution_authority','member'))
        FROM pg_roles WHERE rolname=current_user""",
        role,
    )
    if not valid:
        raise RuntimeError("managed workflow requires a dedicated non-superuser database role")
