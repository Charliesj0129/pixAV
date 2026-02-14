"""Tests for BeautifulSoupExtractor."""

from __future__ import annotations

import pytest

from pixav.sht_probe.parser import BeautifulSoupExtractor


@pytest.fixture
def extractor() -> BeautifulSoupExtractor:
    return BeautifulSoupExtractor()


class TestBeautifulSoupExtractor:
    async def test_extract_from_a_tags(self, extractor: BeautifulSoupExtractor) -> None:
        html = """
        <html><body>
            <a href="magnet:?xt=urn:btih:abc123&dn=Test+Video">Download</a>
            <a href="magnet:?xt=urn:btih:def456&dn=Other+Video">Download 2</a>
        </body></html>
        """
        magnets = await extractor.extract(html)
        assert len(magnets) == 2
        assert any("abc123" in m for m in magnets)
        assert any("def456" in m for m in magnets)

    async def test_extract_from_js_embedded(self, extractor: BeautifulSoupExtractor) -> None:
        html = """
        <html><body>
            <script>
                var link = "magnet:?xt=urn:btih:hidden999&dn=Hidden";
            </script>
        </body></html>
        """
        magnets = await extractor.extract(html)
        assert len(magnets) == 1
        assert "hidden999" in magnets[0]

    async def test_no_magnets_returns_empty(self, extractor: BeautifulSoupExtractor) -> None:
        html = "<html><body><p>No magnets here</p></body></html>"
        magnets = await extractor.extract(html)
        assert magnets == []

    async def test_deduplicates(self, extractor: BeautifulSoupExtractor) -> None:
        html = """
        <html><body>
            <a href="magnet:?xt=urn:btih:same123&dn=Test">Link 1</a>
            <a href="magnet:?xt=urn:btih:same123&dn=Test">Link 2</a>
        </body></html>
        """
        magnets = await extractor.extract(html)
        assert len(magnets) == 1

    async def test_handles_malformed_html(self, extractor: BeautifulSoupExtractor) -> None:
        html = "<html><body><a href='magnet:?xt=urn:btih:ok123'>unclosed"
        magnets = await extractor.extract(html)
        assert len(magnets) == 1
        assert "ok123" in magnets[0]

    async def test_ignores_non_magnet_hrefs(self, extractor: BeautifulSoupExtractor) -> None:
        html = """
        <html><body>
            <a href="https://example.com">Normal link</a>
            <a href="magnet:?xt=urn:btih:real123">Magnet</a>
        </body></html>
        """
        magnets = await extractor.extract(html)
        assert len(magnets) == 1
        assert "real123" in magnets[0]
