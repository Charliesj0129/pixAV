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
from pixav.shared.exceptions import DownloadError
from pixav.shared.models import Task
from pixav.shared.queue import TaskQueue
from pixav.shared.redis_client import create_redis
from pixav.shared.repository import TaskRepository, VideoRepository

logger = logging.getLogger(__name__)


async def run_loop(settings: Settings) -> None:  # noqa: C901
    """Consume tasks from the download queue and process them.

    Durable claim loop:
    - Claim payloads via BRPOPLPUSH into ``:processing``.
    - ACK on handled payloads (including invalid payload drop).
    - NACK+requeue on unexpected loop errors.
    """
    pool = await create_pool(settings)
    redis = await create_redis(settings)

    try:
        video_repo = VideoRepository(pool)
        task_repo = TaskRepository(pool)
        download_queue = TaskQueue(redis=redis, queue_name=settings.queue_download)
        download_dlq_queue = TaskQueue(redis=redis, queue_name=settings.queue_download_dlq)

        client = QBitClient(
            base_url=settings.qbit_url,
            username=settings.qbit_user,
            password=settings.qbit_password,
            download_dir=settings.download_dir,
        )
        try:
            version = await client.health_check()
            logger.info("qBittorrent health check ok (version=%s)", version)
        except DownloadError as exc:
            logger.error("qBittorrent health check failed: %s", exc)
            logger.error("hint: run `uv run python scripts/bootstrap_qbittorrent_webui.py` to set stable credentials")
            return
        remuxer = FFmpegRemuxer()
        scraper = StashMetadataScraper(settings.stash_url) if settings.stash_url else None

        service = MediaLoaderService(
            client=client,
            remuxer=remuxer,
            scraper=scraper,
            video_repo=video_repo,
            task_repo=task_repo,
            upload_queue_name=settings.queue_upload,
            retry_queue=download_queue,
            dlq_queue=download_dlq_queue,
            output_dir=settings.download_dir,
            mode=settings.media_loader_mode,
        )

        logger.info("media-loader worker started, listening on %s", download_queue.name)
        try:
            recovered = int(await download_queue.requeue_inflight())
        except (TypeError, ValueError):
            recovered = 0
        if recovered:
            logger.warning("requeued %d in-flight payload(s) for %s", recovered, download_queue.name)

        while True:
            receipt: str | None = None
            acked = False
            try:
                claimed = await download_queue.pop_claim(timeout=5)
                if claimed is None:
                    continue
                payload, receipt = claimed

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
                    logger.info(
                        "task %s result: %s (trace_id=%s)",
                        result.id,
                        result.state.value,
                        result.trace_id,
                    )
                except Exception as exc:
                    logger.exception("unexpected error processing task %s: %s", task.id, exc)

                await download_queue.ack(receipt)
                acked = True
            except Exception as exc:
                logger.exception("media-loader worker loop error: %s", exc)
                if receipt is not None and not acked:
                    try:
                        await download_queue.nack(receipt, requeue=True)
                    except Exception as nack_exc:  # pragma: no cover - defensive logging
                        logger.error("failed to nack payload on %s: %s", download_queue.name, nack_exc)
                await asyncio.sleep(1)

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


def _parse_int(val: Any, *, default: int, minimum: int) -> int:
    """Parse arbitrary values into bounded integers."""
    try:
        parsed = int(val)
    except (TypeError, ValueError):
        return default
    return max(parsed, minimum)


def main() -> None:
    """Entry point for ``python -m pixav.media_loader.worker``."""
    import uvicorn

    from pixav.shared.health import create_health_app

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    health_app = create_health_app("media_loader")

    async def _run() -> None:
        config = uvicorn.Config(
            health_app,
            host=settings.health_host,
            port=settings.media_loader_health_port,
            log_level="warning",
            access_log=False,
        )
        server = uvicorn.Server(config)
        worker_task = asyncio.ensure_future(run_loop(settings))
        server_task = asyncio.ensure_future(server.serve())
        done, pending = await asyncio.wait([worker_task, server_task], return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: S110
                pass
        for t in done:
            exc = t.exception()
            if exc is not None:
                raise exc

    asyncio.run(_run())


if __name__ == "__main__":
    main()
