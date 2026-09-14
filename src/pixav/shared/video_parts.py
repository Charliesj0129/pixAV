"""PostgreSQL manifest and per-part effect journal for the isolated film flow."""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from typing import Any

import asyncpg

from pixav.shared.models import VideoPart
from pixav.shared.repository import AccountRepository


def part_from_row(row: Any) -> VideoPart:
    data = dict(row)
    for key in ("media_info", "recovery", "verification"):
        if isinstance(data[key], str):
            data[key] = json.loads(data[key])
    return VideoPart.model_validate(data)


class VideoPartRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def list(self, video_id: uuid.UUID) -> list[VideoPart]:
        rows = await self.pool.fetch("SELECT * FROM video_parts WHERE video_id=$1 ORDER BY part_index", video_id)
        return [part_from_row(row) for row in rows]

    async def recover_manifest(self, video_id: uuid.UUID, info_hash: str) -> tuple[Sequence[VideoPart], dict] | None:
        """Read a committed manifest in one snapshot after a lost run checkpoint."""
        async with self.pool.acquire() as conn, conn.transaction(isolation="repeatable_read", readonly=True):
            parent = await conn.fetchrow("SELECT * FROM videos WHERE id=$1", video_id)
            rows = await conn.fetch("SELECT * FROM video_parts WHERE video_id=$1 ORDER BY part_index", video_id)
        if parent is None:
            raise ValueError("saved candidate video is missing")
        if parent["info_hash"] != info_hash:
            raise ValueError("saved candidate source identity changed")
        if not rows and parent["manifest_version"] is None and parent["expected_part_count"] is None:
            return None
        provenance = parent["source_provenance"]
        if isinstance(provenance, str):
            provenance = json.loads(provenance)
        parts = [part_from_row(row) for row in rows]
        if (
            len(parts) < 2
            or parent["manifest_version"] != 1
            or len(parts) != parent["expected_part_count"]
            or not provenance
            or provenance.get("local_merge") != "PASS"
            or provenance.get("discovery", {}).get("info_hash") != info_hash
            or not provenance.get("reference")
            or any(p.part_index != i or p.manifest_version != 1 for i, p in enumerate(parts))
            or parts[0].start_seconds != 0
            or any(abs(a.end_seconds - b.start_seconds) > 0.001 for a, b in zip(parts, parts[1:], strict=False))
            or abs(parts[-1].end_seconds - provenance["reference"]["duration"]) > 0.001
        ):
            raise ValueError("committed manifest incomplete or conflicting; preserve evidence")
        return parts, provenance

    async def install(  # noqa: C901 - atomic immutable manifest checks
        self, video_id: uuid.UUID, parts: Sequence[VideoPart], provenance: dict
    ) -> None:
        """Commit the complete immutable manifest, or reject a differing resume."""
        if len(parts) < 2 or [p.part_index for p in parts] != list(range(len(parts))):
            raise ValueError("live manifest requires at least two contiguous parts")
        if any(p.video_id != video_id or p.manifest_version != 1 for p in parts):
            raise ValueError("manifest identity mismatch")
        if any(p.end_seconds <= p.start_seconds for p in parts):
            raise ValueError("invalid part interval")
        if parts[0].start_seconds != 0 or any(
            abs(a.end_seconds - b.start_seconds) > 0.001 for a, b in zip(parts, parts[1:], strict=False)
        ):
            raise ValueError("manifest timeline is not contiguous")
        async with self.pool.acquire() as conn, conn.transaction():
            parent = await conn.fetchrow("SELECT * FROM videos WHERE id=$1 FOR UPDATE", video_id)
            if parent is None:
                raise ValueError("parent video missing")
            existing = await conn.fetch("SELECT * FROM video_parts WHERE video_id=$1 ORDER BY part_index", video_id)
            if existing:
                saved_provenance = parent["source_provenance"]
                if isinstance(saved_provenance, str):
                    saved_provenance = json.loads(saved_provenance)
                if (
                    parent["manifest_version"] != 1
                    or parent["expected_part_count"] != len(parts)
                    or saved_provenance != provenance
                ):
                    raise ValueError("immutable manifest provenance changed")
                keys = (
                    "video_id",
                    "part_index",
                    "manifest_version",
                    "sha256",
                    "size_bytes",
                    "filename",
                    "start_seconds",
                    "end_seconds",
                    "media_info",
                )
                if len(existing) != len(parts) or any(
                    any(getattr(part_from_row(row), k) != getattr(part, k) for k in keys)
                    for row, part in zip(existing, parts, strict=True)
                ):
                    raise ValueError("immutable manifest changed")
                return
            if parent["manifest_version"] is not None or parent["expected_part_count"] is not None:
                raise ValueError("committed manifest rows missing; preserve evidence")
            for part in parts:
                await conn.execute(
                    """INSERT INTO video_parts(video_id,part_index,manifest_version,start_seconds,end_seconds,
                         size_bytes,sha256,filename,media_info) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb)""",
                    video_id,
                    part.part_index,
                    part.manifest_version,
                    part.start_seconds,
                    part.end_seconds,
                    part.size_bytes,
                    part.sha256,
                    part.filename,
                    json.dumps(part.media_info),
                )
            await conn.execute(
                """UPDATE videos SET manifest_version=1,expected_part_count=$2,source_provenance=$3::jsonb,
                   playback_manifest_version=NULL,local_path=NULL,share_url=NULL,updated_at=now() WHERE id=$1""",
                video_id,
                len(parts),
                json.dumps(provenance),
            )

    async def journal(
        self, part: VideoPart, state: str, recovery: dict, *, account_id: uuid.UUID | None = None
    ) -> None:
        """Persist intent before an external effect; retain unknown guest identity."""
        tag = await self.pool.execute(
            """UPDATE video_parts SET state=$3,recovery=$4::jsonb,account_id=COALESCE($5,account_id),updated_at=now()
               WHERE video_id=$1 AND part_index=$2 AND usage_counted_at IS NULL""",
            part.video_id,
            part.part_index,
            state,
            json.dumps(recovery),
            account_id,
        )
        if tag != "UPDATE 1":
            raise ValueError("cannot modify a confirmed part")

    async def confirm_backup(self, part: VideoPart, share_url: str, evidence: dict) -> None:
        """Backup fact and quota debit commit together, exactly once."""
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT * FROM video_parts WHERE video_id=$1 AND part_index=$2 FOR UPDATE",
                part.video_id,
                part.part_index,
            )
            if row is None or row["account_id"] is None:
                raise ValueError("part has no reserved account")
            if row["usage_counted_at"] is not None:
                if row["share_url"] != share_url:
                    raise ValueError("confirmed share location changed")
                return
            if not share_url.startswith(("https://photos.app.goo.gl/", "https://photos.google.com/")):
                raise ValueError("invalid Photos share location")
            if not evidence.get("backed_up") or not evidence.get("original_quality"):
                raise ValueError("backup evidence missing")
            await AccountRepository(conn).apply_upload_usage(row["account_id"], row["size_bytes"])  # type: ignore[arg-type]
            await conn.execute(
                """UPDATE video_parts SET share_url=$3,state='backed_up',uploaded_at=now(),usage_counted_at=now(),
                   verification=$4::jsonb,retry_not_before=NULL,updated_at=now() WHERE video_id=$1 AND part_index=$2""",
                part.video_id,
                part.part_index,
                share_url,
                json.dumps(evidence),
            )

    async def confirm_original(self, part: VideoPart, evidence: dict) -> None:
        if evidence.get("sha256") != part.sha256 or evidence.get("size") != part.size_bytes:
            raise ValueError("cloud original identity mismatch")
        if evidence.get("method") != "photos-original-browser":
            raise ValueError("independent cloud provenance required")
        tag = await self.pool.execute(
            """UPDATE video_parts SET state='verified',verification=verification || $3::jsonb,updated_at=now()
               WHERE video_id=$1 AND part_index=$2 AND usage_counted_at IS NOT NULL AND share_url IS NOT NULL""",
            part.video_id,
            part.part_index,
            json.dumps({"original": evidence}),
        )
        if tag != "UPDATE 1":
            raise ValueError("backup must be confirmed before original")

    async def publish(self, video_id: uuid.UUID, path: str, evidence: dict) -> None:
        """Publish only a complete verified cloud manifest; never the first URL."""
        async with self.pool.acquire() as conn, conn.transaction():
            parent = await conn.fetchrow("SELECT * FROM videos WHERE id=$1 FOR UPDATE", video_id)
            rows = await conn.fetch("SELECT * FROM video_parts WHERE video_id=$1 ORDER BY part_index", video_id)
            if (
                parent is None
                or len(rows) < 2
                or len(rows) != parent["expected_part_count"]
                or any(
                    r["state"] != "verified"
                    or r["part_index"] != i
                    or r["manifest_version"] != parent["manifest_version"]
                    for i, r in enumerate(rows)
                )
            ):
                raise ValueError("cloud manifest is incomplete")
            if evidence.get("content") != "PASS" or evidence.get("cold_inputs") != "photos-only":
                raise ValueError("merged cloud verification missing")
            await conn.execute(
                """UPDATE videos SET local_path=$2,playback_manifest_version=manifest_version,status='available',
                   metadata_json=COALESCE(metadata_json,'{}'::jsonb) || $3::jsonb,updated_at=now() WHERE id=$1""",
                video_id,
                path,
                json.dumps({"segmented_playback": evidence}),
            )
