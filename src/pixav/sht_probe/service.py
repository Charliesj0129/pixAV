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
from pixav.sht_probe.scoring import QualityScorer

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
        min_quality_score: int = 0,
        embeddings_enabled: bool = False,
    ) -> None:
        self._video_repo = video_repo
        self._queue = queue
        self._crawler = crawler
        self._extractor = extractor or BeautifulSoupExtractor()
        self._jackett = jackett
        self._scorer = QualityScorer()
        self._min_quality_score = min_quality_score
        self._embedding_service = None
        if embeddings_enabled:
            from pixav.shared.embedding import EmbeddingService

            self._embedding_service = EmbeddingService()

    async def run_crawl(
        self,
        seed_url: str,
        link_pattern: str | None = None,
        tags: list[str] | None = None,
        max_pages: int | None = None,
    ) -> list[str]:
        """Crawl a seed URL and discover new magnet URIs.

        1. Fetch the seed page and extract page links.
        2. For each page, extract magnet URIs.
        3. De-duplicate against existing videos in DB.
        4. Insert new videos (status=discovered).
        5. Push ``{video_id, magnet_uri}`` to crawl queue.

        Args:
            seed_url: Starting URL for the crawl.
            link_pattern: Optional regex to filter links to visit.
            tags: Optional list of tags to attach to discovered videos.
            max_pages: Optional cap on the number of page links to visit.

        Returns:
            List of newly discovered magnet URIs.
        """
        if self._crawler is None:
            raise RuntimeError("crawler is required for run_crawl()")

        logger.info("starting crawl from %s (filter=%s, tags=%s)", seed_url, link_pattern, tags)
        page_urls = await self._crawler.crawl(seed_url, link_pattern)
        if isinstance(max_pages, int) and max_pages > 0:
            page_urls = page_urls[:max_pages]
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

        return await self._persist_new(list(all_magnets), tags=tags)

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
        tags: list[str] | None = None,
    ) -> list[str]:
        """De-duplicate magnets, insert new videos, push to queue.

        Args:
            magnets: Raw list of magnet URIs (may contain duplicates).
            results: Optional Jackett result dicts for title extraction.
            tags: Optional tags to attach to new videos.

        Returns:
            List of newly inserted magnet URIs.
        """
        # Build metadata lookup from results if available
        result_by_magnet: dict[str, dict[str, Any]] = {}
        if results:
            for item in results:
                magnet = item.get("magnet_uri")
                if magnet:
                    result_by_magnet[magnet] = item

        new_magnets: list[str] = []
        for magnet in set(magnets):
            info_hash = self._scorer.extract_info_hash(magnet)
            if not info_hash:
                logger.warning("skipping invalid magnet: %s", magnet[:40])
                continue

            existing = await self._find_existing_video(info_hash, magnet)
            if existing is not None:
                logger.debug("video exists (hash=%s): %s", info_hash, magnet[:40])
                continue

            item = result_by_magnet.get(magnet, {})
            title = str(item.get("title") or _title_from_magnet(magnet))
            seeders = _coerce_int(item.get("seeders"))
            size_bytes = _coerce_int(item.get("size"))
            score = self._scorer.score(title, seeders=seeders, size_bytes=size_bytes)
            if score < self._min_quality_score:
                logger.info("skip low-quality magnet (score=%d): %s", score, title[:80])
                continue

            embedding = None
            if self._embedding_service is not None:
                # Combine title and tags for richer retrieval context.
                embedding_text = f"{title} {' '.join(tags or [])}".strip()
                embedding = self._embedding_service.generate(embedding_text)

            video = Video(
                id=uuid.uuid4(),
                title=title,
                magnet_uri=magnet,
                info_hash=info_hash,
                quality_score=score,
                tags=tags or [],
                embedding=embedding,
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
            logger.info("new video %s (score=%d): %s", video.id, score, title[:80])

        logger.info("crawl complete: %d new, %d skipped", len(new_magnets), len(set(magnets)) - len(new_magnets))
        return new_magnets

    async def _find_existing_video(self, info_hash: str, magnet_uri: str) -> Video | None:
        """Find existing video by info hash, then fallback to exact magnet match.

        The fallback keeps compatibility with older repository mocks/tests that only
        provide ``find_by_magnet``.
        """
        by_hash = getattr(self._video_repo, "find_by_info_hash", None)
        if callable(by_hash):
            existing = await by_hash(info_hash)
            if isinstance(existing, Video):
                return existing

        by_magnet = getattr(self._video_repo, "find_by_magnet", None)
        if callable(by_magnet):
            existing = await by_magnet(magnet_uri)
            if isinstance(existing, Video):
                return existing

        return None


def _title_from_magnet(magnet: str) -> str:
    """Best-effort title extraction from a magnet URI's dn= parameter."""
    import re
    from urllib.parse import unquote

    match = re.search(r"[&?]dn=([^&]+)", magnet)
    if match:
        return unquote(match.group(1)).replace("+", " ")
    return "Untitled"


def _coerce_int(value: Any) -> int:
    """Convert unknown input to int, defaulting to 0 on invalid values."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
