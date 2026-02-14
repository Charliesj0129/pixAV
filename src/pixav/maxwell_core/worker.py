"""Periodic worker for Maxwell-Core orchestrator."""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

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


async def ingest_crawl_queue(
    *,
    crawl_queue: TaskQueue,
    task_repo: TaskRepository,
    video_repo: VideoRepository,
    download_queue_name: str,
    batch_size: int = 100,
) -> int:
    """Drain crawl queue payloads and create pending download tasks."""
    created = 0
    for _ in range(batch_size):
        payload = await crawl_queue.pop(timeout=1)
        if payload is None:
            break

        video_id = _parse_uuid(payload.get("video_id"))
        if video_id is None:
            logger.warning("skip crawl payload with invalid video_id: %s", payload)
            continue

        video = await video_repo.find_by_id(video_id)
        if video is None:
            logger.warning("skip crawl payload for missing video %s", video_id)
            continue

        if await task_repo.has_open_task(video_id):
            logger.info("skip crawl payload; open task already exists for video %s", video_id)
            continue

        await task_repo.insert(
            Task(
                video_id=video_id,
                state=TaskState.PENDING,
                queue_name=download_queue_name,
            )
        )
        created += 1

    return created


async def run_loop(settings: Settings, *, interval: int = 30) -> None:
    """Run the Maxwell orchestrator tick loop.

    Args:
        settings: Application settings.
        interval: Seconds between ticks (default: 30).
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
        )

        logger.info("maxwell-core worker started (interval=%ds)", interval)

        while True:
            try:
                created = await ingest_crawl_queue(
                    crawl_queue=crawl_queue,
                    task_repo=task_repo,
                    video_repo=video_repo,
                    download_queue_name=settings.queue_download,
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
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    asyncio.run(run_loop(settings))


if __name__ == "__main__":
    main()
