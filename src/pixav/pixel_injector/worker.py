"""Queue consumer worker for pixel_injector."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from pydantic import ValidationError

from pixav.config import Settings, get_settings
from pixav.pixel_injector.adb import AdbConnection
from pixav.pixel_injector.redroid import DockerRedroidManager
from pixav.pixel_injector.service import PixelInjectorService
from pixav.pixel_injector.uploader import UIAutomatorUploader
from pixav.pixel_injector.verifier import GooglePhotosVerifier
from pixav.shared.enums import TaskState
from pixav.shared.models import Task
from pixav.shared.queue import TaskQueue
from pixav.shared.redis_client import create_redis

logger = logging.getLogger(__name__)


def _task_from_payload(payload: dict[str, Any]) -> Task:
    """Convert queue payload into a typed Task model."""
    normalized = dict(payload)
    if "task_id" in normalized and "id" not in normalized:
        normalized["id"] = normalized.pop("task_id")
    return Task.model_validate(normalized)


async def run_worker(
    queue: TaskQueue,
    service: PixelInjectorService,
    *,
    poll_timeout: int = 5,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Run the BLPOP consumer loop for the upload queue."""
    logger.info("pixel injector worker starting on queue %s", queue.name)
    while True:
        if stop_event is not None and stop_event.is_set():
            logger.info("stop_event set; shutting down worker")
            return

        try:
            payload = await queue.pop(timeout=poll_timeout)
            if payload is None:
                continue

            task = _task_from_payload(payload)
            result = await service.process_task(task)
            if result.state == TaskState.COMPLETE:
                logger.info("task %s complete", result.id)
            else:
                logger.error("task %s failed: %s", result.id, result.error_message)
        except ValidationError as exc:
            logger.error("invalid upload payload: %s", exc)
        except Exception as exc:  # pragma: no cover - long running worker resilience
            logger.exception("worker loop error: %s", exc)
            await asyncio.sleep(1)


async def run_from_settings(settings: Settings) -> None:
    """Wire dependencies from settings and start the worker loop."""
    redis = await create_redis(settings)
    queue = TaskQueue(redis=redis, queue_name=settings.queue_upload)
    adb = AdbConnection()
    service = PixelInjectorService(
        redroid=DockerRedroidManager(settings.redroid_image),
        uploader=UIAutomatorUploader(adb=adb),
        verifier=GooglePhotosVerifier(adb=adb),
    )
    try:
        await run_worker(queue=queue, service=service)
    finally:
        await redis.aclose()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(run_from_settings(get_settings()))


if __name__ == "__main__":
    main()
