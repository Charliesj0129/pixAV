"""Exact positive remote-outcome review; absence in a search is never proof."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from pixav.shared.cold_receipts import AccountColdBinding, validate_account_cold_receipt
from pixav.shared.storage_models import IntegrityError


def recovery_digest(recovery: dict) -> str:
    facts = {k: v for k, v in recovery.items() if k != "upload_outcome_review"}
    return hashlib.sha256(
        json.dumps(facts, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def validate_review_cold_binding(segment, share_url: str, receipt: dict) -> None:
    try:
        validate_account_cold_receipt(segment, receipt)
    except ValueError:
        raise IntegrityError("review cold account binding invalid") from None
    if (
        receipt.get("provider_asset_id") != str(segment.asset_id)
        or receipt.get("provider_segment_index") != segment.segment_index
        or receipt.get("provider_share_sha256") != hashlib.sha256(share_url.encode()).hexdigest()
        or receipt.get("cold_inputs") != "provider-only"
        or sorted(receipt.get("cold_mounts", [])) not in (["/work:rw"], ["/app/src:ro", "/work:rw"])
    ):
        raise IntegrityError("review cold session is not bound to the exact original")


class UploadReviewBinding(BaseModel):
    """Private evidence, queued for the owning authority, never a success result."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    database_identity: str = Field(min_length=1)
    database: str = Field(min_length=1)
    account_id: uuid.UUID
    guest_owner: str = Field(min_length=1)
    profile: Literal["gphotos_pixel_xl_v1"]
    execution_id: uuid.UUID
    operation_id: uuid.UUID
    asset_id: uuid.UUID
    segment_index: int = Field(ge=0)
    generation: int = Field(ge=0)
    segment_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    recovery_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    artifact_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    observed_at: AwareDatetime
    provider_completed_after: AwareDatetime
    provider_completed_before: AwareDatetime

    @model_validator(mode="after")
    def completion_interval(self) -> UploadReviewBinding:
        if not self.provider_completed_after <= self.provider_completed_before <= self.observed_at:
            raise ValueError("invalid provider completion interval")
        return self

    def validate_current(
        self, execution, segment, *, operation_id, instance: str, database: str, now: datetime
    ) -> None:
        if not now - timedelta(hours=1) <= self.observed_at <= now:
            raise ValueError("upload outcome evidence expired or in the future")
        expected = (
            instance,
            database,
            execution["id"],
            operation_id,
            segment.asset_id,
            segment.segment_index,
            execution["generation"],
            segment.account_id,
            segment.sha256,
            recovery_digest(segment.recovery),
        )
        observed = (
            self.database_identity,
            self.database,
            self.execution_id,
            self.operation_id,
            self.asset_id,
            self.segment_index,
            self.generation,
            self.account_id,
            self.segment_sha256,
            self.recovery_sha256,
        )
        if expected != observed:
            raise ValueError("upload outcome binding or recovery changed")


class UploadOutcomeReview(UploadReviewBinding):
    """An existing original already independently downloaded by the reviewer."""

    version: Literal["upload-outcome-review-v1"]
    outcome: Literal["EXISTING_ORIGINAL"]
    share_url: str = Field(min_length=1, repr=False)
    backup_evidence: dict = Field(repr=False)
    cold_readback: dict = Field(repr=False)

    @model_validator(mode="after")
    def cold_account_owner(self) -> UploadOutcomeReview:
        authentication = self.cold_readback.get("authentication")
        if authentication is not None:
            binding = AccountColdBinding.model_validate(authentication)
            if binding.account_id != self.account_id or str(binding.owner) != self.guest_owner:
                raise ValueError("cold authentication differs from review account/owner")
        return self


class ExistingItemShareReview(UploadReviewBinding):
    """Authorize inspection/sharing of an existing item, never an upload.

    The selected guest UI must independently match these observations before
    creating any link. Neither acceptance nor a link is a success result.
    """

    version: Literal["upload-outcome-review-v2"]
    outcome: Literal["EXISTING_ITEM_REQUIRES_COLD"]
    filename: str = Field(min_length=1)
    same_account_confirmed: Literal[True]
    backed_up: Literal[True]
    original_quality: Literal[True]


UploadReview = Annotated[UploadOutcomeReview | ExistingItemShareReview, Field(discriminator="version")]


def parse_upload_review(value: dict) -> UploadOutcomeReview | ExistingItemShareReview:
    return TypeAdapter(UploadReview).validate_python(value)


def validate_share_recovery(stored: dict, recovery: dict) -> None:
    """Only the share journal may grow after this exact review was accepted."""
    baseline = stored.get("recovery_before")
    if not isinstance(baseline, dict) or recovery_digest(baseline) != stored["evidence"]["recovery_sha256"]:
        raise ValueError("missing original recovery binding")
    current = {k: v for k, v in recovery.items() if k != "upload_outcome_review"}
    allowed = {"share_intent", "link_intent", "clipboard_before", "share_url", "existing_item_share"}
    if set(current) - set(baseline) - allowed - {"operations"}:
        raise ValueError("unexpected recovery mutation")
    for key, value in baseline.items():
        if key != "operations" and current.get(key) != value:
            raise ValueError("original recovery fact changed")
    old_operations = baseline.get("operations", {})
    operations = current.get("operations", {})
    if set(operations) - set(old_operations) - {"share"}:
        raise ValueError("non-share operation added")
    for key, value in old_operations.items():
        if key == "share":
            if any(operations.get(key, {}).get(k) != v for k, v in value.items()):
                raise ValueError("original share receipt changed")
        elif operations.get(key) != value:
            raise ValueError("original operation receipt changed")
