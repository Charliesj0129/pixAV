"""Account cold receipts add binding without reinterpreting anonymous history."""

from __future__ import annotations

import hashlib
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

AUTHENTICATED_RECEIPT = "photos-cold-account-v1"


class AccountColdBinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: Literal["original-account"]
    account_id: uuid.UUID
    owner: uuid.UUID
    account_email_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    session_id: uuid.UUID
    item_url_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    provider_filename_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    original_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    fresh_context: Literal[True]
    cookie_only: Literal[True]
    local_media_inputs: Literal[False]


def validate_account_cold_receipt(segment, receipt: dict) -> None:
    """Absent version means legacy anonymous; partial/unknown new receipts fail."""
    version = receipt.get("receipt_version")
    authentication = receipt.get("authentication")
    if version is None and authentication is None:
        return
    if version != AUTHENTICATED_RECEIPT:
        raise ValueError("unsupported cold authentication receipt")
    binding = AccountColdBinding.model_validate(authentication)
    if (
        segment.account_id is None
        or binding.account_id != segment.account_id
        or binding.original_sha256 != segment.sha256
        or binding.provider_filename_sha256 != hashlib.sha256(segment.provider_filename.encode()).hexdigest()
        or receipt.get("cold_inputs") != "provider-only"
        or receipt.get("cold_mounts") != ["/work:rw"]
        or receipt.get("decode") != "PASS"
    ):
        raise ValueError("cold account receipt differs from the original binding")
