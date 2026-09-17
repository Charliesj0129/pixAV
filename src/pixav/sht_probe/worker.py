"""Queue-driven worker for SHT-Probe crawling."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Protocol

from pixav.config import Settings, get_settings
from pixav.shared.cookies import load_cookies
from pixav.shared.db import create_pool
from pixav.shared.metrics import (
    record_crawl_cookie_error,
    record_crawl_cycle_timeout,
    record_task_failed,
    record_task_processed,
    set_crawl_interval,
    set_crawl_state,
)
from pixav.shared.queue import TaskQueue
from pixav.shared.redis_client import create_redis
from pixav.shared.repository import SourceCandidateRepository, VideoRepository
from pixav.sht_probe.crawler import HttpxCrawler
from pixav.sht_probe.flaresolverr_client import FlareSolverrSession
from pixav.sht_probe.jackett_client import JackettClient
from pixav.sht_probe.models import CrawlResult
from pixav.sht_probe.parser import BeautifulSoupExtractor
from pixav.sht_probe.sehuatang import SehuatangCrawler, SehuatangExtractor
from pixav.sht_probe.service import ShtProbeService

logger = logging.getLogger(__name__)

_METRICS_MODULE = "sht_probe"


class _SeedableCrawler(Protocol):
    """Any crawler that accepts an externally supplied cookie jar."""

    def seed_cookies(self, cookies: dict[str, str]) -> None: ...


def _seed_crawler_cookies(settings: Settings, *crawlers: _SeedableCrawler | None) -> None:
    """Seed the configured browser session into every crawler that needs it.

    Every crawler gets the cookies, not just the generic one: unseeded,
    Sehuatang serves the logged-out guest view, which carries the age-gate and
    almost no thread links, so the crawl "succeeds" with nothing to show for it.
    """
    try:
        cookies, source = load_cookies(
            cookie_header=settings.crawl_cookie_header,
            cookie_file=settings.crawl_cookie_file,
        )
    except FileNotFoundError:
        record_crawl_cookie_error("missing")
        raise
    except (OSError, UnicodeError, ValueError):
        record_crawl_cookie_error("invalid")
        raise
    if (settings.crawl_cookie_header.strip() or settings.crawl_cookie_file.strip()) and not cookies:
        record_crawl_cookie_error("invalid")
        raise ValueError("configured crawler cookies are empty or invalid")
    if not cookies:
        return
    for crawler in crawlers:
        if crawler is not None:
            crawler.seed_cookies(cookies)
    logger.info("seeded %d crawl cookie(s) (%s)", len(cookies), source)


async def run_once(settings: Settings) -> CrawlResult:  # noqa: C901
    """Run a single crawl cycle against all configured seed URLs.

    Returns:
        Combined list of newly discovered magnet URIs.
    """
    pool = await create_pool(settings)
    redis = await create_redis(settings)
    sehuatang_crawler: SehuatangCrawler | None = None

    try:
        video_repo = VideoRepository(pool)
        candidate_repo = SourceCandidateRepository(pool)
        queue = TaskQueue(redis=redis, queue_name=settings.queue_crawl)

        # Build optional components
        flaresolverr = FlareSolverrSession(settings.flaresolverr_url) if settings.flaresolverr_url else None
        crawler = HttpxCrawler(flaresolverr=flaresolverr)
        # Default generic components
        generic_extractor = BeautifulSoupExtractor()
        sehuatang_crawler = (
            SehuatangCrawler(
                flaresolverr=flaresolverr,
                request_delay_seconds=settings.crawl_request_delay_seconds,
                max_board_pages=settings.crawl_max_board_pages,
            )
            if flaresolverr
            else None
        )
        _seed_crawler_cookies(settings, crawler, sehuatang_crawler)
        sehuatang_extractor = SehuatangExtractor()
        jackett = JackettClient(settings.jackett_url, settings.jackett_api_key) if settings.jackett_api_key else None

        generic_service = ShtProbeService(
            video_repo=video_repo,
            queue=queue,
            candidate_repo=candidate_repo,
            crawler=crawler,
            extractor=generic_extractor,
            jackett=jackett,
            embeddings_enabled=settings.embeddings_enabled,
            managed_media_workflow=settings.managed_media_workflow,
            min_quality_score=settings.source_min_quality_score,
        )
        sehuatang_service = (
            ShtProbeService(
                video_repo=video_repo,
                queue=queue,
                candidate_repo=candidate_repo,
                crawler=sehuatang_crawler,
                extractor=sehuatang_extractor,
                jackett=jackett,
                embeddings_enabled=settings.embeddings_enabled,
                managed_media_workflow=settings.managed_media_workflow,
                page_fetch_concurrency=4,
                min_quality_score=settings.source_min_quality_score,
            )
            if sehuatang_crawler
            else None
        )

        all_new: list[str] = []
        extracted_magnets = 0
        thread_links = 0
        untitled_rejected = 0

        # Crawl seed URLs
        seed_entries = _parse_csv(settings.crawl_seed_urls)
        for entry in seed_entries:
            tags: list[str] = []
            if "|" in entry:
                url, tag_str = entry.split("|", 1)
                # Support multiple tags with '+' e.g. "url|tag1+tag2"
                tags = [t.strip() for t in tag_str.split("+") if t.strip()]
            else:
                url = entry.strip()

            try:
                is_sehuatang = "sehuatang.org" in url
                active_service = sehuatang_service if (is_sehuatang and sehuatang_service) else generic_service

                crawl_result = await active_service.run_crawl(
                    url,
                    link_pattern=settings.crawl_link_filter_pattern,
                    tags=tags,
                    max_pages=settings.crawl_max_pages,
                )
                all_new.extend(crawl_result)
                if isinstance(crawl_result, CrawlResult):
                    extracted_magnets += crawl_result.extracted_magnets
                    thread_links += crawl_result.thread_links
                    untitled_rejected += crawl_result.untitled_rejected
                else:
                    # Compatibility for alternate services: additions prove extraction,
                    # but zero additions must not be treated as a confirmed empty crawl.
                    extracted_magnets += len(crawl_result)
                record_task_processed(_METRICS_MODULE, len(crawl_result))
            except Exception as exc:
                record_task_failed(_METRICS_MODULE)
                logger.error("crawl failed for %s: %s", url, exc)

        # Search Jackett queries
        queries = _parse_csv(settings.crawl_queries)
        for query in queries:
            try:
                if not jackett:
                    logger.warning("skipping query %r (no jackett configured)", query)
                    continue
                search_result = await generic_service.run_search(query)
                all_new.extend(search_result)
                extracted_magnets += len(search_result)
                record_task_processed(_METRICS_MODULE, len(search_result))
            except Exception as exc:
                record_task_failed(_METRICS_MODULE)
                logger.error("search failed for %r: %s", query, exc)

        if extracted_magnets == 0:
            raw_empty = await redis.incr(settings.crawl_empty_cycles_key)
            try:
                empty_cycles = int(raw_empty)
            except (TypeError, ValueError):
                empty_cycles = 0
        else:
            await redis.set(settings.crawl_empty_cycles_key, 0)
            empty_cycles = 0
        age_gate_active = bool(sehuatang_crawler and sehuatang_crawler.age_gate_persisted)
        completed = time.time()
        set_crawl_state(
            empty_cycles=empty_cycles,
            completed_timestamp=completed,
            age_gate_active=age_gate_active,
        )
        level = logging.ERROR if empty_cycles >= settings.crawl_empty_error_threshold else logging.INFO
        logger.log(
            level,
            "crawl cycle complete: links=%d extracted=%d new=%d untitled_rejected=%d empty_cycles=%d",
            thread_links,
            extracted_magnets,
            len(all_new),
            untitled_rejected,
            empty_cycles,
        )
        return CrawlResult(
            all_new,
            thread_links=thread_links,
            extracted_magnets=extracted_magnets,
            untitled_rejected=untitled_rejected,
        )
    finally:
        if sehuatang_crawler is not None:
            try:
                await sehuatang_crawler.aclose()
            except Exception as exc:
                logger.warning("failed to close sehuatang crawler client: %s", exc)
        await redis.aclose()
        await pool.close()


async def run_loop(settings: Settings, *, health_state: object | None = None) -> None:
    """Run crawl cycles in a loop with configurable interval."""
    logger.info(
        "sht-probe worker starting (interval=%ds, seeds=%s)",
        settings.crawl_interval_seconds,
        settings.crawl_seed_urls[:80] if settings.crawl_seed_urls else "<none>",
    )
    set_crawl_interval(settings.crawl_interval_seconds)
    if health_state is not None and hasattr(health_state, "mark_ready"):
        health_state.mark_ready()

    while True:
        try:
            await asyncio.wait_for(
                run_once(settings),
                timeout=settings.crawl_interval_seconds + 15 * 60,
            )
        except asyncio.TimeoutError as exc:
            record_crawl_cycle_timeout()
            record_task_failed(_METRICS_MODULE)
            logger.error("crawl cycle exceeded interval plus 15 minute grace: %s", exc)
        except Exception as exc:
            record_task_failed(_METRICS_MODULE)
            logger.exception("crawl cycle error: %s", exc)

        logger.info("sleeping %ds until next crawl cycle", settings.crawl_interval_seconds)
        await asyncio.sleep(settings.crawl_interval_seconds)


def _parse_csv(raw: str) -> list[str]:
    """Split comma-separated string into a list."""
    if not raw or not raw.strip():
        return []
    return [x.strip() for x in raw.split(",") if x.strip()]


def main() -> None:
    """Entry point for ``python -m pixav.sht_probe.worker``."""
    import sys

    from pixav.shared.health import HealthState, create_health_app
    from pixav.shared.health_server import run_with_health

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()

    if "--once" in sys.argv:
        asyncio.run(run_once(settings))
        return

    health_state = HealthState("sht_probe", stale_after_seconds=settings.heartbeat_stale_seconds)
    health_app = create_health_app("sht_probe", state=health_state)

    async def _run() -> None:
        await run_with_health(
            worker_coro=run_loop(settings, health_state=health_state),
            health_app=health_app,
            host=settings.health_host,
            port=settings.sht_probe_health_port,
            health_state=health_state,
            heartbeat_interval_seconds=settings.heartbeat_interval_seconds,
        )

    asyncio.run(_run())


if __name__ == "__main__":
    main()
