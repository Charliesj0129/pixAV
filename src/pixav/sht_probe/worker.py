"""Queue-driven worker for SHT-Probe crawling."""

from __future__ import annotations

import asyncio
import logging

from pixav.config import Settings, get_settings
from pixav.shared.db import create_pool
from pixav.shared.queue import TaskQueue
from pixav.shared.redis_client import create_redis
from pixav.shared.repository import VideoRepository
from pixav.sht_probe.crawler import HttpxCrawler
from pixav.sht_probe.flaresolverr_client import FlareSolverrSession
from pixav.sht_probe.jackett_client import JackettClient
from pixav.sht_probe.parser import BeautifulSoupExtractor
from pixav.sht_probe.service import ShtProbeService

logger = logging.getLogger(__name__)


async def run_once(settings: Settings) -> list[str]:
    """Run a single crawl cycle against all configured seed URLs.

    Returns:
        Combined list of newly discovered magnet URIs.
    """
    pool = await create_pool(settings)
    redis = await create_redis(settings)

    try:
        video_repo = VideoRepository(pool)
        queue = TaskQueue(redis=redis, queue_name=settings.queue_crawl)

        # Build optional components
        flaresolverr = FlareSolverrSession(settings.flaresolverr_url) if settings.flaresolverr_url else None
        crawler = HttpxCrawler(flaresolverr=flaresolverr)
        extractor = BeautifulSoupExtractor()
        jackett = JackettClient(settings.jackett_url, settings.jackett_api_key) if settings.jackett_api_key else None

        service = ShtProbeService(
            video_repo=video_repo,
            queue=queue,
            crawler=crawler,
            extractor=extractor,
            jackett=jackett,
        )

        all_new: list[str] = []

        # Crawl seed URLs
        seed_urls = _parse_seed_urls(settings.crawl_seed_urls)
        for url in seed_urls:
            try:
                new = await service.run_crawl(url)
                all_new.extend(new)
            except Exception as exc:
                logger.error("crawl failed for %s: %s", url, exc)

        logger.info("crawl cycle complete: %d new magnets total", len(all_new))
        return all_new
    finally:
        await redis.aclose()
        await pool.close()


async def run_loop(settings: Settings) -> None:
    """Run crawl cycles in a loop with configurable interval."""
    logger.info(
        "sht-probe worker starting (interval=%ds, seeds=%s)",
        settings.crawl_interval_seconds,
        settings.crawl_seed_urls[:80] if settings.crawl_seed_urls else "<none>",
    )

    while True:
        try:
            await run_once(settings)
        except Exception as exc:
            logger.exception("crawl cycle error: %s", exc)

        logger.info("sleeping %ds until next crawl cycle", settings.crawl_interval_seconds)
        await asyncio.sleep(settings.crawl_interval_seconds)


def _parse_seed_urls(raw: str) -> list[str]:
    """Split comma-separated seed URL string into a list."""
    if not raw.strip():
        return []
    return [u.strip() for u in raw.split(",") if u.strip()]


def main() -> None:
    """Entry point for ``python -m pixav.sht_probe.worker``."""
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()

    if "--once" in sys.argv:
        asyncio.run(run_once(settings))
    else:
        asyncio.run(run_loop(settings))


if __name__ == "__main__":
    main()
