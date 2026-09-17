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
                    "Details": "https://indexer.example/thread/1",
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
        assert results[0]["source_url"] == "https://indexer.example/thread/1"
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
        assert results[0]["source_url"] == ""

    @respx.mock
    async def test_search_normalizes_scoring_inputs_and_guid_fallback(self, client: JackettClient) -> None:
        respx.get("http://jackett:9117/api/v2.0/indexers/all/results").mock(
            return_value=httpx.Response(
                200,
                json={
                    "Results": [
                        {
                            "Title": None,
                            "MagnetUri": " magnet:?xt=urn:btih:abc123 ",
                            "Guid": "https://indexer.example/thread/2",
                            "Size": "2048",
                            "Seeders": -3,
                        }
                    ]
                },
            )
        )

        assert await client.search("test") == [
            {
                "title": "",
                "magnet_uri": "magnet:?xt=urn:btih:abc123",
                "source_url": "https://indexer.example/thread/2",
                "size": 2048,
                "seeders": 0,
            }
        ]

    @respx.mock
    async def test_search_rejects_non_list_results(self, client: JackettClient) -> None:
        respx.get("http://jackett:9117/api/v2.0/indexers/all/results").mock(
            return_value=httpx.Response(200, json={"Results": {"unexpected": "mapping"}})
        )

        with pytest.raises(CrawlError, match="Results is not a list"):
            await client.search("test")

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


@respx.mock
async def test_observed_download_contract_and_existing_magnet():
    import json
    from pathlib import Path

    evidence = json.loads(Path("tests/fixtures/cardigann_20260907/download-contract.json").read_text())
    item = evidence["responses"][0]
    link = "http://jackett:9117/dl/sehuatang-pixav/?jackett_apikey=private-key&path=encoded&file=title"
    client = JackettClient("http://jackett:9117", "private-key", indexer="sehuatang-pixav", resolve_download_links=True)
    respx.get("http://jackett:9117/api/v2.0/indexers/sehuatang-pixav/results").respond(
        200, json={"Results": [{"Link": link}, {"MagnetUri": "existing", "Link": link}]}
    )
    download = respx.get(link).respond(item["status"], headers={"Location": "magnet:?xt=urn:btih:" + item["hashes"][0]})
    results = await client.search("")
    assert results[0]["magnet_uri"] == "magnet:?xt=urn:btih:" + item["hashes"][0].lower()
    assert results[1]["magnet_uri"] == "existing"
    assert download.call_count == 1


@pytest.mark.parametrize(
    "link",
    [
        "http://evil/dl/sehuatang-pixav/key/source/title",
        "http://jackett:9117/dl/other/key/source/title",
        "http://user@jackett:9117/dl/sehuatang-pixav/key/source/title",
        "http://jackett:9117/dl/sehuatang-pixav/key/source/title?secret=key",
    ],
)
@respx.mock
async def test_download_rejects_destination_without_request(link):
    client = JackettClient("http://jackett:9117", "key", indexer="sehuatang-pixav", resolve_download_links=True)
    with pytest.raises(CrawlError, match="destination rejected"):
        await client._resolve_link(link)
    assert not respx.calls


@pytest.mark.parametrize(
    "status,location", [(302, "https://evil/private-key"), (200, ""), (302, "magnet:?xt=urn:btih:bad"), (500, "")]
)
@respx.mock
async def test_download_contract_errors_are_redacted(status, location):
    link = "http://jackett:9117/dl/sehuatang-pixav/?jackett_apikey=private-key&path=source&file=title"
    respx.get(link).respond(status, headers={"Location": location}, text="private-body")
    client = JackettClient("http://jackett:9117", "private-key", indexer="sehuatang-pixav", resolve_download_links=True)
    with pytest.raises(CrawlError) as caught:
        await client._resolve_link(link)
    assert "private" not in str(caught.value)
    assert caught.value.__cause__ is None


@respx.mock
async def test_download_timeout_redacted():
    link = "http://jackett:9117/dl/sehuatang-pixav/?jackett_apikey=private-key&path=source&file=title"
    respx.get(link).mock(side_effect=httpx.ReadTimeout("private-key"))
    client = JackettClient("http://jackett:9117", "private-key", indexer="sehuatang-pixav", resolve_download_links=True)
    with pytest.raises(CrawlError, match="download request failed") as caught:
        await client._resolve_link(link)
    assert "private-key" not in str(caught.value)
