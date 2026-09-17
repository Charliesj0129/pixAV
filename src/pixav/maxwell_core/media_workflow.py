"""PostgreSQL-owned MediaWorkflow, advanced only by Maxwell.

The caller supplies a dedicated execution-authority pool. Admission is explicit;
legacy cohorts must be quiescent before calling admit().
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg

from pixav.maxwell_core.storage_workflow import StorageWorkflow
from pixav.shared.retry import DEFAULT_RETRY_BACKOFF_SECONDS
from pixav.shared.workflow import ActivityRequest, ActivityResult, Execution, ExecutionAttempt
from pixav.sht_probe.policy import SourcePolicy

logger = logging.getLogger(__name__)

_SHA256 = re.compile(r"^[a-f0-9]{64}$")


def _file_sha256(path: Path) -> str:
    """Hash the whole file; a declared digest is never taken on trust."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class MediaWorkflow:
    def __init__(
        self,
        pool: asyncpg.Pool,
        *,
        backoff: tuple[int, ...] = DEFAULT_RETRY_BACKOFF_SECONDS,
        lease_seconds: int = 120,
        cooldown_seconds: int = 21600,
        source_policy: SourcePolicy | None = None,
        max_retries: int = 6,
    ) -> None:
        if not backoff or any(delay <= 0 for delay in backoff) or lease_seconds <= 0 or cooldown_seconds <= 0:
            raise ValueError("positive backoff, lease and cooldown required")
        self.pool = pool
        self.backoff = backoff
        self.lease_seconds = lease_seconds
        self.cooldown_seconds = cooldown_seconds
        if max_retries < 0:
            raise ValueError("max_retries must be nonnegative")
        self.max_retries = max_retries
        self.source_policy = source_policy or SourcePolicy()
        # Storage is a second set of stages on the same authority, never a
        # second authority: one writer of execution state, as the contract says.
        self.storage = StorageWorkflow(pool, lease_seconds=lease_seconds)

    async def admit(self, video_id: UUID, *, max_retries: int | None = None) -> UUID:
        """Create a stable domain task once; never adopt a live legacy task."""
        async with self.pool.acquire() as conn, conn.transaction():
            if not await conn.fetchval("SELECT id FROM videos WHERE id=$1 FOR UPDATE", video_id):
                raise ValueError("unknown video")
            existing = await conn.fetchval("SELECT task_id FROM workflow_tasks WHERE video_id=$1", video_id)
            if existing:
                return existing
            if await conn.fetchval(
                "SELECT EXISTS(SELECT FROM tasks WHERE video_id=$1 AND state NOT IN ('complete','failed'))", video_id
            ):
                raise ValueError("legacy tasks must be reconciled before admission")
            task_id = uuid4()
            await conn.execute(
                "INSERT INTO tasks(id,video_id,queue_name) VALUES($1,$2,'pixav:media-managed')", task_id, video_id
            )
            await conn.execute("INSERT INTO workflow_tasks(task_id,video_id) VALUES($1,$2)", task_id, video_id)
            await conn.execute(
                "INSERT INTO executions(id,task_id,max_retries) VALUES($1,$2,$3)",
                uuid4(),
                task_id,
                self.max_retries if max_retries is None else max_retries,
            )
            return task_id

    async def adopt_local_source(
        self,
        execution_id: UUID,
        *,
        path: str,
        declared_sha256: str,
        source_url: str,
        operator: str,
        reason: str,
    ) -> UUID:
        """Record an operator-supplied file as this execution's download result.

        The download stage only dispatches a swarm identified by a 40-hex
        info_hash, so a file an operator already holds has no way in. This is
        that way in, and it is deliberately not a fake torrent: the artifact is
        marked ``operator-supplied`` with the reference it came from, so nothing
        downstream can mistake it for something the pipeline fetched itself.

        The authority writes it, as it writes every other execution transition.
        The hash is recomputed here rather than trusted, so a file that changed
        between the operator reading it and this call cannot enter the pipeline.
        """
        if not operator.strip() or not reason.strip():
            raise ValueError("adoption requires operator and reason")
        if not _SHA256.fullmatch(declared_sha256):
            raise ValueError("declared_sha256 must be a hex SHA-256 digest")
        artifact = Path(path)
        if artifact.is_symlink() or not artifact.is_file():
            raise ValueError("source must be an existing regular file")
        observed = await asyncio.to_thread(_file_sha256, artifact)
        if observed != declared_sha256:
            raise ValueError("source file does not match the declared SHA-256")

        async with self.pool.acquire() as conn, conn.transaction():
            execution = await conn.fetchrow("SELECT * FROM executions WHERE id=$1 FOR UPDATE", execution_id)
            if execution is None:
                raise ValueError("unknown execution")
            if execution["stage"] != "download" or execution["state"] != "READY":
                raise ValueError("only a READY download execution may adopt a local source")
            identity = f"operator-supplied:{observed}"
            operation = await conn.fetchval(
                """INSERT INTO operation_intents(id,execution_id,stage,identity)
                VALUES($1,$2,'download',$3) ON CONFLICT(execution_id,stage,identity)
                DO UPDATE SET identity=EXCLUDED.identity RETURNING id""",
                uuid4(),
                execution_id,
                identity,
            )
            facts = {
                "provenance": "operator-supplied",
                "source_url": source_url,
                "sha256": observed,
                "size_bytes": artifact.stat().st_size,
                "operator": operator,
                "reason": reason,
            }
            await conn.execute(
                """INSERT INTO workflow_artifacts(id,execution_id,operation_id,path,facts)
                VALUES($1,$2,$3,$4,$5::jsonb) ON CONFLICT(operation_id) DO NOTHING""",
                uuid4(),
                execution_id,
                operation,
                str(artifact),
                json.dumps(facts),
            )
            await conn.execute(
                """UPDATE executions SET stage='prepare',state='READY',checkpoint=$2::jsonb,
                due_at=now(),owner=NULL,lease_until=NULL,blocked_reason=NULL,updated_at=now() WHERE id=$1""",
                execution_id,
                json.dumps({"download_path": str(artifact), "download_operation": str(operation)}),
            )
            return operation

    async def replay(self, execution_id: UUID, *, operator: str, reason: str) -> UUID:
        if not operator.strip() or not reason.strip():
            raise ValueError("replay requires operator and reason")
        async with self.pool.acquire() as conn, conn.transaction():
            old = await conn.fetchrow("SELECT * FROM executions WHERE id=$1 FOR UPDATE", execution_id)
            if old is None or old["state"] not in {"FAILED", "CANCELLED"}:
                raise ValueError("only failed or cancelled execution may be manually replayed")
            new_id = uuid4()
            await conn.execute(
                """INSERT INTO executions(id,task_id,max_retries,replay_of,replay_operator,replay_reason)
                VALUES($1,$2,$3,$4,$5,$6)""",
                new_id,
                old["task_id"],
                old["max_retries"],
                execution_id,
                operator,
                reason,
            )
            return new_id

    async def next_activity(self, *, download_paused: bool = False) -> ActivityRequest | None:
        """DB clock, row locking, durable operation intent precede Redis dispatch."""
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(721034001)")
            execution = await conn.fetchrow(
                """SELECT e.*, t.video_id FROM executions e
                JOIN workflow_tasks t ON t.task_id=e.task_id
                WHERE e.state IN ('READY','WAITING_RETRY','WAITING_QUOTA') AND e.due_at <= now()
                AND (NOT $1::boolean OR e.stage <> 'download')
                AND NOT EXISTS (SELECT FROM executions active WHERE active.state='RUNNING' AND active.stage=e.stage)
                ORDER BY CASE WHEN e.stage='download' THEN 1 ELSE 0 END,e.due_at,e.created_at,e.id
                LIMIT 1 FOR UPDATE OF e SKIP LOCKED""",
                download_paused,
            )
            if execution is None:
                return None
            candidate_id = execution["candidate_id"]
            checkpoint = json.loads(execution["checkpoint"])
            owner, attempt = uuid4(), uuid4()
            generation = execution["generation"] + 1
            extra: dict = {}
            stage = execution["stage"]
            if execution["stage"] in ("upload", "verify"):
                resolved = await self.storage.next_activity(conn, execution, owner=owner, generation=generation)
                if resolved is None:
                    return None
                identity, extra = resolved
                # Storage may advance the stage while resolving, so the intent,
                # the attempt and the envelope must all name the resolved one.
                stage = extra.pop("stage")
            elif execution["stage"] == "download":
                candidate = await conn.fetchrow(
                    """SELECT c.* FROM source_candidates c
                    JOIN LATERAL (SELECT provider,provider_id,score FROM source_observations o
                      WHERE o.video_id=c.video_id AND o.info_hash=c.info_hash AND o.eligible
                      ORDER BY score DESC,provider,provider_id LIMIT 1) best ON true
                    WHERE c.video_id=$1
                    AND (state IN ('pending','succeeded') OR unavailable_until <= now())
                    AND info_hash ~ '^[a-f0-9]{40}$'
                    ORDER BY (id=$2) DESC NULLS LAST, best.score DESC,best.provider,best.provider_id,
                    info_hash,magnet_uri LIMIT 1""",
                    execution["video_id"],
                    candidate_id,
                )
                if candidate is None:
                    if execution["blocked_reason"] != "SOURCE_UNAVAILABLE":
                        logger.info("source unavailable: execution=%s video=%s", execution["id"], execution["video_id"])
                    await conn.execute(
                        """UPDATE executions SET state='READY', blocked_reason='SOURCE_UNAVAILABLE',
                        due_at=COALESCE((SELECT min(unavailable_until) FROM source_candidates
                            WHERE video_id=$2 AND unavailable_until > now()), now()+interval '60 seconds')
                        WHERE id=$1""",
                        execution["id"],
                        execution["video_id"],
                    )
                    return None
                candidate_id = candidate["id"]
                identity = candidate["info_hash"]
            else:
                identity = checkpoint["download_operation"]
            operation = await conn.fetchval(
                """INSERT INTO operation_intents(id,execution_id,stage,identity)
                VALUES($1,$2,$3,$4) ON CONFLICT(execution_id,stage,identity)
                DO UPDATE SET identity=EXCLUDED.identity RETURNING id""",
                uuid4(),
                execution["id"],
                stage,
                identity,
            )
            await conn.execute(
                """UPDATE executions SET state='RUNNING',owner=$2,generation=$3,
                lease_until=now()+make_interval(secs=>$4),lease_seconds=$4,
                candidate_id=$5,blocked_reason=NULL,updated_at=now()
                WHERE id=$1""",
                execution["id"],
                owner,
                generation,
                self.lease_seconds,
                candidate_id,
            )
            await conn.execute(
                """INSERT INTO activity_attempts(id,execution_id,generation,owner,stage,operation_id)
                VALUES($1,$2,$3,$4,$5,$6)""",
                attempt,
                execution["id"],
                generation,
                owner,
                stage,
                operation,
            )
            return ActivityRequest(
                task_id=execution["task_id"],
                execution_id=execution["id"],
                attempt_id=attempt,
                owner=owner,
                generation=generation,
                operation_id=operation,
                stage=stage,
                identity=identity,
                input_path=extra.pop("input_path", checkpoint.get("download_path")),
                **extra,
            )

    async def consume_results(self) -> int:
        """One committed result causes at most one transition, including handoff."""
        count = 0
        async with self.pool.acquire() as conn, conn.transaction():
            rows = await conn.fetch("""SELECT r.payload,r.attempt_id,a.stage,a.execution_id FROM activity_results r
                JOIN activity_attempts a ON a.id=r.attempt_id WHERE r.consumed_at IS NULL
                ORDER BY r.received_at LIMIT 100 FOR UPDATE OF r SKIP LOCKED""")
            for row in rows:
                execution = await conn.fetchrow("SELECT * FROM executions WHERE id=$1 FOR UPDATE", row["execution_id"])
                try:
                    result = ActivityResult.model_validate_json(row["payload"])
                except ValueError:
                    # A malformed report cannot block unrelated results or be
                    # reinterpreted as a successful external operation.
                    await conn.execute(
                        """UPDATE executions SET state='USER_ACTION_REQUIRED',
                        failure_class='invalid_report',error_code='INVALID_ACTIVITY_REPORT',
                        owner=NULL,lease_until=NULL,updated_at=now() WHERE id=$1 AND state='RUNNING'
                        AND generation=(SELECT generation FROM activity_attempts WHERE id=$2)""",
                        row["execution_id"],
                        row["attempt_id"],
                    )
                    await conn.execute(
                        "UPDATE activity_results SET consumed_at=now() WHERE attempt_id=$1", row["attempt_id"]
                    )
                    await conn.execute("UPDATE activity_attempts SET completed_at=now() WHERE id=$1", row["attempt_id"])
                    continue
                if (
                    execution
                    and execution["state"] == "RUNNING"
                    and execution["generation"] == result.generation
                    and execution["owner"] == result.owner
                ):
                    try:
                        async with conn.transaction():
                            await self._transition(conn, execution, row["stage"], result)
                    except (ValueError, KeyError):
                        await conn.execute(
                            """UPDATE executions SET state='USER_ACTION_REQUIRED',
                            failure_class='invalid_report',error_code='INVALID_ACTIVITY_REPORT',
                            owner=NULL,lease_until=NULL,updated_at=now() WHERE id=$1""",
                            result.execution_id,
                        )
                    count += 1
                await conn.execute(
                    "UPDATE activity_results SET consumed_at=now() WHERE attempt_id=$1", result.attempt_id
                )
                await conn.execute("UPDATE activity_attempts SET completed_at=now() WHERE id=$1", result.attempt_id)
        return count

    async def _transition(self, conn, execution, stage: str, result: ActivityResult) -> None:
        if stage in {"download", "prepare"} and result.outcome not in {
            "success",
            "infrastructure",
            "unknown_effect",
            "invalid_media",
            *(("source_unavailable",) if stage == "download" else ()),
        }:
            raise ValueError("outcome is not valid for this media activity")
        if result.outcome != "success":
            await conn.execute(
                """UPDATE executions SET failure_class=$2,error_code=$3,updated_at=now()
                WHERE id=$1""",
                result.execution_id,
                result.outcome,
                result.error_code,
            )
        if stage in ("upload", "verify"):
            if await self.storage.apply_result(conn, execution, result):
                return
            # Infrastructure and unknown-effect outcomes share the generic retry
            # policy below, but must not keep holding somebody else's account.
            await self.storage.release_lease(conn, execution["id"])
        if result.outcome == "success":
            await self._success(conn, execution, stage, result)
        elif result.outcome == "source_unavailable":
            await conn.execute(
                """UPDATE source_candidates SET state='unavailable',attempts=attempts+1,
                unavailable_until=now()+make_interval(secs=>$2),last_error='SOURCE_UNAVAILABLE',updated_at=now()
                WHERE id=$1""",
                execution["candidate_id"],
                self.cooldown_seconds,
            )
            await conn.execute(
                "UPDATE executions SET state='READY',due_at=now(),owner=NULL,lease_until=NULL WHERE id=$1",
                result.execution_id,
            )
        elif result.outcome == "infrastructure":
            retries = execution["infrastructure_retries"] + 1
            state = "FAILED" if retries > execution["max_retries"] else "WAITING_RETRY"
            delay = self.backoff[min(retries - 1, len(self.backoff) - 1)]
            await conn.execute(
                """UPDATE executions SET state=$2,infrastructure_retries=$3,
                due_at=now()+make_interval(secs=>$4),owner=NULL,lease_until=NULL WHERE id=$1""",
                result.execution_id,
                state,
                retries,
                delay,
            )
        else:
            state = "USER_ACTION_REQUIRED" if result.outcome == "unknown_effect" else "FAILED"
            await conn.execute(
                """UPDATE executions SET state=$2,blocked_reason=$3,owner=NULL,
                lease_until=NULL WHERE id=$1""",
                result.execution_id,
                state,
                result.error_code,
            )

    async def _success(self, conn, execution, stage: str, result: ActivityResult) -> None:
        if stage == "download":
            if not result.artifact_path:
                raise ValueError("download success requires artifact path")
            facts = json.dumps({"download_path": result.artifact_path, "download_operation": str(result.operation_id)})
            await conn.execute(
                """INSERT INTO workflow_artifacts(id,execution_id,operation_id,path,facts)
                VALUES($1,$2,$3,$4,'{}') ON CONFLICT(operation_id) DO NOTHING""",
                uuid4(),
                result.execution_id,
                result.operation_id,
                result.artifact_path,
            )
            await conn.execute(
                """UPDATE executions SET stage='prepare',state='READY',checkpoint=$2::jsonb,
                due_at=now(),owner=NULL,lease_until=NULL WHERE id=$1""",
                result.execution_id,
                facts,
            )
            return
        if result.prepared is None:
            raise ValueError("preparation success requires verified facts")
        from pixav.media_loader.preparation import PreparationPolicy
        from pixav.shared.exceptions import RemuxError

        source = result.prepared.input_facts
        if source is None or source.sha256 != result.prepared.input_sha256:
            raise ValueError("preparation requires verified source facts")
        checkpoint = json.loads(execution["checkpoint"])
        if result.prepared.input_path != checkpoint.get("download_path"):
            raise ValueError("preparation input does not match the download checkpoint")
        try:
            PreparationPolicy().validate_output(source, result.prepared.facts)
        except RemuxError as exc:
            raise ValueError("preparation violates lossless policy") from exc
        artifact_id = await conn.fetchval(
            """INSERT INTO workflow_artifacts(id,execution_id,operation_id,path,facts)
            VALUES($1,$2,$3,$4,$5::jsonb) ON CONFLICT(operation_id) DO UPDATE SET operation_id=EXCLUDED.operation_id RETURNING id""",
            uuid4(),
            result.execution_id,
            result.operation_id,
            result.prepared.path,
            result.prepared.model_dump_json(),
        )
        # The prepared artifact is handed to storage inside this same execution.
        # A separate legacy upload task would be a second executor writing the
        # same work, so the handoff is a stage change, not a new queue entry.
        video_id = await conn.fetchval("SELECT video_id FROM workflow_tasks WHERE task_id=$1", result.task_id)
        asset_id = await self.storage.create_asset(
            conn, video_id=video_id, artifact_id=artifact_id, artifact=result.prepared
        )
        checkpoint = json.loads(execution["checkpoint"])
        checkpoint.update(asset_id=str(asset_id), prepared_path=result.prepared.path)
        await conn.execute(
            """UPDATE videos SET local_path=$2,status='downloaded',updated_at=now(),
            metadata_json=jsonb_set(COALESCE(metadata_json,'{}'::jsonb),'{media}',$3::jsonb)
            WHERE id=(SELECT video_id FROM workflow_tasks WHERE task_id=$1)""",
            result.task_id,
            result.prepared.path,
            result.prepared.facts.model_dump_json(),
        )
        await conn.execute(
            "UPDATE source_candidates SET state='succeeded',updated_at=now() WHERE id=$1", execution["candidate_id"]
        )
        await conn.execute(
            """UPDATE executions SET stage='upload',state='READY',checkpoint=$2::jsonb,due_at=now(),
            owner=NULL,lease_until=NULL,updated_at=now() WHERE id=$1""",
            result.execution_id,
            json.dumps(checkpoint),
        )

    async def recover_expired(self) -> int:
        """Reissue a reconciliation activity with the same operation identity.

        No retry budget is reset. The activity must query the external operation
        before creating any side effect; unknown ownership is a manual stop.
        """
        tag = await self.pool.execute("""UPDATE executions e SET
            state=CASE WHEN stage IN ('download','prepare') AND recovery_count >= max_retries
                THEN 'USER_ACTION_REQUIRED' ELSE 'READY' END,
            failure_class='lease_expired',error_code='ACTIVITY_LEASE_EXPIRED',
            recovery_count=recovery_count+1,owner=NULL,lease_until=NULL,due_at=now(),updated_at=now()
            WHERE state='RUNNING' AND lease_until <= now()
            AND NOT EXISTS (SELECT FROM activity_results r JOIN activity_attempts a ON a.id=r.attempt_id
                WHERE a.execution_id=e.id AND a.generation=e.generation AND r.consumed_at IS NULL)""")
        # An account whose holder stopped heartbeating becomes acquirable again,
        # but only after its own deadline has passed on the database clock.
        await self.pool.execute("DELETE FROM account_leases WHERE lease_until <= now()")
        return int(tag.split()[-1])

    async def cancel(self, execution_id: UUID, *, operator: str, reason: str) -> bool:
        """Cancel authority-owned execution, fence its workers and retain artifacts."""
        if not operator.strip() or not reason.strip():
            raise ValueError("cancellation requires operator and reason")
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow("SELECT * FROM executions WHERE id=$1 FOR UPDATE", execution_id)
            if row is None or row["state"] in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                return False
            # Keep unknown remote effects for the storage reconciliation path.
            if row["stage"] not in {"download", "prepare"}:
                raise ValueError("storage cancellation requires remote reconciliation")
            checkpoint = json.loads(row["checkpoint"])
            checkpoint["cancellation"] = {"operator": operator, "reason": reason}
            await conn.execute(
                """UPDATE executions SET state='CANCELLED',owner=NULL,lease_until=NULL,
                generation=generation+1,checkpoint=$2::jsonb,updated_at=now() WHERE id=$1""",
                execution_id,
                json.dumps(checkpoint),
            )
            return True

    async def retry_now(self, execution_id: UUID, *, operator: str, reason: str) -> bool:
        """Bring a waiting execution's next attempt forward to now.

        The backoff exists to stop the authority hammering a dependency that is
        still broken. It says nothing useful once an operator has repaired the
        cause, and the later steps are hours long, so a fixed deployment would
        otherwise sit idle waiting out a delay whose reason has gone.

        This moves a deadline and nothing else: the retry counter still stands,
        so the attempt bound is unchanged, and the segment's own recovery
        journal still decides what may be attempted again. It can therefore
        never repeat an external effect that already happened.
        """
        if not operator.strip() or not reason.strip():
            raise ValueError("retry requires operator and reason")
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow("SELECT * FROM executions WHERE id=$1 FOR UPDATE", execution_id)
            if row is None or row["state"] not in {"WAITING_RETRY", "WAITING_QUOTA"}:
                raise ValueError("only an execution waiting on a retry or quota clock may be brought forward")
            checkpoint = json.loads(row["checkpoint"])
            checkpoint["retry_now"] = {"operator": operator, "reason": reason}
            await conn.execute(
                """UPDATE executions SET due_at=now(),checkpoint=$2::jsonb,updated_at=now() WHERE id=$1""",
                execution_id,
                json.dumps(checkpoint),
            )
            return True

    async def reopen(self, execution_id: UUID, *, operator: str, reason: str) -> bool:
        """Give a retry-exhausted execution its attempt budget back.

        ``max_retries`` bounds how long the authority keeps retrying a fault it
        cannot diagnose. Once an operator has diagnosed and fixed one, that
        bound is measuring nothing, and the only route back would otherwise be
        :meth:`replay` -- which starts a fresh execution, and so a fresh
        artifact and a fresh remote asset. For a stage that has already had an
        external effect, that is a second upload of media that is already
        uploaded. Reopening keeps the execution, its checkpoint, its asset and
        the segment's recovery journal exactly as they are, so every operation
        already recorded as done stays done and is not attempted again.

        Only an infrastructure failure may be reopened. A verification failure
        is a fact about the remote copy rather than a fault in the attempt, and
        forgiving retries would not change it; those stay FAILED with their
        staging retained.
        """
        if not operator.strip() or not reason.strip():
            raise ValueError("reopen requires operator and reason")
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow("SELECT * FROM executions WHERE id=$1 FOR UPDATE", execution_id)
            if row is None or row["state"] != "FAILED":
                raise ValueError("only a FAILED execution may be reopened")
            if row["failure_class"] != "infrastructure":
                raise ValueError("only an infrastructure failure may be reopened")
            checkpoint = json.loads(row["checkpoint"])
            # A list: every forgiveness stays on the record, including how many
            # attempts it covered, so the history is not quietly flattened.
            checkpoint.setdefault("reopen", []).append(
                {
                    "operator": operator,
                    "reason": reason,
                    "at": datetime.now(timezone.utc).isoformat(),
                    "retries_forgiven": row["infrastructure_retries"],
                }
            )
            await conn.execute(
                """UPDATE executions SET state='READY',due_at=now(),infrastructure_retries=0,
                failure_class=NULL,error_code=NULL,blocked_reason=NULL,owner=NULL,lease_until=NULL,
                checkpoint=$2::jsonb,updated_at=now() WHERE id=$1""",
                execution_id,
                json.dumps(checkpoint),
            )
            return True

    async def inspect(self, execution_id: UUID) -> Execution | None:
        """Expose task/attempt history and classifications, never raw payloads."""
        async with self.pool.acquire() as conn, conn.transaction(isolation="repeatable_read", readonly=True):
            row = await conn.fetchrow("SELECT * FROM executions WHERE id=$1", execution_id)
            if row is None:
                return None
            attempts = await conn.fetch(
                """SELECT a.*,r.payload->>'outcome' AS outcome,
                r.payload->>'error_code' AS error_code FROM activity_attempts a
                LEFT JOIN activity_results r ON r.attempt_id=a.id WHERE a.execution_id=$1
                ORDER BY a.generation""",
                execution_id,
            )
            return Execution.model_validate(
                {**dict(row), "attempts": tuple(ExecutionAttempt.model_validate(dict(attempt)) for attempt in attempts)}
            )

    async def observe_states(self) -> dict[str, object]:
        """Count executions by the distinction an operator has to act on.

        Waiting for quota, waiting for a source and stopping for an operator are
        three healthy, recoverable states. Reporting any of them as a generic
        failure would hide a working pipeline behind an alert nobody can act on.
        """
        rows = await self.pool.fetch("""SELECT state, blocked_reason, failure_class, count(*) AS total
            FROM executions GROUP BY state, blocked_reason, failure_class""")
        terminal: dict[str, int] = {}
        counts = {"waiting_quota": 0, "source_unavailable": 0, "user_action_required": 0}
        for row in rows:
            total = int(row["total"])
            if row["state"] == "WAITING_QUOTA":
                counts["waiting_quota"] += total
            elif row["state"] == "USER_ACTION_REQUIRED":
                counts["user_action_required"] += total
            elif row["state"] in ("FAILED", "CANCELLED"):
                key = row["failure_class"] or ("cancelled" if row["state"] == "CANCELLED" else "unclassified")
                terminal[key] = terminal.get(key, 0) + total
            if row["blocked_reason"] == "SOURCE_UNAVAILABLE" and row["state"] not in (
                "SUCCEEDED",
                "FAILED",
                "CANCELLED",
            ):
                counts["source_unavailable"] += total
        return {**counts, "terminal": terminal}

    async def observe(self, video_id: UUID, observations) -> None:
        """Explicit video relationship only; rediscovery preserves active cooldown."""
        policy = self.source_policy
        async with self.pool.acquire() as conn, conn.transaction():
            if not await conn.fetchval("SELECT id FROM videos WHERE id=$1 FOR UPDATE", video_id):
                raise ValueError("unknown video")
            now = await conn.fetchval("SELECT now()")
            for observation in observations:
                evaluation = policy.evaluate(observation, now=now)
                await conn.execute(
                    """INSERT INTO source_observations(video_id,provider,provider_id,info_hash,observation,eligible,score)
                    VALUES($1,$2,$3,$4,$5::jsonb,$6,$7) ON CONFLICT(video_id,provider,provider_id)
                    DO UPDATE SET info_hash=EXCLUDED.info_hash,observation=EXCLUDED.observation,
                    eligible=EXCLUDED.eligible,score=EXCLUDED.score,observed_at=now()""",
                    video_id,
                    observation.provider,
                    observation.provider_id,
                    observation.info_hash,
                    observation.model_dump_json(),
                    evaluation.eligible,
                    evaluation.score,
                )
                await conn.execute(
                    """INSERT INTO source_candidates(video_id,magnet_uri,info_hash,origin,quality_score)
                    VALUES($1,$2,$3,$4,$5) ON CONFLICT(video_id,magnet_uri) DO UPDATE SET
                    quality_score=EXCLUDED.quality_score,updated_at=now()""",
                    video_id,
                    observation.magnet_uri,
                    observation.info_hash,
                    observation.provider,
                    evaluation.score if evaluation.eligible else -10000,
                )
            await conn.execute(
                """UPDATE source_candidates c SET quality_score=COALESCE(
                (SELECT max(score) FROM source_observations o WHERE o.video_id=c.video_id
                 AND o.info_hash=c.info_hash AND o.eligible), -10000), origin=COALESCE(
                (SELECT min(provider) FROM source_observations o WHERE o.video_id=c.video_id AND o.info_hash=c.info_hash),origin)
                WHERE c.video_id=$1""",
                video_id,
            )
            await conn.execute(
                """UPDATE executions e SET due_at=now() FROM workflow_tasks t
                WHERE t.video_id=$1 AND e.task_id=t.task_id AND e.state='READY'
                AND e.blocked_reason='SOURCE_UNAVAILABLE'""",
                video_id,
            )

    async def tick(self, queue, *, download_paused: bool = False, storage_queue=None) -> int:
        await self.consume_results()
        await self.recover_expired()
        request = await self.next_activity(download_paused=download_paused)
        if request is None:
            return 0
        target = storage_queue if request.stage in ("upload", "verify") else queue
        if target is None:
            raise RuntimeError("no transport configured for the resolved activity stage")
        await target.push(request.model_dump(mode="json"))
        return 1

    async def ingest_observation(self, payload) -> UUID:
        """Discovery identity is torrent hash, never a guessed title equivalence."""
        observation = self.source_policy.normalize(payload)
        async with self.pool.acquire() as conn, conn.transaction():
            video_id = await conn.fetchval(
                """INSERT INTO videos(id,title,info_hash,magnet_uri)
                VALUES($1,$2,$3,$4) ON CONFLICT(info_hash) DO UPDATE SET info_hash=EXCLUDED.info_hash RETURNING id""",
                uuid4(),
                observation.title,
                observation.info_hash,
                observation.magnet_uri,
            )
        await self.observe(video_id, [observation])
        await self.admit(video_id)
        return video_id
