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
        self._cookies: dict[str, str] = {}

    def seed_cookies(self, cookies: dict[str, str]) -> None:
        """Seed the crawler cookie jar (e.g. from a browser session export)."""
        if cookies:
            self._cookies.update(cookies)

    async def crawl(self, url: str, link_pattern: str | None = None) -> list[str]:
        """Fetch a seed URL and extract all internal page links.

        Args:
            url: Seed URL to crawl.
            link_pattern: Optional regex pattern to filter links.

        Returns:
            List of absolute page URLs discovered.

        Raises:
            CrawlError: If both direct and FlareSolverr fetches fail.
        """
        html = await self._fetch_html(url)
        return self._extract_links(html, url, link_pattern)

    async def fetch_page_html(self, url: str) -> str:
        """Public helper: fetch a single page's HTML (for magnet extraction).

        Args:
            url: Page URL to fetch.

        Returns:
            Raw HTML string.
        """
        return await self._fetch_html(url)

    async def _fetch_html(self, url: str) -> str:
        """Try direct httpx first, fallback to FlareSolverr on failure.

        If FlareSolverr succeeds, cache the cookies for future direct requests.
        """
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=True,
                cookies=self._cookies,
                headers={"User-Agent": "Mozilla/5.0"},  # Basic evasion
            ) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                # Update cookies if server sent new ones
                self._cookies.update(resp.cookies)
                return resp.text
        except httpx.HTTPError as direct_err:
            logger.warning("direct fetch failed for %s: %s", url, direct_err)
            if self._flaresolverr is None:
                raise CrawlError(f"direct fetch failed and no FlareSolverr configured: {direct_err}") from direct_err

            logger.info("falling back to FlareSolverr for %s", url)
            html, cookies = await self._flaresolverr.get_html(url, cookies=self._cookies)
            if cookies:
                self._cookies.update(cookies)
                logger.info("cached %d cookies from FlareSolverr", len(cookies))
            return html

    @staticmethod
    def _extract_links(html: str, base_url: str, link_pattern: str | None = None) -> list[str]:
        """Parse HTML and return links, optionally filtered by regex."""
        import re
        from urllib.parse import urljoin, urlparse

        pattern = re.compile(link_pattern) if link_pattern else None

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
                # Apply optional regex filter
                full_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
                if parsed.query:
                    full_url = f"{full_url}?{parsed.query}"
                if pattern and not pattern.search(full_url):
                    continue
                links.add(full_url)

        logger.debug("extracted %d links from %s", len(links), base_url)
        # Deterministic ordering improves debuggability and keeps max_pages slicing stable.
        return sorted(links)
