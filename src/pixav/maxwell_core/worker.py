"""Periodic worker for Maxwell-Core orchestrator."""

from __future__ import annotations

import asyncio
import logging

from pixav.config import Settings, get_settings
from pixav.maxwell_core.backpressure import QueueDepthMonitor
from pixav.maxwell_core.dispatcher import RedisTaskDispatcher
from pixav.maxwell_core.gc import OrphanTaskCleaner
from pixav.maxwell_core.orchestrator import MaxwellOrchestrator
from pixav.maxwell_core.scheduler import LruAccountScheduler
from pixav.shared.db import create_pool
from pixav.shared.queue import TaskQueue
from pixav.shared.redis_client import create_redis
from pixav.shared.repository import TaskRepository, VideoRepository

logger = logging.getLogger(__name__)


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
                stats = await orchestrator.tick()
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
