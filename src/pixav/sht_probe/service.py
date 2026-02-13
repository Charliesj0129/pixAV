"""SHT-Probe service for content discovery crawling."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from pixav.shared.enums import VideoStatus
from pixav.shared.models import Video
from pixav.shared.queue import TaskQueue
from pixav.shared.repository import VideoRepository
from pixav.sht_probe.crawler import HttpxCrawler
from pixav.sht_probe.jackett_client import JackettClient
from pixav.sht_probe.parser import BeautifulSoupExtractor

logger = logging.getLogger(__name__)


class ShtProbeService:
    """Service for running content discovery crawls.

    Two modes of operation:

    1. **Crawl mode**: Crawl a seed URL → extract page links → extract magnets from each page
    2. **Search mode**: Search Jackett indexers by query → collect magnet URIs
    """

    def __init__(
        self,
        *,
        video_repo: VideoRepository,
        queue: TaskQueue,
        crawler: HttpxCrawler | None = None,
        extractor: BeautifulSoupExtractor | None = None,
        jackett: JackettClient | None = None,
    ) -> None:
        self._video_repo = video_repo
        self._queue = queue
        self._crawler = crawler
        self._extractor = extractor or BeautifulSoupExtractor()
        self._jackett = jackett

    async def run_crawl(self, seed_url: str) -> list[str]:
        """Crawl a seed URL and discover new magnet URIs.

        1. Fetch the seed page and extract page links.
        2. For each page, extract magnet URIs.
        3. De-duplicate against existing videos in DB.
        4. Insert new videos (status=discovered).
        5. Push ``{video_id, magnet_uri}`` to crawl queue.

        Args:
            seed_url: Starting URL for the crawl.

        Returns:
            List of newly discovered magnet URIs.
        """
        if self._crawler is None:
            raise RuntimeError("crawler is required for run_crawl()")

        logger.info("starting crawl from %s", seed_url)
        page_urls = await self._crawler.crawl(seed_url)
        logger.info("found %d page links from %s", len(page_urls), seed_url)

        all_magnets: set[str] = set()

        # Also check the seed page itself for magnets
        seed_html = await self._crawler.fetch_page_html(seed_url)
        seed_magnets = await self._extractor.extract(seed_html)
        all_magnets.update(seed_magnets)

        for page_url in page_urls:
            try:
                html = await self._crawler.fetch_page_html(page_url)
                magnets = await self._extractor.extract(html)
                all_magnets.update(magnets)
            except Exception as exc:
                logger.warning("failed to extract from %s: %s", page_url, exc)

        return await self._persist_new(list(all_magnets))

    async def run_search(self, query: str, *, limit: int = 50) -> list[str]:
        """Search Jackett for torrents and discover new magnet URIs.

        Args:
            query: Search query.
            limit: Max results from Jackett.

        Returns:
            List of newly discovered magnet URIs.
        """
        if self._jackett is None:
            raise RuntimeError("jackett is required for run_search()")

        logger.info("searching jackett for %r", query)
        results = await self._jackett.search(query, limit=limit)

        magnets: list[str] = []
        for item in results:
            magnet = item.get("magnet_uri")
            if magnet:
                magnets.append(magnet)

        return await self._persist_new(magnets, results=results)

    async def _persist_new(
        self,
        magnets: list[str],
        *,
        results: list[dict[str, Any]] | None = None,
    ) -> list[str]:
        """De-duplicate magnets, insert new videos, push to queue.

        Args:
            magnets: Raw list of magnet URIs (may contain duplicates).
            results: Optional Jackett result dicts for title extraction.

        Returns:
            List of newly inserted magnet URIs.
        """
        # Build a title lookup from results if available
        title_by_magnet: dict[str, str] = {}
        if results:
            for item in results:
                magnet = item.get("magnet_uri")
                if magnet:
                    title_by_magnet[magnet] = item.get("title", "Untitled")

        new_magnets: list[str] = []
        for magnet in set(magnets):
            existing = await self._video_repo.find_by_magnet(magnet)
            if existing is not None:
                logger.debug("magnet already known: %s", magnet[:60])
                continue

            title = title_by_magnet.get(magnet, _title_from_magnet(magnet))
            video = Video(
                id=uuid.uuid4(),
                title=title,
                magnet_uri=magnet,
                status=VideoStatus.DISCOVERED,
            )
            await self._video_repo.insert(video)

            await self._queue.push(
                {
                    "video_id": str(video.id),
                    "magnet_uri": magnet,
                }
            )
            new_magnets.append(magnet)
            logger.info("new video %s: %s", video.id, title[:80])

        logger.info("crawl complete: %d new, %d skipped", len(new_magnets), len(set(magnets)) - len(new_magnets))
        return new_magnets


def _title_from_magnet(magnet: str) -> str:
    """Best-effort title extraction from a magnet URI's dn= parameter."""
    import re
    from urllib.parse import unquote

    match = re.search(r"[&?]dn=([^&]+)", magnet)
    if match:
        return unquote(match.group(1)).replace("+", " ")
    return "Untitled"
