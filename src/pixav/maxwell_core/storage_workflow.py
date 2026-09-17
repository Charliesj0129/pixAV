"""Storage half of the managed execution authority.

The same authority that owns download and preparation also owns upload and
verification, so there is exactly one writer of execution state. A worker never
chooses an account, a segment or a retry: it is told what to attempt and reports
what happened. Remote creation is only ever a fact about the provider; durable
means an independent cold read-back satisfied a recorded policy version.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import asyncpg

from pixav.media_loader.preparation import PreparedArtifact, segment_plan
from pixav.shared.enums import AccountStatus
from pixav.shared.metrics import (
    record_remote_asset_durable,
    record_remote_asset_invalidated,
    record_remote_upload_failure,
    record_remote_verification_failure,
)
from pixav.shared.remote_assets import RemoteAssetRepository
from pixav.shared.storage_models import IntegrityError, current_policy
from pixav.shared.workflow import ActivityResult

logger = logging.getLogger(__name__)

# A read-back that came back wrong is confirmed remote loss only for these
# reasons, and the label set stays closed so the counter cannot be widened by
# whatever string a worker happens to report.
CONFIRMED_LOSS_REASONS = ("INTEGRITY_MISMATCH", "READBACK_FAILED")

# Withdrawn by a person who established the loss themselves, rather than by
# a failed read-back. Kept in the same closed label set for the counter.
OPERATOR_CONFIRMED_LOSS = "OPERATOR_CONFIRMED"


class StorageWorkflow:
    """Account leasing, segment sequencing and durability commits."""

    def __init__(self, pool: asyncpg.Pool, *, lease_seconds: int = 120) -> None:
        self.pool = pool
        self.lease_seconds = lease_seconds
        self.assets = RemoteAssetRepository(pool)
        self.policy = current_policy()

    async def create_asset(
        self,
        conn: asyncpg.Connection,
        *,
        video_id: uuid.UUID,
        artifact_id: uuid.UUID,
        artifact: PreparedArtifact,
    ) -> uuid.UUID:
        """Record the intent to place a prepared artifact with the provider."""
        return await self.assets.create(
            conn,
            video_id=video_id,
            artifact_id=artifact_id,
            expected=json.loads(artifact.facts.model_dump_json()),
            segments=segment_plan(artifact),
        )

    async def next_activity(
        self, conn: asyncpg.Connection, execution: Any, *, owner: uuid.UUID, generation: int
    ) -> tuple[str, dict] | None:
        """Resolve the next storage activity, or advance/block and return None.

        Returning ``None`` always means this execution has no dispatchable work
        right now and its state already reflects why.
        """
        checkpoint = json.loads(execution["checkpoint"])
        asset_id = uuid.UUID(checkpoint["asset_id"])
        if execution["stage"] == "upload":
            return await self._next_upload(conn, execution, asset_id, owner=owner, generation=generation)
        return await self._next_verify(conn, execution, asset_id)

    async def _next_upload(
        self, conn, execution, asset_id: uuid.UUID, *, owner: uuid.UUID, generation: int
    ) -> tuple[str, dict] | None:
        # Reaching here means the authority already judged the execution due on
        # the database clock. A quota wait must not also hold its own deadline,
        # or the segment would stay blocked past the reset that released it.
        await conn.execute(
            """UPDATE remote_asset_segments SET state='prepared',retry_not_before=NULL,updated_at=now()
            WHERE asset_id=$1 AND state='quota_wait' AND usage_counted_at IS NULL""",
            asset_id,
        )
        segment = await conn.fetchrow(
            """SELECT * FROM remote_asset_segments WHERE asset_id=$1 AND usage_counted_at IS NULL
            AND state <> 'user_action_required'
            AND (retry_not_before IS NULL OR retry_not_before <= now())
            ORDER BY segment_index LIMIT 1 FOR UPDATE""",
            asset_id,
        )
        if segment is None:
            if await conn.fetchval(
                "SELECT count(*) FROM remote_asset_segments WHERE asset_id=$1 AND usage_counted_at IS NULL",
                asset_id,
            ):
                # Something is waiting on a clock or on an operator; do not
                # advance and do not invent a reason here.
                return None
            await conn.execute(
                "UPDATE executions SET stage='verify',state='READY',due_at=now(),updated_at=now() WHERE id=$1",
                execution["id"],
            )
            # Verification is the same execution's next step, not a new one, so
            # resolve it here rather than idling until the following tick.
            return await self._next_verify(conn, {**dict(execution), "stage": "verify"}, asset_id)
        account_id = await self._lease_account(conn, execution, segment, owner=owner, generation=generation)
        if account_id is None:
            await self._wait_for_quota(conn, execution, asset_id)
            return None
        await conn.execute(
            "UPDATE remote_asset_segments SET account_id=$3,updated_at=now() WHERE asset_id=$1 AND segment_index=$2",
            asset_id,
            segment["segment_index"],
            account_id,
        )
        return (
            f"upload:{asset_id}:{segment['segment_index']}",
            {
                "stage": "upload",
                "asset_id": asset_id,
                "segment_index": segment["segment_index"],
                "account_id": account_id,
                "input_path": segment["local_path"],
            },
        )

    async def _next_verify(self, conn, execution, asset_id: uuid.UUID) -> tuple[str, dict] | None:
        segment = await conn.fetchrow(
            """SELECT * FROM remote_asset_segments WHERE asset_id=$1 AND state='backed_up'
            ORDER BY segment_index LIMIT 1 FOR UPDATE""",
            asset_id,
        )
        if segment is None:
            await self._promote(conn, execution, asset_id)
            return None
        return (
            f"verify:{asset_id}:{segment['segment_index']}",
            {
                "stage": "verify",
                "asset_id": asset_id,
                "segment_index": segment["segment_index"],
                "share_url": segment["share_url"],
            },
        )

    async def _lease_account(self, conn, execution, segment, *, owner: uuid.UUID, generation: int) -> uuid.UUID | None:
        """Least-recently-used eligible account, leased to this execution.

        Eligibility is decided entirely by the database clock, so a worker's
        wall clock cannot make an exhausted or cooling account look usable.
        """
        row = await conn.fetchrow(
            """SELECT a.id FROM accounts a
            WHERE a.status=$1
              AND (a.cooldown_until IS NULL OR a.cooldown_until <= now())
              AND (CASE WHEN a.quota_reset_at <= now() THEN 0 ELSE a.daily_uploaded_bytes END) + $2
                  <= a.daily_quota_bytes
              AND NOT EXISTS (SELECT FROM account_leases l WHERE l.account_id=a.id AND l.lease_until > now())
            ORDER BY a.last_used_at ASC NULLS FIRST
            FOR UPDATE SKIP LOCKED LIMIT 1""",
            AccountStatus.ACTIVE.value,
            segment["size_bytes"],
        )
        if row is None:
            return None
        account_id: uuid.UUID = row["id"]
        await conn.execute(
            "DELETE FROM account_leases WHERE account_id=$1 OR execution_id=$2", account_id, execution["id"]
        )
        await conn.execute(
            """INSERT INTO account_leases(account_id,execution_id,generation,owner,lease_until)
            VALUES($1,$2,$3,$4,now()+make_interval(secs=>$5))""",
            account_id,
            execution["id"],
            generation,
            owner,
            self.lease_seconds,
        )
        return account_id

    async def _wait_for_quota(self, conn, execution, asset_id: uuid.UUID) -> None:
        """No eligible account. The media stays local and the wait is explicit."""
        due = await conn.fetchval(
            """SELECT COALESCE(
                (SELECT min(GREATEST(quota_reset_at, COALESCE(cooldown_until, quota_reset_at)))
                   FROM accounts WHERE status=$1),
                now() + interval '1 hour')""",
            AccountStatus.ACTIVE.value,
        )
        await conn.execute(
            """UPDATE executions SET state='WAITING_QUOTA',blocked_reason='WAITING_QUOTA',
            due_at=$2,owner=NULL,lease_until=NULL,updated_at=now() WHERE id=$1""",
            execution["id"],
            due,
        )
        await conn.execute(
            """UPDATE remote_asset_segments SET state='quota_wait',retry_not_before=$2,updated_at=now()
            WHERE asset_id=$1 AND usage_counted_at IS NULL AND state='prepared'""",
            asset_id,
            due,
        )
        await self.release_lease(conn, execution["id"])

    async def _promote(self, conn, execution, asset_id: uuid.UUID) -> None:
        """Every segment read back cold and whole: commit durability."""
        rows = await conn.fetch(
            "SELECT verification FROM remote_asset_segments WHERE asset_id=$1 ORDER BY segment_index", asset_id
        )
        receipts = [json.loads(row["verification"]).get("readback", {}) for row in rows]
        if not receipts or any(receipt.get("cold_inputs") != "provider-only" for receipt in receipts):
            # A read-back that cannot prove it used no local input proves nothing.
            await conn.execute(
                """UPDATE executions SET state='FAILED',blocked_reason='REMOTE_VERIFICATION_INCOMPLETE',
                owner=NULL,lease_until=NULL,updated_at=now() WHERE id=$1""",
                execution["id"],
            )
            await self.release_lease(conn, execution["id"])
            record_remote_verification_failure("REMOTE_VERIFICATION_INCOMPLETE")
            return
        try:
            promoted = await self.assets.promote(
                conn, asset_id, {"cold_inputs": "provider-only", "segments": len(receipts)}
            )
        except IntegrityError as exc:
            # The manifest no longer reconstructs the film, or it never passed
            # the verification transition. Durability stays refused and the
            # local staging copy stays exactly where it is.
            logger.warning("refusing durability for %s: %s", asset_id, exc)
            await conn.execute(
                """UPDATE executions SET state='FAILED',blocked_reason='REMOTE_MANIFEST_INCONSISTENT',
                owner=NULL,lease_until=NULL,updated_at=now() WHERE id=$1""",
                execution["id"],
            )
            await self.release_lease(conn, execution["id"])
            record_remote_verification_failure("REMOTE_MANIFEST_INCONSISTENT")
            return
        await conn.execute(
            """UPDATE executions SET state='SUCCEEDED',owner=NULL,lease_until=NULL,updated_at=now()
            WHERE id=$1""",
            execution["id"],
        )
        await conn.execute("UPDATE tasks SET state='complete',updated_at=now() WHERE id=$1", execution["task_id"])
        await self.release_lease(conn, execution["id"])
        if promoted:
            record_remote_asset_durable()

    async def release_lease(self, conn: asyncpg.Connection, execution_id: uuid.UUID) -> None:
        """Release the account so another execution may eventually acquire it."""
        account_id = await conn.fetchval(
            "DELETE FROM account_leases WHERE execution_id=$1 RETURNING account_id", execution_id
        )
        if account_id is not None:
            await conn.execute("UPDATE accounts SET last_used_at=now() WHERE id=$1", account_id)

    # The only recovery facts an operator resume may drop. Everything else in
    # the blob records an effect the guest or the provider actually performed
    # -- a push, a scan, a backup, a share -- and clearing one of those would
    # be an instruction to redo an external side effect blindly.
    RESUMABLE_RECOVERY = ("user_action", "email_submitted", "password_submitted")
    RESUMABLE_OPERATIONS = ("credential_email", "credential_password")

    async def resume_after_user_action(
        self, execution_id: uuid.UUID, *, operator: str, reason: str, cleared: tuple[str, ...] = ()
    ) -> bool:
        """Hand a held execution back to the authority after a human looked.

        ``USER_ACTION_REQUIRED`` means the walk stopped rather than resubmit a
        credential, so only a person who has inspected the device may release
        it. The operator names which credential guards their inspection found
        unsent; nothing that records a remote effect can be named, so a resume
        can never turn into a second external attempt.
        """
        if not operator.strip() or not reason.strip():
            raise ValueError("resume requires operator and reason")
        unknown = [name for name in cleared if name not in self.RESUMABLE_RECOVERY + self.RESUMABLE_OPERATIONS]
        if unknown:
            raise ValueError(f"refusing to clear recovery facts about remote effects: {', '.join(sorted(unknown))}")
        async with self.pool.acquire() as conn, conn.transaction():
            execution = await conn.fetchrow("SELECT * FROM executions WHERE id=$1 FOR UPDATE", execution_id)
            if execution is None or execution["state"] != "USER_ACTION_REQUIRED":
                raise ValueError("only an execution held for user action may be resumed")
            asset_id = json.loads(execution["checkpoint"]).get("asset_id")
            if asset_id is not None:
                await self._clear_segment_guards(conn, uuid.UUID(str(asset_id)), cleared)
            checkpoint = json.loads(execution["checkpoint"])
            checkpoint["user_action_resume"] = {
                "operator": operator,
                "reason": reason,
                "cleared": sorted(cleared),
            }
            await conn.execute(
                """UPDATE executions SET state='READY',blocked_reason=NULL,error_code=NULL,owner=NULL,
                lease_until=NULL,due_at=now(),generation=generation+1,checkpoint=$2::jsonb,updated_at=now()
                WHERE id=$1""",
                execution_id,
                json.dumps(checkpoint),
            )
            return True

    async def _clear_segment_guards(self, conn, asset_id: uuid.UUID, cleared: tuple[str, ...]) -> None:
        rows = await conn.fetch(
            """SELECT segment_index,recovery FROM remote_asset_segments
            WHERE asset_id=$1 AND state='user_action_required' FOR UPDATE""",
            asset_id,
        )
        for row in rows:
            recovery = json.loads(row["recovery"])
            operations = dict(recovery.get("operations") or {})
            for name in cleared:
                recovery.pop(name, None)
                operations.pop(name, None)
            if operations or "operations" in recovery:
                recovery["operations"] = operations
            await conn.execute(
                """UPDATE remote_asset_segments SET state='prepared',recovery=$3::jsonb,updated_at=now()
                WHERE asset_id=$1 AND segment_index=$2""",
                asset_id,
                row["segment_index"],
                json.dumps(recovery),
            )

    async def apply_result(self, conn: asyncpg.Connection, execution: Any, result: ActivityResult) -> bool:
        """Apply one storage activity result. Returns True when it was handled."""
        if result.asset_id is None or result.segment_index is None:
            raise ValueError("storage result must address a segment")
        if result.outcome == "success":
            await self._apply_success(conn, execution, result)
            return True
        if result.outcome == "quota_exhausted":
            await self._wait_for_quota(conn, execution, result.asset_id)
            return True
        if result.outcome == "user_action":
            # Stop submitting credentials. Only an operator resumes from here.
            await conn.execute(
                """UPDATE remote_asset_segments SET state='user_action_required',updated_at=now()
                WHERE asset_id=$1 AND segment_index=$2 AND usage_counted_at IS NULL""",
                result.asset_id,
                result.segment_index,
            )
            await conn.execute(
                """UPDATE executions SET state='USER_ACTION_REQUIRED',blocked_reason=$2,owner=NULL,
                lease_until=NULL,updated_at=now() WHERE id=$1""",
                execution["id"],
                result.error_code or "USER_ACTION_REQUIRED",
            )
            await self.release_lease(conn, execution["id"])
            record_remote_upload_failure(result.error_code or "USER_ACTION_REQUIRED")
            return True
        if result.outcome == "verification_failed":
            # Creation stands, durability does not. Local staging stays protected.
            await conn.execute(
                """UPDATE remote_asset_segments SET state='failed',updated_at=now()
                WHERE asset_id=$1 AND segment_index=$2""",
                result.asset_id,
                result.segment_index,
            )
            await self._record_confirmed_loss(conn, result)
            await conn.execute(
                """UPDATE executions SET state='FAILED',blocked_reason='REMOTE_VERIFICATION_FAILED',
                owner=NULL,lease_until=NULL,updated_at=now() WHERE id=$1""",
                execution["id"],
            )
            await self.release_lease(conn, execution["id"])
            record_remote_verification_failure(result.error_code or "REMOTE_VERIFICATION_FAILED")
            return True
        return False

    async def _record_confirmed_loss(self, conn, result: ActivityResult) -> None:
        """A read-back failure on an already durable asset is remote loss.

        Before durability the same report means the upload has not been proven
        yet, which is a stalled execution and not a statement about the
        provider. After it, the bytes were read back once and cannot be now, so
        the durable claim is withdrawn rather than left standing. A share
        location that merely expired never reaches here: only a completed,
        failed read-back is reported as ``verification_failed``.
        """
        asset_id = result.asset_id
        if asset_id is None:
            return
        state = await conn.fetchval("SELECT state FROM remote_assets WHERE id=$1 FOR UPDATE", asset_id)
        if state != "DURABLE":
            return
        code = result.error_code or ""
        reason = code if code in CONFIRMED_LOSS_REASONS else "REMOTE_VERIFICATION_FAILED"
        await self.assets.invalidate(asset_id, code or reason, conn=conn)
        record_remote_asset_invalidated(reason)

    async def invalidate_asset(self, asset_id: uuid.UUID, *, operator: str, reason: str) -> str:
        """Withdraw a durability claim an operator has confirmed is no longer true.

        This is for loss the operator established themselves -- the album gone,
        the media removed. It is never a guess from a share location that
        stopped resolving, which is an expected, recoverable event. Returns the
        state the asset was in, so the caller can report what was withdrawn.
        """
        if not operator.strip() or not reason.strip():
            raise ValueError("invalidation requires operator and reason")
        async with self.pool.acquire() as conn, conn.transaction():
            state = await conn.fetchval("SELECT state FROM remote_assets WHERE id=$1 FOR UPDATE", asset_id)
            if state is None:
                raise ValueError("unknown remote asset")
            if state == "INVALID":
                return state
            await self.assets.invalidate(asset_id, f"{operator}: {reason}", conn=conn)
        record_remote_asset_invalidated(OPERATOR_CONFIRMED_LOSS)
        return state

    async def due_reverification(self, *, interval_days: int) -> int:
        """How many durable assets are overdue for an independent re-read."""
        if interval_days <= 0:
            return 0
        return int(
            await self.pool.fetchval(
                """SELECT count(*) FROM remote_assets WHERE state='DURABLE'
                AND COALESCE(reverified_at, durable_at) <= now() - make_interval(days => $1)""",
                interval_days,
            )
        )

    async def reverify_due(self, *, interval_days: int, limit: int = 5, max_retries: int = 1) -> list[uuid.UUID]:
        """Open verify-only executions for durable assets nobody has re-read.

        A remote copy nobody ever looks at again is an assumption, not a fact.
        These executions start at the ``verify`` stage and can never reach
        ``upload``: the segments are already counted against their account, so
        re-reading them cannot spend quota or create a second remote object.
        """
        if interval_days <= 0:
            return []
        opened: list[uuid.UUID] = []
        async with self.pool.acquire() as conn, conn.transaction():
            rows = await conn.fetch(
                """SELECT a.id, wt.task_id FROM remote_assets a
                JOIN workflow_tasks wt ON wt.video_id = a.video_id
                WHERE a.state='DURABLE'
                  AND COALESCE(a.reverified_at, a.durable_at) <= now() - make_interval(days => $1)
                  AND NOT EXISTS (SELECT FROM executions e WHERE e.task_id = wt.task_id
                                  AND e.state NOT IN ('SUCCEEDED','FAILED','CANCELLED'))
                ORDER BY COALESCE(a.reverified_at, a.durable_at)
                LIMIT $2 FOR UPDATE OF a SKIP LOCKED""",
                interval_days,
                limit,
            )
            for row in rows:
                execution_id = uuid.uuid4()
                await conn.execute(
                    """INSERT INTO executions(id,task_id,max_retries,stage,state,checkpoint)
                    VALUES($1,$2,$3,'verify','READY',$4::jsonb)""",
                    execution_id,
                    row["task_id"],
                    max_retries,
                    json.dumps({"asset_id": str(row["id"]), "reverification": True}),
                )
                # Creation still stands; it is the verification that is being
                # asked again, so the segments go back to the state that means
                # "backed up, not currently proven".
                await conn.execute(
                    """UPDATE remote_asset_segments SET state='backed_up',updated_at=now()
                    WHERE asset_id=$1 AND state='verified'""",
                    row["id"],
                )
                opened.append(execution_id)
        return opened

    async def _apply_success(self, conn, execution, result: ActivityResult) -> None:
        asset_id, segment_index = result.asset_id, result.segment_index
        if asset_id is None or segment_index is None:
            raise ValueError("storage result must address a segment")
        if execution["stage"] == "upload":
            if not result.share_url:
                raise ValueError("upload success requires a share location")
            await self.assets.confirm_backup(conn, asset_id, segment_index, result.share_url, result.evidence)
        else:
            await self.assets.confirm_readback(conn, asset_id, segment_index, result.evidence)
        await conn.execute(
            """UPDATE executions SET state='READY',due_at=now(),owner=NULL,lease_until=NULL,
            infrastructure_retries=0,blocked_reason=NULL,updated_at=now() WHERE id=$1""",
            execution["id"],
        )
        if execution["stage"] == "upload":
            await self.release_lease(conn, execution["id"])
