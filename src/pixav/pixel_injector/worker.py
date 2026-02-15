"""Queue consumer worker for pixel_injector."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import redis.asyncio as aioredis
from pydantic import ValidationError

from pixav.config import Settings, get_settings
from pixav.pixel_injector.adb import AdbConnection
from pixav.pixel_injector.interfaces import PixelInjector
from pixav.pixel_injector.redroid import DockerRedroidManager
from pixav.pixel_injector.service import LocalPixelInjectorService, PixelInjectorService
from pixav.pixel_injector.uploader import UIAutomatorUploader
from pixav.pixel_injector.verifier import GooglePhotosVerifier
from pixav.shared.db import create_pool
from pixav.shared.enums import TaskState, VideoStatus
from pixav.shared.models import Task
from pixav.shared.queue import TaskQueue
from pixav.shared.redis_client import create_redis
from pixav.shared.repository import AccountRepository, TaskRepository, VideoRepository

logger = logging.getLogger(__name__)


def _task_from_payload(payload: dict[str, Any], *, default_max_retries: int) -> Task:
    """Convert queue payload into a typed Task model."""
    normalized = dict(payload)
    if "task_id" in normalized and "id" not in normalized:
        normalized["id"] = normalized.pop("task_id")
    normalized.setdefault("retries", 0)
    normalized.setdefault("max_retries", default_max_retries)
    return Task.model_validate(normalized)


def _build_retry_payload(task: Task, retries: int) -> dict[str, str | int]:
    payload: dict[str, str | int] = {
        "task_id": str(task.id),
        "video_id": str(task.video_id),
        "queue_name": task.queue_name,
        "retries": retries,
        "max_retries": task.max_retries,
    }
    if task.local_path:
        payload["local_path"] = task.local_path
    if task.account_id is not None:
        payload["account_id"] = str(task.account_id)
    return payload


def _build_dlq_payload(task: Task, error_message: str) -> dict[str, str | int]:
    payload: dict[str, str | int] = {
        "task_id": str(task.id),
        "video_id": str(task.video_id),
        "queue_name": task.queue_name,
        "stage": "upload",
        "attempts": task.retries,
        "max_retries": task.max_retries,
        "error_message": error_message,
        "failed_at": datetime.now(timezone.utc).isoformat(),
        "dlq_replays": 0,
    }
    if task.account_id is not None:
        payload["account_id"] = str(task.account_id)
    return payload


def _is_retryable_failure(error_message: str) -> bool:
    lowered = error_message.lower()
    non_retryable_tokens = (
        "local_path is required",
        "local_path is missing",
    )
    return not any(token in lowered for token in non_retryable_tokens)


def _parse_backoff_seconds(raw: str) -> tuple[int, ...]:
    values: list[int] = []
    for token in raw.split(","):
        stripped = token.strip()
        if not stripped:
            continue
        try:
            parsed = int(stripped)
        except ValueError:
            continue
        if parsed > 0:
            values.append(parsed)
    if not values:
        return (60, 300, 900)
    return tuple(values)


def _is_paused_value(raw: Any) -> bool:
    if raw is None:
        return False
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _safe_uuid(value: Any) -> uuid.UUID | None:
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


async def _is_paused(redis_client: aioredis.Redis, pause_key: str) -> bool:
    value = await redis_client.get(pause_key)
    return _is_paused_value(value)


async def _acquire_upload_lock(
    redis_client: aioredis.Redis,
    *,
    lock_key: str,
    lock_token: str,
    ttl_seconds: int,
) -> bool:
    result = await redis_client.set(lock_key, lock_token, ex=ttl_seconds, nx=True)
    return bool(result)


async def _release_upload_lock(
    redis_client: aioredis.Redis,
    *,
    lock_key: str,
    lock_token: str,
) -> None:
    holder = await redis_client.get(lock_key)
    if holder == lock_token:
        await redis_client.delete(lock_key)


async def _schedule_dlq_replay(
    *,
    redis_client: aioredis.Redis,
    schedule_key: str,
    dlq_payload: dict[str, str | int],
    backoff_seconds: tuple[int, ...],
    max_replays: int,
) -> bool:
    error_message = str(dlq_payload.get("error_message", ""))
    if not _is_retryable_failure(error_message):
        return False

    current_replays = int(dlq_payload.get("dlq_replays", 0))
    if current_replays >= max_replays:
        return False

    backoff_idx = min(current_replays, len(backoff_seconds) - 1)
    delay = backoff_seconds[backoff_idx]

    payload = dict(dlq_payload)
    payload["dlq_replays"] = current_replays + 1

    score = int(time.time()) + delay
    await redis_client.zadd(schedule_key, {json.dumps(payload, sort_keys=True): score})
    return True


async def _replay_due_dlq(
    *,
    redis_client: aioredis.Redis,
    schedule_key: str,
    retry_queue: TaskQueue,
    task_repo: TaskRepository | None,
    video_repo: VideoRepository | None,
    default_max_retries: int,
    queue_name: str,
    max_items: int = 20,
) -> int:
    now = int(time.time())
    due_items = await redis_client.zrangebyscore(
        schedule_key,
        min="-inf",
        max=now,
        start=0,
        num=max_items,
    )

    replayed = 0
    for raw in due_items:
        removed = await redis_client.zrem(schedule_key, raw)
        if removed == 0:
            continue

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("skip malformed scheduled dlq payload: %s", raw)
            continue

        task_id = payload.get("task_id")
        video_id = payload.get("video_id")
        if not isinstance(task_id, str) or not isinstance(video_id, str):
            logger.warning("skip scheduled dlq payload with missing IDs: %s", payload)
            continue

        replay_payload: dict[str, str | int] = {
            "task_id": task_id,
            "video_id": video_id,
            "queue_name": queue_name,
            "retries": 0,
            "max_retries": default_max_retries,
        }
        account_id = payload.get("account_id")
        if isinstance(account_id, str):
            replay_payload["account_id"] = account_id

        await retry_queue.push(replay_payload)

        task_uuid = _safe_uuid(task_id)
        video_uuid = _safe_uuid(video_id)
        replay_count = int(payload.get("dlq_replays", 0))
        if task_repo is not None and task_uuid is not None:
            await task_repo.update_state(
                task_uuid,
                TaskState.PENDING,
                error_message=f"replayed from dlq ({replay_count})",
            )
        if video_repo is not None and video_uuid is not None:
            await video_repo.update_status(video_uuid, VideoStatus.DOWNLOADED)

        replayed += 1

    return replayed


async def _hydrate_local_path(task: Task, *, video_repo: VideoRepository | None) -> Task:
    if task.local_path:
        return task
    if video_repo is None:
        return task

    video = await video_repo.find_by_id(task.video_id)
    if video is None or not video.local_path:
        return task
    return task.model_copy(update={"local_path": video.local_path})


async def _mark_uploading(task: Task, *, task_repo: TaskRepository | None, video_repo: VideoRepository | None) -> None:
    if task_repo is not None:
        await task_repo.update_state(task.id, TaskState.UPLOADING)
    if video_repo is not None:
        await video_repo.update_status(task.video_id, VideoStatus.UPLOADING)


async def _persist_success(
    result: Task,
    *,
    task_repo: TaskRepository | None,
    video_repo: VideoRepository | None,
    account_repo: AccountRepository | None,
) -> None:
    if task_repo is not None:
        await task_repo.update_state(result.id, TaskState.COMPLETE)
    if video_repo is not None:
        await video_repo.update_upload_result(result.video_id, share_url=result.share_url or "")
    if account_repo is not None and result.account_id is not None:
        uploaded_bytes = _uploaded_bytes_from_task(result)
        await account_repo.apply_upload_usage(result.account_id, uploaded_bytes)
    logger.info("task %s complete", result.id)


async def _persist_failure(
    result: Task,
    *,
    task_repo: TaskRepository | None,
    video_repo: VideoRepository | None,
    retry_queue: TaskQueue | None,
    dlq_queue: TaskQueue | None,
) -> dict[str, str | int] | None:
    error_message = result.error_message or "upload stage failed"
    next_retry = result.retries + 1

    if _is_retryable_failure(error_message) and next_retry <= result.max_retries and retry_queue is not None:
        if task_repo is not None:
            await task_repo.set_retry(
                result.id,
                next_retry,
                state=TaskState.PENDING,
                error_message=error_message,
            )
        if video_repo is not None:
            await video_repo.update_status(result.video_id, VideoStatus.DOWNLOADED)
        await retry_queue.push(_build_retry_payload(result, next_retry))
        logger.warning(
            "task %s failed (attempt %d/%d), requeued: %s",
            result.id,
            next_retry,
            result.max_retries,
            error_message,
        )
        return None

    if task_repo is not None:
        await task_repo.update_state(result.id, TaskState.FAILED, error_message=error_message)
    if video_repo is not None:
        await video_repo.update_status(result.video_id, VideoStatus.FAILED)

    dlq_payload = _build_dlq_payload(result, error_message)
    if dlq_queue is not None:
        await dlq_queue.push(dlq_payload)
    logger.error("task %s failed permanently: %s", result.id, error_message)
    return dlq_payload


def _uploaded_bytes_from_task(task: Task) -> int:
    path = task.local_path
    if not path:
        return 0
    try:
        return int(os.path.getsize(path))
    except OSError:
        return 0


async def run_worker(  # noqa: C901
    queue: TaskQueue,
    service: PixelInjector,
    *,
    task_repo: TaskRepository | None = None,
    video_repo: VideoRepository | None = None,
    account_repo: AccountRepository | None = None,
    retry_queue: TaskQueue | None = None,
    dlq_queue: TaskQueue | None = None,
    redis_client: aioredis.Redis | None = None,
    default_max_retries: int = 10,
    poll_timeout: int = 5,
    stop_event: asyncio.Event | None = None,
    pause_key: str = "system:pause",
    enforce_single_flight: bool = True,
    upload_lock_key: str = "pixav:upload:lock",
    upload_lock_ttl_seconds: int = 7200,
    dlq_replay_enabled: bool = True,
    dlq_replay_max: int = 3,
    dlq_replay_backoff_seconds: tuple[int, ...] = (60, 300, 900),
    dlq_replay_schedule_key: str | None = None,
) -> None:
    """Run the BLPOP consumer loop for the upload queue."""
    retry_queue = retry_queue or queue
    if dlq_replay_schedule_key is None:
        dlq_replay_schedule_key = f"{queue.name}:dlq:replay"

    logger.info("pixel injector worker starting on queue %s", queue.name)
    while True:
        if stop_event is not None and stop_event.is_set():
            logger.info("stop_event set; shutting down worker")
            return

        try:
            if redis_client is not None and await _is_paused(redis_client, pause_key):
                logger.info("system paused via redis key %s; skip polling", pause_key)
                await asyncio.sleep(max(1, min(poll_timeout, 5)))
                continue

            if (
                redis_client is not None
                and dlq_replay_enabled
                and dlq_queue is not None
                and retry_queue is not None
                and dlq_replay_max > 0
            ):
                replayed = await _replay_due_dlq(
                    redis_client=redis_client,
                    schedule_key=dlq_replay_schedule_key,
                    retry_queue=retry_queue,
                    task_repo=task_repo,
                    video_repo=video_repo,
                    default_max_retries=default_max_retries,
                    queue_name=queue.name,
                )
                if replayed > 0:
                    logger.warning("replayed %d task(s) from scheduled upload DLQ", replayed)

            payload = await queue.pop(timeout=poll_timeout)
            if payload is None:
                continue

            lock_token: str | None = None
            if enforce_single_flight and redis_client is not None:
                candidate = str(uuid.uuid4())
                acquired = await _acquire_upload_lock(
                    redis_client,
                    lock_key=upload_lock_key,
                    lock_token=candidate,
                    ttl_seconds=upload_lock_ttl_seconds,
                )
                if not acquired:
                    await queue.push(payload)
                    logger.info("upload lock busy (%s), payload requeued", upload_lock_key)
                    await asyncio.sleep(1)
                    continue
                lock_token = candidate

            try:
                task = _task_from_payload(payload, default_max_retries=default_max_retries)
                task = await _hydrate_local_path(task, video_repo=video_repo)
                if not task.local_path:
                    result = task.model_copy(
                        update={
                            "state": TaskState.FAILED,
                            "error_message": "video local_path is missing",
                        }
                    )
                    dlq_payload = await _persist_failure(
                        result,
                        task_repo=task_repo,
                        video_repo=video_repo,
                        retry_queue=retry_queue,
                        dlq_queue=dlq_queue,
                    )
                else:
                    await _mark_uploading(task, task_repo=task_repo, video_repo=video_repo)
                    result = await service.process_task(task)
                    if result.state == TaskState.COMPLETE and result.share_url:
                        await _persist_success(
                            result,
                            task_repo=task_repo,
                            video_repo=video_repo,
                            account_repo=account_repo,
                        )
                        dlq_payload = None
                    else:
                        dlq_payload = await _persist_failure(
                            result,
                            task_repo=task_repo,
                            video_repo=video_repo,
                            retry_queue=retry_queue,
                            dlq_queue=dlq_queue,
                        )

                if (
                    dlq_payload is not None
                    and redis_client is not None
                    and dlq_replay_enabled
                    and dlq_replay_max > 0
                    and dlq_queue is not None
                ):
                    scheduled = await _schedule_dlq_replay(
                        redis_client=redis_client,
                        schedule_key=dlq_replay_schedule_key,
                        dlq_payload=dlq_payload,
                        backoff_seconds=dlq_replay_backoff_seconds,
                        max_replays=dlq_replay_max,
                    )
                    if scheduled:
                        logger.warning(
                            "task %s scheduled for delayed DLQ replay",
                            dlq_payload.get("task_id", "unknown"),
                        )
            finally:
                if lock_token is not None and redis_client is not None:
                    await _release_upload_lock(
                        redis_client,
                        lock_key=upload_lock_key,
                        lock_token=lock_token,
                    )
        except ValidationError as exc:
            logger.error("invalid upload payload: %s", exc)
        except Exception as exc:  # pragma: no cover - long running worker resilience
            logger.exception("worker loop error: %s", exc)
            await asyncio.sleep(1)


async def run_from_settings(settings: Settings) -> None:
    """Wire dependencies from settings and start the worker loop."""
    pool = await create_pool(settings)
    redis = await create_redis(settings)
    queue = TaskQueue(redis=redis, queue_name=settings.queue_upload)
    dlq_queue = TaskQueue(redis=redis, queue_name=settings.queue_upload_dlq)
    injector_mode = settings.pixel_injector_mode.strip().lower()
    service: PixelInjector
    if injector_mode == "local":
        service = LocalPixelInjectorService(share_scheme=settings.pixel_injector_local_share_scheme)
        logger.warning("pixel-injector running in LOCAL mode (no Redroid/ADB)")
    else:
        adb = AdbConnection()
        service = PixelInjectorService(
            redroid=DockerRedroidManager(
                settings.redroid_image,
                adb_host=settings.redroid_adb_host,
                adb_port_start=settings.redroid_adb_port_start,
                network=settings.redroid_network or None,
            ),
            uploader=UIAutomatorUploader(adb=adb),
            verifier=GooglePhotosVerifier(adb=adb),
            ready_timeout_seconds=settings.upload_ready_timeout_seconds,
            verify_timeout_seconds=settings.upload_verify_timeout_seconds,
            task_timeout_seconds=settings.upload_task_timeout_seconds,
        )
    task_repo = TaskRepository(pool)
    video_repo = VideoRepository(pool)
    account_repo = AccountRepository(pool)
    try:
        await run_worker(
            queue=queue,
            service=service,
            task_repo=task_repo,
            video_repo=video_repo,
            account_repo=account_repo,
            retry_queue=queue,
            dlq_queue=dlq_queue,
            redis_client=redis,
            default_max_retries=settings.upload_max_retries,
            pause_key=settings.system_pause_key,
            enforce_single_flight=settings.upload_max_concurrency <= 1,
            upload_lock_key=settings.upload_lock_key,
            upload_lock_ttl_seconds=settings.upload_lock_ttl_seconds,
            dlq_replay_enabled=settings.upload_dlq_replay_max > 0,
            dlq_replay_max=settings.upload_dlq_replay_max,
            dlq_replay_backoff_seconds=_parse_backoff_seconds(settings.upload_dlq_replay_backoff_seconds),
        )
    finally:
        await redis.aclose()
        await pool.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(run_from_settings(get_settings()))


if __name__ == "__main__":
    main()
