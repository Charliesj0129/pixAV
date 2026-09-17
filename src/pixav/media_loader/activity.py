"""Managed activity worker: reports facts, never schedules retry or edits tasks."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

import asyncpg
from pydantic import ValidationError

from pixav.media_loader.interfaces import Remuxer, TorrentClient
from pixav.media_loader.preparation import prepare_media
from pixav.media_loader.remuxer import select_media_input
from pixav.shared.exceptions import MediaDependencyError, RemuxError, SourceUnavailableError, TorrentOwnershipError
from pixav.shared.queue import TaskQueue
from pixav.shared.workflow import ActivityRequest, ActivityResult

logger = logging.getLogger(__name__)


class MediaActivityWorker:
    def __init__(self, pool: asyncpg.Pool, client: TorrentClient, remuxer: Remuxer, *, output_dir: str) -> None:
        self.pool, self.client, self.remuxer = pool, client, remuxer
        self.output_dir = output_dir

    async def execute(self, request: ActivityRequest) -> ActivityResult:
        fields = request.model_dump(exclude={"stage", "identity", "input_path"})
        try:
            if request.stage not in {"download", "prepare"}:
                raise ValueError("unsupported media activity stage")
            if request.stage == "download":
                path = await self.client.reconcile_download(request.identity, str(request.operation_id))
                return ActivityResult(**fields, outcome="success", artifact_path=select_media_input(path))
            if not request.input_path:
                raise RemuxError("missing downloaded artifact")
            output = str(Path(self.output_dir) / str(request.operation_id) / "prepared.mp4")
            artifact = await prepare_media(request.input_path, output, self.remuxer)
            return ActivityResult(**fields, outcome="success", prepared=artifact)
        except SourceUnavailableError:
            return ActivityResult(**fields, outcome="source_unavailable", error_code="SOURCE_UNAVAILABLE")
        except TorrentOwnershipError:
            return ActivityResult(**fields, outcome="unknown_effect", error_code="TORRENT_OWNERSHIP_UNKNOWN")
        except MediaDependencyError:
            return ActivityResult(**fields, outcome="infrastructure", error_code="MEDIA_DEPENDENCY")
        except RemuxError:
            return ActivityResult(**fields, outcome="invalid_media", error_code="INVALID_MEDIA")
        except Exception:
            return ActivityResult(**fields, outcome="infrastructure", error_code="DEPENDENCY_FAILURE")

    async def run_one(self, queue: TaskQueue, *, download_paused: bool = False) -> bool:
        claimed = await queue.pop_claim(timeout=1)
        if claimed is None:
            return False
        payload, receipt = claimed
        try:
            request = ActivityRequest.model_validate(payload)
        except ValidationError:
            logger.warning("invalid activity envelope")
            await queue.ack(receipt)
            return True
        if request.stage not in {"download", "prepare"}:
            logger.warning("non-media activity delivered to media worker")
            await queue.ack(receipt)
            return True
        if download_paused and request.stage == "download":
            await queue.nack(receipt, requeue=True)
            return False
        row = await self.pool.fetchrow(
            """SELECT e.task_id,e.owner,e.generation,e.stage,e.checkpoint,
            a.operation_id,o.identity FROM executions e JOIN activity_attempts a ON a.execution_id=e.id
            JOIN operation_intents o ON o.id=a.operation_id WHERE e.id=$1 AND a.id=$2
            AND a.generation=e.generation AND e.state='RUNNING' AND e.lease_until > now()""",
            request.execution_id,
            request.attempt_id,
        )
        if row is None:
            await queue.ack(receipt)
            return True
        import json

        expected = (
            row["task_id"],
            row["owner"],
            row["generation"],
            row["stage"],
            row["operation_id"],
            row["identity"],
            json.loads(row["checkpoint"]).get("download_path"),
        )
        observed = (
            request.task_id,
            request.owner,
            request.generation,
            request.stage,
            request.operation_id,
            request.identity,
            request.input_path,
        )
        if expected != observed:
            logger.warning("activity envelope does not match persisted intent")
            await queue.ack(receipt)
            return True
        token = uuid4()
        if not await self.pool.fetchval("SELECT claim_activity($1,$2)", request.attempt_id, token):
            await queue.ack(receipt)
            return True
        await self._run_claimed(request, token, queue, receipt)
        return True

    async def _run_claimed(self, request, token, queue, receipt) -> None:
        running = asyncio.create_task(self.execute(request))
        heartbeat = asyncio.create_task(self._heartbeat(request, token, running))
        try:
            result = await running
            await self.pool.fetchval("SELECT report_activity($1::jsonb)", result.model_dump_json())
            # A stale report cannot advance execution. Its receipt may be dropped;
            # Maxwell owns reconciliation of the current generation.
            await queue.ack(receipt)
        except asyncio.CancelledError:
            # Lease loss cancels the activity, not the long-lived worker. An
            # external cancellation of run_one still propagates to shutdown.
            if not heartbeat.done() or heartbeat.cancelled():
                raise
        except Exception:
            # Keep the receipt in processing if the result cannot be committed.
            # Only Maxwell decides when the activity may be reconciled again.
            logger.warning("activity result could not be committed; awaiting authority recovery")
        finally:
            heartbeat.cancel()
            running.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
            with suppress(asyncio.CancelledError):
                await running

    async def _heartbeat(self, request, token, running) -> None:
        while True:
            await asyncio.sleep(15)  # asyncio uses a monotonic clock
            try:
                owned = await self.pool.fetchval("SELECT heartbeat_activity($1,$2)", request.attempt_id, token)
            except Exception:
                owned = False
            if not owned:
                running.cancel()
                return
