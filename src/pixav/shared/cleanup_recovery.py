"""Validate private cleanup recovery receipts without creating any backup.

Restore evidence is a privileged operator attestation. This adapter checks its
files, source sidecar, complete inventory comparison, exact target and runtime
binding; it does not run a backup or independently perform a restore.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from pixav.shared.cleanup_binding import CleanupTargetBinding
from pixav.shared.media_file import file_version, open_media_file

_SHA = r"^[a-f0-9]{64}$"
_MAX_JSON = 1024 * 1024


class CleanupApprovalError(ValueError):
    """Only bounded refusal codes may leave the operator boundary."""


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EvidenceFile(_Frozen):
    path: str = Field(min_length=1, max_length=4096, repr=False)
    sha256: str = Field(pattern=_SHA)
    size_bytes: int = Field(gt=0, strict=True)

    @field_validator("path")
    @classmethod
    def canonical(cls, value: str) -> str:
        if "\0" in value or not Path(value).is_absolute() or os.path.abspath(value) != value:
            raise ValueError("CLEANUP_PRIVATE_PATH_REQUIRED")
        return value


class CleanupRuntimeBinding(_Frozen):
    docker_context: str = Field(min_length=1, max_length=200, repr=False)
    docker_daemon_id: str = Field(min_length=1, max_length=200, repr=False)
    redis_run_id: str = Field(pattern=r"^[a-f0-9]{40}$", repr=False)


class ContentSummary(_Frozen):
    sha256: str = Field(pattern=_SHA)
    count: int = Field(ge=0, strict=True)


class PreservedInventory(_Frozen):
    database: ContentSummary
    roles: ContentSummary
    ledger: ContentSummary
    queue: ContentSummary
    guest: ContentSummary
    watch_history: ContentSummary
    selected_rows: ContentSummary


class CleanupInventory(_Frozen):
    version: Literal["cleanup-inventory-v1"]
    snapshot_id: UUID
    database_identifier: str = Field(pattern=r"^[0-9]{1,20}$", repr=False)
    database_name: str = Field(min_length=1, max_length=63, repr=False)
    target_binding_sha256: str = Field(pattern=_SHA)
    runtime: CleanupRuntimeBinding
    contents: PreservedInventory


class _RecoveryReceipt(_Frozen):
    database_identifier: str = Field(pattern=r"^[0-9]{1,20}$", repr=False)
    database_name: str = Field(min_length=1, max_length=63, repr=False)
    target_binding_sha256: str = Field(pattern=_SHA)
    runtime: CleanupRuntimeBinding
    observed_at: AwareDatetime
    expires_at: AwareDatetime


class RestoreReceipt(_RecoveryReceipt):
    kind: Literal["verified-restore"]
    method: Literal["independent-full-restore-v1"]
    snapshot_id: UUID
    encrypted_backup: EvidenceFile
    source_sidecar: EvidenceFile
    source_inventory: EvidenceFile
    restored_inventory: EvidenceFile
    off_host_attested: bool = Field(strict=True)


class OperatorExceptionReceipt(_RecoveryReceipt):
    kind: Literal["operator-exception"]
    scope: Literal["isolated-acceptance"]
    authorized_by: str = Field(min_length=1, max_length=200, repr=False)
    reason: str = Field(min_length=1, max_length=2000, repr=False)
    instruction: EvidenceFile


Receipt = Annotated[RestoreReceipt | OperatorExceptionReceipt, Field(discriminator="kind")]
_RECEIPT: TypeAdapter[RestoreReceipt | OperatorExceptionReceipt] = TypeAdapter(Receipt)


class CleanupApprovalRequest(_Frozen):
    version: Literal["cleanup-approval-v1"]
    id: UUID
    scope: Literal["isolated-acceptance", "production"]
    expected_count: int = Field(strict=True, ge=1, le=1)
    target: CleanupTargetBinding
    runtime: CleanupRuntimeBinding
    recovery_receipt: EvidenceFile
    approved_by: str = Field(min_length=1, max_length=200, repr=False)
    reason: str = Field(min_length=1, max_length=2000, repr=False)
    expires_at: AwareDatetime


class CleanupRecoveryAuthorization(_Frozen):
    version: Literal["cleanup-recovery-authorization-v1"]
    mode: Literal["verified-restore", "operator-exception"]
    scope: Literal["isolated-acceptance", "production"]
    target_binding_sha256: str = Field(pattern=_SHA)
    runtime: CleanupRuntimeBinding
    receipt_sha256: str = Field(pattern=_SHA)
    proof_sha256: str = Field(pattern=_SHA)
    validated_at: AwareDatetime
    expires_at: AwareDatetime
    off_host_attested: bool = Field(strict=True)


class CleanupAuthorizationEvidence(_Frozen):
    version: Literal["cleanup-authorization-evidence-v1"]
    authorization_id: UUID
    approval_sha256: str = Field(pattern=_SHA)
    target: CleanupTargetBinding
    recovery: CleanupRecoveryAuthorization


def cleanup_approval_digest(request: CleanupApprovalRequest) -> str:
    payload = request.model_dump(mode="json")
    payload["target"]["playback_verified_at"] = request.target.playback_verified_at.astimezone(timezone.utc).isoformat(
        timespec="microseconds"
    )
    payload["expires_at"] = request.expires_at.astimezone(timezone.utc).isoformat(timespec="microseconds")
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def target_binding_digest(target: CleanupTargetBinding) -> str:
    payload = target.model_dump(mode="json")
    payload["playback_verified_at"] = target.playback_verified_at.astimezone(timezone.utc).isoformat(
        timespec="microseconds"
    )
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _unique_object(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise CleanupApprovalError("CLEANUP_DUPLICATE_JSON_FIELD")
        result[key] = value
    return result


def _private_json(path: Path, *, expected: EvidenceFile | None = None) -> tuple[dict, tuple[int, int]]:
    """Read from a locked descriptor; never follow a path or change its atime."""
    with open_media_file(path) as source:
        info = os.fstat(source.descriptor)
        parent = os.fstat(source.parent_descriptor)
        if stat.S_IMODE(info.st_mode) != 0o600 or stat.S_IMODE(parent.st_mode) != 0o700:
            raise CleanupApprovalError("CLEANUP_PRIVATE_FILE_REQUIRED")
        if info.st_size > _MAX_JSON:
            raise CleanupApprovalError("CLEANUP_JSON_TOO_LARGE")
        with source.reader() as reader:
            raw = reader.read(_MAX_JSON + 1)
        if len(raw) != info.st_size or file_version(info) != file_version(os.fstat(source.descriptor)):
            raise CleanupApprovalError("CLEANUP_EVIDENCE_CHANGED")
        if expected is not None and (len(raw), hashlib.sha256(raw).hexdigest()) != (
            expected.size_bytes,
            expected.sha256,
        ):
            raise CleanupApprovalError("CLEANUP_EVIDENCE_CHANGED")
        source.assert_current()
        value = json.loads(raw, object_pairs_hook=_unique_object)
        if not isinstance(value, dict):
            raise CleanupApprovalError("CLEANUP_JSON_OBJECT_REQUIRED")
        return value, (info.st_dev, info.st_ino)


def read_cleanup_request(path: Path) -> CleanupApprovalRequest:
    try:
        if not path.is_absolute() or os.path.abspath(path) != str(path):
            raise CleanupApprovalError("CLEANUP_PRIVATE_PATH_REQUIRED")
        payload, _ = _private_json(path)
        return CleanupApprovalRequest.model_validate(payload)
    except CleanupApprovalError:
        raise
    except (OSError, ValueError, TypeError):
        raise CleanupApprovalError("CLEANUP_REQUEST_INVALID") from None


def _verified_artifact(reference: EvidenceFile) -> None:
    with open_media_file(reference.path) as source:
        info = os.fstat(source.descriptor)
        if stat.S_IMODE(info.st_mode) != 0o600 or stat.S_IMODE(os.fstat(source.parent_descriptor).st_mode) != 0o700:
            raise CleanupApprovalError("CLEANUP_PRIVATE_FILE_REQUIRED")
        if source.facts() != (reference.sha256, reference.size_bytes):
            raise CleanupApprovalError("CLEANUP_EVIDENCE_CHANGED")


def _restore_proof(receipt: RestoreReceipt, request: CleanupApprovalRequest) -> str:
    _verified_artifact(receipt.encrypted_backup)
    sidecar, _ = _private_json(Path(receipt.source_sidecar.path), expected=receipt.source_sidecar)
    if (
        sidecar.get("schema_version") != 1
        or sidecar.get("system_identifier") != receipt.database_identifier
        or sidecar.get("database") != receipt.database_name
        or sidecar.get("backup") != Path(receipt.encrypted_backup.path).name
    ):
        raise CleanupApprovalError("CLEANUP_BACKUP_INSTANCE_MISMATCH")
    source, source_inode = _private_json(Path(receipt.source_inventory.path), expected=receipt.source_inventory)
    restored, restored_inode = _private_json(Path(receipt.restored_inventory.path), expected=receipt.restored_inventory)
    original, recovered = CleanupInventory.model_validate(source), CleanupInventory.model_validate(restored)
    if source_inode == restored_inode or original != recovered:
        raise CleanupApprovalError("CLEANUP_RESTORE_COMPARISON_FAILED")
    if (
        original.snapshot_id != receipt.snapshot_id
        or original.database_identifier != receipt.database_identifier
        or original.database_name != receipt.database_name
        or original.target_binding_sha256 != receipt.target_binding_sha256
        or original.runtime != receipt.runtime
        or (request.scope == "production" and not receipt.off_host_attested)
    ):
        raise CleanupApprovalError("CLEANUP_RESTORE_BINDING_MISMATCH")
    return hashlib.sha256(
        json.dumps(original.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def validate_cleanup_recovery(
    request: CleanupApprovalRequest, *, checked_at: datetime, allow_operator_exception: bool = False
) -> CleanupRecoveryAuthorization:
    """Verify an exact supplied receipt; default policy requires verified restore."""
    try:
        if checked_at.tzinfo is None:
            raise CleanupApprovalError("CLEANUP_DB_CLOCK_REQUIRED")
        payload, _ = _private_json(Path(request.recovery_receipt.path), expected=request.recovery_receipt)
        receipt = _RECEIPT.validate_python(payload)
        if (
            receipt.database_identifier != request.target.database_identifier
            or receipt.database_name != request.target.database_name
            or receipt.target_binding_sha256 != target_binding_digest(request.target)
            or receipt.runtime != request.runtime
        ):
            raise CleanupApprovalError("CLEANUP_RECOVERY_BINDING_MISMATCH")
        if not (
            receipt.observed_at <= checked_at < request.expires_at <= receipt.expires_at
            and receipt.expires_at - receipt.observed_at <= timedelta(hours=24)
            and request.expires_at - checked_at <= timedelta(hours=1)
        ):
            raise CleanupApprovalError("CLEANUP_APPROVAL_WINDOW_INVALID")
        if isinstance(receipt, OperatorExceptionReceipt):
            if (
                not allow_operator_exception
                or request.scope != "isolated-acceptance"
                or receipt.authorized_by != request.approved_by
            ):
                raise CleanupApprovalError("CLEANUP_OPERATOR_EXCEPTION_NOT_ALLOWED")
            _verified_artifact(receipt.instruction)
            proof, off_host = receipt.instruction.sha256, False
        else:
            proof, off_host = _restore_proof(receipt, request), receipt.off_host_attested
        return CleanupRecoveryAuthorization(
            version="cleanup-recovery-authorization-v1",
            mode=receipt.kind,
            scope=request.scope,
            target_binding_sha256=receipt.target_binding_sha256,
            runtime=request.runtime,
            receipt_sha256=request.recovery_receipt.sha256,
            proof_sha256=proof,
            validated_at=checked_at,
            expires_at=request.expires_at,
            off_host_attested=off_host,
        )
    except CleanupApprovalError:
        raise
    except (OSError, ValueError, TypeError):
        raise CleanupApprovalError("CLEANUP_RECOVERY_UNCONFIRMED") from None


def recovery_authorizes(
    authorization: CleanupRecoveryAuthorization,
    target: CleanupTargetBinding,
    runtime: CleanupRuntimeBinding,
    *,
    checked_at: datetime,
    allow_operator_exception: bool = False,
) -> bool:
    return (
        checked_at.tzinfo is not None
        and authorization.validated_at <= checked_at < authorization.expires_at
        and authorization.expires_at - authorization.validated_at <= timedelta(hours=1)
        and authorization.target_binding_sha256 == target_binding_digest(target)
        and authorization.runtime == runtime
        and (
            (
                authorization.mode == "operator-exception"
                and authorization.scope == "isolated-acceptance"
                and allow_operator_exception
            )
            or (
                authorization.mode == "verified-restore"
                and (authorization.scope != "production" or authorization.off_host_attested)
            )
        )
    )
