"""Tests for ShtProbeService."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from pixav.shared.enums import VideoStatus
from pixav.shared.models import Video
from pixav.sht_probe.service import ShtProbeService, _title_from_magnet


@pytest.fixture
def mock_video_repo() -> AsyncMock:
    repo = AsyncMock()
    repo.find_by_magnet.return_value = None  # No existing videos by default
    repo.insert.side_effect = lambda v: v  # Return the video as-is
    return repo


@pytest.fixture
def mock_queue() -> AsyncMock:
    queue = AsyncMock()
    queue.push.return_value = 1
    return queue


@pytest.fixture
def mock_crawler() -> AsyncMock:
    crawler = AsyncMock()
    crawler.crawl.return_value = ["https://example.com/page/1"]
    crawler.fetch_page_html.return_value = '<html><a href="magnet:?xt=urn:btih:newmag&dn=New+Video">link</a></html>'
    return crawler


@pytest.fixture
def mock_extractor() -> AsyncMock:
    extractor = AsyncMock()
    extractor.extract.return_value = ["magnet:?xt=urn:btih:newmag&dn=New+Video"]
    return extractor


@pytest.fixture
def mock_jackett() -> AsyncMock:
    jackett = AsyncMock()
    jackett.search.return_value = [
        {
            "title": "Jackett Video",
            "magnet_uri": "magnet:?xt=urn:btih:jackett123",
            "size": 2 * 1024**3,  # 2 GB
            "seeders": 5,
        }
    ]
    return jackett


class TestShtProbeService:
    async def test_run_crawl_discovers_new_magnets(
        self,
        mock_video_repo: AsyncMock,
        mock_queue: AsyncMock,
        mock_crawler: AsyncMock,
        mock_extractor: AsyncMock,
    ) -> None:
        service = ShtProbeService(
            video_repo=mock_video_repo,
            queue=mock_queue,
            crawler=mock_crawler,
            extractor=mock_extractor,
        )

        result = await service.run_crawl("https://example.com")

        mock_crawler.crawl.assert_awaited_once_with("https://example.com", None)
        assert len(result) == 1
        assert "newmag" in result[0]

        # Should have inserted a video and pushed to queue
        mock_video_repo.insert.assert_awaited_once()
        mock_queue.push.assert_awaited_once()
        push_payload = mock_queue.push.call_args[0][0]
        assert "video_id" in push_payload
        assert "magnet_uri" in push_payload

    async def test_run_crawl_persists_tags(
        self,
        mock_video_repo: AsyncMock,
        mock_queue: AsyncMock,
        mock_crawler: AsyncMock,
    ) -> None:
        mock_crawler.crawl.return_value = []
        mock_crawler.fetch_page_html.return_value = '<a href="magnet:?xt=urn:btih:newmag">Magnet</a>'
        mock_video_repo.find_by_info_hash.return_value = None
        mock_video_repo.insert.return_value = None

        service = ShtProbeService(video_repo=mock_video_repo, queue=mock_queue, crawler=mock_crawler)
        await service.run_crawl("http://seed", tags=["tag1", "tag2"])

        mock_video_repo.insert.assert_awaited_once()
        inserted_video = mock_video_repo.insert.call_args[0][0]
        assert inserted_video.tags == ["tag1", "tag2"]

    async def test_run_crawl_skips_existing_magnets(
        self,
        mock_video_repo: AsyncMock,
        mock_queue: AsyncMock,
        mock_crawler: AsyncMock,
        mock_extractor: AsyncMock,
    ) -> None:
        # Simulate magnet already exists in DB
        mock_video_repo.find_by_magnet.return_value = Video(
            id=uuid.uuid4(),
            title="Existing",
            magnet_uri="magnet:?xt=urn:btih:newmag&dn=New+Video",
            status=VideoStatus.DISCOVERED,
        )

        service = ShtProbeService(
            video_repo=mock_video_repo,
            queue=mock_queue,
            crawler=mock_crawler,
            extractor=mock_extractor,
        )

        result = await service.run_crawl("https://example.com")

        assert result == []
        mock_video_repo.insert.assert_not_awaited()
        mock_queue.push.assert_not_awaited()

    async def test_run_crawl_requires_crawler(
        self,
        mock_video_repo: AsyncMock,
        mock_queue: AsyncMock,
    ) -> None:
        service = ShtProbeService(video_repo=mock_video_repo, queue=mock_queue)

        with pytest.raises(RuntimeError, match="crawler is required"):
            await service.run_crawl("https://example.com")

    async def test_run_search_discovers_new_magnets(
        self,
        mock_video_repo: AsyncMock,
        mock_queue: AsyncMock,
        mock_jackett: AsyncMock,
    ) -> None:
        service = ShtProbeService(
            video_repo=mock_video_repo,
            queue=mock_queue,
            jackett=mock_jackett,
        )

        result = await service.run_search("test query")

        mock_jackett.search.assert_awaited_once_with("test query", limit=50)
        assert len(result) == 1
        assert "jackett123" in result[0]

        mock_video_repo.insert.assert_awaited_once()
        inserted_video = mock_video_repo.insert.call_args[0][0]
        assert inserted_video.title == "Jackett Video"

        mock_queue.push.assert_awaited_once()

    async def test_run_search_skips_existing(
        self,
        mock_video_repo: AsyncMock,
        mock_queue: AsyncMock,
        mock_jackett: AsyncMock,
    ) -> None:
        mock_video_repo.find_by_magnet.return_value = Video(
            id=uuid.uuid4(),
            title="Old",
            magnet_uri="magnet:?xt=urn:btih:jackett123",
            status=VideoStatus.AVAILABLE,
        )

        service = ShtProbeService(
            video_repo=mock_video_repo,
            queue=mock_queue,
            jackett=mock_jackett,
        )

        result = await service.run_search("test query")
        assert result == []

    async def test_run_search_requires_jackett(
        self,
        mock_video_repo: AsyncMock,
        mock_queue: AsyncMock,
    ) -> None:
        service = ShtProbeService(video_repo=mock_video_repo, queue=mock_queue)

        with pytest.raises(RuntimeError, match="jackett is required"):
            await service.run_search("test")

    async def test_run_crawl_handles_page_error_gracefully(
        self,
        mock_video_repo: AsyncMock,
        mock_queue: AsyncMock,
        mock_crawler: AsyncMock,
        mock_extractor: AsyncMock,
    ) -> None:
        # First call (seed page html) succeeds, second call (page link) fails
        mock_crawler.fetch_page_html.side_effect = [
            '<html><a href="magnet:?xt=urn:btih:seed123&dn=Seed">link</a></html>',
            Exception("connection reset"),
        ]
        mock_extractor.extract.return_value = ["magnet:?xt=urn:btih:seed123&dn=Seed"]

        service = ShtProbeService(
            video_repo=mock_video_repo,
            queue=mock_queue,
            crawler=mock_crawler,
            extractor=mock_extractor,
        )

        # Should not raise, should still get the seed page magnet
        result = await service.run_crawl("https://example.com")
        assert len(result) == 1


class TestTitleFromMagnet:
    def test_extracts_dn_parameter(self) -> None:
        magnet = "magnet:?xt=urn:btih:abc123&dn=Hello+World&tr=udp://tracker"
        assert _title_from_magnet(magnet) == "Hello World"

    def test_url_decodes_dn(self) -> None:
        magnet = "magnet:?xt=urn:btih:abc123&dn=Hello%20World%21"
        assert _title_from_magnet(magnet) == "Hello World!"

    def test_returns_untitled_without_dn(self) -> None:
        magnet = "magnet:?xt=urn:btih:abc123"
        assert _title_from_magnet(magnet) == "Untitled"
