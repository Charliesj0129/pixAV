"""Runner for the managed storage activity worker.

The Photos guest is a retained, owned runtime: creating one is an external side
effect that is journalled before it happens, exactly like an upload. That
reconciliation lives in :mod:`pixav.pixel_injector.managed_runtime`, which this
runner uses by default. A caller may still inject its own runtime factory — the
contract tests do — but no path here substitutes a different upload environment
for the configured Pixel-compatible one.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import asyncpg
import redis.asyncio as aioredis

from pixav.config import Settings
from pixav.pixel_injector.managed_runtime import ManagedRuntime
from pixav.pixel_injector.photos_storage import (
    PIXEL_COMPATIBLE_MODE,
    MaestroSegmentUploader,
    PhotosColdReadback,
)
from pixav.pixel_injector.storage_activity import StorageActivityWorker
from pixav.shared.pause import is_paused_value
from pixav.shared.queue import TaskQueue
from pixav.shared.workflow import STORAGE_QUEUE, require_workflow_role

logger = logging.getLogger(__name__)


def default_runtime(pool: asyncpg.Pool, settings: Settings, *, owner: str) -> Callable[[Any], Any]:
    """The journalled retained guest this worker owns."""
    import docker

    return ManagedRuntime(
        pool,
        cast(Any, docker).from_env(),
        owner=owner,
        staging_root=Path(settings.storage_staging_dir),
        guest_data_root=Path(settings.storage_guest_data_dir),
        profile_name=settings.redroid_profile,
        profiles_path=settings.redroid_profiles_path or None,
        host_project_root=settings.host_project_root,
    ).acquire


def build_worker(
    pool: asyncpg.Pool,
    settings: Settings,
    runtime: Callable[[Any], Any] | None = None,
    *,
    owner: str,
) -> StorageActivityWorker:
    """Assemble the worker around the configured Pixel-compatible environment."""
    if settings.pixel_injector_mode != PIXEL_COMPATIBLE_MODE:
        raise RuntimeError("managed storage requires the Pixel-compatible upload environment")
    uploader = MaestroSegmentUploader(
        runtime if runtime is not None else default_runtime(pool, settings, owner=owner),
        owner=owner,
        flows=Path(settings.storage_flows_dir),
        mode=settings.pixel_injector_mode,
        staging_root=Path(settings.storage_staging_dir),
    )
    readback = PhotosColdReadback(
        Path(settings.storage_readback_dir),
        image=settings.storage_readback_image,
        source_root=Path(__file__).resolve().parents[2],
        host_project_root=settings.host_project_root,
    )
    return StorageActivityWorker(pool, uploader, readback)


async def run_storage_activity_worker(
    pool: asyncpg.Pool,
    redis: aioredis.Redis,
    settings: Settings,
    runtime: Callable[[Any], Any] | None = None,
    *,
    owner: str,
    poll_seconds: float = 1.0,
) -> None:
    """Claim and report storage activities until cancelled."""
    await require_workflow_role(pool, "pixav_activity_worker")
    worker = build_worker(pool, settings, runtime, owner=owner)
    queue = TaskQueue(redis=redis, queue_name=STORAGE_QUEUE)
    await queue.requeue_inflight()
    logger.info("managed storage worker started on %s", STORAGE_QUEUE)
    while True:
        if is_paused_value(await redis.get(settings.system_pause_key)):
            await asyncio.sleep(5)
            continue
        await worker.run_one(queue)
        await asyncio.sleep(poll_seconds)


def _owner(settings: Settings) -> str:
    """The stable identity of the guest this deployment retains."""
    try:
        return str(uuid.UUID(settings.storage_worker_owner.strip()))
    except (AttributeError, ValueError):
        raise RuntimeError(
            "PIXAV_STORAGE_WORKER_OWNER must be a UUID; the retained guest is keyed by it "
            "and a new value each start would provision a second signed-in device"
        ) from None


async def run_from_settings(settings: Settings, *, health_state: Any = None) -> None:
    """Open the worker's own pool and queue, then claim storage activities.

    This runner exists because the activity worker and the execution authority
    cannot share a database login: ``require_workflow_role`` refuses a login
    that is a member of both group roles, and the legacy pixel_injector loop
    needs writes the activity role must never hold.
    """
    from pixav.shared.db import create_pool
    from pixav.shared.redis_client import create_redis

    owner = _owner(settings)
    pool = await create_pool(settings)
    redis = await create_redis(settings)
    try:
        if health_state is not None:
            health_state.mark_ready()
        await run_storage_activity_worker(pool, redis, settings, owner=owner)
    finally:
        await redis.aclose()
        await pool.close()


def main() -> None:
    """Entry point for ``python -m pixav.pixel_injector.storage_worker``."""
    from pixav.config import get_settings
    from pixav.shared.health import HealthState, create_health_app
    from pixav.shared.health_server import run_with_health

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    health_state = HealthState("storage_worker", stale_after_seconds=settings.heartbeat_stale_seconds)
    health_app = create_health_app("storage_worker", state=health_state)

    async def _run() -> None:
        await run_with_health(
            worker_coro=run_from_settings(settings, health_state=health_state),
            health_app=health_app,
            host=settings.health_host,
            port=settings.storage_worker_health_port,
            health_state=health_state,
            heartbeat_interval_seconds=settings.heartbeat_interval_seconds,
        )

    asyncio.run(_run())


if __name__ == "__main__":
    main()
