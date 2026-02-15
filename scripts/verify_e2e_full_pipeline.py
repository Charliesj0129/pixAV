"""Live E2E verification: SHT-Probe -> Maxwell-Core -> Media-Loader -> Pixel-Injector -> Strm-Resolver.

This script runs a real integration flow without mocks. It is intended for
manual verification on a single host with Docker Compose infra running:

  - postgres
  - redis
  - flaresolverr
  - qbittorrent

Upload stage note:
  For an actually "stable" MVP loop on a single old server, this script uses a
  LOCAL upload mode (no Redroid/ADB/Google Photos automation). It writes a
  synthetic share_url scheme that strm_resolver can resolve to /local/{video_id}.

Run:
  uv run python scripts/verify_e2e_full_pipeline.py

Useful env vars:
  - PIXAV_E2E_MAGNET_URI (skip live crawl; seed one magnet into the pipeline)
  - PIXAV_E2E_SEED_URL / PIXAV_E2E_LINK_PATTERN
  - PIXAV_CRAWL_COOKIE_HEADER / PIXAV_CRAWL_COOKIE_FILE
  - PIXAV_E2E_ISOLATED_DB=1 (default) to create/drop a temp DB
  - PIXAV_E2E_ADMIN_DSN=postgresql://user:pass@host:port/postgres (optional override)
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from pathlib import Path
from urllib.parse import ParseResult, urlparse, urlunparse

import asyncpg
from httpx import ASGITransport, AsyncClient

from pixav.config import Settings, get_settings
from pixav.maxwell_core.dispatcher import RedisTaskDispatcher
from pixav.maxwell_core.scheduler import LruAccountScheduler
from pixav.maxwell_core.worker import ingest_crawl_queue
from pixav.media_loader.qbittorrent import QBitClient
from pixav.media_loader.remuxer import FFmpegRemuxer
from pixav.media_loader.service import MediaLoaderService
from pixav.pixel_injector.service import LocalPixelInjectorService
from pixav.shared.cookies import load_cookies
from pixav.shared.enums import TaskState, VideoStatus
from pixav.shared.exceptions import CrawlError
from pixav.shared.models import Task, Video
from pixav.shared.queue import TaskQueue
from pixav.shared.redis_client import create_redis
from pixav.shared.repository import AccountRepository, TaskRepository, VideoRepository
from pixav.sht_probe.crawler import HttpxCrawler
from pixav.sht_probe.flaresolverr_client import FlareSolverrSession
from pixav.sht_probe.parser import BeautifulSoupExtractor
from pixav.sht_probe.service import ShtProbeService
from pixav.strm_resolver.app import create_app

logger = logging.getLogger("verify_e2e_full_pipeline")

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_MIGRATIONS_DIR = _PROJECT_ROOT / "migrations"


def _extract_info_hash(magnet_uri: str) -> str | None:
    import re

    match = re.search(r"btih:([a-fA-F0-9]{40}|[a-zA-Z2-7]{32})", magnet_uri)
    if match:
        return match.group(1).lower()
    return None


def _replace_db_name(dsn: str, db_name: str) -> str:
    parsed: ParseResult = urlparse(dsn)
    return urlunparse(parsed._replace(path=f"/{db_name}"))


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
            raw_tags = raw_tags.replace(",", "+")
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
    if entries:
        url, tags = entries[0]
        merged = list(dict.fromkeys([*tags, "e2e-live"]))
        return url, merged

    raise RuntimeError(
        "no crawl seed configured; set PIXAV_E2E_SEED_URL or PIXAV_CRAWL_SEED_URLS, or use PIXAV_E2E_MAGNET_URI"
    )


def _queue_names(settings: Settings, run_id: str) -> tuple[str, str, str]:
    return (
        f"{settings.queue_crawl}:e2e:{run_id}",
        f"{settings.queue_download}:e2e:{run_id}",
        f"{settings.queue_upload}:e2e:{run_id}",
    )


async def _create_isolated_db(settings: Settings, *, run_id: str) -> tuple[str, str]:
    base_dsn = settings.dsn
    admin_dsn = os.getenv("PIXAV_E2E_ADMIN_DSN", "").strip()
    if not admin_dsn:
        admin_dsn = _replace_db_name(base_dsn, "postgres")

    db_name = f"pixav_e2e_full_{run_id}_{uuid.uuid4().hex[:6]}"
    admin_conn = await asyncpg.connect(admin_dsn)
    try:
        await admin_conn.execute(f'CREATE DATABASE "{db_name}"')
    finally:
        await admin_conn.close()

    target_dsn = _replace_db_name(admin_dsn, db_name)
    await _apply_migrations(target_dsn)
    return target_dsn, db_name


async def _apply_migrations(dsn: str) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS _migrations (
                filename TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """)

        applied: set[str] = {row["filename"] for row in await conn.fetch("SELECT filename FROM _migrations")}

        sql_files = sorted(_MIGRATIONS_DIR.glob("*.sql"))
        for path in sql_files:
            name = path.name
            if name in applied:
                continue
            sql = path.read_text(encoding="utf-8")
            await conn.execute(sql)
            await conn.execute("INSERT INTO _migrations (filename) VALUES ($1)", name)
    finally:
        await conn.close()


async def _drop_isolated_db(admin_dsn: str, db_name: str) -> None:
    admin_conn = await asyncpg.connect(admin_dsn)
    try:
        await admin_conn.execute(
            """
            SELECT pg_terminate_backend(pid)
              FROM pg_stat_activity
             WHERE datname = $1
               AND pid <> pg_backend_pid()
            """,
            db_name,
        )
        await admin_conn.execute(f'DROP DATABASE IF EXISTS "{db_name}"')
    finally:
        await admin_conn.close()


async def _ensure_active_account(pool: asyncpg.Pool, *, run_id: str) -> None:
    active = await pool.fetchval("SELECT count(*) FROM accounts WHERE status = 'active'")
    if int(active) > 0:
        return
    await pool.execute(
        "INSERT INTO accounts (email, status) VALUES ($1, 'active')",
        f"e2e-{run_id}@local",
    )


async def _dispatch_pending_for_queue(
    *,
    task_repo: TaskRepository,
    dispatcher: RedisTaskDispatcher,
    target_queue_name: str,
    next_state: TaskState,
    limit: int = 50,
) -> list[uuid.UUID]:
    pending = await task_repo.list_pending(limit=limit)
    target_tasks = [task for task in pending if task.queue_name == target_queue_name]

    dispatched: list[uuid.UUID] = []
    for task in target_tasks:
        await dispatcher.dispatch(str(task.id), target_queue_name)
        await task_repo.update_state(task.id, next_state)
        dispatched.append(task.id)

    return dispatched


async def main() -> None:
    settings = get_settings()
    run_id = uuid.uuid4().hex[:8]

    isolated_db = os.getenv("PIXAV_E2E_ISOLATED_DB", "1").strip() != "0"
    seed_magnet = os.getenv("PIXAV_E2E_MAGNET_URI", "").strip()
    seed_url = ""
    seed_tags: list[str] = []
    if not seed_magnet:
        seed_url, seed_tags = _pick_seed(settings)

    link_pattern = os.getenv("PIXAV_E2E_LINK_PATTERN", settings.crawl_link_filter_pattern).strip()

    crawl_queue_name, download_queue_name, upload_queue_name = _queue_names(settings, run_id)
    output_dir = str(Path(settings.download_dir) / "e2e" / run_id)

    # DB wiring (isolated by default to avoid polluting real data)
    admin_dsn = os.getenv("PIXAV_E2E_ADMIN_DSN", "").strip() or _replace_db_name(settings.dsn, "postgres")
    db_dsn = settings.dsn
    db_name = ""
    if isolated_db:
        logger.info("creating isolated E2E database (run_id=%s)", run_id)
        db_dsn, db_name = await _create_isolated_db(settings, run_id=run_id)

    pool = await asyncpg.create_pool(dsn=db_dsn, min_size=1, max_size=5)
    redis = await create_redis(settings)

    placeholder_path: str | None = None

    try:
        await redis.ping()
        video_repo = VideoRepository(pool)
        task_repo = TaskRepository(pool)
        account_repo = AccountRepository(pool)

        crawl_queue = TaskQueue(redis=redis, queue_name=crawl_queue_name)
        download_queue = TaskQueue(redis=redis, queue_name=download_queue_name)
        upload_queue = TaskQueue(redis=redis, queue_name=upload_queue_name)

        if seed_magnet:
            logger.info("[Stage 1] E2E magnet seed provided; skipping live crawl")
            info_hash = _extract_info_hash(seed_magnet)
            if not info_hash:
                raise RuntimeError("PIXAV_E2E_MAGNET_URI is invalid (missing btih info_hash)")

            video = Video(
                title=f"E2E Magnet Seed {run_id}",
                magnet_uri=seed_magnet,
                info_hash=info_hash,
                tags=["e2e-live", "seed", f"e2e-run-{run_id}"],
                status=VideoStatus.DISCOVERED,
            )
            await video_repo.insert(video)
            await crawl_queue.push({"video_id": str(video.id), "magnet_uri": seed_magnet})
            discovered = [seed_magnet]
        else:
            # Stage 1: Crawl seed URL (real network)
            logger.info("[Stage 1] SHT-Probe crawl: %s", seed_url)
            flaresolverr = FlareSolverrSession(settings.flaresolverr_url) if settings.flaresolverr_url else None
            crawler = HttpxCrawler(flaresolverr=flaresolverr, timeout=60)
            cookies, cookie_source = load_cookies(
                cookie_header=settings.crawl_cookie_header,
                cookie_file=settings.crawl_cookie_file,
            )
            if cookies:
                crawler.seed_cookies(cookies)
                logger.info("seeded %d crawl cookie(s) from %s", len(cookies), cookie_source)

            extractor = BeautifulSoupExtractor()
            probe = ShtProbeService(
                video_repo=video_repo,
                queue=crawl_queue,
                crawler=crawler,
                extractor=extractor,
                min_quality_score=-10000,
            )

            try:
                await crawler.fetch_page_html(seed_url)
            except CrawlError as exc:
                raise RuntimeError(
                    "failed to fetch seed URL; ensure the URL is reachable and, if needed, "
                    "configure PIXAV_CRAWL_COOKIE_HEADER/PIXAV_CRAWL_COOKIE_FILE. "
                    "If the site requires JS challenges, ensure FlareSolverr is reachable at PIXAV_FLARESOLVERR_URL"
                ) from exc

            tags = list(dict.fromkeys([*seed_tags, f"e2e-run-{run_id}"]))
            discovered = await probe.run_crawl(
                seed_url,
                link_pattern=link_pattern,
                tags=tags,
                max_pages=int(os.getenv("PIXAV_E2E_CRAWL_MAX_PAGES", "10")),
            )
            logger.info("crawl discovered %d new magnet(s)", len(discovered))
            if not discovered:
                raise RuntimeError("no new magnets discovered; try another subforum or a cleaner database")

        # Stage 2: Maxwell-Core ingest (create one pending download task)
        logger.info("[Stage 2] Maxwell-Core ingest (batch_size=1)")
        ingested = await ingest_crawl_queue(
            crawl_queue=crawl_queue,
            task_repo=task_repo,
            video_repo=video_repo,
            download_queue_name=download_queue.name,
            max_retries=settings.download_max_retries,
            batch_size=1,
        )
        if ingested == 0:
            raise RuntimeError("ingest produced zero tasks")

        dispatcher = RedisTaskDispatcher(
            task_repo=task_repo,
            queues={
                download_queue.name: download_queue,
                upload_queue.name: upload_queue,
            },
        )

        dispatched_download = await _dispatch_pending_for_queue(
            task_repo=task_repo,
            dispatcher=dispatcher,
            target_queue_name=download_queue.name,
            next_state=TaskState.DOWNLOADING,
            limit=10,
        )
        if not dispatched_download:
            raise RuntimeError("no pending download tasks were dispatched")
        task_id = dispatched_download[0]
        logger.info("dispatched download task: %s", task_id)

        # Stage 3: Media-Loader (verify mode)
        logger.info("[Stage 3] Media-Loader verify mode + qBittorrent")
        qbit = QBitClient(
            base_url=settings.qbit_url,
            username=settings.qbit_user,
            password=settings.qbit_password,
            download_dir=settings.download_dir,
        )
        version = await qbit.health_check()
        logger.info("qBittorrent ok (version=%s)", version)

        payload = await download_queue.pop(timeout=10)
        if payload is None:
            raise RuntimeError("download queue pop timeout")
        raw_task_id = payload.get("task_id")
        raw_video_id = payload.get("video_id")
        if not isinstance(raw_task_id, str) or not isinstance(raw_video_id, str):
            raise RuntimeError(f"invalid download payload: {payload}")

        download_task = Task(
            id=uuid.UUID(raw_task_id),
            video_id=uuid.UUID(raw_video_id),
            state=TaskState.PENDING,
            queue_name=download_queue.name,
            retries=int(payload.get("retries", 0) or 0),
            max_retries=int(payload.get("max_retries", settings.download_max_retries) or settings.download_max_retries),
        )

        media = MediaLoaderService(
            client=qbit,
            remuxer=FFmpegRemuxer(),
            scraper=None,
            video_repo=video_repo,
            task_repo=task_repo,
            upload_queue_name=upload_queue.name,
            retry_queue=download_queue,
            dlq_queue=None,
            output_dir=output_dir,
            mode="verify",
        )
        media_result = await media.process_task(download_task)
        if not media_result.local_path:
            raise RuntimeError("media-loader did not produce local_path")
        placeholder_path = media_result.local_path
        logger.info("media-loader produced placeholder file: %s", placeholder_path)

        # Stage 4: Maxwell-Core dispatch upload task (needs an active account)
        logger.info("[Stage 4] Maxwell-Core schedule+dispatch upload")
        await _ensure_active_account(pool, run_id=run_id)
        scheduler = LruAccountScheduler(pool)
        account_id = await scheduler.next_account()

        pending = await task_repo.list_pending(limit=20)
        upload_tasks = [t for t in pending if t.queue_name == upload_queue.name and t.video_id == media_result.video_id]
        if not upload_tasks:
            raise RuntimeError("no pending upload task found after media-loader route_to_queue")

        upload_task_row = upload_tasks[0]
        await task_repo.assign_account(upload_task_row.id, account_id)
        await dispatcher.dispatch(str(upload_task_row.id), upload_queue.name)
        await task_repo.update_state(upload_task_row.id, TaskState.UPLOADING)
        await scheduler.mark_used(account_id)

        # Stage 5: Pixel-Injector (LOCAL mode)
        logger.info("[Stage 5] Pixel-Injector LOCAL mode")
        upload_payload = await upload_queue.pop(timeout=10)
        if upload_payload is None:
            raise RuntimeError("upload queue pop timeout")
        raw_task_id = upload_payload.get("task_id")
        raw_video_id = upload_payload.get("video_id")
        if not isinstance(raw_task_id, str) or not isinstance(raw_video_id, str):
            raise RuntimeError(f"invalid upload payload: {upload_payload}")

        video = await video_repo.find_by_id(uuid.UUID(raw_video_id))
        if video is None or not video.local_path:
            raise RuntimeError("video local_path missing in DB for upload stage")

        injector = LocalPixelInjectorService(share_scheme=settings.pixel_injector_local_share_scheme)
        upload_task = Task(
            id=uuid.UUID(raw_task_id),
            video_id=uuid.UUID(raw_video_id),
            account_id=uuid.UUID(account_id),
            state=TaskState.UPLOADING,
            queue_name=upload_queue.name,
            local_path=video.local_path,
            retries=int(upload_payload.get("retries", 0) or 0),
            max_retries=int(
                upload_payload.get("max_retries", settings.upload_max_retries) or settings.upload_max_retries
            ),
        )

        await task_repo.update_state(upload_task.id, TaskState.UPLOADING)
        await video_repo.update_status(upload_task.video_id, VideoStatus.UPLOADING)
        inject_result = await injector.process_task(upload_task)
        if inject_result.state != TaskState.COMPLETE or not inject_result.share_url:
            raise RuntimeError(f"pixel-injector local mode failed: {inject_result.error_message}")

        await task_repo.update_state(upload_task.id, TaskState.COMPLETE)
        await video_repo.update_upload_result(upload_task.video_id, share_url=inject_result.share_url)
        uploaded_bytes = os.path.getsize(video.local_path)
        await account_repo.apply_upload_usage(uuid.UUID(account_id), uploaded_bytes)
        logger.info("upload stage complete (share_url=%s)", inject_result.share_url)

        # Stage 6: Strm-Resolver resolve+stream
        logger.info("[Stage 6] Strm-Resolver resolve+stream+local")
        app = create_app(redis_url=None, db_dsn=None)
        # httpx.ASGITransport in our httpx version does not run lifespan hooks,
        # so wire live resources directly for the route handlers.
        app.state.db_pool = pool
        app.state.redis = redis
        app.state.local_share_scheme = settings.pixel_injector_local_share_scheme
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(f"/resolve/{upload_task.video_id}")
            resp.raise_for_status()
            payload = resp.json()
            logger.info("resolve result: %s", payload)

            stream = await client.get(f"/stream/{upload_task.video_id}", follow_redirects=False)
            if stream.status_code != 302:
                raise RuntimeError(f"unexpected stream status: {stream.status_code} {stream.text[:200]}")

            local = await client.get(f"/local/{upload_task.video_id}")
            local.raise_for_status()
            if not local.content:
                raise RuntimeError("local stream returned empty body")

        logger.info("E2E FULL pipeline verification succeeded (run_id=%s)", run_id)

    finally:
        try:
            await redis.delete(crawl_queue_name, download_queue_name, upload_queue_name)
        except Exception as exc:
            logger.debug("failed to cleanup E2E redis queues: %s", exc)
        await redis.aclose()
        await pool.close()

        if placeholder_path:
            try:
                Path(placeholder_path).unlink(missing_ok=True)
                Path(output_dir).rmdir()
            except Exception as exc:
                logger.debug("failed to cleanup placeholder/output_dir: %s", exc)

        if isolated_db and db_name:
            logger.info("dropping isolated E2E database %s", db_name)
            await _drop_isolated_db(admin_dsn, db_name)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    asyncio.run(main())
