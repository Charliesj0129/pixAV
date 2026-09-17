"""RemoteAsset persistence: intent before effect, quota charged exactly once.

Every method here is written so that a crash between any two statements leaves
a state the next execution can reconcile. A remote side effect is journalled
before it is attempted, and the fact that it succeeded is committed in the same
transaction as the quota it consumed.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from typing import Any

import asyncpg

from pixav.shared.repository import AccountRepository
from pixav.shared.storage_models import (
    IntegrityError,
    RemoteAsset,
    RemoteAssetSegment,
    current_policy,
    policy_for,
)


def asset_from_row(row: Any) -> RemoteAsset:
    data = dict(row)
    for key in ("expected", "evidence"):
        if isinstance(data.get(key), str):
            data[key] = json.loads(data[key])
    return RemoteAsset.model_validate(data)


def expectation(asset: Any) -> dict:
    """The media facts the asset was created to stand for."""
    expected = asset["expected"]
    return json.loads(expected) if isinstance(expected, str) else dict(expected or {})


def segment_from_row(row: Any) -> RemoteAssetSegment:
    data = dict(row)
    for key in ("media_info", "recovery", "verification"):
        if isinstance(data.get(key), str):
            data[key] = json.loads(data[key])
    return RemoteAssetSegment.model_validate(data)


class RemoteAssetRepository:
    """Write side of remote storage facts. The execution authority owns it."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool
        # New assets are bound to the current version; existing ones keep theirs.
        self.policy = current_policy()

    async def create(
        self,
        conn: asyncpg.Connection,
        *,
        video_id: uuid.UUID,
        artifact_id: uuid.UUID,
        expected: dict,
        segments: Sequence[Any],
    ) -> uuid.UUID:
        """Record the intent to place one prepared artifact with the provider."""
        if not segments:
            raise ValueError("a remote asset requires at least one segment")
        existing = await conn.fetchval("SELECT id FROM remote_assets WHERE artifact_id=$1", artifact_id)
        if existing is not None:
            return existing
        asset_id = uuid.uuid4()
        await conn.execute(
            """INSERT INTO remote_assets(id,video_id,artifact_id,policy_version,expected,segment_count)
            VALUES($1,$2,$3,$4,$5::jsonb,$6)""",
            asset_id,
            video_id,
            artifact_id,
            self.policy.version,
            json.dumps(expected),
            len(segments),
        )
        for segment in segments:
            await conn.execute(
                """INSERT INTO remote_asset_segments(asset_id,segment_index,start_seconds,end_seconds,
                size_bytes,sha256,local_path,media_info) VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb)""",
                asset_id,
                segment.index,
                segment.start_seconds,
                segment.end_seconds,
                segment.size_bytes,
                segment.sha256,
                segment.path,
                json.dumps(segment.media_info),
            )
        return asset_id

    async def get(self, asset_id: uuid.UUID) -> RemoteAsset | None:
        row = await self.pool.fetchrow("SELECT * FROM remote_assets WHERE id=$1", asset_id)
        return asset_from_row(row) if row else None

    async def segments(self, asset_id: uuid.UUID) -> list[RemoteAssetSegment]:
        rows = await self.pool.fetch(
            "SELECT * FROM remote_asset_segments WHERE asset_id=$1 ORDER BY segment_index", asset_id
        )
        return [segment_from_row(row) for row in rows]

    async def journal(
        self,
        segment: RemoteAssetSegment,
        state: str,
        recovery: dict,
        *,
        account_id: uuid.UUID | None = None,
    ) -> None:
        """Persist intent before an external effect; keep unknown guest identity."""
        tag = await self.pool.execute(
            """UPDATE remote_asset_segments SET state=$3,recovery=$4::jsonb,
            account_id=COALESCE($5,account_id),updated_at=now()
            WHERE asset_id=$1 AND segment_index=$2 AND usage_counted_at IS NULL""",
            segment.asset_id,
            segment.segment_index,
            state,
            json.dumps(recovery),
            account_id,
        )
        if tag != "UPDATE 1":
            raise ValueError("cannot modify a confirmed segment")

    async def confirm_backup(
        self,
        conn: asyncpg.Connection,
        asset_id: uuid.UUID,
        segment_index: int,
        share_url: str,
        evidence: dict,
    ) -> None:
        """Remote creation and its quota debit commit together, exactly once.

        The caller supplies the transaction so that the execution transition
        recording this success cannot commit without the debit, or vice versa.
        """
        row = await conn.fetchrow(
            "SELECT * FROM remote_asset_segments WHERE asset_id=$1 AND segment_index=$2 FOR UPDATE",
            asset_id,
            segment_index,
        )
        if row is None or row["account_id"] is None:
            raise ValueError("segment has no reserved account")
        if row["usage_counted_at"] is not None:
            # A retried commit re-observes its own committed effect. Charging
            # again here is how a crash turns into a double debit.
            if row["share_url"] != share_url:
                raise ValueError("confirmed share location changed")
            return
        self.policy.validate_share_location(share_url)
        self.policy.validate_backup_evidence(evidence)
        await AccountRepository(conn).apply_upload_usage(row["account_id"], row["size_bytes"])
        await conn.execute(
            """UPDATE remote_asset_segments SET share_url=$3,state='backed_up',uploaded_at=now(),
            usage_counted_at=now(),verification=$4::jsonb,retry_not_before=NULL,updated_at=now()
            WHERE asset_id=$1 AND segment_index=$2""",
            asset_id,
            segment_index,
            share_url,
            json.dumps(evidence),
        )
        await conn.execute(
            "UPDATE remote_assets SET state='CREATED',updated_at=now() WHERE id=$1 AND state='REQUESTED'",
            asset_id,
        )

    async def confirm_readback(
        self, conn: asyncpg.Connection, asset_id: uuid.UUID, segment_index: int, evidence: dict
    ) -> None:
        """A cold read-back that satisfied the policy. Creation must precede it."""
        asset = await conn.fetchrow("SELECT * FROM remote_assets WHERE id=$1 FOR UPDATE", asset_id)
        if asset is None:
            raise ValueError("unknown asset")
        policy = policy_for(asset["policy_version"])
        row = await conn.fetchrow(
            "SELECT * FROM remote_asset_segments WHERE asset_id=$1 AND segment_index=$2 FOR UPDATE",
            asset_id,
            segment_index,
        )
        if row is None:
            raise ValueError("unknown segment")
        segment = segment_from_row(row)
        policy.validate_segment_readback(segment, evidence)
        policy.validate_segment_media(segment, expectation(asset), evidence)
        tag = await conn.execute(
            """UPDATE remote_asset_segments SET state='verified',
            verification=verification || $3::jsonb,updated_at=now()
            WHERE asset_id=$1 AND segment_index=$2 AND usage_counted_at IS NOT NULL AND share_url IS NOT NULL""",
            asset_id,
            segment_index,
            json.dumps({"readback": evidence}),
        )
        if tag != "UPDATE 1":
            raise ValueError("remote creation must be confirmed before read-back")
        # The third step of the declared state machine: every segment has now
        # come back cold and whole. Nothing is durable yet -- the manifest as a
        # whole still has to answer for itself in promote().
        await conn.execute(
            """UPDATE remote_assets SET state='VERIFIED',updated_at=now()
            WHERE id=$1 AND state='CREATED'
            AND NOT EXISTS (SELECT FROM remote_asset_segments WHERE asset_id=$1 AND state <> 'verified')""",
            asset_id,
        )

    async def promote(self, conn: asyncpg.Connection, asset_id: uuid.UUID, evidence: dict) -> bool:
        """Commit DURABLE only for a complete, fully verified manifest.

        Returns True when this call made the asset durable, and False when it
        re-checked one that already was -- a re-verification refreshes the
        evidence, it does not repeat the transition.
        """
        asset = await conn.fetchrow("SELECT * FROM remote_assets WHERE id=$1 FOR UPDATE", asset_id)
        rows = await conn.fetch(
            "SELECT * FROM remote_asset_segments WHERE asset_id=$1 ORDER BY segment_index", asset_id
        )
        if asset is None or len(rows) != asset["segment_count"]:
            raise IntegrityError("remote manifest is incomplete")
        if any(row["state"] != "verified" or row["segment_index"] != index for index, row in enumerate(rows)):
            raise IntegrityError("every segment must be read back before durability")
        # The asset's own recorded version, never whichever one is current: an
        # asset is promoted under the rules its evidence was collected under.
        policy = policy_for(asset["policy_version"])
        if evidence.get("cold_inputs") != "provider-only":
            raise IntegrityError("durability requires evidence that no local input was used")
        policy.validate_manifest_timeline(expectation(asset), [segment_from_row(row) for row in rows])
        record = json.dumps({**evidence, "policy_version": policy.version})
        if asset["state"] == "DURABLE":
            await conn.execute(
                """UPDATE remote_assets SET reverified_at=now(),
                evidence=evidence || jsonb_build_object('reverification',$2::jsonb),updated_at=now()
                WHERE id=$1""",
                asset_id,
                record,
            )
            return False
        if asset["state"] != "VERIFIED":
            raise IntegrityError("durability requires a committed verification of every segment")
        await conn.execute(
            """UPDATE remote_assets SET state='DURABLE',durable_at=now(),reverified_at=now(),
            evidence=evidence || jsonb_build_object('durability',$2::jsonb),updated_at=now() WHERE id=$1""",
            asset_id,
            record,
        )
        return True

    async def invalidate(self, asset_id: uuid.UUID, reason: str, *, conn: asyncpg.Connection | None = None) -> None:
        """Confirmed remote loss or corruption. Expiry of a URL is not this.

        The caller may supply the transaction that decided it, so the decision
        and the state it is based on commit together or not at all.
        """
        await (conn or self.pool).execute(
            """UPDATE remote_assets SET state='INVALID',durable_at=NULL,
            invalidated_reason=$2,updated_at=now() WHERE id=$1""",
            asset_id,
            reason,
        )
