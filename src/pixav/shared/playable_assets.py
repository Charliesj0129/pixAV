"""PlayableAsset persistence. The execution authority owns every write here.

READY is only ever committed in the same statement that re-checks the remote
asset is still DURABLE, so a withdrawal that races a preparation cannot leave a
READY object standing on a copy that no longer exists.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import asyncpg
from pydantic import ValidationError

from pixav.shared.client_playback_evidence import client_matches_current_manifest, parse_client_playback_evidence
from pixav.shared.playback_models import PREPARATION_VERSION, PlayableAsset


def playable_from_row(row: Any) -> PlayableAsset:
    data = dict(row)
    for key in ("evidence", "playback_evidence"):
        if isinstance(data.get(key), str):
            data[key] = json.loads(data[key])
    return PlayableAsset.model_validate(data)


class PlayableAssetRepository:
    """Write side of playback readiness facts."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def create_preparing(
        self,
        conn: asyncpg.Connection,
        *,
        video_id: uuid.UUID,
        remote_asset_id: uuid.UUID,
        preparation_version: str = PREPARATION_VERSION,
    ) -> uuid.UUID:
        """Record the intent to prepare a remote asset for playback, once."""
        created = await conn.fetchval(
            """INSERT INTO playable_assets(id,video_id,remote_asset_id,preparation_version)
            VALUES($1,$2,$3,$4)
            ON CONFLICT (remote_asset_id, preparation_version) WHERE state IN ('PREPARING','READY')
            DO NOTHING RETURNING id""",
            uuid.uuid4(),
            video_id,
            remote_asset_id,
            preparation_version,
        )
        if created is not None:
            return created
        return await conn.fetchval(
            """SELECT id FROM playable_assets WHERE remote_asset_id=$1 AND preparation_version=$2
            AND state IN ('PREPARING','READY')""",
            remote_asset_id,
            preparation_version,
        )

    async def get(
        self, playable_asset_id: uuid.UUID, *, conn: asyncpg.Connection | None = None
    ) -> PlayableAsset | None:
        row = await (conn or self.pool).fetchrow("SELECT * FROM playable_assets WHERE id=$1", playable_asset_id)
        return playable_from_row(row) if row else None

    async def mark_ready(
        self,
        conn: asyncpg.Connection,
        playable_asset_id: uuid.UUID,
        *,
        size_bytes: int,
        sha256: str,
        evidence: dict,
    ) -> bool:
        """Commit READY for a verified materialization of a still-durable asset.

        A newer READY object for the same video supersedes the older one, which
        becomes STALE in the same transaction: one stable URL, one object.
        """
        row = await conn.fetchrow(
            """SELECT p.video_id FROM playable_assets p JOIN remote_assets r ON r.id=p.remote_asset_id
            WHERE p.id=$1 AND p.state='PREPARING' AND r.state='DURABLE' FOR UPDATE OF p, r""",
            playable_asset_id,
        )
        if row is None:
            return False
        await conn.execute(
            """UPDATE playable_assets SET state='STALE',updated_at=now()
            WHERE video_id=$1 AND state='READY' AND id<>$2""",
            row["video_id"],
            playable_asset_id,
        )
        tag = await conn.execute(
            """UPDATE playable_assets SET state='READY',size_bytes=$2,sha256=$3,ready_at=now(),
            materialized_at=now(),evidence=evidence || jsonb_build_object('preparation',$4::jsonb),updated_at=now()
            WHERE id=$1 AND state='PREPARING'""",
            playable_asset_id,
            size_bytes,
            sha256,
            json.dumps(evidence),
        )
        return tag == "UPDATE 1"

    async def record_materialization(
        self, conn: asyncpg.Connection, playable_asset_id: uuid.UUID, evidence: dict
    ) -> bool:
        """A rebuild reproduced exactly the READY bytes and republished them."""
        tag = await conn.execute(
            """UPDATE playable_assets SET materialized_at=now(),
            evidence=evidence || jsonb_build_object('rebuild',$2::jsonb),updated_at=now()
            WHERE id=$1 AND state='READY'""",
            playable_asset_id,
            json.dumps(evidence),
        )
        return tag == "UPDATE 1"

    async def mark_stale(self, conn: asyncpg.Connection, playable_asset_id: uuid.UUID, reason: str) -> bool:
        """The recorded object can no longer be reproduced; a new version is due."""
        tag = await conn.execute(
            """UPDATE playable_assets SET state='STALE',
            evidence=evidence || jsonb_build_object('stale',$2::text),updated_at=now()
            WHERE id=$1 AND state IN ('PREPARING','READY')""",
            playable_asset_id,
            reason,
        )
        return tag == "UPDATE 1"

    async def invalidate(self, conn: asyncpg.Connection, playable_asset_id: uuid.UUID, reason: str) -> bool:
        tag = await conn.execute(
            """UPDATE playable_assets SET state='INVALID',invalidated_reason=$2,updated_at=now()
            WHERE id=$1 AND state<>'INVALID'""",
            playable_asset_id,
            reason,
        )
        return tag == "UPDATE 1"

    async def eligible_for_video(self, video_id: uuid.UUID) -> PlayableAsset | None:
        """The one object a stable URL may serve: READY on a DURABLE remote."""
        row = await self.pool.fetchrow(
            """SELECT p.* FROM playable_assets p JOIN remote_assets r ON r.id=p.remote_asset_id
            WHERE p.video_id=$1 AND p.state='READY' AND r.state='DURABLE'""",
            video_id,
        )
        return playable_from_row(row) if row else None

    async def record_playback_verification(self, playable_asset_id: uuid.UUID, evidence: dict) -> bool:
        """An end-to-end stream check matched the READY object's bytes exactly."""
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """SELECT p.*, now() AS checked_at FROM playable_assets p
                JOIN remote_assets r ON r.id=p.remote_asset_id
                WHERE p.id=$1 AND p.state='READY' AND r.state='DURABLE' FOR UPDATE OF p""",
                playable_asset_id,
            )
            if row is None or evidence.get("size") != row["size_bytes"] or evidence.get("sha256") != row["sha256"]:
                return False
            if "client_playback" in evidence:
                client = parse_client_playback_evidence(evidence["client_playback"])
                if not await client_matches_current_manifest(client, dict(row), conn):
                    return False
            else:
                previous = row["playback_evidence"]
                if isinstance(previous, str):
                    previous = json.loads(previous)
                try:
                    client = parse_client_playback_evidence((previous or {}).get("client_playback"))
                except ValidationError:
                    pass
                else:
                    if await client_matches_current_manifest(client, dict(row), conn):
                        evidence = {**evidence, "client_playback": previous["client_playback"]}
            await conn.execute(
                """UPDATE playable_assets SET playback_verified_at=now(),playback_evidence=$2::jsonb,updated_at=now()
                WHERE id=$1""",
                playable_asset_id,
                json.dumps(evidence),
            )
            return True


async def invalidate_for_remote(conn: asyncpg.Connection, remote_asset_id: uuid.UUID) -> None:
    """A withdrawn remote copy takes every playable object built on it along."""
    await conn.execute(
        """UPDATE playable_assets SET state='INVALID',invalidated_reason='REMOTE_ASSET_INVALID',updated_at=now()
        WHERE remote_asset_id=$1 AND state<>'INVALID'""",
        remote_asset_id,
    )
