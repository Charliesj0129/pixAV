"""Queue-driven worker for Media-Loader downloading pipeline."""

from __future__ import annotations

import argparse
import asyncio
import logging
import uuid
from typing import Any

from pixav.config import Settings, get_settings
from pixav.media_loader.metadata import StashMetadataScraper
from pixav.media_loader.qbittorrent import QBitClient, parse_extra_trackers
from pixav.media_loader.remuxer import FFmpegRemuxer
from pixav.media_loader.service import MediaLoaderService
from pixav.shared.db import create_pool
from pixav.shared.dead_letter import DeadLetterStore
from pixav.shared.disk import DownloadSpaceGuard
from pixav.shared.enums import TaskState
from pixav.shared.exceptions import DownloadError
from pixav.shared.metrics import record_task_failed, record_task_processed, record_task_retried
from pixav.shared.models import Task
from pixav.shared.pause import is_paused_value
from pixav.shared.queue import TaskQueue
from pixav.shared.redis_client import create_redis
from pixav.shared.repository import SourceCandidateRepository, TaskRepository, VideoRepository
from pixav.shared.retry import parse_retry_backoff
from pixav.shared.vpn import REFERENCE_SIDE, PublicIpProbe, VpnEgressMonitor

logger = logging.getLogger(__name__)

_METRICS_MODULE = "media_loader"


class _OneShotGuardError(RuntimeError):
    """Abort a guarded one-shot without consuming a different queue item."""


def _assert_expected_payload(
    payload: dict[str, Any],
    *,
    expected_task_id: uuid.UUID | None,
    expected_video_id: uuid.UUID | None,
) -> None:
    """Refuse a one-shot claim when Redis no longer contains the selected target."""
    if expected_task_id is None and expected_video_id is None:
        return

    observed_task_id = _parse_uuid(payload.get("task_id"))
    observed_video_id = _parse_uuid(payload.get("video_id"))
    if observed_task_id != expected_task_id or observed_video_id != expected_video_id:
        raise _OneShotGuardError(
            "download queue head changed: "
            f"expected task={expected_task_id} video={expected_video_id}, "
            f"observed task={observed_task_id} video={observed_video_id}"
        )


def build_egress_monitor(settings: Settings, redis: Any) -> VpnEgressMonitor | None:
    """Return the VPN egress monitor, or None when no echo endpoint is configured.

    Absent configuration disables the *detector*, not the protection: gluetun's
    kill switch is unaffected either way.
    """
    if not settings.vpn_egress_echo_url:
        return None
    return VpnEgressMonitor(
        redis,
        probe=PublicIpProbe(settings.vpn_egress_echo_url, side=REFERENCE_SIDE),
        key=settings.vpn_egress_redis_key,
        interval_seconds=settings.vpn_egress_interval_seconds,
    )


def _record_task_outcome(state: TaskState) -> None:
    """Translate a terminal task state into a Prometheus counter increment."""
    if state is TaskState.COMPLETE:
        record_task_processed(_METRICS_MODULE)
    elif state is TaskState.FAILED:
        record_task_failed(_METRICS_MODULE)
    else:
        record_task_retried(_METRICS_MODULE)


async def run_loop(  # noqa: C901
    settings: Settings,
    *,
    health_state: Any = None,
    max_tasks: int = 0,
    expected_db_identity: str | None = None,
    expected_redis_identity: str | None = None,
    expected_task_id: uuid.UUID | None = None,
    expected_video_id: uuid.UUID | None = None,
) -> None:
    """Consume tasks from the download queue and process them.

    Durable claim loop:
    - Claim payloads via BRPOPLPUSH into ``:processing``.
    - ACK on handled payloads (including invalid payload drop).
    - NACK+requeue on unexpected loop errors.
    """
    pool = await create_pool(settings)
    redis = await create_redis(settings)
    client: QBitClient | None = None

    try:
        if expected_redis_identity is not None:
            observed_run_id = str((await redis.info("server")).get("run_id", ""))
            if observed_run_id != expected_redis_identity:
                raise RuntimeError("Redis identity changed before worker initialization")
        if expected_db_identity is not None:
            live_identity = str(await pool.fetchval("SELECT system_identifier FROM pg_control_system()"))
            if live_identity != expected_db_identity:
                raise RuntimeError(f"database identity mismatch: expected {expected_db_identity}, got {live_identity}")

        video_repo = VideoRepository(pool)
        task_repo = TaskRepository(pool)
        candidate_repo = SourceCandidateRepository(pool)
        download_queue = TaskQueue(redis=redis, queue_name=settings.queue_download)
        dlq_store = DeadLetterStore(
            redis,
            retention_days=settings.dlq_retention_days,
            max_items_per_stage=settings.dlq_max_items_per_stage,
        )
        disk_guard = DownloadSpaceGuard(
            redis,
            path=settings.download_dir,
            pause_key=settings.download_pause_key,
            min_free_bytes=settings.download_min_free_bytes,
            min_free_percent=settings.download_min_free_percent,
        )
        egress_monitor = build_egress_monitor(settings, redis)

        client = QBitClient(
            base_url=settings.qbit_url,
            username=settings.qbit_user,
            password=settings.qbit_password,
            download_dir=settings.qbit_download_dir,
            local_download_dir=settings.download_dir,
            extra_trackers=parse_extra_trackers(settings.qbit_extra_trackers),
            download_timeout=settings.qbit_download_timeout_seconds,
            no_peer_grace_seconds=settings.qbit_no_peer_grace_seconds,
        )
        try:
            version = await client.health_check()
            logger.info("qBittorrent health check ok (version=%s)", version)
        except DownloadError as exc:
            logger.error("qBittorrent health check failed: %s", exc)
            logger.error("hint: run `uv run python scripts/bootstrap_qbittorrent_webui.py` to set stable credentials")
            if max_tasks > 0 or expected_task_id is not None or expected_video_id is not None:
                raise _OneShotGuardError("qBittorrent health check failed before the guarded claim") from exc
            if not settings.managed_media_workflow:
                return
            # The managed activity worker serves two stages with different
            # dependencies: `prepare` needs only ffmpeg and the local artifact.
            # Refusing to start would also refuse preparation, while a `download`
            # activity still fails honestly through the torrent client itself and
            # is reported as infrastructure for the authority to reconcile.
            logger.warning("managed activity worker starting without a torrent client; download activities will fail")
        remuxer = FFmpegRemuxer()
        scraper = StashMetadataScraper(settings.stash_url) if settings.stash_enabled and settings.stash_url else None

        service = MediaLoaderService(
            client=client,
            remuxer=remuxer,
            scraper=scraper,
            video_repo=video_repo,
            task_repo=task_repo,
            candidate_repo=candidate_repo,
            upload_queue_name=settings.queue_upload,
            dlq_store=dlq_store,
            source_cooldown_hours=settings.source_candidate_cooldown_hours,
            retry_backoff_seconds=parse_retry_backoff(settings.retry_backoff_seconds),
            failure_retention_days=settings.local_cleanup_failure_days,
            output_dir=settings.remux_dir,
            mode=settings.media_loader_mode,
        )
        if health_state is not None:
            health_state.mark_ready()

        if settings.managed_media_workflow:
            from pixav.media_loader.activity import MediaActivityWorker
            from pixav.shared.workflow import MANAGED_QUEUE, require_workflow_role

            await require_workflow_role(pool, "pixav_activity_worker")
            activity_worker = MediaActivityWorker(pool, client, remuxer, output_dir=settings.remux_dir)
            activity_queue = TaskQueue(redis=redis, queue_name=MANAGED_QUEUE)
            await activity_queue.requeue_inflight()
            while True:
                if is_paused_value(await redis.get(settings.system_pause_key)):
                    await asyncio.sleep(5)
                    continue
                disk_status = await disk_guard.check_and_latch()
                try:
                    await activity_worker.run_one(activity_queue, download_paused=disk_status.paused)
                except Exception:
                    logger.warning("managed activity dependency unavailable; authority retains recovery ownership")
                await asyncio.sleep(1)

        logger.info("media-loader worker started, listening on %s", download_queue.name)
        try:
            recovered = int(await download_queue.requeue_inflight())
        except (TypeError, ValueError):
            recovered = 0
        if recovered:
            logger.warning("requeued %d in-flight payload(s) for %s", recovered, download_queue.name)

        handled = 0
        while True:
            receipt: str | None = None
            acked = False
            try:
                if egress_monitor is not None:
                    # Reported, never enforced. gluetun's kill switch is what
                    # stops a leak; pausing here as well would let an operator
                    # believe downloads are gated on this check, which a
                    # misconfigured echo endpoint could silently disable.
                    await egress_monitor.observe()

                if is_paused_value(await redis.get(settings.system_pause_key)):
                    if max_tasks > 0:
                        raise _OneShotGuardError("global system pause became active before the guarded claim")
                    logger.info("system paused via redis key %s; skip download claim", settings.system_pause_key)
                    await asyncio.sleep(5)
                    continue

                disk_status = await disk_guard.check_and_latch()
                if disk_status.paused:
                    if max_tasks > 0:
                        raise _OneShotGuardError(f"download safety pause is active: {disk_status.reason}")
                    logger.warning("download claims paused: %s", disk_status.reason)
                    await asyncio.sleep(5)
                    continue
                claimed = await download_queue.pop_claim(timeout=5)
                if claimed is None:
                    if max_tasks > 0:
                        raise _OneShotGuardError("download queue became empty before the guarded claim")
                    continue
                payload, receipt = claimed
                _assert_expected_payload(
                    payload,
                    expected_task_id=expected_task_id,
                    expected_video_id=expected_video_id,
                )

                task_id_raw = payload.get("task_id") or payload.get("video_id")
                if not isinstance(task_id_raw, str):
                    logger.warning("invalid payload (no task_id): %s", payload)
                    await download_queue.ack(receipt)
                    acked = True
                    continue

                video_id_raw = payload.get("video_id", task_id_raw)
                if not isinstance(video_id_raw, str):
                    logger.warning("invalid payload (non-string video_id): %s", payload)
                    await download_queue.ack(receipt)
                    acked = True
                    continue

                video_id = _parse_uuid(video_id_raw)
                if video_id is None:
                    logger.warning("invalid payload (bad video_id=%r): %s", video_id_raw, payload)
                    await download_queue.ack(receipt)
                    acked = True
                    continue

                task_id = _parse_uuid(task_id_raw) or uuid.uuid4()
                retries = _parse_int(payload.get("retries"), default=0, minimum=0)
                max_retries = _parse_int(payload.get("max_retries"), default=settings.download_max_retries, minimum=1)
                queue_name = payload.get("queue_name", settings.queue_download)
                if not isinstance(queue_name, str) or not queue_name:
                    queue_name = settings.queue_download
                trace_id_raw = payload.get("trace_id")
                trace_id = trace_id_raw if isinstance(trace_id_raw, str) and trace_id_raw else str(uuid.uuid4())

                task = Task(
                    id=task_id,
                    video_id=video_id,
                    state=TaskState.PENDING,
                    queue_name=queue_name,
                    retries=retries,
                    max_retries=max_retries,
                    trace_id=trace_id,
                )

                try:
                    result = await service.process_task(task)
                    _record_task_outcome(result.state)
                    logger.info(
                        "task %s result: %s (trace_id=%s)",
                        result.id,
                        result.state.value,
                        result.trace_id,
                    )
                except Exception as exc:
                    record_task_failed(_METRICS_MODULE)
                    logger.exception("unexpected error processing task %s: %s", task.id, exc)
                    if max_tasks > 0:
                        raise _OneShotGuardError(f"guarded download task {task.id} crashed before ACK") from exc

                await download_queue.ack(receipt)
                acked = True
                handled += 1
                if max_tasks > 0 and handled >= max_tasks:
                    logger.info("one-shot task limit reached (%d); exiting cleanly", max_tasks)
                    return
            except _OneShotGuardError:
                if receipt is not None and not acked:
                    try:
                        await download_queue.nack(receipt, requeue=True, front=True)
                    except Exception as nack_exc:  # pragma: no cover - defensive logging
                        logger.error("failed to restore guarded payload on %s: %s", download_queue.name, nack_exc)
                raise
            except Exception as exc:
                logger.exception("media-loader worker loop error: %s", exc)
                if receipt is not None and not acked:
                    try:
                        await download_queue.nack(receipt, requeue=True)
                    except Exception as nack_exc:  # pragma: no cover - defensive logging
                        logger.error("failed to nack payload on %s: %s", download_queue.name, nack_exc)
                await asyncio.sleep(1)

    finally:
        if client is not None:
            await client.aclose()
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


def _parse_int(val: Any, *, default: int, minimum: int) -> int:
    """Parse arbitrary values into bounded integers."""
    try:
        parsed = int(val)
    except (TypeError, ValueError):
        return default
    return max(parsed, minimum)


def main() -> None:
    """Entry point for ``python -m pixav.media_loader.worker``."""
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
    health_state = HealthState("media_loader", stale_after_seconds=settings.heartbeat_stale_seconds)
    health_app = create_health_app("media_loader", state=health_state)

    async def _run() -> None:
        await run_with_health(
            worker_coro=run_loop(
                settings,
                health_state=health_state,
                max_tasks=args.max_tasks,
                expected_db_identity=args.expect_db_identity,
                expected_task_id=args.expect_task_id,
                expected_video_id=args.expect_video_id,
            ),
            health_app=health_app,
            host=settings.health_host,
            port=settings.media_loader_health_port,
            health_state=health_state,
            heartbeat_interval_seconds=settings.heartbeat_interval_seconds,
        )

    asyncio.run(_run())


if __name__ == "__main__":
    main()
