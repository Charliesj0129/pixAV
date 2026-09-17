"""Sehuatang.org specific crawler handles Cloudflare and Discuz! pagination."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import cast
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, SoupStrainer

from pixav.shared.metrics import record_watermark_rejected
from pixav.sht_probe.flaresolverr_client import FlareSolverrSession
from pixav.sht_probe.models import MagnetCandidate

logger = logging.getLogger(__name__)

_USER_AGENT_FALLBACK = "Mozilla/5.0"
_SEHUATANG_SAFE_COOKIE = "_safe"
_BOARD_URL_RE = re.compile(r"(.+/forum-\d+-)\d+(\.html.*)$")
_THREAD_HINTS = ("thread-", "viewthread")
_MAGNET_RE = re.compile(r"magnet:\?xt=urn:btih:[a-zA-Z0-9]+[^\s\"'<>]*")
_INFOHASH_RE = re.compile(r"\b([a-fA-F0-9]{40})\b")
_SAFEID_RE = re.compile(r"var\s+safeid='([^']+)'")
_ANCHOR_ONLY = SoupStrainer("a")

# Discuz! post template fields, e.g. "【影片容量】：2.27G". The label wording varies
# across boards ("影片容量" / "容量" / "文件大小"), and the separator may be the
# full-width colon. Only these observed shapes are accepted; nothing is inferred.
_SIZE_FIELD_RE = re.compile(
    r"(?:影片容量|文件大小|檔案大小|文件容量|容量|大小)\s*[】\]]?\s*[:：]\s*"
    r"([0-9]+(?:\.[0-9]+)?)\s*(TB|GB|MB|KB|T|G|M|K)\b",
    re.IGNORECASE,
)
_SIZE_UNIT_BYTES = {
    "K": 1024,
    "KB": 1024,
    "M": 1024**2,
    "MB": 1024**2,
    "G": 1024**3,
    "GB": 1024**3,
    "T": 1024**4,
    "TB": 1024**4,
}
# A resolution claim, not a measurement: the real gate stays PartMedia.validate_source().
_RESOLUTION_HINT_RE = re.compile(r"(?<![0-9a-z])(2160p?|4k|uhd)(?![0-9a-z])", re.IGNORECASE)


def _is_obfuscated_text(info_hash: str) -> bool:
    """Return True when a 40-hex string is XOR-obfuscated ASCII, not an info hash.

    Sehuatang stamps pages with its contact address encoded as 20 bytes: a random
    key byte followed by ``sehuatang@gmail.com`` XOR-ed with that key. It matches
    the bare-info-hash pattern exactly, so 27% of discovered "magnets" were this
    watermark rather than a torrent. A real info hash is SHA-1 output, so the odds
    of all 19 trailing bytes decoding to printable ASCII are negligible.
    """
    try:
        raw = bytes.fromhex(info_hash)
    except ValueError:
        return True
    key = raw[0]
    return all(0x20 <= byte ^ key <= 0x7E for byte in raw[1:])


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
        self._age_gate_lock = asyncio.Lock()
        self.age_gate_detected = 0
        self.age_gate_persisted = 0

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
        return await self._handle_age_gate_if_needed(url, fetched)

    async def _fetch_via_flaresolverr(self, url: str) -> str:
        """Fetch via FlareSolverr and merge returned cookies / user-agent."""
        result = await self._flaresolverr.get_html(
            url,
            timeout=self._timeout,
            cookies=dict(self._cookies),
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

    async def _handle_age_gate_if_needed(self, url: str, html: str) -> str:
        """Retry once via FlareSolverr if Sehuatang returns the 18+ gate page.

        The gate is cleared by the ``_safe`` cookie, whose value is the ``safeid``
        token embedded in the gate page itself.  ``safeid``/``agree`` are sent
        alongside it for older Discuz! deployments, but ``_safe`` is the one the
        current site actually checks -- setting only the other two leaves the gate
        up and the crawl silently returns zero links.

        Resolution is serialized: every gated page yields its own token, and all
        of them land in the single shared cookie jar.  Without the lock, N
        concurrent board fetches each store their token before any of them
        retries, so every retry goes out carrying whichever token was written
        last and only that one page comes back real.
        """
        if not self._looks_like_age_gate(html):
            return html

        self.age_gate_detected += 1

        safeid = self._extract_safeid(html)
        if not safeid:
            logger.warning("Sehuatang age-gate detected but safeid not found for %s", url)
            return html

        async with self._age_gate_lock:
            if self._cookies.get(_SEHUATANG_SAFE_COOKIE) == safeid:
                logger.warning("Sehuatang age-gate persists for %s with existing _safe cookie", url)
                return html

            gate_cookies = {_SEHUATANG_SAFE_COOKIE: safeid, "safeid": safeid, "agree": "1"}
            self._cookies.update(gate_cookies)
            if self._client is not None:
                self._client.cookies.update(gate_cookies)
            logger.info("Sehuatang age-gate detected for %s; retrying with _safe cookie", url)

            retried = await self._fetch_via_flaresolverr(url)

        if self._looks_like_age_gate(retried):
            self.age_gate_persisted += 1
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
        watermarks = 0
        for match in _INFOHASH_RE.finditer(html):
            info_hash = match.group(1)
            if _is_obfuscated_text(info_hash):
                watermarks += 1
                record_watermark_rejected()
                continue
            magnets.add(f"magnet:?xt=urn:btih:{info_hash.upper()}")

        if watermarks:
            # Otherwise the filter is invisible: "discarded everything" and
            # "found nothing" both surface as new=0 in the cycle summary.
            logger.info("SehuatangExtractor discarded %d watermark hash(es)", watermarks)
        logger.debug("SehuatangExtractor extracted %d magnet(s)", len(magnets))
        return list(magnets)

    async def extract_candidates(self, html: str, source_url: str) -> list[MagnetCandidate]:
        """Attach the Discuz thread title to every magnet, including bare hashes."""
        magnets = await self.extract(html)
        title = self.extract_title(html)
        return [MagnetCandidate(uri=uri, title=title, source_url=source_url) for uri in magnets]

    @staticmethod
    def parse_size_bytes(text: str) -> int:
        """Read a Discuz! post's declared file size, or 0 when it does not state one."""
        match = _SIZE_FIELD_RE.search(text)
        if match is None:
            return 0
        unit = _SIZE_UNIT_BYTES.get(match.group(2).upper())
        if unit is None:
            return 0
        return int(float(match.group(1)) * unit)

    @staticmethod
    def _post_body_text(html: str) -> str:
        """Text of the opening post only.

        Discuz! renders site chrome such as the "visited boards" list on every
        thread, and one of those board names is "4K原版". Reading the whole page
        would hand every thread a 4K claim it never made.
        """
        soup = BeautifulSoup(html, "lxml")
        body = soup.select_one("[id^=postmessage_]") or soup.select_one("td.t_f")
        return (body or soup).get_text(" ", strip=True)

    def extract_details(self, html: str) -> dict[str, object]:
        """Poster-declared size and resolution claim, used only to order candidates.

        These are the uploader's words, never a measurement. Selection may rank and
        pre-reject on them, but acceptance still comes from probing the real file.
        """
        text = self._post_body_text(html)
        hint = _RESOLUTION_HINT_RE.search(text) or _RESOLUTION_HINT_RE.search(self.extract_title(html))
        return {
            "size_bytes": self.parse_size_bytes(text),
            "resolution_hint": hint.group(1).lower() if hint else None,
        }

    @staticmethod
    def extract_title(html: str) -> str:
        soup = BeautifulSoup(html, "lxml")
        subject = soup.select_one("#thread_subject")
        if subject is not None:
            title = subject.get_text(" ", strip=True)
            if title:
                return title
        if soup.title is not None:
            title = soup.title.get_text(" ", strip=True)
            # Discuz commonly appends the site name after a dash/pipe.
            for separator in (" - ", " | ", " – "):
                if separator in title:
                    title = title.split(separator, 1)[0].strip()
                    break
            if title:
                return title
        return "Untitled"
