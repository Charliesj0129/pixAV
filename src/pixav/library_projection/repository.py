"""PostgreSQL facts LibraryProjection reads and writes.

Overrides and artwork are written only by an operator surface, with a name and
a reason; the reconciler reads them. Publication rows record what the library
actually shows, so the files can always be rebuilt from here.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from pixav.library_projection.metadata import parse_override

ArtworkKind = Literal["poster", "backdrop"]
# One reconciliation pass at a time, whether the tick or an operator started it.
RECONCILE_LOCK = 721034002


class ArtworkRecord(BaseModel):
    model_config = {"frozen": True}

    id: uuid.UUID
    video_id: uuid.UUID
    kind: ArtworkKind
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int = Field(gt=0)
    media_type: Literal["image/jpeg", "image/png", "image/webp"]
    provenance: dict = Field(default_factory=dict)
    created_at: datetime | None = None


class ProjectionSource(BaseModel):
    """A video that may be published: its READY object is on a DURABLE copy."""

    model_config = {"frozen": True}

    video_id: uuid.UUID
    playable_asset_id: uuid.UUID
    title: str
    tags: tuple[str, ...] = ()
    metadata_json: Any = None


class Publication(BaseModel):
    model_config = {"frozen": True}

    video_id: uuid.UUID
    playable_asset_id: uuid.UUID | None = None
    state: Literal["PENDING", "PUBLISHED", "STALE", "FAILED"]
    projection_version: int = 0
    desired_digest: str | None = None
    active_digest: str | None = None
    active_version: str | None = None
    failure_code: str | None = None


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _require(operator: str, reason: str) -> None:
    if not operator.strip() or not reason.strip():
        raise ValueError("an operator and a reason are required")


class LibraryRepository:
    def __init__(self, pool: Any) -> None:
        self.pool = pool

    @asynccontextmanager
    async def exclusive(self) -> AsyncIterator[bool]:
        """Hold the reconciliation lock for a pass; yields False when another pass holds it."""
        async with self.pool.acquire() as conn:
            held = bool(await conn.fetchval("SELECT pg_try_advisory_lock($1)", RECONCILE_LOCK))
            try:
                yield held
            finally:
                if held:
                    await conn.execute("SELECT pg_advisory_unlock($1)", RECONCILE_LOCK)

    async def set_override(
        self, video_id: uuid.UUID, field: str, value: object, *, operator: str, reason: str
    ) -> str | list[str]:
        """Record a person's value for one field; later scrapes cannot touch it."""
        _require(operator, reason)
        stored = parse_override(field, value)
        await self.pool.execute(
            """INSERT INTO video_metadata_overrides(video_id,field,value,operator,reason)
            VALUES($1,$2,$3::jsonb,$4,$5)
            ON CONFLICT (video_id,field) DO UPDATE
               SET value=EXCLUDED.value, operator=EXCLUDED.operator, reason=EXCLUDED.reason, updated_at=now()""",
            video_id,
            field,
            json.dumps(stored),
            operator.strip(),
            reason.strip(),
        )
        return stored

    async def clear_override(self, video_id: uuid.UUID, field: str, *, operator: str, reason: str) -> bool:
        """Return the field to the providers. The caller reports who and why."""
        _require(operator, reason)
        result = await self.pool.execute(
            "DELETE FROM video_metadata_overrides WHERE video_id=$1 AND field=$2", video_id, field
        )
        return str(result).endswith(" 1")

    async def overrides(self, video_id: uuid.UUID) -> dict[str, object]:
        rows = await self.pool.fetch(
            "SELECT field, value FROM video_metadata_overrides WHERE video_id=$1 ORDER BY field", video_id
        )
        return {row["field"]: _json(row["value"]) for row in rows}

    async def record_artwork(
        self,
        video_id: uuid.UUID,
        kind: ArtworkKind,
        *,
        sha256: str,
        size_bytes: int,
        media_type: str,
        provenance: dict,
    ) -> uuid.UUID:
        """Retain one original for a kind, withdrawing the previous one in the same transaction."""
        async with self.pool.acquire() as conn, conn.transaction():
            current = await conn.fetchrow(
                """SELECT id, sha256 FROM video_artwork WHERE video_id=$1 AND kind=$2 AND state='retained'
                FOR UPDATE""",
                video_id,
                kind,
            )
            if current is not None and current["sha256"] == sha256:
                return uuid.UUID(str(current["id"]))
            if current is not None:
                await conn.execute(
                    "UPDATE video_artwork SET state='withdrawn', withdrawn_at=now() WHERE id=$1", current["id"]
                )
            artwork_id = uuid.uuid4()
            await conn.execute(
                """INSERT INTO video_artwork(id,video_id,kind,sha256,size_bytes,media_type,provenance)
                VALUES($1,$2,$3,$4,$5,$6,$7::jsonb)""",
                artwork_id,
                video_id,
                kind,
                sha256,
                size_bytes,
                media_type,
                json.dumps(provenance),
            )
            return artwork_id

    async def retained_artwork(self, video_id: uuid.UUID) -> list[ArtworkRecord]:
        return (await self.artwork_for([video_id])).get(video_id, [])

    async def artwork_for(self, video_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[ArtworkRecord]]:
        rows = await self.pool.fetch(
            """SELECT id, video_id, kind, sha256, size_bytes, media_type, provenance, created_at
            FROM video_artwork WHERE video_id = ANY($1::uuid[]) AND state='retained' ORDER BY video_id, kind""",
            video_ids,
        )
        found: dict[uuid.UUID, list[ArtworkRecord]] = {}
        for row in rows:
            record = ArtworkRecord(**{**dict(row), "provenance": _json(row["provenance"])})
            found.setdefault(record.video_id, []).append(record)
        return found

    async def overrides_for(self, video_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict[str, object]]:
        rows = await self.pool.fetch(
            "SELECT video_id, field, value FROM video_metadata_overrides WHERE video_id = ANY($1::uuid[])",
            video_ids,
        )
        found: dict[uuid.UUID, dict[str, object]] = {}
        for row in rows:
            found.setdefault(row["video_id"], {})[row["field"]] = _json(row["value"])
        return found

    async def eligible_sources(self) -> list[ProjectionSource]:
        """Every video whose READY object sits on a DURABLE copy (BDD-085, 086)."""
        rows = await self.pool.fetch(
            """SELECT v.id AS video_id, p.id AS playable_asset_id, v.title, v.tags, v.metadata_json
            FROM playable_assets p
            JOIN remote_assets r ON r.id = p.remote_asset_id AND r.state = 'DURABLE'
            JOIN videos v ON v.id = p.video_id
            WHERE p.state = 'READY'
            ORDER BY v.id"""
        )
        return [
            ProjectionSource(**{**dict(row), "tags": tuple(row["tags"] or ()), "metadata_json": row["metadata_json"]})
            for row in rows
        ]

    async def publications(self) -> dict[uuid.UUID, Publication]:
        rows = await self.pool.fetch(
            """SELECT video_id, playable_asset_id, state, projection_version, desired_digest, active_digest,
            active_version, failure_code FROM library_publications"""
        )
        return {row["video_id"]: Publication(**dict(row)) for row in rows}

    async def begin_attempt(self, video_id: uuid.UUID, *, desired_digest: str, layout_version: str) -> None:
        """Record what is about to be written; an existing active projection becomes STALE."""
        await self.pool.execute(
            """INSERT INTO library_publications(video_id,state,layout_version,desired_digest,attempted_at)
            VALUES($1,'PENDING',$2,$3,now())
            ON CONFLICT (video_id) DO UPDATE SET
                desired_digest=EXCLUDED.desired_digest, layout_version=EXCLUDED.layout_version,
                attempted_at=now(), updated_at=now(), failure_code=NULL,
                state=CASE WHEN library_publications.active_digest IS NULL THEN 'PENDING' ELSE 'STALE' END""",
            video_id,
            layout_version,
            desired_digest,
        )

    async def mark_published(
        self,
        video_id: uuid.UUID,
        *,
        playable_asset_id: uuid.UUID,
        digest: str,
        version: str,
        projection_version: int,
        effective_metadata: dict,
    ) -> bool:
        """Commit the active projection, unless its object stopped being playable meanwhile."""
        result = await self.pool.execute(
            """UPDATE library_publications SET state='PUBLISHED', playable_asset_id=$2, desired_digest=$3,
                active_digest=$3, active_version=$4, projection_version=$5, effective_metadata=$6::jsonb,
                published_at=now(), withdrawn_at=NULL, withdrawn_reason=NULL, failure_code=NULL, updated_at=now()
            WHERE video_id=$1 AND EXISTS(
                SELECT FROM playable_assets p JOIN remote_assets r ON r.id=p.remote_asset_id
                WHERE p.id=$2 AND p.video_id=$1 AND p.state='READY' AND r.state='DURABLE')""",
            video_id,
            playable_asset_id,
            digest,
            version,
            projection_version,
            json.dumps(effective_metadata),
        )
        return str(result) == "UPDATE 1"

    async def record_failure(self, video_id: uuid.UUID, code: str, *, layout_version: str) -> None:
        """The last attempt failed; whatever was active stays active."""
        await self.pool.execute(
            """INSERT INTO library_publications(video_id,state,layout_version,failure_code,attempted_at)
            VALUES($1,'FAILED',$2,$3,now())
            ON CONFLICT (video_id) DO UPDATE SET state='FAILED', failure_code=EXCLUDED.failure_code,
                attempted_at=now(), updated_at=now()""",
            video_id,
            layout_version,
            code,
        )

    async def mark_withdrawn(self, video_id: uuid.UUID, reason: str) -> None:
        """Nothing is active any more. Remote assets are not touched (BDD-108)."""
        await self.pool.execute(
            """UPDATE library_publications SET state='PENDING', playable_asset_id=NULL, active_digest=NULL,
                active_version=NULL, withdrawn_at=now(), withdrawn_reason=$2, failure_code=NULL, updated_at=now()
            WHERE video_id=$1""",
            video_id,
            reason,
        )

    async def publication_counts(self) -> dict[str, int]:
        rows = await self.pool.fetch("SELECT state, count(*) AS n FROM library_publications GROUP BY state")
        return {row["state"]: int(row["n"]) for row in rows}
