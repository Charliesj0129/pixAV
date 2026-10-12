"""Playback half of the managed execution authority.

A durable remote copy becomes something a player can read only after the
authority has had it brought back and checked. That preparation is an ordinary
execution: opened, leased, retried and fenced by the same single authority that
owns download, preparation and storage. The playback cache it fills is
disposable; the facts it commits are the PlayableAsset rows.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any

import asyncpg

from pixav.shared.metrics import record_playback_materialization
from pixav.shared.playable_assets import PlayableAssetRepository
from pixav.shared.playback_models import PREPARATION_VERSION, validate_materialization
from pixav.shared.remote_assets import expectation, segment_from_row
from pixav.shared.storage_models import IntegrityError
from pixav.shared.workflow import ActivityResult

logger = logging.getLogger(__name__)

# The only outcomes a playback worker reports. Anything else is a malformed
# report, never something to reinterpret.
PLAYBACK_OUTCOMES = ("success", "infrastructure", "verification_failed", "user_action")

# A rebuild produced other bytes than the READY record from correct segments:
# the recipe no longer reproduces the object, so the object is STALE.
REBUILD_MISMATCH = "PLAYBACK_REBUILD_MISMATCH"

# Where the segment bytes may honestly be said to have come from.
PLAYBACK_INPUTS = ("verified-readback", "photos", "mixed")


class PlaybackWorkflow:
    """Opens, resolves and commits playback materializations."""

    def __init__(self, pool: asyncpg.Pool, *, preparation_version: str = PREPARATION_VERSION) -> None:
        self.pool = pool
        self.preparation_version = preparation_version
        self.playable = PlayableAssetRepository(pool)

    async def resume_unclaimed_preparation(
        self,
        execution_id: uuid.UUID,
        *,
        generation: int,
        operator: str,
        reason: str,
        review_sha256: str,
    ) -> bool:
        """Resume a reviewed transport failure without replaying storage.

        Call only while holding the sole execution authority. The review binds
        the deployment, repaired transport and absence of a published cache
        effect. Claimed unfinished attempts and pending reports need separate
        reconciliation; this entry cannot release them or reset retry counts.
        """
        if not operator.strip() or not reason.strip() or not re.fullmatch(r"[a-f0-9]{64}", review_sha256):
            raise ValueError("resume requires operator, reason and review digest")
        async with self.pool.acquire() as conn, conn.transaction():
            execution = await conn.fetchrow("SELECT * FROM executions WHERE id=$1 FOR UPDATE", execution_id)
            if (
                execution is None
                or execution["state"] != "USER_ACTION_REQUIRED"
                or execution["stage"] != "playback"
                or execution["error_code"] != "ACTIVITY_LEASE_EXPIRED"
                or execution["generation"] != generation
            ):
                raise ValueError("only the reviewed held playback generation may be resumed")
            checkpoint = json.loads(execution["checkpoint"])
            if checkpoint.get("purpose") != "prepare":
                raise ValueError("cache rebuild needs effect reconciliation")
            valid = await conn.fetchval(
                """SELECT EXISTS(SELECT FROM playable_assets p JOIN remote_assets a ON a.id=p.remote_asset_id
                WHERE p.id=$1 AND p.state='PREPARING' AND p.ready_at IS NULL AND p.materialized_at IS NULL
                  AND a.id=$2 AND a.state='DURABLE'
                  AND (SELECT count(*) FROM remote_asset_segments s WHERE s.asset_id=a.id)=a.segment_count
                  AND NOT EXISTS(SELECT FROM remote_asset_segments s WHERE s.asset_id=a.id AND s.state<>'verified'))""",
                uuid.UUID(checkpoint["playable_asset_id"]),
                uuid.UUID(checkpoint["remote_asset_id"]),
            )
            unresolved = await conn.fetchval(
                """SELECT EXISTS(SELECT FROM activity_attempts a WHERE a.execution_id=$1 AND
                  ((a.worker_token IS NOT NULL AND a.completed_at IS NULL)
                   OR EXISTS(SELECT FROM activity_results r WHERE r.attempt_id=a.id AND r.consumed_at IS NULL)))
                   OR EXISTS(SELECT FROM executions e WHERE e.task_id=$2 AND e.id<>$1
                             AND e.state NOT IN ('SUCCEEDED','FAILED','CANCELLED'))""",
                execution_id,
                execution["task_id"],
            )
            if not valid or unresolved:
                raise ValueError("preparation has unresolved effects or is no longer eligible")
            history = checkpoint.setdefault("transport_resumes", [])
            clock = await conn.fetchval("SELECT now()")
            history.append(
                {
                    "operator": operator,
                    "reason": reason,
                    "review_sha256": review_sha256,
                    "generation": generation,
                    "observed_at": clock.isoformat(),
                }
            )
            await conn.execute(
                """UPDATE executions SET state='READY',blocked_reason=NULL,error_code=NULL,failure_class=NULL,
                owner=NULL,lease_until=NULL,due_at=now(),generation=generation+1,checkpoint=$2::jsonb,updated_at=now()
                WHERE id=$1""",
                execution_id,
                json.dumps(checkpoint),
            )
            return True

    async def open_due(self, *, limit: int = 5, max_retries: int = 2) -> list[uuid.UUID]:
        """Open executions for durable assets not yet playable and for unmet demand.

        A preparation is opened once per remote asset and recipe; a rebuild only
        when a reader asked for an object after the last one was published, and
        no playback execution has answered that request since. Nothing here
        runs while another execution holds the same task.
        """
        opened: list[uuid.UUID] = []
        async with self.pool.acquire() as conn, conn.transaction():
            prepare = await conn.fetch(
                """SELECT a.id, a.video_id, wt.task_id FROM remote_assets a
                JOIN workflow_tasks wt ON wt.video_id = a.video_id
                WHERE a.state='DURABLE'
                  AND NOT EXISTS (SELECT FROM remote_assets newer WHERE newer.video_id=a.video_id
                                  AND newer.state='DURABLE' AND (newer.durable_at, newer.id) > (a.durable_at, a.id))
                  AND NOT EXISTS (SELECT FROM playable_assets p WHERE p.remote_asset_id=a.id
                                  AND p.preparation_version=$1 AND p.state IN ('PREPARING','READY','INVALID'))
                  AND NOT EXISTS (SELECT FROM executions e WHERE e.task_id = wt.task_id
                                  AND e.state NOT IN ('SUCCEEDED','FAILED','CANCELLED'))
                ORDER BY a.durable_at LIMIT $2 FOR UPDATE OF a SKIP LOCKED""",
                self.preparation_version,
                limit,
            )
            for row in prepare:
                playable_id = await self.playable.create_preparing(
                    conn,
                    video_id=row["video_id"],
                    remote_asset_id=row["id"],
                    preparation_version=self.preparation_version,
                )
                opened.append(await self._open(conn, row["task_id"], playable_id, row["id"], "prepare", max_retries))
            rebuild = await conn.fetch(
                """SELECT p.id, p.remote_asset_id, wt.task_id FROM playable_assets p
                JOIN remote_assets a ON a.id = p.remote_asset_id AND a.state='DURABLE'
                JOIN playback_cache_requests d ON d.playable_asset_id = p.id
                JOIN workflow_tasks wt ON wt.video_id = p.video_id
                WHERE p.state='READY'
                  AND d.requested_at > COALESCE(p.materialized_at, p.ready_at)
                  AND NOT EXISTS (SELECT FROM executions e WHERE e.task_id = wt.task_id
                                  AND (e.state NOT IN ('SUCCEEDED','FAILED','CANCELLED')
                                       OR (e.stage='playback' AND e.created_at >= d.requested_at)))
                ORDER BY d.requested_at LIMIT $1 FOR UPDATE OF p SKIP LOCKED""",
                max(0, limit - len(opened)),
            )
            for row in rebuild:
                opened.append(
                    await self._open(conn, row["task_id"], row["id"], row["remote_asset_id"], "rebuild", max_retries)
                )
        return opened

    @staticmethod
    async def _open(conn, task_id, playable_id, remote_asset_id, purpose: str, max_retries: int) -> uuid.UUID:
        execution_id = uuid.uuid4()
        await conn.execute(
            """INSERT INTO executions(id,task_id,max_retries,stage,state,checkpoint)
            VALUES($1,$2,$3,'playback','READY',$4::jsonb)""",
            execution_id,
            task_id,
            max_retries,
            json.dumps(
                {"playable_asset_id": str(playable_id), "remote_asset_id": str(remote_asset_id), "purpose": purpose}
            ),
        )
        return execution_id

    async def next_activity(
        self, conn: asyncpg.Connection, execution: Any, *, owner: uuid.UUID, generation: int
    ) -> tuple[str, dict] | None:
        """Address the materialization, or close an execution that lost its footing."""
        checkpoint = json.loads(execution["checkpoint"])
        playable_id = uuid.UUID(checkpoint["playable_asset_id"])
        purpose = checkpoint["purpose"]
        wanted = "PREPARING" if purpose == "prepare" else "READY"
        row = await conn.fetchrow(
            """SELECT p.remote_asset_id FROM playable_assets p JOIN remote_assets a ON a.id=p.remote_asset_id
            WHERE p.id=$1 AND p.state=$2 AND a.state='DURABLE'""",
            playable_id,
            wanted,
        )
        if row is None:
            # The remote copy was withdrawn, or the object was superseded, while
            # this execution waited. There is nothing honest left to prepare.
            await self._close(conn, execution["id"], "FAILED", "PLAYBACK_NOT_ELIGIBLE")
            return None
        return (
            f"playback:{playable_id}:{purpose}",
            {
                "stage": "playback",
                "playable_asset_id": playable_id,
                "purpose": purpose,
                "asset_id": row["remote_asset_id"],
            },
        )

    async def apply_result(self, conn: asyncpg.Connection, execution: Any, result: ActivityResult) -> bool:
        """Commit one playback result. Returns False to use the generic retry policy."""
        if result.outcome not in PLAYBACK_OUTCOMES:
            raise ValueError("outcome is not valid for a playback activity")
        checkpoint = json.loads(execution["checkpoint"])
        playable_id = uuid.UUID(checkpoint["playable_asset_id"])
        purpose = checkpoint["purpose"]
        if result.playable_asset_id != playable_id or result.purpose != purpose:
            raise ValueError("playback result addresses a different materialization")
        if result.outcome == "infrastructure":
            record_playback_materialization(purpose, "infrastructure")
            return False
        if result.outcome in {"verification_failed", "user_action"}:
            await self._apply_refusal(conn, execution, playable_id, purpose, result)
            return True
        playable = await conn.fetchrow("SELECT * FROM playable_assets WHERE id=$1 FOR UPDATE", playable_id)
        asset = await conn.fetchrow("SELECT * FROM remote_assets WHERE id=$1", playable["remote_asset_id"])
        segments = [
            segment_from_row(row)
            for row in await conn.fetch(
                "SELECT * FROM remote_asset_segments WHERE asset_id=$1 ORDER BY segment_index", asset["id"]
            )
        ]
        try:
            if result.evidence.get("inputs") not in PLAYBACK_INPUTS:
                raise IntegrityError("materialization does not say where its bytes came from")
            receipt = validate_materialization(expectation(asset), segments, result.evidence)
        except IntegrityError as exc:
            logger.warning("refusing playback materialization for %s: %s", playable_id, exc)
            await self._refuse(conn, execution, playable_id, purpose, "PLAYBACK_INTEGRITY_MISMATCH")
            return True
        if purpose == "rebuild":
            if (receipt.size, receipt.sha256) != (playable["size_bytes"], playable["sha256"]):
                await self._refuse(conn, execution, playable_id, purpose, REBUILD_MISMATCH)
                return True
            committed = await self.playable.record_materialization(conn, playable_id, result.evidence)
        else:
            committed = await self.playable.mark_ready(
                conn, playable_id, size_bytes=receipt.size, sha256=receipt.sha256, evidence=result.evidence
            )
        if not committed:
            await self._close(conn, execution["id"], "FAILED", "PLAYBACK_NOT_ELIGIBLE")
            record_playback_materialization(purpose, "not_eligible")
            return True
        await self._close(conn, execution["id"], "SUCCEEDED", None)
        record_playback_materialization(purpose, "success")
        return True

    async def _apply_refusal(self, conn, execution, playable_id, purpose, result) -> None:
        if result.outcome == "user_action":
            await self._close(conn, execution["id"], "USER_ACTION_REQUIRED", "PHOTOS_ACCOUNT_SESSION_REQUIRED")
        else:
            await self._refuse(
                conn, execution, playable_id, purpose, result.error_code or "PLAYBACK_INTEGRITY_MISMATCH"
            )

    async def _refuse(self, conn, execution, playable_id: uuid.UUID, purpose: str, code: str) -> None:
        """A materialization that is not the film never becomes, or stays, playable.

        A first preparation that fails integrity is INVALID for this recipe. A
        rebuild whose correct segments join into other bytes is STALE, so a new
        version is prepared; a rebuild that could not fetch the segments intact
        leaves the READY record alone -- the cache is simply still missing, and
        what the provider now holds is the storage re-verification's question.
        """
        if purpose == "prepare":
            await self.playable.invalidate(conn, playable_id, code)
        elif code == REBUILD_MISMATCH:
            await self.playable.mark_stale(conn, playable_id, code)
        await self._close(conn, execution["id"], "FAILED", code)
        record_playback_materialization(purpose, "verification_failed")

    @staticmethod
    async def _close(conn, execution_id: uuid.UUID, state: str, reason: str | None) -> None:
        await conn.execute(
            """UPDATE executions SET state=$2,blocked_reason=$3,owner=NULL,lease_until=NULL,updated_at=now()
            WHERE id=$1""",
            execution_id,
            state,
            reason,
        )

    async def observe(self) -> dict[str, int]:
        """PlayableAsset counts by state, for the operator gauge."""
        rows = await self.pool.fetch("SELECT state, count(*) AS total FROM playable_assets GROUP BY state")
        return {row["state"]: int(row["total"]) for row in rows}
