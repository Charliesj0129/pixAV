"""Tests for HttpxCrawler."""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from pixav.shared.exceptions import CrawlError
from pixav.sht_probe.crawler import HttpxCrawler


class TestHttpxCrawler:
    @respx.mock
    async def test_crawl_extracts_same_domain_links(self) -> None:
        html = """
        <html><body>
            <a href="/page/1">Page 1</a>
            <a href="/page/2">Page 2</a>
            <a href="https://other.com/ext">External</a>
            <a href="magnet:?xt=urn:btih:abc">Magnet</a>
        </body></html>
        """
        respx.get("https://example.com/").mock(return_value=httpx.Response(200, text=html))

        crawler = HttpxCrawler()
        links = await crawler.crawl("https://example.com/")

        # Should include internal links, exclude external, magnet, and javascript
        assert "https://example.com/page/1" in links
        assert "https://example.com/page/2" in links
        assert not any("other.com" in link for link in links)
        assert not any("magnet:" in link for link in links)

    @respx.mock
    async def test_crawl_deduplicates_links(self) -> None:
        html = """
        <html><body>
            <a href="/page/1">Link 1</a>
            <a href="/page/1#section">Link 1 again</a>
        </body></html>
        """
        respx.get("https://example.com/").mock(return_value=httpx.Response(200, text=html))

        crawler = HttpxCrawler()
        links = await crawler.crawl("https://example.com/")
        assert links.count("https://example.com/page/1") == 1

    @respx.mock
    async def test_crawl_fallback_to_flaresolverr(self) -> None:
        respx.get("https://protected.com/").mock(return_value=httpx.Response(403, text="Blocked"))

        mock_flare = AsyncMock()
        mock_flare.get_html.return_value = ('<html><body><a href="/ok">Link</a></body></html>', {})

        crawler = HttpxCrawler(flaresolverr=mock_flare)
        links = await crawler.crawl("https://protected.com/")

        mock_flare.get_html.assert_awaited_once_with("https://protected.com/", cookies={})
        assert "https://protected.com/ok" in links

    @respx.mock
    async def test_crawl_raises_without_flaresolverr(self) -> None:
        respx.get("https://protected.com/").mock(return_value=httpx.Response(403, text="Blocked"))

        crawler = HttpxCrawler()
        with pytest.raises(CrawlError, match="no FlareSolverr"):
            await crawler.crawl("https://protected.com/")

    @respx.mock
    async def test_fetch_page_html(self) -> None:
        respx.get("https://example.com/page").mock(return_value=httpx.Response(200, text="<html>Page</html>"))

        crawler = HttpxCrawler()
        html = await crawler.fetch_page_html("https://example.com/page")
        assert "<html>Page</html>" == html

    def test_extract_links_skips_javascript(self) -> None:
        html = '<html><body><a href="javascript:void(0)">JS</a></body></html>'
        links = HttpxCrawler._extract_links(html, "https://example.com/")
        assert links == []

    def test_extract_links_keeps_query_string(self) -> None:
        html = "<html><body>" '<a href="/forum.php?mod=viewthread&tid=123&page=1">T1</a>' "</body></html>"
        links = HttpxCrawler._extract_links(
            html,
            "https://example.com/forum.php?mod=forumdisplay&fid=103",
            link_pattern=r"mod=viewthread",
        )
        assert "https://example.com/forum.php?mod=viewthread&tid=123&page=1" in links
