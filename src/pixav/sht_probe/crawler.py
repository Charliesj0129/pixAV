"""Content crawler using httpx with optional FlareSolverr fallback."""

from __future__ import annotations

import logging
from typing import cast

import httpx
from bs4 import BeautifulSoup

from pixav.shared.exceptions import CrawlError
from pixav.sht_probe.flaresolverr_client import FlareSolverrSession

logger = logging.getLogger(__name__)


class HttpxCrawler:
    """Crawl a seed URL and return page links.

    Implements the ``ContentCrawler`` protocol.

    When ``flaresolverr`` is provided, it is used as a fallback when a
    direct httpx request fails (e.g. Cloudflare 403).
    """

    def __init__(
        self,
        *,
        flaresolverr: FlareSolverrSession | None = None,
        timeout: int = 30,
    ) -> None:
        self._flaresolverr = flaresolverr
        self._timeout = timeout

    async def crawl(self, url: str) -> list[str]:
        """Fetch a seed URL and extract all internal page links.

        Args:
            url: Seed URL to crawl.

        Returns:
            List of absolute page URLs discovered.

        Raises:
            CrawlError: If both direct and FlareSolverr fetches fail.
        """
        html = await self._fetch_html(url)
        return self._extract_links(html, url)

    async def fetch_page_html(self, url: str) -> str:
        """Public helper: fetch a single page's HTML (for magnet extraction).

        Args:
            url: Page URL to fetch.

        Returns:
            Raw HTML string.
        """
        return await self._fetch_html(url)

    async def _fetch_html(self, url: str) -> str:
        """Try direct httpx first, fallback to FlareSolverr on failure."""
        try:
            async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=True) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                return resp.text
        except httpx.HTTPError as direct_err:
            logger.warning("direct fetch failed for %s: %s", url, direct_err)
            if self._flaresolverr is None:
                raise CrawlError(f"direct fetch failed and no FlareSolverr configured: {direct_err}") from direct_err
            logger.info("falling back to FlareSolverr for %s", url)
            return await self._flaresolverr.get_html(url)

    @staticmethod
    def _extract_links(html: str, base_url: str) -> list[str]:
        """Parse HTML and return absolute <a href> links from the same domain."""
        from urllib.parse import urljoin, urlparse

        base_domain = urlparse(base_url).netloc
        soup = BeautifulSoup(html, "lxml")
        links: set[str] = set()

        for tag in soup.find_all("a", href=True):
            href_raw = tag.get("href")
            if not isinstance(href_raw, str):
                continue
            href = cast(str, href_raw)
            if href.startswith("magnet:") or href.startswith("javascript:") or href.startswith("#"):
                continue
            absolute = urljoin(base_url, href)
            parsed = urlparse(absolute)
            if parsed.netloc == base_domain and parsed.scheme in ("http", "https"):
                # Remove fragments for dedup
                links.add(f"{parsed.scheme}://{parsed.netloc}{parsed.path}")

        logger.debug("extracted %d links from %s", len(links), base_url)
        return list(links)
