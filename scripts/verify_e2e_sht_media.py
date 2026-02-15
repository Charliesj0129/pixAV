"""Live E2E verification: SHT-Probe -> Maxwell-Core -> Media-Loader.

This script runs a real integration flow without mocks:
1. Crawl a configured seed URL and extract magnets (optional).
2. Persist discovered magnets and push to crawl queue.
3. Ingest/dispatch tasks to a dedicated download queue.
4. Pop one dispatched task and submit its magnet to qBittorrent.

Run:
  uv run python scripts/verify_e2e_sht_media.py

Notes:
- Settings are loaded from `PIXAV_*` env vars through `get_settings()`.
- To avoid live crawling, provide `PIXAV_E2E_MAGNET_URI` and the script will seed
  a single magnet into the pipeline and skip Stage 1.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid

from pixav.config import Settings, get_settings
from pixav.maxwell_core.dispatcher import RedisTaskDispatcher
from pixav.maxwell_core.worker import ingest_crawl_queue
from pixav.media_loader.qbittorrent import QBitClient
from pixav.shared.cookies import load_cookies
from pixav.shared.db import create_pool
from pixav.shared.enums import TaskState
from pixav.shared.queue import TaskQueue
from pixav.shared.redis_client import create_redis
from pixav.shared.repository import TaskRepository, VideoRepository
from pixav.sht_probe.crawler import HttpxCrawler
from pixav.sht_probe.flaresolverr_client import FlareSolverrSession
from pixav.sht_probe.parser import BeautifulSoupExtractor
from pixav.sht_probe.service import ShtProbeService

logger = logging.getLogger("verify_e2e_sht_media")


def _extract_info_hash(magnet_uri: str) -> str | None:
    import re

    match = re.search(r"btih:([a-fA-F0-9]{40}|[a-zA-Z2-7]{32})", magnet_uri)
    if match:
        return match.group(1).lower()
    return None


def _parse_seed_entries(raw: str) -> list[tuple[str, list[str]]]:
    entries: list[tuple[str, list[str]]] = []
    if not raw.strip():
        return entries

    for item in raw.split(","):
        token = item.strip()
        if not token:
            continue

        if "|" in token:
            url, raw_tags = token.split("|", 1)
            tags = [tag.strip() for tag in raw_tags.split("+") if tag.strip()]
            entries.append((url.strip(), tags))
        else:
            entries.append((token, []))

    return entries


def _pick_seed(settings: Settings) -> tuple[str, list[str]]:
    override = os.getenv("PIXAV_E2E_SEED_URL", "").strip()
    if override:
        return override, ["e2e-live"]

    entries = _parse_seed_entries(settings.crawl_seed_urls)
    if not entries:
        raise RuntimeError("no crawl seed configured; set PIXAV_E2E_SEED_URL or PIXAV_CRAWL_SEED_URLS")

    url, tags = entries[0]
    merged = list(dict.fromkeys([*tags, "e2e-live"]))
    return url, merged


def _queue_names(settings: Settings, run_id: str) -> tuple[str, str, str]:
    return (
        f"{settings.queue_crawl}:e2e:{run_id}",
        f"{settings.queue_download}:e2e:{run_id}",
        f"{settings.queue_upload}:e2e:{run_id}",
    )


async def _build_crawler(settings: Settings) -> HttpxCrawler:
    flaresolverr = FlareSolverrSession(settings.flaresolverr_url) if settings.flaresolverr_url else None
    crawler = HttpxCrawler(flaresolverr=flaresolverr, timeout=60)

    cookies, source = load_cookies(
        cookie_header=settings.crawl_cookie_header,
        cookie_file=settings.crawl_cookie_file,
    )
    if cookies:
        crawler.seed_cookies(cookies)
        logger.info("seeded %d crawl cookie(s) from %s", len(cookies), source)

    return crawler


async def _run_crawl_stage(
    *,
    settings: Settings,
    video_repo: VideoRepository,
    crawl_queue: TaskQueue,
    seed_url: str,
    link_pattern: str,
    seed_tags: list[str],
    run_id: str,
) -> list[str]:
    crawler = await _build_crawler(settings)
    extractor = BeautifulSoupExtractor()
    service = ShtProbeService(
        video_repo=video_repo,
        queue=crawl_queue,
        crawler=crawler,
        extractor=extractor,
        min_quality_score=-10000,
    )

    logger.info("[Stage 1] Crawl seed=%s pattern=%s", seed_url, link_pattern)

    tags = list(dict.fromkeys([*seed_tags, f"e2e-run-{run_id}"]))
    new_magnets = await service.run_crawl(
        seed_url,
        link_pattern=link_pattern,
        tags=tags,
        max_pages=int(os.getenv("PIXAV_E2E_CRAWL_MAX_PAGES", "10")),
    )
    logger.info("crawl discovered %d new magnet(s)", len(new_magnets))

    if not new_magnets:
        raise RuntimeError("no new magnets discovered; try another subforum or a cleaner database")
    return new_magnets


async def _dispatch_pending_for_queue(
    *,
    task_repo: TaskRepository,
    dispatcher: RedisTaskDispatcher,
    target_queue_name: str,
) -> int:
    pending = await task_repo.list_pending(limit=500)
    target_tasks = [task for task in pending if task.queue_name == target_queue_name]

    dispatched = 0
    for task in target_tasks:
        await dispatcher.dispatch(str(task.id), target_queue_name)
        await task_repo.update_state(task.id, TaskState.DOWNLOADING)
        dispatched += 1

    return dispatched


async def _run_ingest_dispatch_stage(
    *,
    settings: Settings,
    video_repo: VideoRepository,
    task_repo: TaskRepository,
    crawl_queue: TaskQueue,
    download_queue: TaskQueue,
    upload_queue: TaskQueue,
) -> None:
    logger.info("[Stage 2] Ingest + dispatch through Maxwell-Core")
    ingested = await ingest_crawl_queue(
        crawl_queue=crawl_queue,
        task_repo=task_repo,
        video_repo=video_repo,
        download_queue_name=download_queue.name,
        max_retries=settings.download_max_retries,
    )
    logger.info("ingested %d task(s) from crawl queue", ingested)
    if ingested == 0:
        raise RuntimeError("ingest produced zero tasks")

    dispatcher = RedisTaskDispatcher(
        task_repo=task_repo,
        queues={
            download_queue.name: download_queue,
            upload_queue.name: upload_queue,
        },
    )
    dispatched = await _dispatch_pending_for_queue(
        task_repo=task_repo,
        dispatcher=dispatcher,
        target_queue_name=download_queue.name,
    )
    logger.info("dispatched %d task(s) into %s", dispatched, download_queue.name)
    if dispatched == 0:
        raise RuntimeError("no pending tasks were dispatched")


async def _run_media_loader_stage(
    *,
    settings: Settings,
    video_repo: VideoRepository,
    download_queue: TaskQueue,
) -> None:
    logger.info("[Stage 3] Verify Media-Loader handoff + qBittorrent")
    payload = await download_queue.pop(timeout=10)
    if payload is None:
        raise RuntimeError("download queue pop timeout; no payload received")
    logger.info("popped payload: %s", payload)

    video_id_str = payload.get("video_id")
    if not isinstance(video_id_str, str):
        raise RuntimeError(f"payload missing video_id: {payload}")

    video_id = uuid.UUID(video_id_str)
    video = await video_repo.find_by_id(video_id)
    if video is None:
        raise RuntimeError(f"video not found in DB: {video_id}")

    qbit = QBitClient(
        base_url=settings.qbit_url,
        username=settings.qbit_user,
        password=settings.qbit_password,
        download_dir=settings.download_dir,
    )
    logger.info("running qBittorrent health check: %s", settings.qbit_url)
    version = await qbit.health_check()
    logger.info("qBittorrent health check passed (version=%s)", version)

    info_hash = await qbit.add_magnet(video.magnet_uri)
    logger.info("submitted magnet to qBittorrent (hash=%s)", info_hash)
    await qbit.delete_torrent(info_hash)
    logger.info("cleanup completed: deleted qBittorrent torrent %s", info_hash)


async def main() -> None:
    settings = get_settings()
    run_id = uuid.uuid4().hex[:8]
    seed_magnet = os.getenv("PIXAV_E2E_MAGNET_URI", "").strip()
    seed_url = ""
    seed_tags: list[str] = []
    if not seed_magnet:
        seed_url, seed_tags = _pick_seed(settings)

    link_pattern = os.getenv("PIXAV_E2E_LINK_PATTERN", settings.crawl_link_filter_pattern).strip()

    crawl_queue_name, download_queue_name, upload_queue_name = _queue_names(settings, run_id)
    pool = await create_pool(settings)
    redis = await create_redis(settings)

    try:
        video_repo = VideoRepository(pool)
        task_repo = TaskRepository(pool)
        crawl_queue = TaskQueue(redis, crawl_queue_name)
        download_queue = TaskQueue(redis, download_queue_name)
        upload_queue = TaskQueue(redis, upload_queue_name)

        if seed_magnet:
            info_hash = _extract_info_hash(seed_magnet)
            if not info_hash:
                raise RuntimeError("PIXAV_E2E_MAGNET_URI is invalid (missing btih info_hash)")

            from pixav.shared.enums import VideoStatus
            from pixav.shared.models import Video

            video = Video(
                title=f"E2E Magnet Seed {run_id}",
                magnet_uri=seed_magnet,
                info_hash=info_hash,
                tags=["e2e-live", "seed", f"e2e-run-{run_id}"],
                status=VideoStatus.DISCOVERED,
            )
            await video_repo.insert(video)
            await crawl_queue.push({"video_id": str(video.id), "magnet_uri": seed_magnet})
        else:
            await _run_crawl_stage(
                settings=settings,
                video_repo=video_repo,
                crawl_queue=crawl_queue,
                seed_url=seed_url,
                link_pattern=link_pattern,
                seed_tags=seed_tags,
                run_id=run_id,
            )
        await _run_ingest_dispatch_stage(
            settings=settings,
            video_repo=video_repo,
            task_repo=task_repo,
            crawl_queue=crawl_queue,
            download_queue=download_queue,
            upload_queue=upload_queue,
        )
        await _run_media_loader_stage(
            settings=settings,
            video_repo=video_repo,
            download_queue=download_queue,
        )

        logger.info("E2E verification succeeded for run_id=%s", run_id)
    finally:
        # Keep integration queues isolated and clean after each run.
        await redis.delete(crawl_queue_name, download_queue_name, upload_queue_name)
        await redis.aclose()
        await pool.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    asyncio.run(main())
