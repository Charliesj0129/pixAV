"""Runner for the managed playback activity worker.

The cold child only receives a fresh workspace and account-bound cookies over
stdin. The parent uses the retained runtime's UI fence, the Docker daemon,
verified read-back inputs and the writable playback cache, without staging.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import asyncpg
import redis.asyncio as aioredis

from pixav.config import Settings
from pixav.pixel_injector.playback_activity import PhotosPlaybackMaterializer, PlaybackActivityWorker
from pixav.shared.disk import build_download_space_guard
from pixav.shared.pause import is_paused_value
from pixav.shared.queue import TaskQueue
from pixav.shared.workflow import PLAYBACK_QUEUE, require_workflow_role

logger = logging.getLogger(__name__)


def build_worker(pool: asyncpg.Pool, settings: Settings) -> PlaybackActivityWorker:
    native = None
    if settings.pixel_injector_mode == "windows_android":
        from pixav.pixel_injector.storage_worker import _owner, windows_runtime

        native = windows_runtime(pool, settings, owner=_owner(settings))
    cache_root = Path(settings.playback_cache_dir)
    materializer = PhotosPlaybackMaterializer(
        cache_root,
        image=settings.storage_readback_image,
        host_project_root=settings.host_project_root,
        authentication_secret=settings.photos_cold_auth_secret,
        ui_pool=pool,
        owner=settings.storage_worker_owner,
        windows_runtime=native,
    )
    return PlaybackActivityWorker(
        pool,
        materializer,
        cache_root=cache_root,
        readback_root=Path(settings.storage_readback_dir),
    )


async def run_playback_activity_worker(
    pool: asyncpg.Pool, redis: aioredis.Redis, settings: Settings, *, poll_seconds: float = 1.0
) -> None:
    """Claim and report playback activities until cancelled."""
    await require_workflow_role(pool, "pixav_activity_worker")
    disk_guard = build_download_space_guard(settings, redis)
    worker = build_worker(pool, settings)
    queue = TaskQueue(redis=redis, queue_name=PLAYBACK_QUEUE)
    recovered = False
    logger.info("managed playback worker started on %s", PLAYBACK_QUEUE)
    try:
        while True:
            if is_paused_value(await redis.get(settings.system_pause_key)):
                await asyncio.sleep(5)
                continue
            disk_status = await disk_guard.check_and_latch()
            if disk_status.paused or is_paused_value(await redis.get(settings.system_pause_key)):
                await asyncio.sleep(5)
                continue
            if not recovered:
                await queue.requeue_inflight()
                recovered = True
            await worker.run_one(queue)
            await asyncio.sleep(poll_seconds)
    finally:
        if isinstance(worker.materializer, PhotosPlaybackMaterializer) and worker.materializer._windows_runtime:
            worker.materializer._windows_runtime.control.close()


async def run_from_settings(settings: Settings, *, health_state: Any = None) -> None:
    """Open the worker's own pool and queue; the login must be an activity worker."""
    from pixav.shared.db import create_pool
    from pixav.shared.redis_client import create_redis

    pool = await create_pool(settings)
    redis = await create_redis(settings)
    try:
        if health_state is not None:
            health_state.mark_ready()
        await run_playback_activity_worker(pool, redis, settings)
    finally:
        await redis.aclose()
        await pool.close()


def main() -> None:
    """Entry point for ``python -m pixav.pixel_injector.playback_worker``."""
    from pixav.config import get_settings
    from pixav.shared.health import HealthState, create_health_app
    from pixav.shared.health_server import run_with_health

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    health_state = HealthState("playback_worker", stale_after_seconds=settings.heartbeat_stale_seconds)
    health_app = create_health_app("playback_worker", state=health_state)

    async def _run() -> None:
        await run_with_health(
            worker_coro=run_from_settings(settings, health_state=health_state),
            health_app=health_app,
            host=settings.health_host,
            port=settings.playback_worker_health_port,
            health_state=health_state,
            heartbeat_interval_seconds=settings.heartbeat_interval_seconds,
        )

    asyncio.run(_run())


if __name__ == "__main__":
    main()
