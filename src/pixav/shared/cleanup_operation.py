"""Private, bounded selections for an operator cleanup and its reconciliation.

The selection identifies existing approvals. It never creates consent or
relaxes the janitor's durability, publication, retention or reader gates.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from pixav.shared.cleanup_recovery import (
    CleanupApprovalError,
    CleanupApprovalRequest,
    CleanupAuthorizationEvidence,
    CleanupRuntimeBinding,
    EvidenceFile,
    _private_json,
    cleanup_approval_digest,
)
from pixav.shared.media_file import open_media_directory, open_media_file


class CleanupOperationUnconfirmedError(CleanupApprovalError):
    """An apply may have a committed prefix or a missing file to reconcile."""


class CleanupDownloaderMount(BaseModel):
    """A mount observed inside the exact downloader before its last stop."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal["cleanup-downloader-mount-v1"]
    runtime: CleanupRuntimeBinding
    container_id: str = Field(pattern=r"^[a-f0-9]{64}$", repr=False)
    image_id: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    compose_project: str = Field(min_length=1, max_length=200, repr=False)
    compose_service: Literal["qbittorrent"]
    created_at: str = Field(min_length=1, max_length=100)
    started_at: str = Field(min_length=1, max_length=100)
    mount_destination: str = Field(min_length=1, max_length=4096, repr=False)
    mount_source: str = Field(min_length=1, max_length=4096, repr=False)
    root_device: int = Field(ge=0, strict=True)
    root_inode: int = Field(gt=0, strict=True)
    observed_at: AwareDatetime

    @field_validator("mount_destination", "mount_source")
    @classmethod
    def canonical(cls, value: str) -> str:
        if "\0" in value or not Path(value).is_absolute() or os.path.abspath(value) != value:
            raise ValueError("CLEANUP_DOWNLOADER_CANONICAL_PATH_REQUIRED")
        return value


class CleanupSelection(BaseModel):
    """One immutable set of independent files and their pre-existing approvals.

    ``runtime`` identifies the current maintenance session. Recovery may bind a
    new daemon/Redis session while preserving the original selection digest and
    historical approvals. Apply still requires every approval's original runtime.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal["cleanup-selection-v1"]
    id: UUID
    expected_count: int = Field(ge=1, le=100, strict=True)
    runtime: CleanupRuntimeBinding
    downloader_mount: EvidenceFile
    requests: tuple[CleanupApprovalRequest, ...] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def exact_set(self) -> CleanupSelection:
        if len(self.requests) != self.expected_count:
            raise ValueError("CLEANUP_SELECTION_COUNT_MISMATCH")
        unique = (
            {item.id for item in self.requests},
            {item.target.video_id for item in self.requests},
            {item.target.path for item in self.requests},
            {(item.target.file.device, item.target.file.inode) for item in self.requests},
        )
        if any(len(values) != self.expected_count for values in unique):
            raise ValueError("CLEANUP_SELECTION_DUPLICATE_TARGET")
        identities = {(item.target.database_identifier, item.target.database_name) for item in self.requests}
        if len(identities) != 1 or len({item.scope for item in self.requests}) != 1:
            raise ValueError("CLEANUP_SELECTION_MIXED_SCOPE_OR_INSTANCE")
        return self

    def request_for(self, video_id: UUID) -> CleanupApprovalRequest:
        for item in self.requests:
            if item.target.video_id == video_id:
                return item
        raise CleanupApprovalError("CLEANUP_TARGET_NOT_SELECTED")


def cleanup_selection_stamp(selection: CleanupSelection) -> dict:
    """Bind the exact set, independently of a later maintenance session."""
    payload = {
        "id": str(selection.id),
        "expected_count": selection.expected_count,
        "approvals": sorted(cleanup_approval_digest(item) for item in selection.requests),
    }
    return {
        "id": str(selection.id),
        "sha256": hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "expected_count": selection.expected_count,
    }


def selection_matches_authorization(selection: CleanupSelection, evidence: CleanupAuthorizationEvidence) -> bool:
    request = selection.request_for(evidence.target.video_id)
    return (
        evidence.authorization_id == request.id
        and evidence.approval_sha256 == cleanup_approval_digest(request)
        and evidence.target == request.target
        and evidence.recovery.receipt_sha256 == request.recovery_receipt.sha256
        and evidence.recovery.scope == request.scope
    )


def read_cleanup_selection(path: Path) -> CleanupSelection:
    try:
        if not path.is_absolute() or os.path.abspath(path) != str(path):
            raise CleanupApprovalError("CLEANUP_PRIVATE_PATH_REQUIRED")
        payload, _ = _private_json(path)
        return CleanupSelection.model_validate(payload)
    except CleanupApprovalError:
        raise
    except (OSError, ValueError, TypeError):
        raise CleanupApprovalError("CLEANUP_SELECTION_INVALID") from None


def read_downloader_mount(reference: EvidenceFile) -> CleanupDownloaderMount:
    try:
        payload, _ = _private_json(Path(reference.path), expected=reference)
        return CleanupDownloaderMount.model_validate(payload)
    except CleanupApprovalError:
        raise
    except (OSError, ValueError, TypeError):
        raise CleanupApprovalError("CLEANUP_DOWNLOADER_RECEIPT_INVALID") from None


def read_cleanup_runtime(path: Path) -> CleanupRuntimeBinding:
    try:
        if not path.is_absolute() or os.path.abspath(path) != str(path):
            raise CleanupApprovalError("CLEANUP_PRIVATE_PATH_REQUIRED")
        payload, _ = _private_json(path)
        return CleanupRuntimeBinding.model_validate(payload)
    except CleanupApprovalError:
        raise
    except (OSError, ValueError, TypeError):
        raise CleanupApprovalError("CLEANUP_RUNTIME_FILE_INVALID") from None


def write_downloader_mount(path: Path, proof: CleanupDownloaderMount) -> EvidenceFile:
    """Create new private observation evidence; never replace an existing file."""
    try:
        if not path.is_absolute() or os.path.abspath(path) != str(path):
            raise CleanupApprovalError("CLEANUP_PRIVATE_PATH_REQUIRED")
        raw = proof.model_dump_json().encode()
        with open_media_directory(path, exclusive=True) as directory:
            parent = os.fstat(directory.descriptor)
            if stat.S_IMODE(parent.st_mode) != 0o700 or parent.st_uid != os.geteuid():
                raise CleanupApprovalError("CLEANUP_PRIVATE_FILE_REQUIRED")
            with open_media_file(path, exclusive=True, writable=True, create=True, directory=directory) as output:
                with os.fdopen(os.dup(output.descriptor), "wb") as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                output.assert_current()
                os.fsync(directory.descriptor)
        return EvidenceFile(path=str(path), sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw))
    except CleanupApprovalError:
        raise
    except (OSError, ValueError, TypeError):
        raise CleanupApprovalError("CLEANUP_DOWNLOADER_OUTPUT_EXISTS_OR_UNSAFE") from None
