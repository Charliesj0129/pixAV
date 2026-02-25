"""Sehuatang.org specific crawler handles Cloudflare and Discuz! pagination."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import cast
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, SoupStrainer

from pixav.sht_probe.flaresolverr_client import FlareSolverrSession

logger = logging.getLogger(__name__)

_USER_AGENT_FALLBACK = "Mozilla/5.0"
_SEHUATANG_SAFE_COOKIE = "_safe"
_BOARD_URL_RE = re.compile(r"(.+/forum-\d+-)\d+(\.html.*)$")
_THREAD_HINTS = ("thread-", "viewthread")
_MAGNET_RE = re.compile(r"magnet:\?xt=urn:btih:[a-zA-Z0-9]+[^\s\"'<>]*")
_INFOHASH_RE = re.compile(r"\b([a-fA-F0-9]{40})\b")
_SAFEID_RE = re.compile(r"var\s+safeid='([^']+)'")
_ANCHOR_ONLY = SoupStrainer("a")


class SehuatangCrawler:
    """Crawl Sehuatang.org, handle Cloudflare IUAM, and parse Discuz! pagination.

    Uses httpx for direct requests (fast path) and falls back to FlareSolverr
    when Cloudflare challenges are encountered.  A session-scoped page cache
    avoids duplicate network requests for URLs already fetched.
    """

    def __init__(
        self,
        flaresolverr: FlareSolverrSession,
        *,
        timeout: int = 60,
        request_delay_seconds: float = 2.0,
        max_board_pages: int = 1,
        board_fetch_concurrency: int = 3,
    ) -> None:
        self._flaresolverr = flaresolverr
        self._timeout = timeout
        self._delay = request_delay_seconds
        self._max_board_pages = max_board_pages
        self._board_fetch_concurrency = max(1, board_fetch_concurrency)
        self._user_agent = _USER_AGENT_FALLBACK
        self._cookies: dict[str, str] = {}
        self._page_cache: dict[str, str] = {}
        self._client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()
        self._inflight_fetches: dict[str, asyncio.Task[str]] = {}
        self._inflight_lock = asyncio.Lock()

    def seed_cookies(self, cookies: dict[str, str]) -> None:
        """Seed the crawler cookie jar from an external source."""
        if cookies:
            self._cookies.update(cookies)
            if self._client is not None:
                self._client.cookies.update(cookies)

    async def aclose(self) -> None:
        """Close the shared HTTP client if it was initialized."""
        client = self._client
        self._client = None
        if client is not None:
            await client.aclose()

    async def __aenter__(self) -> SehuatangCrawler:
        return self

    async def __aexit__(self, _exc_type, _exc, _tb) -> None:
        await self.aclose()

    async def _get_client(self) -> httpx.AsyncClient:
        """Lazily create and return a shared AsyncClient."""
        if self._client is not None:
            return self._client

        async with self._client_lock:
            if self._client is None:
                self._client = httpx.AsyncClient(
                    timeout=self._timeout,
                    follow_redirects=True,
                    headers={
                        "User-Agent": self._user_agent,
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                        "Accept-Language": "en-US,en;q=0.5",
                        "Upgrade-Insecure-Requests": "1",
                    },
                    limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
                    cookies=self._cookies,
                )
        return self._client

    async def _httpx_fetch(self, url: str) -> str | None:
        try:
            client = await self._get_client()
            if self._cookies:
                client.cookies.update(self._cookies)
            response = await client.get(url)
            response.raise_for_status()
            if response.cookies:
                self._cookies.update(dict(response.cookies.items()))
            if self._client is not None and self._cookies:
                self._client.cookies.update(self._cookies)
            html = response.text
            if not self._looks_like_age_gate(html):
                return html
            logger.info("Sehuatang age-gate detected on direct fetch for %s; using FlareSolverr retry", url)
            return None
        except httpx.HTTPError as exc:
            logger.debug("httpx failed for %s, falling back to FlareSolverr: %s", url, exc)
            return None

    async def _do_fetch(self, url: str) -> str:
        """Fetch HTML: httpx first, FlareSolverr fallback."""
        html = await self._httpx_fetch(url)
        if html is not None:
            return html

        fetched = await self._fetch_via_flaresolverr(url)
        if not self._looks_like_age_gate(fetched):
            return fetched

        safeid = self._extract_safeid(fetched)
        if not safeid:
            logger.warning("Sehuatang age-gate detected but safeid not found for %s", url)
            return fetched

        retry_needed = self._cookies.get("safeid") != safeid or self._cookies.get("agree") != "1"
        if retry_needed:
            self._cookies.update({"safeid": safeid, "agree": "1"})
            if self._client is not None:
                self._client.cookies.update({"safeid": safeid, "agree": "1"})
            logger.info("Sehuatang age-gate detected for %s; retrying with safeid/agree cookies", url)
            fetched = await self._fetch_via_flaresolverr(url)

        if self._looks_like_age_gate(fetched):
            logger.warning("Sehuatang age-gate persists after retry for %s", url)
        return fetched

    async def _fetch_via_flaresolverr(self, url: str) -> str:
        """Fetch via FlareSolverr and merge returned cookies / user-agent."""
        result = await self._flaresolverr.get_html(
            url,
            timeout=self._timeout,
            cookies=self._cookies,
        )
        if len(result) == 2:
            fetched, new_cookies = result
            user_agent = ""
        else:
            fetched, new_cookies, user_agent = result

        if new_cookies:
            self._cookies.update(new_cookies)
            if self._client is not None:
                self._client.cookies.update(new_cookies)
        if user_agent:
            self._user_agent = user_agent
            if self._client is not None:
                self._client.headers["User-Agent"] = user_agent
        return fetched

    async def _handle_age_gate_if_needed(self, url: str, html: str, *, source: str) -> str:
        """Retry once via FlareSolverr if Sehuatang returns the 18+ gate page."""
        if not self._looks_like_age_gate(html):
            return html

        if source == "direct":
            logger.info("Sehuatang age-gate detected on direct fetch for %s; using FlareSolverr retry", url)

        safeid = self._extract_safeid(html)
        if not safeid:
            logger.warning("Sehuatang age-gate detected but safeid not found for %s", url)
            return html

        retry_needed = self._cookies.get(_SEHUATANG_SAFE_COOKIE) != safeid
        if not retry_needed:
            logger.warning("Sehuatang age-gate persists for %s with existing _safe cookie", url)
            return html

        self._cookies.update({_SEHUATANG_SAFE_COOKIE: safeid, "safeid": safeid, "agree": "1"})
        if self._client is not None:
            self._client.cookies.update({_SEHUATANG_SAFE_COOKIE: safeid, "safeid": safeid, "agree": "1"})
        logger.info("Sehuatang age-gate detected for %s; retrying with _safe cookie", url)

        retried = await self._fetch_via_flaresolverr(url)
        if self._looks_like_age_gate(retried):
            logger.warning("Sehuatang age-gate persists after retry for %s", url)
        return retried

    async def _fetch_html(self, url: str) -> str:
        """Return HTML for *url*, consulting the session cache first.

        Cache hits return instantly without any network I/O or sleep.
        On a cache miss, ``_do_fetch`` is called and the result is cached.
        """
        if url in self._page_cache:
            return self._page_cache[url]

        created_task = False
        async with self._inflight_lock:
            if url in self._page_cache:
                return self._page_cache[url]
            task = self._inflight_fetches.get(url)
            if task is None:
                task = asyncio.create_task(self._fetch_and_cache(url))
                self._inflight_fetches[url] = task
                created_task = True

        try:
            return await task
        finally:
            if created_task:
                async with self._inflight_lock:
                    self._inflight_fetches.pop(url, None)

    async def _fetch_and_cache(self, url: str) -> str:
        html = await self._do_fetch(url)
        if self._delay > 0:
            await asyncio.sleep(self._delay)
        self._page_cache[url] = html
        return html

    async def crawl(
        self,
        url: str,
        link_pattern: str | None = None,
        *,
        max_board_pages: int | None = None,
    ) -> list[str]:
        r"""Fetch board pages and extract thread links.

        Args:
            url: The forum section URL (e.g., https://www.sehuatang.org/forum-103-1.html)
            link_pattern: Regex to filter inner page links.
            max_board_pages: Number of board pages to crawl.  Defaults to the
                value set at construction time.
        """
        pages = max_board_pages if max_board_pages is not None else self._max_board_pages
        board_urls = self._board_page_urls(url, pages)

        parsed_url = urlparse(url)
        base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"

        all_links: set[str] = set()
        if len(board_urls) == 1:
            html = await self._fetch_html(board_urls[0])
            all_links.update(self._extract_links(html, base_url, link_pattern))
        else:
            semaphore = asyncio.Semaphore(self._board_fetch_concurrency)

            async def _fetch_board(board_url: str) -> str:
                async with semaphore:
                    return await self._fetch_html(board_url)

            html_pages = await asyncio.gather(*(_fetch_board(board_url) for board_url in board_urls))
            for html in html_pages:
                all_links.update(self._extract_links(html, base_url, link_pattern))

        result = sorted(all_links)
        logger.info(
            "SehuatangCrawler discovered %d links from %s (%d board page(s))",
            len(result),
            url,
            len(board_urls),
        )
        return result

    async def fetch_page_html(self, url: str) -> str:
        """Fetch arbitrary page HTML (cache-aware)."""
        return await self._fetch_html(url)

    @staticmethod
    def _board_page_urls(base_url: str, max_pages: int) -> list[str]:
        """Generate paginated Discuz! board URLs.

        Matches patterns like ``forum-103-1.html`` and generates
        ``forum-103-2.html``, ``forum-103-3.html``, etc.
        Falls back to ``[base_url]`` for non-standard URL formats.
        """
        m = _BOARD_URL_RE.match(base_url)
        if not m:
            return [base_url]
        prefix, suffix = m.group(1), m.group(2)
        return [f"{prefix}{i}{suffix}" for i in range(1, max_pages + 1)]

    @staticmethod
    def _looks_like_age_gate(html: str) -> bool:
        """Detect sehuatang's 18+ landing page."""
        return "var safeid=" in html and ("enter-btn" in html or "If you are over 18" in html or "满18岁" in html)

    @staticmethod
    def _extract_safeid(html: str) -> str | None:
        """Extract the dynamic safeid token from the age-gate page."""
        match = _SAFEID_RE.search(html)
        if not match:
            return None
        return match.group(1)

    @staticmethod
    def _extract_links(html: str, base_url: str, link_pattern: str | None = None) -> list[str]:
        """Parse HTML for thread links matching the optional pattern.

        In Discuz!, thread links are often ``forum.php?mod=viewthread&tid=…``
        or ``thread-xxx-1-1.html``.
        """
        pattern = re.compile(link_pattern) if link_pattern else None
        soup = BeautifulSoup(html, "lxml", parse_only=_ANCHOR_ONLY)
        links: set[str] = set()
        base_domain = urlparse(base_url).netloc

        for tag in soup.find_all("a", href=True):
            href_raw = tag.get("href")
            if not isinstance(href_raw, str):
                continue
            href = cast(str, href_raw)
            if href.startswith(("javascript:", "magnet:", "#")):
                continue
            if not pattern and not any(hint in href for hint in _THREAD_HINTS):
                continue

            absolute = urljoin(base_url, href)
            parsed = urlparse(absolute)

            # Ensure it stays on the same domain
            if parsed.netloc == base_domain:
                full_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
                if parsed.query:
                    full_url = f"{full_url}?{parsed.query}"

                if pattern and not pattern.search(full_url):
                    continue

                links.add(SehuatangCrawler._canonicalize_thread_url(full_url))

        return sorted(links)

    @staticmethod
    def _canonicalize_thread_url(url: str) -> str:
        """Collapse viewthread pagination variants into a canonical thread URL.

        Sehuatang board pages often contain many links to the same thread with
        different ``page=`` / ``extra=`` query parameters. For crawl discovery we
        only need one URL per thread ID.
        """
        parsed = urlparse(url)
        if not parsed.query:
            return url

        query = parse_qs(parsed.query, keep_blank_values=True)
        mod = query.get("mod", [""])[0]
        tid = query.get("tid", [""])[0]
        if parsed.path.endswith("/forum.php") and mod == "viewthread" and tid:
            return f"{parsed.scheme}://{parsed.netloc}{parsed.path}?mod=viewthread&tid={tid}"

        return url


class SehuatangExtractor:
    """Extract magnet URIs and raw info-hashes from Sehuatang HTML."""

    async def extract(self, html: str) -> list[str]:
        magnets: set[str] = set()
        if "magnet:?" not in html and not _INFOHASH_RE.search(html):
            return []

        # 1. BeautifulSoup: parse <a> tags with magnet hrefs
        soup = BeautifulSoup(html, "lxml", parse_only=_ANCHOR_ONLY)
        for tag in soup.find_all("a", href=True):
            href_raw = tag.get("href")
            if not isinstance(href_raw, str):
                continue
            href = cast(str, href_raw)
            if href.startswith("magnet:?"):
                magnets.add(href)

        # 2. Regex fallback: catch magnets embedded in JS or other contexts
        for match in _MAGNET_RE.finditer(html):
            magnets.add(match.group(0))

        # 3. Sehuatang specific: catch raw 40-char hex info hashes often posted as text
        for match in _INFOHASH_RE.finditer(html):
            # Convert to magnet
            magnets.add(f"magnet:?xt=urn:btih:{match.group(1).upper()}")

        logger.debug("SehuatangExtractor extracted %d magnet(s)", len(magnets))
        return list(magnets)
