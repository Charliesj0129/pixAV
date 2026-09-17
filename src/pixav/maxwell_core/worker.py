"""Periodic worker for Maxwell-Core orchestrator."""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

import redis.asyncio as aioredis

from pixav.config import Settings, get_settings
from pixav.maxwell_core.backpressure import QueueDepthMonitor
from pixav.maxwell_core.dispatcher import RedisTaskDispatcher
from pixav.maxwell_core.gc import LocalFileJanitor, OrphanTaskCleaner
from pixav.maxwell_core.orchestrator import MaxwellOrchestrator
from pixav.maxwell_core.scheduler import LruAccountScheduler
from pixav.shared.db import create_pool
from pixav.shared.disk import DownloadSpaceGuard
from pixav.shared.enums import TaskState
from pixav.shared.metrics import (
    record_source_adapter_error,
    record_task_failed,
    record_task_processed,
    set_executions_source_unavailable,
    set_executions_terminal,
    set_executions_user_action_required,
    set_executions_waiting_quota,
    set_queue_depth,
    set_remote_assets_due_reverification,
)
from pixav.shared.models import Task
from pixav.shared.pause import is_paused_value
from pixav.shared.queue import TaskQueue
from pixav.shared.redis_client import create_redis
from pixav.shared.repository import SourceCandidateRepository, TaskRepository, VideoRepository

logger = logging.getLogger(__name__)

_METRICS_MODULE = "maxwell_core"


def _parse_uuid(value: Any) -> uuid.UUID | None:
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


async def _is_paused(redis: aioredis.Redis, pause_key: str) -> bool:
    value = await redis.get(pause_key)
    return is_paused_value(value)


async def ingest_crawl_queue(  # noqa: C901
    *,
    crawl_queue: TaskQueue,
    task_repo: TaskRepository,
    video_repo: VideoRepository,
    download_queue_name: str,
    max_retries: int = 10,
    batch_size: int = 100,
    managed_workflow: Any = None,
) -> int:
    """Drain crawl queue payloads and create pending download tasks."""
    created = 0
    for _ in range(batch_size):
        receipt: str | None = None
        acked = False
        try:
            claimed = await crawl_queue.pop_claim(timeout=1)
            if claimed is None:
                break
            payload, receipt = claimed

            if payload.get("schema") == "source-observation-v1":
                if managed_workflow is None:
                    raise RuntimeError("managed discovery requires the execution authority")
                try:
                    managed_workflow.source_policy.normalize(payload["observation"])
                except (ValueError, TypeError, KeyError, AttributeError):
                    # Two different facts: the queue item is done with (below),
                    # and a provider payload could not be understood (here).
                    logger.warning("adapter error: INVALID_PROVIDER_PAYLOAD")
                    record_source_adapter_error()
                    record_task_failed(_METRICS_MODULE)
                    await crawl_queue.ack(receipt)
                    acked = True
                    continue
                await managed_workflow.ingest_observation(payload["observation"])
                await crawl_queue.ack(receipt)
                acked = True
                created += 1
                continue

            video_id = _parse_uuid(payload.get("video_id"))
            if video_id is None:
                logger.warning("skip crawl payload with invalid video_id: %s", payload)
                await crawl_queue.ack(receipt)
                acked = True
                continue

            video = await video_repo.find_by_id(video_id)
            if video is None:
                logger.warning("skip crawl payload for missing video %s", video_id)
                await crawl_queue.ack(receipt)
                acked = True
                continue

            if await task_repo.has_open_task(video_id):
                logger.info("skip crawl payload; open task already exists for video %s", video_id)
                await crawl_queue.ack(receipt)
                acked = True
                continue

            new_task = Task(
                video_id=video_id,
                state=TaskState.PENDING,
                queue_name=download_queue_name,
                max_retries=max_retries,
            )
            await task_repo.insert(new_task)
            logger.debug("created task %s for video %s (trace_id=%s)", new_task.id, video_id, new_task.trace_id)
            created += 1
            record_task_processed(_METRICS_MODULE)
            await crawl_queue.ack(receipt)
            acked = True
        except Exception as exc:
            record_task_failed(_METRICS_MODULE)
            logger.exception("crawl ingest error: %s", exc)
            if receipt is not None and not acked:
                try:
                    await crawl_queue.nack(receipt, requeue=True)
                except Exception as nack_exc:  # pragma: no cover - defensive logging
                    logger.error("failed to nack crawl payload: %s", nack_exc)

    return created


async def _publish_queue_depths(crawl_queue: TaskQueue, queues: dict[str, TaskQueue]) -> None:
    """Export queued + in-flight depth for every pipeline queue as a gauge."""
    for queue in (crawl_queue, *queues.values()):
        try:
            set_queue_depth(queue.name, await queue.total_depth())
        except Exception as exc:  # pragma: no cover - metrics must never break the tick
            logger.warning("failed to publish depth for queue %s: %s", queue.name, exc)


async def _publish_execution_metrics(managed: Any, *, reverify_interval_days: int = 0) -> None:
    """Export the managed execution states an operator has to tell apart."""
    if managed is None:
        return
    try:
        counts = await managed.observe_states()
        due = await managed.storage.due_reverification(interval_days=reverify_interval_days)
    except Exception as exc:  # pragma: no cover - metrics must never break the tick
        logger.warning("failed to publish execution state metrics: %s", exc)
        return
    set_executions_waiting_quota(counts["waiting_quota"])
    set_executions_source_unavailable(counts["source_unavailable"])
    set_executions_user_action_required(counts["user_action_required"])
    set_executions_terminal(counts["terminal"])
    set_remote_assets_due_reverification(due)


async def _open_reverifications(managed: Any, *, interval_days: int) -> None:
    """Ask again about durable copies nobody has read back lately.

    A failure here must not stop the tick: re-verification is a background
    assurance activity, and losing it for one cycle changes nothing that has
    already been proven.
    """
    if managed is None or interval_days <= 0:
        return
    try:
        opened = await managed.storage.reverify_due(interval_days=interval_days)
    except Exception as exc:  # pragma: no cover - assurance must never break the tick
        logger.warning("failed to open re-verification executions: %s", exc)
        return
    if opened:
        logger.info("opened %d re-verification execution(s)", len(opened))


async def run_loop(  # noqa: C901
    settings: Settings,
    *,
    interval: int = 30,
    health_app: Any = None,
    health_state: Any = None,
) -> None:
    """Run the Maxwell orchestrator tick loop.

    Args:
        settings:    Application settings.
        interval:    Seconds between ticks (default: 30).
        health_app:  Optional FastAPI app; if provided, the orchestrator is
                     mounted on ``health_app.state.orchestrator`` so the
                     ``/health`` endpoint can expose live scheduling status.
    """
    pool = await create_pool(settings)
    redis = await create_redis(settings)

    try:
        managed = None
        managed_queue = None
        storage_queue = None
        if settings.managed_media_workflow:
            from pixav.maxwell_core.media_workflow import MediaWorkflow
            from pixav.shared.retry import parse_retry_backoff
            from pixav.shared.workflow import MANAGED_QUEUE, STORAGE_QUEUE, require_workflow_role
            from pixav.sht_probe.policy import SourcePolicy

            await require_workflow_role(pool, "pixav_execution_authority")
            managed = MediaWorkflow(
                pool,
                backoff=parse_retry_backoff(settings.retry_backoff_seconds),
                cooldown_seconds=settings.source_candidate_cooldown_hours * 3600,
                max_retries=settings.download_max_retries,
                source_policy=SourcePolicy(min_score=settings.source_min_quality_score),
            )
            managed_queue = TaskQueue(redis=redis, queue_name=MANAGED_QUEUE)
            storage_queue = TaskQueue(redis=redis, queue_name=STORAGE_QUEUE)
        task_repo = TaskRepository(pool)
        video_repo = VideoRepository(pool)

        queues = {
            settings.queue_download: TaskQueue(redis=redis, queue_name=settings.queue_download),
            settings.queue_upload: TaskQueue(redis=redis, queue_name=settings.queue_upload),
        }
        crawl_queue = TaskQueue(redis=redis, queue_name=settings.queue_crawl)
        try:
            recovered = int(await crawl_queue.requeue_inflight())
        except (TypeError, ValueError):
            recovered = 0
        if recovered:
            logger.warning("requeued %d in-flight crawl payload(s)", recovered)

        scheduler = LruAccountScheduler(pool)
        dispatcher = RedisTaskDispatcher(task_repo=task_repo, queues=queues)
        monitor = QueueDepthMonitor(queues=queues)
        cleaner = OrphanTaskCleaner(pool)
        janitor = LocalFileJanitor(
            pool,
            download_dir=settings.download_dir,
            batch_size=settings.local_cleanup_batch_size,
            apply_deletions=settings.local_cleanup_apply,
        )
        disk_guard = DownloadSpaceGuard(
            redis,
            path=settings.download_dir,
            pause_key=settings.download_pause_key,
            min_free_bytes=settings.download_min_free_bytes,
            min_free_percent=settings.download_min_free_percent,
        )

        orchestrator = MaxwellOrchestrator(
            scheduler=scheduler,
            dispatcher=dispatcher,
            monitor=monitor,
            cleaner=cleaner,
            task_repo=task_repo,
            video_repo=video_repo,
            download_queue_name=settings.queue_download,
            upload_queue_name=settings.queue_upload,
            no_account_policy=settings.no_account_policy,
            janitor=janitor,
            candidate_repo=SourceCandidateRepository(pool),
        )

        # Expose orchestrator to health app if one was provided
        if health_app is not None:
            health_app.state.orchestrator = orchestrator
        if health_state is not None:
            health_state.mark_ready()

        logger.info("maxwell-core worker started (interval=%ds)", interval)

        while True:
            try:
                if await _is_paused(redis, settings.system_pause_key):
                    logger.info("system paused via redis key %s; skip tick", settings.system_pause_key)
                    await asyncio.sleep(min(interval, 5))
                    continue

                created = await ingest_crawl_queue(
                    crawl_queue=crawl_queue,
                    task_repo=task_repo,
                    video_repo=video_repo,
                    download_queue_name=settings.queue_download,
                    max_retries=settings.download_max_retries,
                    managed_workflow=managed,
                )
                disk_status = await disk_guard.check_and_latch()
                if managed is not None:
                    await managed.tick(managed_queue, download_paused=disk_status.paused, storage_queue=storage_queue)
                stats = await orchestrator.tick(download_paused=disk_status.paused)
                await _open_reverifications(managed, interval_days=settings.remote_reverify_interval_days)
                await _publish_queue_depths(crawl_queue, queues)
                await _publish_execution_metrics(managed, reverify_interval_days=settings.remote_reverify_interval_days)
                if created:
                    logger.info("ingested %d crawl payload(s) into tasks", created)
                logger.info("tick result: %s", stats)
            except Exception as exc:
                logger.exception("tick error: %s", exc)
            await asyncio.sleep(interval)

    finally:
        await redis.aclose()
        await pool.close()


def main() -> None:
    """Entry point for ``python -m pixav.maxwell_core.worker``."""
    from pixav.shared.health import HealthState, create_health_app
    from pixav.shared.health_server import run_with_health

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()

    health_state = HealthState("maxwell_core", stale_after_seconds=settings.heartbeat_stale_seconds)
    health_app = create_health_app("maxwell_core", state=health_state)

    async def _run() -> None:
        await run_with_health(
            worker_coro=run_loop(settings, health_app=health_app, health_state=health_state),
            health_app=health_app,
            host=settings.health_host,
            port=settings.maxwell_core_health_port,
            health_state=health_state,
            heartbeat_interval_seconds=settings.heartbeat_interval_seconds,
        )

    asyncio.run(_run())


if __name__ == "__main__":
    main()
