"""Tests for JackettClient."""

from __future__ import annotations

import httpx
import pytest
import respx

from pixav.shared.exceptions import CrawlError
from pixav.sht_probe.jackett_client import JackettClient


@pytest.fixture
def client() -> JackettClient:
    return JackettClient(base_url="http://jackett:9117", api_key="test-key", timeout=5)


class TestJackettClient:
    @respx.mock
    async def test_search_returns_results(self, client: JackettClient) -> None:
        mock_response = {
            "Results": [
                {
                    "Title": "Test Video 720p",
                    "MagnetUri": "magnet:?xt=urn:btih:abc123",
                    "Size": 1024000,
                    "Seeders": 10,
                },
                {
                    "Title": "Test Video 1080p",
                    "MagnetUri": "magnet:?xt=urn:btih:def456",
                    "Size": 2048000,
                    "Seeders": 25,
                },
            ]
        }
        respx.get("http://jackett:9117/api/v2.0/indexers/all/results").mock(
            return_value=httpx.Response(200, json=mock_response)
        )

        results = await client.search("test video")
        assert len(results) == 2
        assert results[0]["title"] == "Test Video 720p"
        assert results[0]["magnet_uri"] == "magnet:?xt=urn:btih:abc123"
        assert results[1]["seeders"] == 25

    @respx.mock
    async def test_search_empty_results(self, client: JackettClient) -> None:
        respx.get("http://jackett:9117/api/v2.0/indexers/all/results").mock(
            return_value=httpx.Response(200, json={"Results": []})
        )

        results = await client.search("nonexistent")
        assert results == []

    @respx.mock
    async def test_search_respects_limit(self, client: JackettClient) -> None:
        many_results = {
            "Results": [
                {"Title": f"Video {i}", "MagnetUri": f"magnet:?xt=urn:btih:hash{i}", "Size": 100, "Seeders": 1}
                for i in range(100)
            ]
        }
        respx.get("http://jackett:9117/api/v2.0/indexers/all/results").mock(
            return_value=httpx.Response(200, json=many_results)
        )

        results = await client.search("video", limit=10)
        assert len(results) == 10

    @respx.mock
    async def test_search_handles_missing_magnet(self, client: JackettClient) -> None:
        respx.get("http://jackett:9117/api/v2.0/indexers/all/results").mock(
            return_value=httpx.Response(200, json={"Results": [{"Title": "No magnet", "Size": 100, "Seeders": 1}]})
        )

        results = await client.search("test")
        assert len(results) == 1
        assert results[0]["magnet_uri"] is None

    @respx.mock
    async def test_search_raises_on_http_error(self, client: JackettClient) -> None:
        respx.get("http://jackett:9117/api/v2.0/indexers/all/results").mock(
            return_value=httpx.Response(500, text="Internal Server Error")
        )

        with pytest.raises(CrawlError, match="Jackett returned 500"):
            await client.search("test")

    @respx.mock
    async def test_search_raises_on_connection_error(self, client: JackettClient) -> None:
        respx.get("http://jackett:9117/api/v2.0/indexers/all/results").mock(
            side_effect=httpx.ConnectError("Connection refused")
        )

        with pytest.raises(CrawlError, match="Jackett request failed"):
            await client.search("test")
