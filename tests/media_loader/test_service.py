"""Tests for MediaLoaderService."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from pixav.media_loader.service import MediaLoaderService
from pixav.shared.enums import TaskState, VideoStatus
from pixav.shared.exceptions import DownloadError, RemuxError
from pixav.shared.models import Task, Video


@pytest.fixture
def mock_client() -> AsyncMock:
    client = AsyncMock()
    client.add_magnet.return_value = "hash123"
    client.wait_complete.return_value = "/downloads/video.mkv"
    return client


@pytest.fixture
def mock_remuxer() -> AsyncMock:
    remuxer = AsyncMock()
    remuxer.remux.return_value = None
    return remuxer


@pytest.fixture
def mock_scraper() -> AsyncMock:
    scraper = AsyncMock()
    scraper.scrape.return_value = {"found": True, "title": "Test", "tags": ["tag1"]}
    return scraper


@pytest.fixture
def mock_video_repo() -> AsyncMock:
    repo = AsyncMock()
    repo.find_by_id.return_value = Video(
        id=uuid.UUID("00000000-0000-0000-0000-000000000010"),
        title="Test Video",
        magnet_uri="magnet:?xt=urn:btih:abc123",
        status=VideoStatus.DISCOVERED,
    )
    repo.update_status.return_value = None
    return repo


@pytest.fixture
def mock_task_repo() -> AsyncMock:
    repo = AsyncMock()
    repo.update_state.return_value = None
    return repo


@pytest.fixture
def mock_upload_queue() -> AsyncMock:
    queue = AsyncMock()
    queue.push.return_value = 1
    return queue


@pytest.fixture
def sample_task() -> Task:
    return Task(
        id=uuid.UUID("00000000-0000-0000-0000-000000000100"),
        video_id=uuid.UUID("00000000-0000-0000-0000-000000000010"),
        state=TaskState.PENDING,
        queue_name="pixav:download",
    )


@pytest.fixture
def service(
    mock_client: AsyncMock,
    mock_remuxer: AsyncMock,
    mock_scraper: AsyncMock,
    mock_video_repo: AsyncMock,
    mock_task_repo: AsyncMock,
    mock_upload_queue: AsyncMock,
) -> MediaLoaderService:
    return MediaLoaderService(
        client=mock_client,
        remuxer=mock_remuxer,
        scraper=mock_scraper,
        video_repo=mock_video_repo,
        task_repo=mock_task_repo,
        upload_queue=mock_upload_queue,
        output_dir="/data/remuxed",
    )


class TestMediaLoaderService:
    async def test_process_task_happy_path(
        self,
        service: MediaLoaderService,
        sample_task: Task,
        mock_client: AsyncMock,
        mock_remuxer: AsyncMock,
        mock_scraper: AsyncMock,
        mock_video_repo: AsyncMock,
        mock_task_repo: AsyncMock,
        mock_upload_queue: AsyncMock,
    ) -> None:
        result = await service.process_task(sample_task)

        assert result.state == TaskState.COMPLETE
        assert result.local_path is not None

        # Verify full pipeline was called
        mock_client.add_magnet.assert_awaited_once()
        mock_client.wait_complete.assert_awaited_once_with("hash123")
        mock_remuxer.remux.assert_awaited_once()
        mock_scraper.scrape.assert_awaited_once_with("Test Video")

        # Video status updated through pipeline
        assert mock_video_repo.update_status.await_count >= 2  # DOWNLOADING + DOWNLOADED

        # Upload queue received message
        mock_upload_queue.push.assert_awaited_once()
        push_payload = mock_upload_queue.push.call_args[0][0]
        assert "task_id" in push_payload
        assert "video_id" in push_payload
        assert "local_path" in push_payload

    async def test_process_task_video_not_found(
        self,
        service: MediaLoaderService,
        sample_task: Task,
        mock_video_repo: AsyncMock,
    ) -> None:
        mock_video_repo.find_by_id.return_value = None

        result = await service.process_task(sample_task)

        assert result.state == TaskState.FAILED
        assert "not found" in result.error_message

    async def test_process_task_no_magnet(
        self,
        service: MediaLoaderService,
        sample_task: Task,
        mock_video_repo: AsyncMock,
    ) -> None:
        mock_video_repo.find_by_id.return_value = Video(
            title="No Magnet", magnet_uri=None, status=VideoStatus.DISCOVERED
        )

        result = await service.process_task(sample_task)

        assert result.state == TaskState.FAILED
        assert "no magnet_uri" in result.error_message

    async def test_process_task_download_fails(
        self,
        service: MediaLoaderService,
        sample_task: Task,
        mock_client: AsyncMock,
        mock_task_repo: AsyncMock,
        mock_video_repo: AsyncMock,
    ) -> None:
        mock_client.add_magnet.side_effect = DownloadError("torrent client down")

        result = await service.process_task(sample_task)

        assert result.state == TaskState.FAILED
        assert "DownloadError" in result.error_message
        # Task and video status should reflect failure
        mock_task_repo.update_state.assert_any_await(
            sample_task.id,
            TaskState.FAILED,
            error_message=result.error_message,
        )
        mock_video_repo.update_status.assert_any_await(sample_task.video_id, VideoStatus.FAILED)

    async def test_process_task_remux_fails(
        self,
        service: MediaLoaderService,
        sample_task: Task,
        mock_remuxer: AsyncMock,
        mock_task_repo: AsyncMock,
        mock_video_repo: AsyncMock,
    ) -> None:
        mock_remuxer.remux.side_effect = RemuxError("ffmpeg crashed")

        result = await service.process_task(sample_task)

        assert result.state == TaskState.FAILED
        assert "RemuxError" in result.error_message

    async def test_process_task_metadata_failure_non_fatal(
        self,
        service: MediaLoaderService,
        sample_task: Task,
        mock_scraper: AsyncMock,
        mock_upload_queue: AsyncMock,
    ) -> None:
        mock_scraper.scrape.side_effect = Exception("stash down")

        result = await service.process_task(sample_task)

        # Should still succeed — metadata is best-effort
        assert result.state == TaskState.COMPLETE
        mock_upload_queue.push.assert_awaited_once()

    async def test_process_task_without_scraper(
        self,
        mock_client: AsyncMock,
        mock_remuxer: AsyncMock,
        mock_video_repo: AsyncMock,
        mock_task_repo: AsyncMock,
        mock_upload_queue: AsyncMock,
        sample_task: Task,
    ) -> None:
        service_no_scraper = MediaLoaderService(
            client=mock_client,
            remuxer=mock_remuxer,
            scraper=None,
            video_repo=mock_video_repo,
            task_repo=mock_task_repo,
            upload_queue=mock_upload_queue,
        )

        result = await service_no_scraper.process_task(sample_task)
        assert result.state == TaskState.COMPLETE
