"""Live end-to-end test for crawl stage without mocks.

This test is opt-in and uses:
- Real network calls to a configured `PIXAV_LIVE_SEED_URL`
- Real PostgreSQL database (temporary database per test)
- Real Redis queue

Run manually:
    PIXAV_RUN_LIVE_E2E=1 PIXAV_LIVE_SEED_URL=https://example.org/forum.html \\
      uv run pytest -q tests/e2e/test_live_crawl_no_mock.py
"""

from __future__ import annotations

import glob
import os
import uuid
from pathlib import Path
from urllib.parse import ParseResult, urlparse, urlunparse

import asyncpg
import pytest
import redis.asyncio as aioredis

from pixav.shared.cookies import load_cookies
from pixav.shared.queue import TaskQueue
from pixav.shared.repository import VideoRepository
from pixav.sht_probe.crawler import HttpxCrawler
from pixav.sht_probe.flaresolverr_client import FlareSolverrSession
from pixav.sht_probe.service import ShtProbeService

pytestmark = pytest.mark.e2e_live

_DEFAULT_ADMIN_DSN = "postgresql://pixav:pixav@localhost:5432/postgres"
_DEFAULT_REDIS_URL = "redis://localhost:6379/15"
_DEFAULT_THREAD_PATTERN = r"(viewthread|thread)"


def _require_live_enabled() -> None:
    if os.getenv("PIXAV_RUN_LIVE_E2E") != "1":
        pytest.skip("live E2E disabled; set PIXAV_RUN_LIVE_E2E=1 to run")
    if not os.getenv("PIXAV_LIVE_SEED_URL", "").strip():
        pytest.skip("live E2E requires PIXAV_LIVE_SEED_URL")


def _replace_db_name(dsn: str, db_name: str) -> str:
    parsed: ParseResult = urlparse(dsn)
    return urlunparse(parsed._replace(path=f"/{db_name}"))


async def _apply_all_migrations(dsn: str) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        files = sorted(glob.glob(str(Path("migrations") / "*.sql")))
        for path in files:
            sql = Path(path).read_text(encoding="utf-8")
            await conn.execute(sql)
    finally:
        await conn.close()


@pytest.fixture
async def live_db_dsn() -> str:
    _require_live_enabled()
    admin_dsn = os.getenv("PIXAV_E2E_ADMIN_DSN", _DEFAULT_ADMIN_DSN)
    db_name = f"pixav_e2e_live_{uuid.uuid4().hex[:12]}"

    admin_conn = await asyncpg.connect(admin_dsn)
    try:
        await admin_conn.execute(f'CREATE DATABASE "{db_name}"')
    except Exception:
        await admin_conn.close()
        raise
    await admin_conn.close()

    target_dsn = _replace_db_name(admin_dsn, db_name)
    await _apply_all_migrations(target_dsn)

    try:
        yield target_dsn
    finally:
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


@pytest.fixture
async def live_redis() -> aioredis.Redis:
    _require_live_enabled()
    redis_url = os.getenv("PIXAV_E2E_REDIS_URL", _DEFAULT_REDIS_URL)
    client: aioredis.Redis = aioredis.from_url(redis_url, decode_responses=True)
    try:
        await client.ping()
    except Exception:
        await client.aclose()
        raise
    await client.flushdb()
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


@pytest.mark.asyncio
async def test_live_seed_crawl_no_mock(
    live_db_dsn: str,
    live_redis: aioredis.Redis,
) -> None:
    """Live E2E: crawl one configured seed URL and persist discovered magnets."""
    seed_url = os.getenv("PIXAV_LIVE_SEED_URL", "").strip()
    link_pattern = os.getenv("PIXAV_LIVE_LINK_PATTERN", _DEFAULT_THREAD_PATTERN)
    flaresolverr_url = os.getenv("PIXAV_FLARESOLVERR_URL", "http://localhost:8191").strip()
    cookie_header = os.getenv("PIXAV_CRAWL_COOKIE_HEADER", "").strip()
    cookie_file = os.getenv("PIXAV_CRAWL_COOKIE_FILE", "").strip()

    flaresolverr = FlareSolverrSession(flaresolverr_url) if flaresolverr_url else None
    crawler = HttpxCrawler(flaresolverr=flaresolverr, timeout=60)
    cookies, _ = load_cookies(cookie_header=cookie_header, cookie_file=cookie_file)
    if cookies:
        crawler.seed_cookies(cookies)
    await crawler.fetch_page_html(seed_url)

    pool = await asyncpg.create_pool(dsn=live_db_dsn, min_size=1, max_size=3)
    queue_name = f"pixav:e2e:live_crawl:{uuid.uuid4().hex[:8]}"
    queue = TaskQueue(redis=live_redis, queue_name=queue_name)
    repo = VideoRepository(pool)
    service = ShtProbeService(
        video_repo=repo,
        queue=queue,
        crawler=crawler,
        min_quality_score=-10000,
    )

    try:
        new_magnets = await service.run_crawl(
            seed_url,
            link_pattern=link_pattern,
            tags=["e2e-live"],
        )
        assert new_magnets, "no magnets discovered from live subforum"

        depth = await queue.length()
        assert depth == len(new_magnets)

        payload = await queue.pop(timeout=1)
        assert isinstance(payload, dict)
        assert isinstance(payload.get("video_id"), str)
        assert isinstance(payload.get("magnet_uri"), str)

        count = await pool.fetchval("SELECT count(*) FROM videos")
        assert int(count) == len(new_magnets)

        tags = await pool.fetchval("SELECT tags FROM videos LIMIT 1")
        assert isinstance(tags, list)
        assert "e2e-live" in tags
    finally:
        await pool.close()
