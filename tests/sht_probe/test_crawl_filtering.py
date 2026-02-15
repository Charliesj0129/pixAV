"""Tests for forum-style crawling logic with regex filtering."""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, MagicMock

import pytest

from pixav.sht_probe.crawler import HttpxCrawler
from pixav.sht_probe.service import ShtProbeService


@pytest.fixture
def mock_crawler():
    crawler = MagicMock(spec=HttpxCrawler)
    crawler.crawl = AsyncMock()
    crawler.fetch_page_html = AsyncMock()
    return crawler


@pytest.fixture
def service(mock_crawler):
    return ShtProbeService(
        video_repo=AsyncMock(),
        queue=AsyncMock(),
        crawler=mock_crawler,
        min_quality_score=-10000,  # Accept everything for test
    )


@pytest.mark.asyncio
async def test_forum_crawl_filtering(service):
    """Verify that regex filter correctly picks only thread pages."""
    seed_url = "https://example.org/forum-1.html"
    # Matches 'thread-digits-digits-digits.html'
    filter_pattern = r"thread-\d+-\d+-\d+\.html"

    # Setup Real Crawler with mocked fetch
    real_crawler = HttpxCrawler()

    # Correct AsyncMock setup for side_effect
    async def fetch_html_mock(url):
        # Determine content based on URL
        if "thread-" in url:
            # Thread page with a magnet
            match = re.search(r"thread-(\d+)", url)
            uniq = match.group(1) if match else "00000"
            return f'<a href="magnet:?xt=urn:btih:abababababababababababababab{uniq}">Magnet</a>'

        # Board page with links
        return """
        <html>
            <body>
                <a href="thread-11111-1-1.html">Valid Thread 1</a>
                <a href="thread-22222-1-1.html">Valid Thread 2</a>
                <a href="forum-1-2.html">Next Page (Should Skip)</a>
                <a href="space-uid-999.html">User Profile (Should Skip)</a>
            </body>
        </html>
        """

    real_crawler._fetch_html = AsyncMock(side_effect=fetch_html_mock)

    # Setup Service with real crawler
    service._crawler = real_crawler

    # Mock Repo to return None (not found) for new videos
    service._video_repo.find_by_info_hash.return_value = None
    service._video_repo.insert = AsyncMock()
    service._queue.push = AsyncMock()

    # Run crawl with filter pattern
    magnets = await service.run_crawl(seed_url, link_pattern=filter_pattern)

    # 1. Assert Filtering Logic
    # Get all URLs visited by crawler
    visited_urls = [call.args[0] for call in real_crawler._fetch_html.call_args_list]

    # Must visit seed
    assert seed_url in visited_urls

    # Must visit threads
    assert "https://example.org/thread-11111-1-1.html" in visited_urls
    assert "https://example.org/thread-22222-1-1.html" in visited_urls

    # Must NOT visit other links
    assert "https://example.org/forum-1-2.html" not in visited_urls
    assert "https://example.org/space-uid-999.html" not in visited_urls

    # 2. Assert Magnets Found
    # 2 threads -> 2 unique magnets
    assert len(magnets) == 2
    assert any("11111" in m for m in magnets)  # url suffix used in magnet
