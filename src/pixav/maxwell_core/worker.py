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
from pixav.maxwell_core.gc import OrphanTaskCleaner
from pixav.maxwell_core.orchestrator import MaxwellOrchestrator
from pixav.maxwell_core.scheduler import LruAccountScheduler
from pixav.shared.db import create_pool
from pixav.shared.enums import TaskState
from pixav.shared.models import Task
from pixav.shared.queue import TaskQueue
from pixav.shared.redis_client import create_redis
from pixav.shared.repository import TaskRepository, VideoRepository

logger = logging.getLogger(__name__)


def _parse_uuid(value: Any) -> uuid.UUID | None:
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _is_paused_value(raw: Any) -> bool:
    if raw is None:
        return False
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


async def _is_paused(redis: aioredis.Redis, pause_key: str) -> bool:
    value = await redis.get(pause_key)
    return _is_paused_value(value)


async def ingest_crawl_queue(
    *,
    crawl_queue: TaskQueue,
    task_repo: TaskRepository,
    video_repo: VideoRepository,
    download_queue_name: str,
    max_retries: int = 10,
    batch_size: int = 100,
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
            await crawl_queue.ack(receipt)
            acked = True
        except Exception as exc:
            logger.exception("crawl ingest error: %s", exc)
            if receipt is not None and not acked:
                try:
                    await crawl_queue.nack(receipt, requeue=True)
                except Exception as nack_exc:  # pragma: no cover - defensive logging
                    logger.error("failed to nack crawl payload: %s", nack_exc)

    return created


async def run_loop(settings: Settings, *, interval: int = 30, health_app: Any = None) -> None:
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
        )

        # Expose orchestrator to health app if one was provided
        if health_app is not None:
            health_app.state.orchestrator = orchestrator

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
                )
                stats = await orchestrator.tick()
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
    import uvicorn
    from fastapi import FastAPI, Request
    from fastapi.responses import PlainTextResponse

    from pixav.shared.metrics import get_metrics_output

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()

    health_app = FastAPI(title="pixAV maxwell_core health", docs_url=None, redoc_url=None)
    health_app.state.orchestrator = None

    @health_app.get("/health")
    async def health(request: Request) -> dict[str, Any]:
        orchestrator: MaxwellOrchestrator | None = getattr(request.app.state, "orchestrator", None)
        if orchestrator is not None:
            try:
                return {"status": "ok", "module": "maxwell_core", **(await orchestrator.health())}
            except Exception:  # noqa: S110
                pass
        return {"status": "ok", "module": "maxwell_core"}

    @health_app.get("/metrics", response_class=PlainTextResponse)
    async def metrics() -> PlainTextResponse:
        return PlainTextResponse(
            content=get_metrics_output().decode("utf-8"),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    async def _run() -> None:
        config = uvicorn.Config(
            health_app,
            host=settings.health_host,
            port=settings.maxwell_core_health_port,
            log_level="warning",
            access_log=False,
        )
        server = uvicorn.Server(config)
        worker_task = asyncio.ensure_future(run_loop(settings, health_app=health_app))
        server_task = asyncio.ensure_future(server.serve())
        done, pending = await asyncio.wait([worker_task, server_task], return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: S110
                pass
        for t in done:
            exc = t.exception()
            if exc is not None:
                raise exc

    asyncio.run(_run())


if __name__ == "__main__":
    main()
