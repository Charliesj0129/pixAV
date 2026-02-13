"""Queue-driven worker for Media-Loader downloading pipeline."""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from pixav.config import Settings, get_settings
from pixav.media_loader.metadata import StashMetadataScraper
from pixav.media_loader.qbittorrent import QBitClient
from pixav.media_loader.remuxer import FFmpegRemuxer
from pixav.media_loader.service import MediaLoaderService
from pixav.shared.db import create_pool
from pixav.shared.enums import TaskState
from pixav.shared.models import Task
from pixav.shared.queue import TaskQueue
from pixav.shared.redis_client import create_redis
from pixav.shared.repository import TaskRepository, VideoRepository

logger = logging.getLogger(__name__)


async def run_loop(settings: Settings) -> None:
    """Consume tasks from the download queue and process them.

    BLPOP loop: waits for messages on ``pixav:download``, deserialises,
    and passes to ``MediaLoaderService.process_task()``.
    """
    pool = await create_pool(settings)
    redis = await create_redis(settings)

    try:
        video_repo = VideoRepository(pool)
        task_repo = TaskRepository(pool)
        download_queue = TaskQueue(redis=redis, queue_name=settings.queue_download)
        upload_queue = TaskQueue(redis=redis, queue_name=settings.queue_upload)

        client = QBitClient(
            base_url=settings.qbit_url,
            username=settings.qbit_user,
            password=settings.qbit_password,
            download_dir=settings.download_dir,
        )
        remuxer = FFmpegRemuxer()
        scraper = StashMetadataScraper(settings.stash_url) if settings.stash_url else None

        service = MediaLoaderService(
            client=client,
            remuxer=remuxer,
            scraper=scraper,
            video_repo=video_repo,
            task_repo=task_repo,
            upload_queue=upload_queue,
            output_dir=settings.download_dir,
        )

        logger.info("media-loader worker started, listening on %s", download_queue.name)

        while True:
            payload = await download_queue.pop(timeout=5)
            if payload is None:
                continue

            task_id_raw = payload.get("task_id") or payload.get("video_id")
            if not isinstance(task_id_raw, str):
                logger.warning("invalid payload (no task_id): %s", payload)
                continue

            video_id_raw = payload.get("video_id", task_id_raw)
            if not isinstance(video_id_raw, str):
                logger.warning("invalid payload (non-string video_id): %s", payload)
                continue

            video_id = _parse_uuid(video_id_raw)
            if video_id is None:
                logger.warning("invalid payload (bad video_id=%r): %s", video_id_raw, payload)
                continue

            task_id = _parse_uuid(task_id_raw) or uuid.uuid4()

            task = Task(
                id=task_id,
                video_id=video_id,
                state=TaskState.PENDING,
                queue_name=settings.queue_download,
            )

            try:
                result = await service.process_task(task)
                logger.info(
                    "task %s result: %s",
                    result.id,
                    result.state.value,
                )
            except Exception as exc:
                logger.exception("unexpected error processing task %s: %s", task.id, exc)

    finally:
        await redis.aclose()
        await pool.close()


def _parse_uuid(val: Any) -> uuid.UUID | None:
    """Parse UUID string to UUID object, returning None on invalid values."""
    if not isinstance(val, str):
        return None
    try:
        return uuid.UUID(val)
    except (ValueError, AttributeError):
        return None


def main() -> None:
    """Entry point for ``python -m pixav.media_loader.worker``."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    asyncio.run(run_loop(settings))


if __name__ == "__main__":
    main()
