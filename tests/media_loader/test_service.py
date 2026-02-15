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
    repo.route_to_queue.return_value = None
    repo.set_retry.return_value = None
    return repo


@pytest.fixture
def sample_task() -> Task:
    return Task(
        id=uuid.UUID("00000000-0000-0000-0000-000000000100"),
        video_id=uuid.UUID("00000000-0000-0000-0000-000000000010"),
        state=TaskState.PENDING,
        queue_name="pixav:download",
        max_retries=3,
    )


@pytest.fixture
def service(
    mock_client: AsyncMock,
    mock_remuxer: AsyncMock,
    mock_scraper: AsyncMock,
    mock_video_repo: AsyncMock,
    mock_task_repo: AsyncMock,
) -> MediaLoaderService:
    return MediaLoaderService(
        client=mock_client,
        remuxer=mock_remuxer,
        scraper=mock_scraper,
        video_repo=mock_video_repo,
        task_repo=mock_task_repo,
        upload_queue_name="pixav:upload",
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
    ) -> None:
        result = await service.process_task(sample_task)

        assert result.state == TaskState.PENDING
        assert result.queue_name == "pixav:upload"
        assert result.local_path is not None

        mock_client.add_magnet.assert_awaited_once()
        mock_client.wait_complete.assert_awaited_once_with("hash123")
        mock_remuxer.remux.assert_awaited_once()
        mock_scraper.scrape.assert_awaited_once_with("Test Video")
        mock_video_repo.update_download_result.assert_awaited_once()
        mock_task_repo.route_to_queue.assert_awaited_once_with(
            sample_task.id,
            queue_name="pixav:upload",
            state=TaskState.PENDING,
        )
        mock_client.delete_torrent.assert_awaited_once_with("hash123", delete_files=True)

    async def test_process_task_cleanup_failure_non_fatal(
        self,
        service: MediaLoaderService,
        sample_task: Task,
        mock_client: AsyncMock,
    ) -> None:
        mock_client.delete_torrent.side_effect = Exception("delete failed")

        result = await service.process_task(sample_task)

        assert result.state == TaskState.PENDING
        mock_client.delete_torrent.assert_awaited_once()

    async def test_process_task_video_not_found(
        self,
        service: MediaLoaderService,
        sample_task: Task,
        mock_video_repo: AsyncMock,
    ) -> None:
        mock_video_repo.find_by_id.return_value = None

        result = await service.process_task(sample_task)

        assert result.state == TaskState.FAILED
        assert "not found" in (result.error_message or "")

    async def test_process_task_no_magnet(
        self,
        service: MediaLoaderService,
        sample_task: Task,
        mock_video_repo: AsyncMock,
    ) -> None:
        mock_video_repo.find_by_id.return_value = Video(
            title="No Magnet",
            magnet_uri=None,
            status=VideoStatus.DISCOVERED,
        )

        result = await service.process_task(sample_task)

        assert result.state == TaskState.FAILED
        assert "no magnet_uri" in (result.error_message or "")

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
        assert "DownloadError" in (result.error_message or "")
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
    ) -> None:
        mock_remuxer.remux.side_effect = RemuxError("ffmpeg crashed")

        result = await service.process_task(sample_task)

        assert result.state == TaskState.FAILED
        assert "RemuxError" in (result.error_message or "")

    async def test_process_task_metadata_failure_non_fatal(
        self,
        service: MediaLoaderService,
        sample_task: Task,
        mock_scraper: AsyncMock,
    ) -> None:
        mock_scraper.scrape.side_effect = Exception("stash down")

        result = await service.process_task(sample_task)

        assert result.state == TaskState.PENDING
        assert result.queue_name == "pixav:upload"

    async def test_process_task_without_scraper(
        self,
        mock_client: AsyncMock,
        mock_remuxer: AsyncMock,
        mock_video_repo: AsyncMock,
        mock_task_repo: AsyncMock,
        sample_task: Task,
    ) -> None:
        service_no_scraper = MediaLoaderService(
            client=mock_client,
            remuxer=mock_remuxer,
            scraper=None,
            video_repo=mock_video_repo,
            task_repo=mock_task_repo,
            upload_queue_name="pixav:upload",
        )

        result = await service_no_scraper.process_task(sample_task)
        assert result.state == TaskState.PENDING
        assert result.queue_name == "pixav:upload"

    async def test_process_task_requeues_when_retry_enabled(
        self,
        mock_client: AsyncMock,
        mock_remuxer: AsyncMock,
        mock_video_repo: AsyncMock,
        mock_task_repo: AsyncMock,
        sample_task: Task,
    ) -> None:
        retry_queue = AsyncMock()
        retry_queue.push.return_value = 1
        mock_client.add_magnet.side_effect = DownloadError("transient outage")

        retry_service = MediaLoaderService(
            client=mock_client,
            remuxer=mock_remuxer,
            scraper=None,
            video_repo=mock_video_repo,
            task_repo=mock_task_repo,
            upload_queue_name="pixav:upload",
            retry_queue=retry_queue,
        )

        result = await retry_service.process_task(sample_task)

        assert result.state == TaskState.PENDING
        assert result.retries == 1
        mock_task_repo.set_retry.assert_awaited_once()
        retry_queue.push.assert_awaited_once()
        mock_video_repo.update_status.assert_any_await(sample_task.video_id, VideoStatus.DISCOVERED)

    async def test_process_task_exhausted_retries_goes_to_dlq(
        self,
        mock_client: AsyncMock,
        mock_remuxer: AsyncMock,
        mock_video_repo: AsyncMock,
        mock_task_repo: AsyncMock,
        sample_task: Task,
    ) -> None:
        retry_queue = AsyncMock()
        retry_queue.push.return_value = 1
        dlq_queue = AsyncMock()
        dlq_queue.push.return_value = 1

        mock_client.add_magnet.side_effect = DownloadError("permanent failure")
        exhausted = sample_task.model_copy(update={"retries": sample_task.max_retries})

        service = MediaLoaderService(
            client=mock_client,
            remuxer=mock_remuxer,
            scraper=None,
            video_repo=mock_video_repo,
            task_repo=mock_task_repo,
            upload_queue_name="pixav:upload",
            retry_queue=retry_queue,
            dlq_queue=dlq_queue,
        )
        result = await service.process_task(exhausted)

        assert result.state == TaskState.FAILED
        mock_task_repo.update_state.assert_any_await(
            exhausted.id,
            TaskState.FAILED,
            error_message=result.error_message,
        )
        retry_queue.push.assert_not_awaited()
        dlq_queue.push.assert_awaited_once()
