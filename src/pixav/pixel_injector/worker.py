"""Queue consumer worker for pixel_injector."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import uuid
from contextlib import nullcontext
from datetime import datetime, timezone
from typing import Any, cast

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
from pixav.shared.dead_letter import DeadLetterStore
from pixav.shared.enums import TaskState, VideoStatus
from pixav.shared.metrics import record_task_failed, record_task_processed, record_task_retried
from pixav.shared.models import Task
from pixav.shared.pause import is_paused_value
from pixav.shared.phase0_timing import phase0_span
from pixav.shared.queue import TaskQueue
from pixav.shared.redis_client import create_redis
from pixav.shared.repository import AccountRepository, TaskRepository, VideoRepository
from pixav.shared.retry import DEFAULT_RETRY_BACKOFF_SECONDS, parse_retry_backoff, retry_deadline

logger = logging.getLogger(__name__)

_METRICS_MODULE = "pixel_injector"


class _OneShotGuardError(RuntimeError):
    """Abort a guarded one-shot without consuming a different queue item."""


def _assert_expected_payload(
    payload: dict[str, Any],
    *,
    expected_task_id: uuid.UUID | None,
    expected_video_id: uuid.UUID | None,
) -> None:
    if expected_task_id is None and expected_video_id is None:
        return

    observed_task_id = _safe_uuid(payload.get("task_id"))
    observed_video_id = _safe_uuid(payload.get("video_id"))
    if observed_task_id != expected_task_id or observed_video_id != expected_video_id:
        raise _OneShotGuardError(
            "upload queue head changed: "
            f"expected task={expected_task_id} video={expected_video_id}, "
            f"observed task={observed_task_id} video={observed_video_id}"
        )


_RELEASE_UPLOAD_LOCK_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""

_RENEW_UPLOAD_LOCK_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('expire', KEYS[1], tonumber(ARGV[2]))
end
return 0
"""


def _task_from_payload(payload: dict[str, Any], *, default_max_retries: int) -> Task:
    """Convert queue payload into a typed Task model."""
    import uuid as _uuid

    normalized = dict(payload)
    if "task_id" in normalized and "id" not in normalized:
        normalized["id"] = normalized.pop("task_id")
    normalized.setdefault("retries", 0)
    normalized.setdefault("max_retries", default_max_retries)
    # Preserve trace_id from upstream; generate fresh one if absent
    if not isinstance(normalized.get("trace_id"), str) or not normalized.get("trace_id"):
        normalized["trace_id"] = str(_uuid.uuid4())
    return Task.model_validate(normalized)


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
        "trace_id": task.trace_id,
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


def _safe_uuid(value: Any) -> uuid.UUID | None:
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


async def _is_paused(redis_client: aioredis.Redis, pause_key: str) -> bool:
    value = await redis_client.get(pause_key)
    return is_paused_value(value)


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
    try:
        await cast(Any, redis_client).eval(_RELEASE_UPLOAD_LOCK_LUA, 1, lock_key, lock_token)
    except Exception:
        # Fallback for Redis variants/mocks without EVAL support.
        holder = await redis_client.get(lock_key)
        if holder == lock_token:
            await redis_client.delete(lock_key)


async def _renew_upload_lock(
    redis_client: aioredis.Redis,
    *,
    lock_key: str,
    lock_token: str,
    ttl_seconds: int,
) -> bool:
    try:
        renewed = await cast(Any, redis_client).eval(_RENEW_UPLOAD_LOCK_LUA, 1, lock_key, lock_token, str(ttl_seconds))
        return bool(renewed)
    except Exception:
        # Fallback for Redis variants/mocks without EVAL support.
        holder = await redis_client.get(lock_key)
        if holder != lock_token:
            return False
        await redis_client.expire(lock_key, ttl_seconds)
        return True


async def _upload_lock_heartbeat(
    redis_client: aioredis.Redis,
    *,
    lock_key: str,
    lock_token: str,
    ttl_seconds: int,
    interval_seconds: int,
) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        renewed = await _renew_upload_lock(
            redis_client,
            lock_key=lock_key,
            lock_token=lock_token,
            ttl_seconds=ttl_seconds,
        )
        if not renewed:
            logger.warning("upload lock lost or expired while processing (%s)", lock_key)
            return


async def _hydrate_local_path(task: Task, *, video_repo: VideoRepository | None) -> Task:
    if task.local_path:
        return task
    if video_repo is None:
        return task

    video = await video_repo.find_by_id(task.video_id)
    if video is None or not video.local_path:
        return task
    return task.model_copy(update={"local_path": video.local_path})


async def _is_managed_video(task: Task, *, task_repo: TaskRepository | None) -> bool:
    """Whether the managed execution authority already owns this video.

    Two executors writing the same execution state is the failure the handover
    contract forbids, so the legacy uploader stands down rather than racing.
    The database trigger refuses the write anyway; this refuses the work first,
    before a container is started or a file is pushed anywhere.
    """
    if task_repo is None:
        return False
    return await task_repo.is_managed_video(task.video_id)


async def _is_terminal_task(task: Task, *, task_repo: TaskRepository | None) -> bool:
    if task_repo is None:
        return False
    current = await task_repo.find_by_id(task.id)
    if current is None:
        return False
    return current.state in {TaskState.COMPLETE, TaskState.FAILED}


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
    stage = "local_finalize" if (result.share_url or "").startswith("pixav-local://") else None
    with phase0_span(result.id, result.video_id, stage) if stage else nullcontext():
        if task_repo is not None:
            await task_repo.update_state(result.id, TaskState.COMPLETE)
        if video_repo is not None:
            await video_repo.update_upload_result(result.video_id, share_url=result.share_url or "")
        if account_repo is not None and result.account_id is not None:
            uploaded_bytes = _uploaded_bytes_from_task(result)
            await account_repo.apply_upload_usage(result.account_id, uploaded_bytes)
        record_task_processed(_METRICS_MODULE)
        logger.info("task %s complete (trace_id=%s)", result.id, result.trace_id)


async def _persist_failure(
    result: Task,
    *,
    task_repo: TaskRepository | None,
    video_repo: VideoRepository | None,
    dlq_store: DeadLetterStore | None = None,
    retry_backoff_seconds: tuple[int, ...] = DEFAULT_RETRY_BACKOFF_SECONDS,
    failure_retention_days: int = 7,
) -> dict[str, str | int] | None:
    error_message = result.error_message or "upload stage failed"
    next_retry = result.retries + 1

    if _is_retryable_failure(error_message) and next_retry <= result.max_retries:
        due = retry_deadline(next_retry, backoff_seconds=retry_backoff_seconds)
        if task_repo is not None:
            await task_repo.set_retry(
                result.id,
                next_retry,
                state=TaskState.PENDING,
                error_message=error_message,
                retry_not_before=due,
            )
        if video_repo is not None:
            await video_repo.update_status(result.video_id, VideoStatus.DOWNLOADED)
        record_task_retried(_METRICS_MODULE)
        logger.warning(
            "task %s failed (retry %d/%d), due at %s: %s",
            result.id,
            next_retry,
            result.max_retries,
            due.isoformat(),
            error_message,
        )
        return None

    if task_repo is not None:
        await task_repo.update_state(result.id, TaskState.FAILED, error_message=error_message)
    if video_repo is not None:
        await video_repo.update_status(result.video_id, VideoStatus.FAILED)
        schedule_cleanup = getattr(video_repo, "schedule_terminal_cleanup", None)
        if callable(schedule_cleanup):
            await schedule_cleanup(result.video_id, retention_days=failure_retention_days)

    dlq_payload = _build_dlq_payload(result, error_message)
    if dlq_store is not None:
        await dlq_store.put("upload", dlq_payload)
    record_task_failed(_METRICS_MODULE)
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
    dlq_store: DeadLetterStore | None = None,
    redis_client: aioredis.Redis | None = None,
    default_max_retries: int = 10,
    poll_timeout: int = 5,
    stop_event: asyncio.Event | None = None,
    pause_key: str = "system:pause",
    enforce_single_flight: bool = True,
    upload_lock_key: str = "pixav:upload:lock",
    upload_lock_ttl_seconds: int = 7200,
    retry_backoff_seconds: tuple[int, ...] = DEFAULT_RETRY_BACKOFF_SECONDS,
    failure_retention_days: int = 7,
    max_tasks: int = 0,
    expected_task_id: uuid.UUID | None = None,
    expected_video_id: uuid.UUID | None = None,
) -> None:
    """Run the BLPOP consumer loop for the upload queue."""
    logger.info("pixel injector worker starting on queue %s", queue.name)
    try:
        recovered = int(await queue.requeue_inflight())
    except (TypeError, ValueError):
        recovered = 0
    if recovered:
        logger.warning("requeued %d in-flight payload(s) on %s", recovered, queue.name)
    handled = 0
    while True:
        if stop_event is not None and stop_event.is_set():
            logger.info("stop_event set; shutting down worker")
            return

        receipt: str | None = None
        acked = False
        try:
            if redis_client is not None and await _is_paused(redis_client, pause_key):
                if max_tasks > 0:
                    raise _OneShotGuardError("global system pause became active before the guarded claim")
                logger.info("system paused via redis key %s; skip polling", pause_key)
                await asyncio.sleep(max(1, min(poll_timeout, 5)))
                continue

            claimed = await queue.pop_claim(timeout=poll_timeout)
            if claimed is None:
                if max_tasks > 0:
                    raise _OneShotGuardError("upload queue became empty before the guarded claim")
                continue
            payload, receipt = claimed
            _assert_expected_payload(
                payload,
                expected_task_id=expected_task_id,
                expected_video_id=expected_video_id,
            )

            lock_token: str | None = None
            lock_heartbeat_task: asyncio.Task[None] | None = None
            if enforce_single_flight and redis_client is not None:
                candidate = str(uuid.uuid4())
                acquired = await _acquire_upload_lock(
                    redis_client,
                    lock_key=upload_lock_key,
                    lock_token=candidate,
                    ttl_seconds=upload_lock_ttl_seconds,
                )
                if not acquired:
                    await queue.nack(receipt, requeue=True, front=max_tasks > 0)
                    acked = True
                    logger.info("upload lock busy (%s), payload requeued", upload_lock_key)
                    if max_tasks > 0:
                        raise _OneShotGuardError("upload single-flight lock is already held")
                    await asyncio.sleep(1)
                    continue
                lock_token = candidate
                if upload_lock_ttl_seconds > 1:
                    refresh_interval = max(1, upload_lock_ttl_seconds // 3)
                    lock_heartbeat_task = asyncio.create_task(
                        _upload_lock_heartbeat(
                            redis_client,
                            lock_key=upload_lock_key,
                            lock_token=lock_token,
                            ttl_seconds=upload_lock_ttl_seconds,
                            interval_seconds=refresh_interval,
                        )
                    )

            try:
                task = _task_from_payload(payload, default_max_retries=default_max_retries)
                if await _is_managed_video(task, task_repo=task_repo):
                    logger.warning("refusing task %s: the managed authority owns this video", task.id)
                elif await _is_terminal_task(task, task_repo=task_repo):
                    logger.info("drop duplicate payload for terminal task %s", task.id)
                else:
                    task = await _hydrate_local_path(task, video_repo=video_repo)
                    if not task.local_path:
                        result = task.model_copy(
                            update={
                                "state": TaskState.FAILED,
                                "error_message": "video local_path is missing",
                            }
                        )
                        await _persist_failure(
                            result,
                            task_repo=task_repo,
                            video_repo=video_repo,
                            dlq_store=dlq_store,
                            retry_backoff_seconds=retry_backoff_seconds,
                            failure_retention_days=failure_retention_days,
                        )
                    else:
                        await _mark_uploading(task, task_repo=task_repo, video_repo=video_repo)

                        account = None
                        if task.account_id is not None and account_repo is not None:
                            account = await account_repo.find_by_id(task.account_id)

                        result = await service.process_task(task, account)
                        if result.state == TaskState.COMPLETE and result.share_url:
                            await _persist_success(
                                result,
                                task_repo=task_repo,
                                video_repo=video_repo,
                                account_repo=account_repo,
                            )
                        else:
                            await _persist_failure(
                                result,
                                task_repo=task_repo,
                                video_repo=video_repo,
                                dlq_store=dlq_store,
                                retry_backoff_seconds=retry_backoff_seconds,
                                failure_retention_days=failure_retention_days,
                            )

            finally:
                if lock_heartbeat_task is not None:
                    lock_heartbeat_task.cancel()
                    try:
                        await lock_heartbeat_task
                    except asyncio.CancelledError:
                        pass
                    except Exception as exc:  # pragma: no cover - defensive heartbeat cleanup
                        logger.debug("upload lock heartbeat task ended with error: %s", exc)
                if lock_token is not None and redis_client is not None:
                    await _release_upload_lock(
                        redis_client,
                        lock_key=upload_lock_key,
                        lock_token=lock_token,
                    )
            if receipt is not None and not acked:
                await queue.ack(receipt)
                acked = True
            handled += 1
            if max_tasks > 0 and handled >= max_tasks:
                logger.info("one-shot task limit reached (%d); exiting cleanly", max_tasks)
                return
        except _OneShotGuardError:
            if receipt is not None and not acked:
                try:
                    await queue.nack(receipt, requeue=True, front=True)
                except Exception as nack_exc:  # pragma: no cover - defensive logging
                    logger.error("failed to restore guarded upload payload: %s", nack_exc)
            raise
        except ValidationError as exc:
            logger.error("invalid upload payload: %s", exc)
            if receipt is not None and not acked:
                try:
                    await queue.ack(receipt)
                except Exception as ack_exc:  # pragma: no cover - defensive logging
                    logger.error("failed to ack invalid payload: %s", ack_exc)
        except Exception as exc:  # pragma: no cover - long running worker resilience
            logger.exception("worker loop error: %s", exc)
            if receipt is not None and not acked:
                try:
                    await queue.nack(receipt, requeue=True, front=max_tasks > 0)
                except Exception as nack_exc:
                    logger.error("failed to nack payload after worker error: %s", nack_exc)
            if max_tasks > 0:
                raise _OneShotGuardError("guarded upload task crashed before ACK") from exc
            await asyncio.sleep(1)


def _managed_storage_loop(pool, redis, settings: Settings, injector_mode: str):
    """The managed storage claim loop, or nothing when it must not run.

    Managed storage stays off rather than running through a substituted upload
    environment: a remote success recorded from one would describe a device that
    never existed.

    It also stays off once ``PIXAV_STORAGE_WORKER_OWNER`` names a deployment,
    because that means ``pixav.pixel_injector.storage_worker`` is running it in
    its own process. The two cannot share this one: the activity role must not
    be a member of the execution authority, and the legacy loop below needs
    ``tasks``/``videos`` writes that only the authority holds.
    """
    from pixav.pixel_injector.photos_storage import PIXEL_COMPATIBLE_MODE
    from pixav.pixel_injector.storage_worker import run_storage_activity_worker

    if not settings.managed_media_workflow or injector_mode != PIXEL_COMPATIBLE_MODE:
        return None
    if settings.storage_worker_owner.strip():
        return None
    return run_storage_activity_worker(pool, redis, settings, owner=str(uuid.uuid4()))


async def _run_loops(legacy, managed) -> None:
    """Run both claim loops; the first failure stops the other and surfaces.

    Neither loop may take the other's failure as permission to advance an
    execution, so a supervisor restarts the pair rather than half of it.
    """
    if managed is None:
        await legacy
        return
    tasks = {asyncio.ensure_future(legacy), asyncio.ensure_future(managed)}
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    for task in done:
        task.result()


async def run_from_settings(
    settings: Settings,
    *,
    health_state: Any = None,
    max_tasks: int = 0,
    expected_db_identity: str | None = None,
    expected_redis_identity: str | None = None,
    expected_task_id: uuid.UUID | None = None,
    expected_video_id: uuid.UUID | None = None,
) -> None:
    """Wire dependencies from settings and start the worker loop."""
    pool = await create_pool(settings)
    redis = await create_redis(settings)
    try:
        if expected_redis_identity is not None:
            observed_run_id = str((await redis.info("server")).get("run_id", ""))
            if observed_run_id != expected_redis_identity:
                raise RuntimeError("Redis identity changed before worker initialization")
        if expected_db_identity is not None:
            live_identity = str(await pool.fetchval("SELECT system_identifier FROM pg_control_system()"))
            if live_identity != expected_db_identity:
                raise RuntimeError(f"database identity mismatch: expected {expected_db_identity}, got {live_identity}")

        queue = TaskQueue(redis=redis, queue_name=settings.queue_upload)
        dlq_store = DeadLetterStore(
            redis,
            retention_days=settings.dlq_retention_days,
            max_items_per_stage=settings.dlq_max_items_per_stage,
        )
        injector_mode = settings.pixel_injector_mode.strip().lower()
        service: PixelInjector
        if injector_mode == "local":
            service = LocalPixelInjectorService(share_scheme=settings.pixel_injector_local_share_scheme)
            logger.warning("pixel-injector running in LOCAL mode (no Redroid/ADB)")
        else:
            # A multi-gigabyte Phase 0 media transfer can legitimately exceed the
            # old 120-second ADB subprocess default.  The service's task timeout is
            # still the outer finite bound for the complete upload.
            adb = AdbConnection(timeout=settings.upload_task_timeout_seconds)
            redroid = DockerRedroidManager.from_profile_name(
                settings.redroid_profile,
                profiles_path=settings.redroid_profiles_path or None,
                adb_host=settings.redroid_adb_host,
                adb_port_start=settings.redroid_adb_port_start,
                network=settings.redroid_network or None,
            )
            await redroid.cleanup_orphans()
            service = PixelInjectorService(
                redroid=redroid,
                uploader=UIAutomatorUploader(adb=adb),
                verifier=GooglePhotosVerifier(adb=adb),
                ready_timeout_seconds=settings.upload_ready_timeout_seconds,
                verify_timeout_seconds=settings.upload_verify_timeout_seconds,
                task_timeout_seconds=settings.upload_task_timeout_seconds,
            )
        task_repo = TaskRepository(pool)
        video_repo = VideoRepository(pool)
        account_repo = AccountRepository(pool)
        if health_state is not None:
            health_state.mark_ready()
        legacy = run_worker(
            queue=queue,
            service=service,
            task_repo=task_repo,
            video_repo=video_repo,
            account_repo=account_repo,
            dlq_store=dlq_store,
            redis_client=redis,
            default_max_retries=settings.upload_max_retries,
            pause_key=settings.system_pause_key,
            enforce_single_flight=settings.upload_max_concurrency <= 1,
            upload_lock_key=settings.upload_lock_key,
            upload_lock_ttl_seconds=settings.upload_lock_ttl_seconds,
            retry_backoff_seconds=parse_retry_backoff(settings.retry_backoff_seconds),
            failure_retention_days=settings.local_cleanup_failure_days,
            max_tasks=max_tasks,
            expected_task_id=expected_task_id,
            expected_video_id=expected_video_id,
        )
        await _run_loops(legacy, _managed_storage_loop(pool, redis, settings, injector_mode))
    finally:
        await redis.aclose()
        await pool.close()


def main() -> None:
    from pixav.shared.health import HealthState, create_health_app
    from pixav.shared.health_server import run_with_health

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-tasks", type=int, default=0, help="exit after ACKing this many claimed tasks")
    parser.add_argument("--expect-db-identity", default=None, help="refuse to run against another PostgreSQL cluster")
    parser.add_argument("--expect-task-id", type=uuid.UUID, default=None, help="refuse to claim a different task")
    parser.add_argument("--expect-video-id", type=uuid.UUID, default=None, help="refuse to claim a different video")
    args = parser.parse_args()
    if args.max_tasks < 0:
        parser.error("--max-tasks must be zero or positive")
    if (args.expect_task_id is None) != (args.expect_video_id is None):
        parser.error("--expect-task-id and --expect-video-id must be provided together")
    if args.expect_task_id is not None and args.max_tasks != 1:
        parser.error("expected task/video guards require --max-tasks 1")

    settings = get_settings()
    health_state = HealthState("pixel_injector", stale_after_seconds=settings.heartbeat_stale_seconds)
    health_app = create_health_app("pixel_injector", state=health_state)

    async def _run() -> None:
        await run_with_health(
            worker_coro=run_from_settings(
                settings,
                health_state=health_state,
                max_tasks=args.max_tasks,
                expected_db_identity=args.expect_db_identity,
                expected_task_id=args.expect_task_id,
                expected_video_id=args.expect_video_id,
            ),
            health_app=health_app,
            host=settings.health_host,
            port=settings.pixel_injector_health_port,
            health_state=health_state,
            heartbeat_interval_seconds=settings.heartbeat_interval_seconds,
        )

    asyncio.run(_run())


if __name__ == "__main__":
    main()
