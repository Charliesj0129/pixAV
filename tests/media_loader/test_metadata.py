"""Tests for StashMetadataScraper."""

from __future__ import annotations

import httpx
import pytest
import respx

from pixav.media_loader.metadata import StashMetadataScraper
from pixav.shared.exceptions import CrawlError


@pytest.fixture
def scraper() -> StashMetadataScraper:
    return StashMetadataScraper(base_url="http://stash:9999", timeout=5)


class TestStashMetadataScraper:
    @respx.mock
    async def test_scrape_success(self, scraper: StashMetadataScraper) -> None:
        mock_response = {
            "data": {
                "findScenes": {
                    "count": 1,
                    "scenes": [
                        {
                            "id": "42",
                            "title": "Test Scene",
                            "date": "2025-01-01",
                            "details": "A test scene.",
                            "rating100": 85,
                            "organized": True,
                            "studio": {"name": "Test Studio"},
                            "tags": [{"name": "tag1"}, {"name": "tag2"}],
                            "performers": [{"name": "Performer A"}],
                            "files": [
                                {
                                    "path": "/data/test.mp4",
                                    "duration": 3600.0,
                                    "size": 1073741824,
                                    "video_codec": "h264",
                                    "width": 1920,
                                    "height": 1080,
                                }
                            ],
                        }
                    ],
                }
            }
        }
        respx.post("http://stash:9999/graphql").mock(return_value=httpx.Response(200, json=mock_response))

        result = await scraper.scrape("Test Scene")

        assert result["found"] is True
        assert result["stash_id"] == "42"
        assert result["title"] == "Test Scene"
        assert result["studio"] == "Test Studio"
        assert result["tags"] == ["tag1", "tag2"]
        assert result["performers"] == ["Performer A"]
        assert result["file_info"]["width"] == 1920

    @respx.mock
    async def test_scrape_no_results(self, scraper: StashMetadataScraper) -> None:
        mock_response = {
            "data": {
                "findScenes": {
                    "count": 0,
                    "scenes": [],
                }
            }
        }
        respx.post("http://stash:9999/graphql").mock(return_value=httpx.Response(200, json=mock_response))

        result = await scraper.scrape("Unknown Title")
        assert result["found"] is False

    @respx.mock
    async def test_scrape_no_studio(self, scraper: StashMetadataScraper) -> None:
        mock_response = {
            "data": {
                "findScenes": {
                    "count": 1,
                    "scenes": [
                        {
                            "id": "1",
                            "title": "Test",
                            "date": None,
                            "details": None,
                            "rating100": None,
                            "organized": False,
                            "studio": None,
                            "tags": [],
                            "performers": [],
                            "files": [],
                        }
                    ],
                }
            }
        }
        respx.post("http://stash:9999/graphql").mock(return_value=httpx.Response(200, json=mock_response))

        result = await scraper.scrape("Test")
        assert result["found"] is True
        assert result["studio"] is None
        assert "file_info" not in result

    @respx.mock
    async def test_scrape_http_error(self, scraper: StashMetadataScraper) -> None:
        respx.post("http://stash:9999/graphql").mock(return_value=httpx.Response(500, text="Internal Error"))

        with pytest.raises(CrawlError, match="Stash returned 500"):
            await scraper.scrape("Test")

    @respx.mock
    async def test_scrape_connection_error(self, scraper: StashMetadataScraper) -> None:
        respx.post("http://stash:9999/graphql").mock(side_effect=httpx.ConnectError("refused"))

        with pytest.raises(CrawlError, match="Stash request failed"):
            await scraper.scrape("Test")
