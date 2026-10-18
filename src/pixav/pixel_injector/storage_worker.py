"""Managed storage entry for a registered Windows device and persistent UI fence.

Legacy dependency injection remains available to contract tests. The deployed
entry cannot provision redroid or replace a missing retained device/tools.
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
from pixav.pixel_injector.photos_storage import (
    PIXEL_COMPATIBLE_MODE,
    MaestroSegmentUploader,
    PhotosColdReadback,
)
from pixav.pixel_injector.storage_activity import SegmentUploader, StorageActivityWorker
from pixav.pixel_injector.windows_control import WindowsHostClient
from pixav.pixel_injector.windows_managed_runtime import WindowsManagedRuntime
from pixav.pixel_injector.windows_storage import WINDOWS_PIXEL_MODE, WindowsSegmentUploader
from pixav.shared.disk import build_download_space_guard
from pixav.shared.pause import is_paused_value
from pixav.shared.queue import TaskQueue
from pixav.shared.workflow import STORAGE_QUEUE, require_workflow_role

logger = logging.getLogger(__name__)


def default_runtime(
    pool: asyncpg.Pool, settings: Settings, *, owner: str, existing: bool = False
) -> Callable[[Any], Any]:
    """The retired managed redroid entry cannot provision a replacement guest."""
    raise RuntimeError("managed redroid provisioning is retired; configure the registered Windows runtime")


def windows_runtime(pool: asyncpg.Pool, settings: Settings, *, owner: str) -> WindowsManagedRuntime:
    import docker

    try:
        runtime_id, runtime_owner = uuid.UUID(settings.storage_windows_runtime_id), uuid.UUID(owner)
    except ValueError:
        raise RuntimeError("Windows managed storage requires registered runtime and owner UUIDs") from None
    if not all(
        (
            settings.windows_control_url,
            settings.windows_control_ca,
            settings.windows_control_certificate,
            settings.windows_control_key,
            settings.windows_control_docker_daemon_id,
        )
    ):
        raise RuntimeError("Windows native mutual-TLS transport is not configured")
    control = WindowsHostClient(
        settings.windows_control_url,
        ca=Path(settings.windows_control_ca),
        certificate=Path(settings.windows_control_certificate),
        key=Path(settings.windows_control_key),
    )
    return WindowsManagedRuntime(
        pool,
        cast(Any, docker).from_env(),
        control,
        runtime_id=runtime_id,
        generation=settings.storage_windows_runtime_generation,
        owner=runtime_owner,
        staging_root=Path(settings.storage_staging_dir),
        profile=settings.storage_windows_profile,
        server_host=settings.storage_android_adb_server_host,
        docker_daemon_id=settings.windows_control_docker_daemon_id,
    )


def build_worker(
    pool: asyncpg.Pool,
    settings: Settings,
    runtime: Callable[[Any], Any] | None = None,
    *,
    owner: str,
) -> StorageActivityWorker:
    """Assemble the worker around the configured Pixel-compatible environment."""
    if settings.pixel_injector_mode not in {PIXEL_COMPATIBLE_MODE, WINDOWS_PIXEL_MODE}:
        raise RuntimeError("managed storage requires the Pixel-compatible upload environment")

    async def observation_clock():
        return await pool.fetchval("SELECT clock_timestamp()")

    uploader: SegmentUploader
    managed = None
    if settings.pixel_injector_mode == WINDOWS_PIXEL_MODE:
        if runtime is not None:
            raise RuntimeError("Windows managed storage requires its registered runtime factory")
        managed = windows_runtime(pool, settings, owner=owner)
        uploader = WindowsSegmentUploader(
            managed, flows=Path(settings.storage_flows_dir), observation_clock=observation_clock
        )
    else:
        uploader = MaestroSegmentUploader(
            runtime if runtime is not None else default_runtime(pool, settings, owner=owner),
            owner=owner,
            flows=Path(settings.storage_flows_dir),
            mode=settings.pixel_injector_mode,
            staging_root=Path(settings.storage_staging_dir),
            observation_clock=observation_clock,
            ui_pool=pool,
            existing_runtime=(
                runtime if runtime is not None else default_runtime(pool, settings, owner=owner, existing=True)
            ),
        )
    readback = PhotosColdReadback(
        Path(settings.storage_readback_dir),
        image=settings.storage_readback_image,
        host_project_root=settings.host_project_root,
        authentication_secret=settings.photos_cold_auth_secret,
        ui_pool=pool,
        owner=owner,
        windows_runtime=managed if settings.pixel_injector_mode == WINDOWS_PIXEL_MODE else None,
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
    disk_guard = build_download_space_guard(settings, redis)
    worker = build_worker(pool, settings, runtime, owner=owner)
    queue = TaskQueue(redis=redis, queue_name=STORAGE_QUEUE)
    recovered = False
    logger.info("managed storage worker started on %s", STORAGE_QUEUE)
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
        if isinstance(worker.uploader, WindowsSegmentUploader):
            worker.uploader.runtime.control.close()


def _owner(settings: Settings) -> str:
    """The stable owner of the registered runtime this deployment retains."""
    try:
        return str(uuid.UUID(settings.storage_worker_owner.strip()))
    except (AttributeError, ValueError):
        raise RuntimeError("PIXAV_STORAGE_WORKER_OWNER must be a stable UUID matching the registered runtime") from None


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
