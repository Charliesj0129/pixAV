"""Unit tests for Sehuatang specific crawler and extractor."""

import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from pixav.shared.metrics import crawl_watermark_rejected
from pixav.sht_probe.sehuatang import (
    SehuatangCrawler,
    SehuatangExtractor,
    _is_obfuscated_text,
)


@pytest.fixture
def mock_flaresolverr() -> AsyncMock:
    """Mock the FlareSolverr session."""
    mock = AsyncMock()
    mock.get_html.return_value = ("<html>flare</html>", {"cf_clearance": "abc"}, "Mozilla/5.0 TestUA")
    return mock


# ---------------------------------------------------------------------------
# _fetch_html: httpx fast path
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_fetch_html_httpx_success_skips_flaresolverr(mock_flaresolverr: AsyncMock) -> None:
    """When httpx returns 200, FlareSolverr must not be called."""
    crawler = SehuatangCrawler(flaresolverr=mock_flaresolverr, request_delay_seconds=0)

    with respx.mock:
        respx.get("https://www.sehuatang.org/page.html").mock(
            return_value=httpx.Response(200, text="<html>direct</html>")
        )
        with patch("asyncio.sleep") as mock_sleep:
            html = await crawler._fetch_html("https://www.sehuatang.org/page.html")

    assert html == "<html>direct</html>"
    mock_flaresolverr.get_html.assert_not_called()
    mock_sleep.assert_not_called()


@pytest.mark.unit
async def test_fetch_html_httpx_failure_falls_back_to_flaresolverr(mock_flaresolverr: AsyncMock) -> None:
    """When httpx raises HTTPStatusError, FlareSolverr is used and cookies are merged."""
    mock_flaresolverr.get_html.return_value = ("<html>flare</html>", {"cf_clearance": "token123"}, "Mozilla/5.0 TestUA")
    crawler = SehuatangCrawler(flaresolverr=mock_flaresolverr, request_delay_seconds=0)

    with respx.mock:
        respx.get("https://www.sehuatang.org/page.html").mock(return_value=httpx.Response(403, text="Forbidden"))
        html = await crawler._fetch_html("https://www.sehuatang.org/page.html")

    assert html == "<html>flare</html>"
    mock_flaresolverr.get_html.assert_called_once()
    assert crawler._cookies.get("cf_clearance") == "token123"
    assert crawler._user_agent == "Mozilla/5.0 TestUA"


@pytest.mark.unit
async def test_fetch_html_cache_hit_skips_network_and_sleep(mock_flaresolverr: AsyncMock) -> None:
    """A pre-populated cache must be returned instantly with no I/O or sleep."""
    crawler = SehuatangCrawler(flaresolverr=mock_flaresolverr, request_delay_seconds=2.0)
    url = "https://www.sehuatang.org/cached.html"
    crawler._page_cache[url] = "<html>cached</html>"

    with patch("asyncio.sleep") as mock_sleep:
        with respx.mock:
            html = await crawler._fetch_html(url)

    assert html == "<html>cached</html>"
    mock_flaresolverr.get_html.assert_not_called()
    mock_sleep.assert_not_called()


# ---------------------------------------------------------------------------
# crawl(): board pagination
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_crawl_paginates_board_pages(mock_flaresolverr: AsyncMock) -> None:
    """With max_board_pages=3, three board page URLs must be fetched."""
    thread_html = '<html><a href="thread-1-1-1.html">T1</a></html>'
    crawler = SehuatangCrawler(flaresolverr=mock_flaresolverr, request_delay_seconds=0)

    with respx.mock:
        for page in range(1, 4):
            respx.get(f"https://www.sehuatang.org/forum-103-{page}.html").mock(
                return_value=httpx.Response(200, text=thread_html)
            )
        links = await crawler.crawl(
            "https://www.sehuatang.org/forum-103-1.html",
            link_pattern=r"thread(-\d+)+\.html",
            max_board_pages=3,
        )

    assert links == ["https://www.sehuatang.org/thread-1-1-1.html"]
    # All 3 board pages must have been fetched (cache populated)
    assert "https://www.sehuatang.org/forum-103-1.html" in crawler._page_cache
    assert "https://www.sehuatang.org/forum-103-2.html" in crawler._page_cache
    assert "https://www.sehuatang.org/forum-103-3.html" in crawler._page_cache


@pytest.mark.unit
async def test_crawl_non_standard_url_fetches_single_page(mock_flaresolverr: AsyncMock) -> None:
    """A URL that does not match the Discuz! board pattern crawls only one page."""
    crawler = SehuatangCrawler(flaresolverr=mock_flaresolverr, request_delay_seconds=0)
    url = "https://www.sehuatang.org/custom-page.html"

    with respx.mock:
        respx.get(url).mock(return_value=httpx.Response(200, text="<html></html>"))
        links = await crawler.crawl(url, max_board_pages=5)

    assert links == []
    assert len(crawler._page_cache) == 1


def test_extract_links_canonicalizes_viewthread_pagination() -> None:
    html = """
    <html><body>
      <a href="forum.php?mod=viewthread&tid=123&extra=page%3D1">T1</a>
      <a href="forum.php?mod=viewthread&tid=123&extra=page%3D1&page=2">T1 p2</a>
      <a href="forum.php?mod=viewthread&tid=123&page=3">T1 p3</a>
    </body></html>
    """
    links = SehuatangCrawler._extract_links(
        html,
        "https://www.sehuatang.org/forum-103-1.html",
        link_pattern=r"viewthread",
    )
    assert links == ["https://www.sehuatang.org/forum.php?mod=viewthread&tid=123"]


AGE_GATE_PAGE = """
    <html><body>
    <script>var safeid='SAFE123';</script>
    <a class="enter-btn" href="./">If you are over 18，please click here</a>
    </body></html>
    """


@pytest.mark.unit
async def test_fetch_html_age_gate_retries_with_safe_cookie(mock_flaresolverr: AsyncMock) -> None:
    """Age-gate page should trigger an automatic retry carrying the ``_safe`` cookie.

    The cookies actually transmitted are snapshotted per call: ``_cookies`` is a
    single mutable dict, so asserting on ``await_args_list`` directly would read
    the post-retry state and pass even when the retry sent the wrong cookies.
    """
    crawler = SehuatangCrawler(flaresolverr=mock_flaresolverr, request_delay_seconds=0)
    real_page = '<html><a href="thread-1-1-1.html">T1</a></html>'
    sent_cookies: list[dict[str, str]] = []
    responses = [
        (AGE_GATE_PAGE, {"cf_clearance": "cf1"}, "UA-1"),
        (real_page, {"_safe": "SAFE123"}, "UA-2"),
    ]

    async def _capture(_url: str, *, timeout: int, cookies: dict[str, str]) -> tuple[str, dict[str, str], str]:
        sent_cookies.append(dict(cookies))
        return responses[len(sent_cookies) - 1]

    mock_flaresolverr.get_html.side_effect = _capture

    with respx.mock:
        respx.get("https://www.sehuatang.org/forum-103-1.html").mock(return_value=httpx.Response(403, text="Forbidden"))
        html = await crawler._fetch_html("https://www.sehuatang.org/forum-103-1.html")

    assert html == real_page
    assert len(sent_cookies) == 2
    assert sent_cookies[1]["_safe"] == "SAFE123"
    assert sent_cookies[1]["safeid"] == "SAFE123"
    assert sent_cookies[1]["agree"] == "1"
    assert crawler._cookies.get("_safe") == "SAFE123"
    assert crawler._user_agent == "UA-2"


@pytest.mark.unit
async def test_fetch_html_age_gate_does_not_loop_when_safe_cookie_already_set(
    mock_flaresolverr: AsyncMock,
) -> None:
    """A pre-seeded ``_safe`` matching the page token must not trigger a second fetch."""
    crawler = SehuatangCrawler(flaresolverr=mock_flaresolverr, request_delay_seconds=0)
    crawler.seed_cookies({"_safe": "SAFE123"})
    mock_flaresolverr.get_html.return_value = (AGE_GATE_PAGE, {}, "UA-1")

    with respx.mock:
        respx.get("https://www.sehuatang.org/forum-103-1.html").mock(return_value=httpx.Response(403, text="Forbidden"))
        html = await crawler._fetch_html("https://www.sehuatang.org/forum-103-1.html")

    assert html == AGE_GATE_PAGE
    assert mock_flaresolverr.get_html.await_count == 1


@pytest.mark.unit
async def test_seeded_safe_cookie_is_sent_on_the_first_request(mock_flaresolverr: AsyncMock) -> None:
    """Cookies seeded from PIXAV_CRAWL_COOKIE_HEADER must reach FlareSolverr unchanged."""
    crawler = SehuatangCrawler(flaresolverr=mock_flaresolverr, request_delay_seconds=0)
    crawler.seed_cookies({"_safe": "SEEDED", "cPNj_2132_auth": "AUTHVAL"})
    sent_cookies: list[dict[str, str]] = []

    async def _capture(_url: str, *, timeout: int, cookies: dict[str, str]) -> tuple[str, dict[str, str], str]:
        sent_cookies.append(dict(cookies))
        return ('<html><a href="thread-1-1-1.html">T1</a></html>', {}, "UA-1")

    mock_flaresolverr.get_html.side_effect = _capture

    with respx.mock:
        respx.get("https://www.sehuatang.org/forum-103-1.html").mock(return_value=httpx.Response(403, text="Forbidden"))
        await crawler._fetch_html("https://www.sehuatang.org/forum-103-1.html")

    assert sent_cookies[0]["_safe"] == "SEEDED"
    assert sent_cookies[0]["cPNj_2132_auth"] == "AUTHVAL"


@pytest.mark.unit
async def test_fetch_html_inflight_deduplicates_same_url(mock_flaresolverr: AsyncMock) -> None:
    """Concurrent requests for the same URL should share one underlying fetch."""
    crawler = SehuatangCrawler(flaresolverr=mock_flaresolverr, request_delay_seconds=0)
    url = "https://www.sehuatang.org/thread-123-1-1.html"

    started = asyncio.Event()

    async def slow_fetch(_: str) -> str:
        started.set()
        await asyncio.sleep(0.01)
        return "<html>thread</html>"

    crawler._do_fetch = AsyncMock(side_effect=slow_fetch)

    results = await asyncio.gather(
        crawler._fetch_html(url),
        crawler._fetch_html(url),
        crawler._fetch_html(url),
    )

    await started.wait()
    assert results == ["<html>thread</html>"] * 3
    assert crawler._do_fetch.await_count == 1


@pytest.mark.unit
async def test_crawl_fetches_board_pages_concurrently(mock_flaresolverr: AsyncMock) -> None:
    """Board page fetches should run in parallel up to the configured limit."""
    crawler = SehuatangCrawler(
        flaresolverr=mock_flaresolverr,
        request_delay_seconds=0,
        board_fetch_concurrency=3,
    )

    active = 0
    max_seen = 0
    lock = asyncio.Lock()

    async def fake_fetch(url: str) -> str:
        nonlocal active, max_seen
        async with lock:
            active += 1
            max_seen = max(max_seen, active)
        try:
            await asyncio.sleep(0.01)
            suffix = url.rsplit("-", 1)[-1].split(".")[0]
            return f'<html><a href="thread-{suffix}-1-1.html">T</a></html>'
        finally:
            async with lock:
                active -= 1

    crawler._fetch_html = AsyncMock(side_effect=fake_fetch)

    links = await crawler.crawl(
        "https://www.sehuatang.org/forum-103-1.html",
        link_pattern=r"thread(-\d+)+\.html",
        max_board_pages=3,
    )

    assert max_seen > 1
    assert max_seen <= 3
    assert len(links) == 3


# ---------------------------------------------------------------------------
# SehuatangExtractor
# ---------------------------------------------------------------------------

_MAGNET_A_TAG = "magnet:?xt=urn:btih:ABCDEF1234567890ABCDEF1234567890ABCDEF12"
_MAGNET_JS = "magnet:?xt=urn:btih:9999999999999999999999999999999999999999"
_INFOHASH_UPPER = "111122223333444455556666777788889999AAAA"
_INFOHASH_LOWER = "aabbccddeeff00112233445566778899aabbccdd"


@pytest.mark.unit
async def test_extractor_finds_magnet_anchor_tags() -> None:
    """Magnet URIs in <a href> tags are extracted directly."""
    html = f'<html><a href="{_MAGNET_A_TAG}">Download</a></html>'
    magnets = await SehuatangExtractor().extract(html)
    assert _MAGNET_A_TAG in magnets


@pytest.mark.unit
async def test_extractor_finds_magnet_in_js_string() -> None:
    """Magnet URIs embedded in JavaScript strings are captured by regex."""
    html = f'<script>var m = "{_MAGNET_JS}";</script>'
    magnets = await SehuatangExtractor().extract(html)
    assert _MAGNET_JS in magnets


@pytest.mark.unit
async def test_extractor_converts_infohash_to_magnet() -> None:
    """Bare 40-char hex info-hashes are converted to magnet URIs (uppercased)."""
    html = f"<div>Hash: {_INFOHASH_UPPER}</div><div>{_INFOHASH_LOWER}</div>"
    magnets = await SehuatangExtractor().extract(html)
    assert f"magnet:?xt=urn:btih:{_INFOHASH_UPPER}" in magnets
    assert f"magnet:?xt=urn:btih:{_INFOHASH_LOWER.upper()}" in magnets


@pytest.mark.unit
async def test_extractor_attaches_thread_title_to_bare_hash() -> None:
    html = f'<html><h1 id="thread_subject">Real Thread Title</h1><div>{_INFOHASH_UPPER}</div></html>'
    candidates = await SehuatangExtractor().extract_candidates(html, "https://www.sehuatang.org/thread-1.html")
    assert candidates[0].title == "Real Thread Title"
    assert candidates[0].source_url.endswith("thread-1.html")


@pytest.mark.unit
async def test_concurrent_age_gates_each_resolve_with_their_own_token(
    mock_flaresolverr: AsyncMock,
) -> None:
    """Every board page must come back real when several hit the age-gate at once.

    Models the site faithfully: each gated request mints a distinct ``safeid``
    and only accepts a retry carrying that exact token. All three gate responses
    are held until every page has requested one, which is what forces the tasks
    to interleave -- resolving the gate without serialization then lets the
    tokens overwrite one another in the shared cookie jar, and all but the
    last-written page silently comes back as the gate again.
    """
    pages = 3
    crawler = SehuatangCrawler(flaresolverr=mock_flaresolverr, request_delay_seconds=0, max_board_pages=pages)
    issued: dict[str, str] = {}
    all_gated = asyncio.Event()
    gated_count = 0

    async def _serve(url: str, *, timeout: int, cookies: dict[str, str]) -> tuple[str, dict[str, str], str]:
        nonlocal gated_count
        page_id = url.rsplit("/", 1)[-1]
        if cookies.get("_safe") is not None and cookies.get("_safe") == issued.get(page_id):
            tid = page_id.split("-")[2].split(".")[0]
            return (f'<html><a href="forum.php?mod=viewthread&tid={tid}">T</a></html>', {}, "UA")

        issued[page_id] = f"SAFE-{page_id}"
        gated_count += 1
        if gated_count >= pages:
            all_gated.set()
        await all_gated.wait()
        return (AGE_GATE_PAGE.replace("SAFE123", issued[page_id]), {}, "UA")

    mock_flaresolverr.get_html.side_effect = _serve

    with respx.mock:
        respx.route(host="www.sehuatang.org").mock(return_value=httpx.Response(403, text="Forbidden"))
        links = await asyncio.wait_for(
            crawler.crawl("https://www.sehuatang.org/forum-103-1.html", r"mod=viewthread"), timeout=5
        )

    for page_id, html in crawler._page_cache.items():
        assert not SehuatangCrawler._looks_like_age_gate(html), f"{page_id} still shows the age-gate"
    assert len(links) == pages


@pytest.mark.unit
class TestWatermarkRejection:
    """Sehuatang stamps pages with a 40-hex watermark that is not an info hash.

    It is a random key byte followed by ``sehuatang@gmail.com`` XOR-ed with that
    key, so it matches the bare-info-hash pattern exactly. Left unfiltered it
    accounted for over half of all discovered magnets.
    """

    @staticmethod
    def _watermark(key: int) -> str:
        payload = b"sehuatang@gmail.com"
        return bytes([key] + [b ^ key for b in payload]).hex()

    @pytest.mark.parametrize("key", [0x7B, 0x19, 0xA9, 0x2A, 0x00])
    def test_rejects_watermark_for_any_key(self, key: int) -> None:
        assert _is_obfuscated_text(self._watermark(key)) is True

    @pytest.mark.parametrize(
        "info_hash",
        [
            "c7c12339a5792fa68c6a4d77b83a5b123960f617",
            "d7b741650272bde58f3a2803b616b07077984830",
            "da39a3ee5e6b4b0d3255bfef95601890afd80709",
        ],
    )
    def test_keeps_real_info_hashes(self, info_hash: str) -> None:
        assert _is_obfuscated_text(info_hash) is False

    async def test_extractor_drops_watermark_but_keeps_real_hash(self) -> None:
        real = "C7C12339A5792FA68C6A4D77B83A5B123960F617"
        html = f"<html><body>{self._watermark(0x7B)} and {real}</body></html>"

        magnets = await SehuatangExtractor().extract(html)

        assert magnets == [f"magnet:?xt=urn:btih:{real}"]

    async def test_discarded_watermarks_are_counted(self) -> None:
        """A cycle that discards everything and one that finds nothing both
        report new=0; only this counter tells them apart."""
        before = crawl_watermark_rejected.labels(module="sht_probe")._value.get()
        html = f"<html><body>{self._watermark(0x11)} {self._watermark(0x22)}</body></html>"

        await SehuatangExtractor().extract(html)

        assert crawl_watermark_rejected.labels(module="sht_probe")._value.get() == before + 2
