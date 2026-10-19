"""Immutable cleanup approval binding to one inode and its current domain facts.

This proves what an approval names. It does not verify a backup/restore receipt,
establish consent, or replace the janitor's live durability and reader gates.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal
from uuid import UUID

import asyncpg
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from pixav.shared.media_file import MediaFile, open_media_directory


class CleanupFileIdentity(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    device: int = Field(ge=0)
    inode: int = Field(gt=0)
    size: int = Field(gt=0)
    mtime_ns: int
    ctime_ns: int
    parent_device: int = Field(ge=0)
    parent_inode: int = Field(gt=0)


class CleanupTargetBinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal["cleanup-target-v1"]
    database_identifier: str = Field(pattern=r"^[0-9]{1,20}$", repr=False)
    database_name: str = Field(min_length=1, max_length=63, repr=False)
    video_id: UUID
    path: str = Field(min_length=1, max_length=4096, repr=False)
    file: CleanupFileIdentity
    artifact_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    remote_asset_id: UUID
    playable_asset_id: UUID
    preparation_version: str = Field(min_length=1, max_length=200)
    playable_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    playable_size: int = Field(gt=0)
    playback_verified_at: AwareDatetime
    publication_version: str = Field(min_length=1, max_length=200)
    publication_digest: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("path")
    @classmethod
    def canonical_path(cls, value: str) -> str:
        if "\0" in value or not Path(value).is_absolute() or os.path.abspath(value) != value:
            raise ValueError("CLEANUP_BINDING_REQUIRES_CANONICAL_PATH")
        return value


async def capture_cleanup_binding(
    connection: asyncpg.Connection,
    video_id: UUID,
    path: Path,
    descriptor: int,
    facts: tuple[str, int],
    *,
    domain_path: str | None = None,
) -> CleanupTargetBinding:
    """Read current domain facts while the caller retains its row and inode locks.

    ``facts`` must be recomputed from this descriptor under the same lock. The
    operator entry point must hold those locks through authorization commit.
    """
    row = await connection.fetchrow(
        """SELECT current_database() AS database_name,
          (SELECT system_identifier::text FROM pg_control_system()) AS database_identifier,
          v.id AS video_id, r.id AS remote_asset_id, p.id AS playable_asset_id,
          p.preparation_version, p.sha256 AS playable_sha256, p.size_bytes AS playable_size,
          p.playback_verified_at, l.active_version AS publication_version,
          l.active_digest AS publication_digest
        FROM videos v JOIN remote_assets r ON r.video_id=v.id AND r.state='DURABLE'
        JOIN playable_assets p ON p.video_id=v.id AND p.remote_asset_id=r.id AND p.state='READY'
        JOIN library_publications l ON l.video_id=v.id AND l.playable_asset_id=p.id AND l.state='PUBLISHED'
        WHERE v.id=$1 AND v.local_path=$2""",
        video_id,
        str(path) if domain_path is None else domain_path,
    )
    if row is None:
        raise ValueError("CLEANUP_BINDING_ASSET_UNAVAILABLE")
    with open_media_directory(path) as directory:
        MediaFile(path, descriptor, directory.descriptor, False).assert_current()
        file, parent = os.fstat(descriptor), os.fstat(directory.descriptor)
        if facts[1] != file.st_size:
            raise ValueError("CLEANUP_BINDING_SIZE_CHANGED")
        return CleanupTargetBinding(
            **dict(row),
            version="cleanup-target-v1",
            path=str(path),
            artifact_sha256=facts[0],
            file=CleanupFileIdentity(
                device=file.st_dev,
                inode=file.st_ino,
                size=file.st_size,
                mtime_ns=file.st_mtime_ns,
                ctime_ns=file.st_ctime_ns,
                parent_device=parent.st_dev,
                parent_inode=parent.st_ino,
            ),
        )
