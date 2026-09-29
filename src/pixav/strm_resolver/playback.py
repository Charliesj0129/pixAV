"""Opt-in original-file playback with PostgreSQL gates and isolated cold retrieval.

Starlette owns HTTP file transport. This module owns only Photos-specific
eligibility and cache reconstruction. Multi-segment promotion stays closed
until a merged-object/junction verification contract is available.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import HTTPException

from pixav.media_loader.preparation import MediaFacts, PreparationPolicy, inspect_media
from pixav.pixel_injector.photos_storage import PhotosColdReadback
from pixav.shared.remote_assets import asset_from_row, segment_from_row
from pixav.shared.storage_models import IntegrityError, policy_for


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_file(root: Path, path: Path) -> bool:
    return (
        path.is_file()
        and path.resolve().is_relative_to(root.resolve())
        and not any(item.is_symlink() for item in (path, *path.parents))
    )


def manifest_digest(asset, segments) -> str:
    payload = {
        "asset": str(asset.id),
        "policy": asset.policy_version,
        "expected": asset.expected,
        "segments": [
            [s.segment_index, s.sha256, s.size_bytes, s.start_seconds, s.end_seconds, s.media_info] for s in segments
        ],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class PlaybackService:
    def __init__(self, pool, *, cache_root: Path, readback_factory: Callable[[Path], Any]) -> None:
        self.pool = pool
        self.root = cache_root.absolute()
        self.readback_factory = readback_factory
        self._verified: dict[str, tuple] = {}

    @classmethod
    def configured(cls, pool, settings):
        def readback(root):
            return PhotosColdReadback(
                root,
                image=settings.storage_readback_image,
                source_root=Path("src").absolute(),
                host_project_root=settings.host_project_root,
                project_root=Path.cwd(),
            )

        return cls(pool, cache_root=Path(settings.playback_cache_dir), readback_factory=readback)

    async def _manifest(self, conn, video_id):
        row = await conn.fetchrow(
            "SELECT * FROM remote_assets WHERE video_id=$1 AND state='DURABLE' ORDER BY durable_at DESC,id LIMIT 1 FOR SHARE",
            video_id,
        )
        if row is None:
            raise HTTPException(409, "durable remote asset required")
        asset = asset_from_row(row)
        rows = await conn.fetch(
            "SELECT * FROM remote_asset_segments WHERE asset_id=$1 ORDER BY segment_index", asset.id
        )
        segments = [segment_from_row(item) for item in rows]
        if len(segments) != asset.segment_count or any(
            part.segment_index != index or part.state != "verified" for index, part in enumerate(segments)
        ):
            raise HTTPException(409, "verified complete manifest required")
        policy_for(asset.policy_version).validate_manifest_timeline(asset.expected, segments)
        if len(segments) != 1:
            raise HTTPException(409, "segmented playback requires verified merged-object and junction evidence")
        return asset, segments, manifest_digest(asset, segments)

    async def _cached(self, path: Path, segment) -> bool:
        if not safe_file(self.root, path) or path.stat().st_size != segment.size_bytes:
            return False
        stat = path.stat()
        stamp = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, segment.sha256)
        if self._verified.get(str(path)) == stamp:
            return True
        if await asyncio.to_thread(file_hash, path) != segment.sha256:
            return False
        self._verified[str(path)] = stamp
        return True

    async def _retrieve(self, asset, segment, destination: Path) -> None:
        # Every attempt gets a fresh directory: the browser cannot reuse old
        # receipts or mistake a previously cached file for new remote evidence.
        work = Path(tempfile.mkdtemp(prefix="cold-", dir=self.root))
        try:
            adapter = self.readback_factory(work)
            receipt = await adapter.read_back(segment)
            if receipt.get("cold_inputs") != "provider-only":
                raise IntegrityError("independent retrieval required")
            policy = policy_for(asset.policy_version)
            policy.validate_segment_readback(segment, receipt)
            policy.validate_segment_media(segment, asset.expected, receipt)
            source = work / str(asset.id) / str(segment.segment_index) / segment.filename
            if not safe_file(work, source):
                raise IntegrityError("cold retrieval did not produce the expected file")
            facts = await inspect_media(str(source))
            PreparationPolicy().validate_output(MediaFacts.model_validate(asset.expected), facts)
            policy.validate_asset_readback(MediaFacts.model_validate(asset.expected), facts)
            if facts.sha256 != segment.sha256 or facts.size_bytes != segment.size_bytes:
                raise IntegrityError("cold retrieval bytes do not match the manifest")
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.replace(source, destination)
        finally:
            # Only this attempt's disposable cache is removed; never staging.
            shutil.rmtree(work)

    async def prepare(self, video_id: UUID) -> None:
        if any(item.is_symlink() for item in (self.root, *self.root.parents)):
            raise HTTPException(503, "unsafe playback cache root")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        async with self.pool.acquire() as conn, conn.transaction():
            # Same lock as staging cleanup. It also serializes independent
            # resolver processes preparing the same video after a crash.
            if not await conn.fetchval("SELECT id FROM videos WHERE id=$1 FOR UPDATE", video_id):
                raise HTTPException(404, "video not found")
            asset, segments, version = await self._manifest(conn, video_id)
            segment = segments[0]
            destination = self.root / str(asset.id) / version / "original.mp4"
            if any(item.is_symlink() for item in (destination, *destination.parents)):
                raise HTTPException(503, "unsafe playback cache path")
            if not await self._cached(destination, segment):
                await self._retrieve(asset, segment, destination)
            await conn.execute(
                """INSERT INTO playable_assets(video_id,remote_asset_id,state,manifest_sha256,cache_path,size_bytes,sha256,evidence)
                VALUES($1,$2,'READY',$3,$4,$5,$6,$7::jsonb)
                ON CONFLICT(video_id) DO UPDATE SET remote_asset_id=$2,state='READY',manifest_sha256=$3,
                cache_path=$4,size_bytes=$5,sha256=$6,evidence=$7::jsonb,updated_at=now(),
                playback_verified_at=CASE WHEN playable_assets.manifest_sha256=$3
                    THEN playable_assets.playback_verified_at ELSE NULL END""",
                video_id,
                asset.id,
                version,
                str(destination),
                segment.size_bytes,
                segment.sha256,
                json.dumps(
                    {"policy": asset.policy_version, "cold_inputs": "provider-only", "transport": "starlette-file"}
                ),
            )

    @asynccontextmanager
    async def reader(self, video_id: UUID) -> AsyncIterator[Any]:
        # Hold shared row locks until the response completes or disconnects.
        # Janitor, invalidation, cache eviction and replacement must wait; a
        # long-running client never outlives a timed lease.
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """SELECT p.* FROM videos v JOIN playable_assets p ON p.video_id=v.id
                JOIN remote_assets r ON r.id=p.remote_asset_id AND r.video_id=v.id
                WHERE v.id=$1 AND p.state='READY' AND r.state='DURABLE'
                FOR SHARE OF v,p,r""",
                video_id,
            )
            if row is None:
                raise HTTPException(409, "playback is not ready")
            yield row

    async def evict(self, video_id: UUID) -> bool:
        """Explicit cache eviction, serialized with readers; no staging deletion."""
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT id FROM videos WHERE id=$1 FOR UPDATE", video_id)
            row = await conn.fetchrow("SELECT * FROM playable_assets WHERE video_id=$1 FOR UPDATE", video_id)
            if row is None:
                return False
            path = Path(row["cache_path"] or "")
            if not safe_file(self.root, path):
                return False
            path.unlink()
            self._verified.pop(str(path), None)
            return True
