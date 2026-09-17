"""Live E2E verification: SHT-Probe -> Maxwell-Core -> Media-Loader -> Pixel-Injector -> Strm-Resolver.

This script runs a real integration flow without mocks. It is intended for
manual verification on a single host with Docker Compose infra running:

  - postgres
  - redis
  - flaresolverr (only for a live crawl that needs it)
  - qbittorrent (only when PIXAV_E2E_LOCAL_MEDIA_PATH is unset)

Upload stage note:
  For an actually "stable" MVP loop on a single old server, this script uses a
  LOCAL upload mode (no Redroid/ADB/Google Photos automation). It writes a
  synthetic share_url scheme that strm_resolver can resolve to /local/{video_id}.

Run:
  uv run python scripts/verify_e2e_full_pipeline.py

Useful env vars:
  - PIXAV_E2E_FIXTURE=sintel (legal fixed fixture; forces isolated full/qBit mode)
  - PIXAV_E2E_MAGNET_URI (skip live crawl; seed one magnet into the pipeline)
  - PIXAV_E2E_LOCAL_MEDIA_PATH (reuse an existing media file and make no qBit call)
  - PIXAV_E2E_MEDIA_MODE=verify|full (default verify; local media always uses full)
  - PIXAV_E2E_SEED_URL / PIXAV_E2E_LINK_PATTERN
  - PIXAV_CRAWL_COOKIE_HEADER / PIXAV_CRAWL_COOKIE_FILE
  - PIXAV_E2E_ISOLATED_DB=1 (default) to create/drop a temp DB
  - PIXAV_E2E_EXPECT_DB_IDENTITY (required when PIXAV_E2E_ISOLATED_DB=0)
  - PIXAV_E2E_ADMIN_DSN=postgresql://user:pass@host:port/postgres (optional override)
  - PIXAV_E2E_EXTERNAL_HTTP_PORT (serve the isolated resolver to a real player)
  - PIXAV_E2E_EXTERNAL_HTTP_TIMEOUT_SECONDS (default 120; requires explicit acknowledgement)
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import ParseResult, urlparse, urlunparse

import asyncpg
import uvicorn
from httpx import ASGITransport, AsyncClient

from pixav.config import Settings, get_settings
from pixav.maxwell_core.dispatcher import RedisTaskDispatcher
from pixav.maxwell_core.gc import safe_cleanup_candidate
from pixav.maxwell_core.scheduler import LruAccountScheduler
from pixav.maxwell_core.worker import ingest_crawl_queue
from pixav.media_loader.interfaces import TorrentClient
from pixav.media_loader.metadata import probe_media
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
from pixav.shared.repository import (
    AccountRepository,
    SourceCandidateRepository,
    TaskRepository,
    VideoRepository,
)
from pixav.sht_probe.crawler import HttpxCrawler
from pixav.sht_probe.flaresolverr_client import FlareSolverrSession
from pixav.sht_probe.parser import BeautifulSoupExtractor
from pixav.sht_probe.service import ShtProbeService
from pixav.strm_resolver.app import create_app

logger = logging.getLogger("verify_e2e_full_pipeline")

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_MIGRATIONS_DIR = _PROJECT_ROOT / "migrations"
_SINTEL_INFO_HASH = "08ada5a7a6183aae1e09d831df6748d566095a10"
_SINTEL_MAGNET = (
    f"magnet:?xt=urn:btih:{_SINTEL_INFO_HASH}&dn=Sintel"
    "&tr=udp%3A%2F%2Fexplodie.org%3A6969"
    "&tr=udp%3A%2F%2Ftracker.coppersurfer.tk%3A6969"
    "&tr=udp%3A%2F%2Ftracker.empire-js.us%3A1337"
    "&tr=udp%3A%2F%2Ftracker.leechers-paradise.org%3A6969"
    "&tr=udp%3A%2F%2Ftracker.opentrackr.org%3A1337"
    "&tr=wss%3A%2F%2Ftracker.btorrent.xyz"
    "&tr=wss%3A%2F%2Ftracker.fastcast.nz"
    "&tr=wss%3A%2F%2Ftracker.openwebtorrent.com"
    "&ws=https%3A%2F%2Fwebtorrent.io%2Ftorrents%2F"
    "&xs=https%3A%2F%2Fwebtorrent.io%2Ftorrents%2Fsintel.torrent"
)


class _NetworkBlockedTorrentClient:
    """Fail closed if the local-media path accidentally reaches torrent I/O."""

    async def add_magnet(self, uri: str) -> str:
        raise RuntimeError("torrent I/O is disabled by PIXAV_E2E_LOCAL_MEDIA_PATH")

    async def wait_complete(self, torrent_hash: str, timeout: int | None = None) -> str:
        raise RuntimeError("torrent I/O is disabled by PIXAV_E2E_LOCAL_MEDIA_PATH")

    async def delete_torrent(self, torrent_hash: str, delete_files: bool = True) -> None:
        raise RuntimeError("torrent I/O is disabled by PIXAV_E2E_LOCAL_MEDIA_PATH")


class _RecordingTorrentClient:
    """Record the real qBit lifecycle so the live fixture can prove cleanup."""

    def __init__(self, delegate: QBitClient) -> None:
        self._delegate = delegate
        self.attempted_hash: str | None = None
        self.added_hash: str | None = None
        self.completed_path: str | None = None
        self.deleted_hash: str | None = None
        self.deleted_files = False

    async def add_magnet(self, uri: str) -> str:
        self.attempted_hash = _extract_info_hash(uri)
        self.added_hash = await self._delegate.add_magnet(uri)
        return self.added_hash

    async def wait_complete(self, torrent_hash: str, timeout: int | None = None) -> str:
        self.completed_path = await self._delegate.wait_complete(torrent_hash, timeout=timeout)
        return self.completed_path

    async def delete_torrent(self, torrent_hash: str, delete_files: bool = True) -> None:
        await self._delegate.delete_torrent(torrent_hash, delete_files=delete_files)
        self.deleted_hash = torrent_hash
        self.deleted_files = delete_files


def _resolve_fixture_preset(
    fixture: str,
    *,
    seed_magnet: str,
    local_media_path: str,
    isolated_db: bool,
    requested_media_mode: str,
) -> tuple[str, str]:
    """Apply a legal fixture preset while rejecting paths that bypass qBit."""
    normalized = fixture.strip().lower()
    if not normalized:
        return seed_magnet, requested_media_mode
    if normalized != "sintel":
        raise RuntimeError("PIXAV_E2E_FIXTURE must be empty or 'sintel'")
    if seed_magnet:
        raise RuntimeError("PIXAV_E2E_FIXTURE and PIXAV_E2E_MAGNET_URI are mutually exclusive")
    if local_media_path:
        raise RuntimeError("the Sintel fixture forbids PIXAV_E2E_LOCAL_MEDIA_PATH")
    if not isolated_db:
        raise RuntimeError("the Sintel fixture requires PIXAV_E2E_ISOLATED_DB=1")
    return _SINTEL_MAGNET, "full"


async def _wait_torrent_absent(client: QBitClient, torrent_hash: str, *, timeout_seconds: int = 15) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while await client.has_torrent(torrent_hash):
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError(f"qBittorrent still owns fixture torrent {torrent_hash} after cleanup")
        await asyncio.sleep(1)


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


async def _serve_until_acknowledged(
    app: Any,
    *,
    port: int,
    completion_event: asyncio.Event,
    timeout_seconds: int,
) -> None:
    """Expose the isolated resolver until an external player run is acknowledged."""
    config = uvicorn.Config(
        app,
        host="0.0.0.0",  # noqa: S104 - opt-in test window must be reachable from a player container
        port=port,
        log_level="info",
        access_log=True,
        lifespan="off",
    )
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())
    try:
        deadline = asyncio.get_running_loop().time() + 10
        while not server.started:
            if server_task.done():
                await server_task
                raise RuntimeError("external E2E HTTP server stopped before becoming ready")
            if asyncio.get_running_loop().time() >= deadline:
                raise RuntimeError("external E2E HTTP server readiness timeout")
            await asyncio.sleep(0.05)

        try:
            await asyncio.wait_for(completion_event.wait(), timeout=timeout_seconds)
        except asyncio.TimeoutError as exc:
            raise RuntimeError("external player acknowledgement timeout") from exc
    finally:
        server.should_exit = True
        await server_task


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


def _parse_media_mode(raw: str) -> str:
    mode = raw.strip().lower() or "verify"
    if mode not in {"verify", "full"}:
        raise RuntimeError("PIXAV_E2E_MEDIA_MODE must be 'verify' or 'full'")
    return mode


async def _assert_db_identity(pool: asyncpg.Pool, expected: str) -> str:
    """Fail closed before an E2E run writes to a non-isolated database."""
    expected = expected.strip()
    if not expected:
        raise RuntimeError("PIXAV_E2E_EXPECT_DB_IDENTITY is required when PIXAV_E2E_ISOLATED_DB=0")
    live = str(await pool.fetchval("SELECT system_identifier FROM pg_control_system()"))
    if live != expected:
        raise RuntimeError(f"database identity mismatch: expected {expected}, got {live}")
    return live


def resolve_admin_dsn(settings: Settings) -> str:
    """Return the maintenance DSN used to create and drop isolated databases.

    Creation and cleanup must resolve this identically, otherwise a
    ``PIXAV_E2E_ADMIN_DSN`` override can create the test database on one server
    and issue ``DROP DATABASE IF EXISTS`` against another — which reports
    success while leaking the real database.
    """
    override = os.getenv("PIXAV_E2E_ADMIN_DSN", "").strip()
    return override or _replace_db_name(settings.dsn, "postgres")


async def _create_isolated_db(settings: Settings, *, run_id: str) -> tuple[str, str, str]:
    """Create a throwaway database and return ``(target_dsn, db_name, admin_dsn)``.

    The admin DSN is returned so callers cannot re-derive a different one for
    cleanup.
    """
    admin_dsn = resolve_admin_dsn(settings)

    db_name = f"pixav_e2e_full_{run_id}_{uuid.uuid4().hex[:6]}"
    admin_conn = await asyncpg.connect(admin_dsn)
    try:
        await admin_conn.execute(f'CREATE DATABASE "{db_name}"')
    finally:
        await admin_conn.close()

    target_dsn = _replace_db_name(admin_dsn, db_name)
    await _apply_migrations(target_dsn)
    return target_dsn, db_name, admin_dsn


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
    requested_media_mode = _parse_media_mode(os.getenv("PIXAV_E2E_MEDIA_MODE", "verify"))
    fixture_name = os.getenv("PIXAV_E2E_FIXTURE", "").strip().lower()
    seed_magnet = os.getenv("PIXAV_E2E_MAGNET_URI", "").strip()
    local_media_raw = os.getenv("PIXAV_E2E_LOCAL_MEDIA_PATH", "").strip()
    seed_magnet, requested_media_mode = _resolve_fixture_preset(
        fixture_name,
        seed_magnet=seed_magnet,
        local_media_path=local_media_raw,
        isolated_db=isolated_db,
        requested_media_mode=requested_media_mode,
    )
    local_media_path: Path | None = None
    if local_media_raw:
        local_media_path = Path(local_media_raw).expanduser().resolve(strict=True)
        if not local_media_path.is_file():
            raise RuntimeError(f"PIXAV_E2E_LOCAL_MEDIA_PATH is not a regular file: {local_media_path}")
        if local_media_path.stat().st_size <= 0:
            raise RuntimeError(f"PIXAV_E2E_LOCAL_MEDIA_PATH is empty: {local_media_path}")
    external_http_port = int(os.getenv("PIXAV_E2E_EXTERNAL_HTTP_PORT", "0").strip() or "0")
    external_http_timeout = int(os.getenv("PIXAV_E2E_EXTERNAL_HTTP_TIMEOUT_SECONDS", "120").strip() or "120")
    if external_http_port and not 1 <= external_http_port <= 65535:
        raise RuntimeError("PIXAV_E2E_EXTERNAL_HTTP_PORT must be between 1 and 65535")
    if external_http_timeout <= 0:
        raise RuntimeError("PIXAV_E2E_EXTERNAL_HTTP_TIMEOUT_SECONDS must be positive")
    seed_url = ""
    seed_tags: list[str] = []
    if not seed_magnet:
        seed_url, seed_tags = _pick_seed(settings)

    link_pattern = os.getenv("PIXAV_E2E_LINK_PATTERN", settings.crawl_link_filter_pattern).strip()

    crawl_queue_name, download_queue_name, upload_queue_name = _queue_names(settings, run_id)
    media_mode = "full" if local_media_path is not None else requested_media_mode
    output_root = settings.remux_dir if media_mode == "full" else settings.download_dir
    output_dir = str(Path(output_root) / "e2e" / run_id)

    # DB wiring (isolated by default to avoid polluting real data)

    db_dsn = settings.dsn
    db_name = ""
    admin_dsn = resolve_admin_dsn(settings)
    if isolated_db:
        logger.info("creating isolated E2E database (run_id=%s)", run_id)
        db_dsn, db_name, admin_dsn = await _create_isolated_db(settings, run_id=run_id)

    pool = await asyncpg.create_pool(dsn=db_dsn, min_size=1, max_size=5)
    if not isolated_db:
        expected_identity = os.getenv("PIXAV_E2E_EXPECT_DB_IDENTITY", "")
        live_identity = await _assert_db_identity(pool, expected_identity)
        logger.info("authoritative database identity verified: %s", live_identity)
    redis = await create_redis(settings)

    placeholder_path: str | None = None
    qbit: QBitClient | None = None
    recording_qbit: _RecordingTorrentClient | None = None
    resolver_cache_key: str | None = None

    try:
        await redis.ping()
        video_repo = VideoRepository(pool)
        task_repo = TaskRepository(pool)
        account_repo = AccountRepository(pool)
        candidate_repo = SourceCandidateRepository(pool)

        crawl_queue = TaskQueue(redis=redis, queue_name=crawl_queue_name)
        download_queue = TaskQueue(redis=redis, queue_name=download_queue_name)
        upload_queue = TaskQueue(redis=redis, queue_name=upload_queue_name)

        if seed_magnet:
            logger.info("[Stage 1] E2E magnet seed provided; skipping live crawl")
            info_hash = _extract_info_hash(seed_magnet)
            if not info_hash:
                raise RuntimeError("PIXAV_E2E_MAGNET_URI is invalid (missing btih info_hash)")

            video = Video(
                title="Sintel (CC BY 3.0)" if fixture_name == "sintel" else f"E2E Magnet Seed {run_id}",
                magnet_uri=seed_magnet,
                info_hash=info_hash,
                tags=[
                    "e2e-live",
                    "seed",
                    *(("cc-by-3.0",) if fixture_name == "sintel" else ()),
                    f"e2e-run-{run_id}",
                ],
                status=VideoStatus.DISCOVERED,
            )
            await video_repo.insert(video)
            await candidate_repo.register(
                video.id,
                magnet_uri=seed_magnet,
                info_hash=info_hash,
                origin="webtorrent-sintel" if fixture_name == "sintel" else "e2e-seed",
            )
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
                candidate_repo=candidate_repo,
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

        # Stage 3: Media-Loader. The explicit local-media path exercises the
        # production resume fast path and is intentionally unable to contact a
        # torrent client. This keeps the non-VPN acceptance route fail-closed.
        payload = await download_queue.pop(timeout=10)
        if payload is None:
            raise RuntimeError("download queue pop timeout")
        raw_task_id = payload.get("task_id")
        raw_video_id = payload.get("video_id")
        if not isinstance(raw_task_id, str) or not isinstance(raw_video_id, str):
            raise RuntimeError(f"invalid download payload: {payload}")
        resolver_cache_key = f"pixav:cdn:{raw_video_id}"

        download_task = Task(
            id=uuid.UUID(raw_task_id),
            video_id=uuid.UUID(raw_video_id),
            state=TaskState.PENDING,
            queue_name=download_queue.name,
            retries=int(payload.get("retries", 0) or 0),
            max_retries=int(payload.get("max_retries", settings.download_max_retries) or settings.download_max_retries),
        )

        torrent_client: TorrentClient
        if local_media_path is not None:
            logger.info("[Stage 3] Media-Loader local resume path (qBittorrent disabled)")
            await video_repo.update_download_result(
                download_task.video_id,
                local_path=str(local_media_path),
                metadata_json=None,
            )
            torrent_client = _NetworkBlockedTorrentClient()
        else:
            logger.info("[Stage 3] Media-Loader %s mode + qBittorrent", media_mode)
            qbit = QBitClient(
                base_url=settings.qbit_url,
                username=settings.qbit_user,
                password=settings.qbit_password,
                download_dir=settings.qbit_download_dir,
                local_download_dir=settings.download_dir,
                extra_trackers=(),
                download_timeout=settings.qbit_download_timeout_seconds,
                no_peer_grace_seconds=settings.qbit_no_peer_grace_seconds,
            )
            version = await qbit.health_check()
            logger.info("qBittorrent ok (version=%s)", version)
            if fixture_name == "sintel" and await qbit.has_torrent(_SINTEL_INFO_HASH):
                raise RuntimeError(
                    "Sintel already exists in qBittorrent; remove or preserve it explicitly before running the fixture"
                )
            recording_qbit = _RecordingTorrentClient(qbit)
            torrent_client = recording_qbit

        media = MediaLoaderService(
            client=torrent_client,
            remuxer=FFmpegRemuxer(),
            scraper=None,
            video_repo=video_repo,
            task_repo=task_repo,
            candidate_repo=candidate_repo,
            upload_queue_name=upload_queue.name,
            output_dir=output_dir,
            mode=media_mode,
        )
        media_result = await media.process_task(download_task)
        if not media_result.local_path:
            raise RuntimeError("media-loader did not produce local_path")
        if media_mode == "verify":
            placeholder_path = media_result.local_path
            logger.info("media-loader produced placeholder file: %s", placeholder_path)
        elif local_media_path is not None:
            logger.info("media-loader resumed existing media file: %s", media_result.local_path)
        else:
            logger.info("media-loader produced real remuxed media: %s", media_result.local_path)

        if fixture_name == "sintel":
            if qbit is None or recording_qbit is None:
                raise RuntimeError("Sintel fixture did not use the qBittorrent path")
            if recording_qbit.added_hash != _SINTEL_INFO_HASH:
                raise RuntimeError(f"Sintel fixture resolved unexpected infohash {recording_qbit.added_hash}")
            if recording_qbit.deleted_hash != _SINTEL_INFO_HASH or not recording_qbit.deleted_files:
                raise RuntimeError("Sintel fixture did not request qBittorrent source cleanup")
            if not recording_qbit.completed_path:
                raise RuntimeError("Sintel fixture did not record a completed qBittorrent content path")
            if Path(recording_qbit.completed_path).exists():
                raise RuntimeError("Sintel qBittorrent source still exists after deleteFiles cleanup")
            await _wait_torrent_absent(qbit, _SINTEL_INFO_HASH)
            safe_output, missing_output = safe_cleanup_candidate(output_dir, media_result.local_path)
            if safe_output is None or missing_output:
                raise RuntimeError("Sintel remux is missing, non-regular, symlinked, or outside its output directory")
            fixture_probe = await probe_media(str(safe_output))
            if not (
                fixture_probe.get("codec")
                and fixture_probe.get("container")
                and fixture_probe.get("duration_seconds") is not None
            ):
                raise RuntimeError("ffprobe did not return a readable Sintel video stream, container, and duration")
            if int(fixture_probe.get("size_bytes") or 0) != safe_output.stat().st_size:
                raise RuntimeError("Sintel ffprobe and filesystem sizes disagree")

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

        upload_video = await video_repo.find_by_id(uuid.UUID(raw_video_id))
        if upload_video is None or not upload_video.local_path:
            raise RuntimeError("video local_path missing in DB for upload stage")

        injector = LocalPixelInjectorService(share_scheme=settings.pixel_injector_local_share_scheme)
        upload_task = Task(
            id=uuid.UUID(raw_task_id),
            video_id=uuid.UUID(raw_video_id),
            account_id=uuid.UUID(account_id),
            state=TaskState.UPLOADING,
            queue_name=upload_queue.name,
            local_path=upload_video.local_path,
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
        uploaded_bytes = os.path.getsize(upload_video.local_path)
        await account_repo.apply_upload_usage(uuid.UUID(account_id), uploaded_bytes)
        logger.info("upload stage complete (share_url=%s)", inject_result.share_url)

        # Stage 6: Strm-Resolver resolve+stream+range-seek
        logger.info("[Stage 6] Strm-Resolver resolve+stream+range-seek")
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

            initial_end = min(1023, uploaded_bytes - 1)
            range_start = await client.get(
                f"/local/{upload_task.video_id}",
                headers={"Range": f"bytes=0-{initial_end}"},
            )
            if range_start.status_code != 206:
                raise RuntimeError(f"initial range returned {range_start.status_code}: {range_start.text[:200]}")
            with Path(upload_video.local_path).open("rb") as handle:
                expected_start = handle.read(initial_end + 1)
            if range_start.content != expected_start:
                raise RuntimeError("initial range bytes differ from the source file")
            expected_content_range = f"bytes 0-{initial_end}/{uploaded_bytes}"
            if range_start.headers.get("content-range") != expected_content_range:
                raise RuntimeError(f"unexpected initial Content-Range: {range_start.headers.get('content-range')}")

            seek_start = min(max(1024, uploaded_bytes // 2), uploaded_bytes - 1)
            seek_end = min(seek_start + 1023, uploaded_bytes - 1)
            range_seek = await client.get(
                f"/local/{upload_task.video_id}",
                headers={"Range": f"bytes={seek_start}-{seek_end}"},
            )
            if range_seek.status_code != 206:
                raise RuntimeError(f"seek range returned {range_seek.status_code}: {range_seek.text[:200]}")
            with Path(upload_video.local_path).open("rb") as handle:
                handle.seek(seek_start)
                expected_seek = handle.read(seek_end - seek_start + 1)
            if range_seek.content != expected_seek:
                raise RuntimeError("seek range bytes differ from the source file")
            expected_content_range = f"bytes {seek_start}-{seek_end}/{uploaded_bytes}"
            if range_seek.headers.get("content-range") != expected_content_range:
                raise RuntimeError(f"unexpected seek Content-Range: {range_seek.headers.get('content-range')}")

            invalid_range = await client.get(
                f"/local/{upload_task.video_id}",
                headers={"Range": f"bytes={uploaded_bytes}-"},
            )
            if invalid_range.status_code != 416:
                raise RuntimeError(f"invalid range returned {invalid_range.status_code}, expected 416")

            if fixture_name == "sintel":
                expected_digest = await asyncio.to_thread(_sha256_path, Path(upload_video.local_path))
                actual_digest = hashlib.sha256()
                actual_size = 0
                async with client.stream("GET", f"/local/{upload_task.video_id}") as full_response:
                    if full_response.status_code != 200:
                        raise RuntimeError(f"full local GET returned {full_response.status_code}, expected 200")
                    async for chunk in full_response.aiter_bytes():
                        actual_size += len(chunk)
                        actual_digest.update(chunk)
                if actual_size != uploaded_bytes or actual_digest.hexdigest() != expected_digest:
                    raise RuntimeError("full local GET bytes differ from the real Sintel remux")

        if external_http_port:
            if resolver_cache_key is None:  # pragma: no cover - established by the download payload above
                raise RuntimeError("resolver cache key is unavailable")
            # The in-process ASGI checks use base_url=http://test and therefore
            # cache a URL that no external player can resolve. Force the real
            # network request to derive its redirect from its own Host header.
            await redis.delete(resolver_cache_key)
            completion_event = asyncio.Event()
            completion_path = f"/__e2e__/{run_id}/complete"

            async def acknowledge_external_player() -> dict[str, str]:
                completion_event.set()
                return {"status": "acknowledged"}

            app.add_api_route(
                completion_path,
                acknowledge_external_player,
                methods=["POST"],
                include_in_schema=False,
            )
            logger.info(
                "external player URL: http://127.0.0.1:%d/stream/%s",
                external_http_port,
                upload_task.video_id,
            )
            logger.info(
                "acknowledge successful player run: POST http://127.0.0.1:%d%s",
                external_http_port,
                completion_path,
            )
            await _serve_until_acknowledged(
                app,
                port=external_http_port,
                completion_event=completion_event,
                timeout_seconds=external_http_timeout,
            )

        logger.info("E2E FULL pipeline verification succeeded (run_id=%s)", run_id)

    finally:
        if qbit is not None:
            # The fixed fixture was proven absent immediately before add, so an
            # attempted hash is owned by this run even if the add response was
            # lost. Clean only that exact hash; never infer ownership for an
            # arbitrary user-supplied magnet.
            if fixture_name == "sintel" and recording_qbit is not None and recording_qbit.attempted_hash:
                try:
                    if await qbit.has_torrent(recording_qbit.attempted_hash):
                        await qbit.delete_torrent(recording_qbit.attempted_hash, delete_files=True)
                        await _wait_torrent_absent(qbit, recording_qbit.attempted_hash)
                except Exception as exc:
                    logger.error("failed to clean owned Sintel fixture torrent: %s", exc)
            await qbit.aclose()
        try:
            # Also drop the ``:processing`` in-flight lists, otherwise an
            # aborted run leaves claimed payloads behind under the run's keys.
            cleanup_keys = [
                crawl_queue_name,
                download_queue_name,
                upload_queue_name,
                f"{crawl_queue_name}:processing",
                f"{download_queue_name}:processing",
                f"{upload_queue_name}:processing",
            ]
            if resolver_cache_key is not None:
                cleanup_keys.append(resolver_cache_key)
            await redis.delete(*cleanup_keys)
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
