"""SHT-Probe service for content discovery crawling."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from pixav.shared.enums import VideoStatus
from pixav.shared.metrics import record_source_adapter_error, record_untitled_rejected
from pixav.shared.models import Video
from pixav.shared.queue import TaskQueue
from pixav.shared.repository import SourceCandidateRepository, VideoRepository
from pixav.sht_probe.crawler import HttpxCrawler
from pixav.sht_probe.interfaces import IndexerAdapter
from pixav.sht_probe.models import CrawlResult, MagnetCandidate
from pixav.sht_probe.parser import BeautifulSoupExtractor
from pixav.sht_probe.scoring import QualityScorer

if TYPE_CHECKING:
    from pixav.sht_probe.sehuatang import SehuatangCrawler, SehuatangExtractor

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
        candidate_repo: SourceCandidateRepository | None = None,
        crawler: HttpxCrawler | SehuatangCrawler | None = None,
        extractor: BeautifulSoupExtractor | SehuatangExtractor | None = None,
        jackett: IndexerAdapter | None = None,
        min_quality_score: int = 0,
        managed_media_workflow: bool = False,
        embeddings_enabled: bool = False,
        page_fetch_concurrency: int = 1,
    ) -> None:
        self._video_repo = video_repo
        self._queue = queue
        self._candidate_repo = candidate_repo
        self._crawler: HttpxCrawler | SehuatangCrawler | None = crawler
        self._extractor: BeautifulSoupExtractor | SehuatangExtractor = extractor or BeautifulSoupExtractor()
        self._jackett = jackett
        self._scorer = QualityScorer()
        self._min_quality_score = min_quality_score
        self._managed_media_workflow = managed_media_workflow
        self._page_fetch_concurrency = max(1, page_fetch_concurrency)
        self._embedding_service = None
        self._last_untitled_rejected = 0
        if embeddings_enabled:
            from pixav.shared.embedding import EmbeddingService

            self._embedding_service = EmbeddingService()

    async def run_crawl(  # noqa: C901
        self,
        seed_url: str,
        link_pattern: str | None = None,
        tags: list[str] | None = None,
        max_pages: int | None = None,
    ) -> CrawlResult:
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

        candidates: dict[str, MagnetCandidate] = {}

        async def _extract(html: str, source_url: str) -> list[MagnetCandidate]:
            method = getattr(type(self._extractor), "extract_candidates", None)
            if callable(method):
                return await self._extractor.extract_candidates(html, source_url)  # type: ignore[attr-defined]
            magnets = await self._extractor.extract(html)
            return [MagnetCandidate(uri=uri, title=_title_from_magnet(uri), source_url=source_url) for uri in magnets]

        # Also check the seed page itself for magnets
        seed_html = await self._crawler.fetch_page_html(seed_url)
        for candidate in await _extract(seed_html, seed_url):
            candidates[candidate.uri] = candidate

        if page_urls:
            if self._page_fetch_concurrency <= 1 or len(page_urls) == 1:
                for page_url in page_urls:
                    try:
                        html = await self._crawler.fetch_page_html(page_url)
                        for candidate in await _extract(html, page_url):
                            candidates[candidate.uri] = candidate
                    except Exception as exc:
                        logger.warning("failed to extract from %s: %s", page_url, exc)
            else:
                semaphore = asyncio.Semaphore(self._page_fetch_concurrency)
                crawler = self._crawler

                async def _fetch_and_extract(page_url: str) -> list[MagnetCandidate]:
                    async with semaphore:
                        html = await crawler.fetch_page_html(page_url)
                        return await _extract(html, page_url)

                results = await asyncio.gather(
                    *(_fetch_and_extract(page_url) for page_url in page_urls),
                    return_exceptions=True,
                )
                for page_url, result in zip(page_urls, results, strict=True):
                    if isinstance(result, BaseException):
                        logger.warning("failed to extract from %s: %s", page_url, result)
                        continue
                    for candidate in result:
                        candidates[candidate.uri] = candidate

        result_metadata = [
            {"magnet_uri": item.uri, "title": item.title, "source_url": item.source_url} for item in candidates.values()
        ]
        self._last_untitled_rejected = 0
        new_magnets = await self._persist_new(list(candidates), results=result_metadata, tags=tags)
        return CrawlResult(
            new_magnets,
            thread_links=len(page_urls),
            extracted_magnets=len(candidates),
            untitled_rejected=self._last_untitled_rejected,
        )

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

    async def _persist_new(  # noqa: C901
        self,
        magnets: list[str],
        *,
        results: Sequence[Mapping[str, Any]] | None = None,
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
        if self._managed_media_workflow:
            return await self._publish_observations(magnets, results=results)
        # Build metadata lookup from results if available
        result_by_magnet: dict[str, Mapping[str, Any]] = {}
        if results:
            for item in results:
                magnet = item.get("magnet_uri")
                if magnet:
                    result_by_magnet[magnet] = item

        new_magnets: list[str] = []
        for magnet in sorted(set(magnets)):
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
            source_url = str(item.get("source_url") or "")
            if ("sehuatang.org" in source_url or "sehuatang" in (tags or [])) and _is_untitled(title):
                self._last_untitled_rejected += 1
                record_untitled_rejected()
                logger.warning("reject Sehuatang candidate without title: %s", magnet[:80])
                continue
            seeders = _coerce_int(item.get("seeders"))
            size_bytes = _coerce_int(item.get("size"))
            score = self._scorer.score(title, seeders=seeders, size_bytes=size_bytes)
            if self._scorer.eligibility_reasons(title, size_bytes) or score < self._min_quality_score:
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
                metadata_json=json.dumps(
                    {
                        "discovery": {
                            "source_url": source_url or None,
                            "title": title,
                            "tags": tags or [],
                        }
                    }
                ),
                status=VideoStatus.DISCOVERED,
            )
            await self._admit_video(video, magnet=magnet, info_hash=info_hash, score=score)
            new_magnets.append(magnet)
            logger.info("new video %s (score=%d): %s", video.id, score, title[:80])

        logger.info("crawl complete: %d new, %d skipped", len(new_magnets), len(set(magnets)) - len(new_magnets))
        return new_magnets

    async def _publish_observations(self, magnets, *, results) -> list[str]:
        from urllib.parse import urlsplit

        from pixav.sht_probe.policy import SourcePolicy

        accepted = []
        rows = results or [{"magnet_uri": magnet, "title": _title_from_magnet(magnet)} for magnet in magnets]
        for row in rows:
            try:
                magnet = row["magnet_uri"]
                info_hash = self._scorer.extract_info_hash(magnet)
                provider = row.get("provider") or urlsplit(row.get("source_url") or "").hostname or "indexer"
                payload = {
                    "provider": provider,
                    "provider_id": row.get("provider_id") or info_hash,
                    "magnet_uri": magnet,
                    "title": row.get("title") or _title_from_magnet(magnet),
                    "seeders": row.get("seeders", 0),
                    "size_bytes": row.get("size", 0),
                }
                candidate = SourcePolicy(min_score=self._min_quality_score).normalize(payload)
            except (ValueError, KeyError, TypeError, AttributeError):
                # One unusable row is discarded and counted; the rest of the
                # provider's answer is still worth having.
                logger.warning("adapter error: INVALID_PROVIDER_PAYLOAD")
                record_source_adapter_error()
                continue
            await self._queue.push(
                {"schema": "source-observation-v1", "observation": candidate.model_dump(mode="json")}
            )
            accepted.append(candidate.magnet_uri)
        return accepted

    async def _admit_video(self, video: Video, *, magnet: str, info_hash: str, score: int) -> None:
        """Persist a newly discovered video, its source candidate, and its task."""
        await self._video_repo.insert(video)
        # Discovery is where sources are known, so this is where the media item's
        # first source candidate is recorded. Media-Loader cools these down on a
        # dead swarm rather than failing the media item.
        if self._candidate_repo is not None:
            await self._candidate_repo.register(
                video.id,
                magnet_uri=magnet,
                info_hash=info_hash,
                quality_score=score,
            )
        await self._queue.push(
            {
                "video_id": str(video.id),
                "magnet_uri": magnet,
            }
        )

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


def _is_untitled(title: str) -> bool:
    return not title.strip() or title.strip().casefold() == "untitled"
