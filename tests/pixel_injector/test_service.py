"""Tests for PixelInjectorService."""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock

import pytest

from pixav.pixel_injector.service import PixelInjectorService
from pixav.pixel_injector.session import RedroidSession
from pixav.shared.enums import TaskState
from pixav.shared.exceptions import RedroidError, UploadError, VerificationError
from pixav.shared.models import Task


@pytest.fixture
def redroid_session() -> RedroidSession:
    return RedroidSession(
        task_id="00000000-0000-0000-0000-000000000500",
        container_id="container-123",
        adb_host="127.0.0.1",
        adb_port=32768,
    )


@pytest.fixture
def mock_redroid(redroid_session: RedroidSession) -> AsyncMock:
    mock = AsyncMock()
    mock.create.return_value = redroid_session
    mock.wait_ready.return_value = True
    mock.destroy.return_value = None
    return mock


@pytest.fixture
def mock_uploader() -> AsyncMock:
    mock = AsyncMock()
    mock.push_file.return_value = "/sdcard/DCIM/test.jpg"
    mock.trigger_upload.return_value = None
    return mock


@pytest.fixture
def mock_verifier() -> AsyncMock:
    mock = AsyncMock()
    mock.wait_for_share_url.return_value = "https://photos.app.goo.gl/test123"
    mock.validate_share_url.return_value = True
    return mock


@pytest.fixture
def service(mock_redroid: AsyncMock, mock_uploader: AsyncMock, mock_verifier: AsyncMock) -> PixelInjectorService:
    return PixelInjectorService(redroid=mock_redroid, uploader=mock_uploader, verifier=mock_verifier)


@pytest.fixture
def sample_task() -> Task:
    return Task(
        id=uuid.UUID("00000000-0000-0000-0000-000000000500"),
        video_id=uuid.UUID("00000000-0000-0000-0000-000000000600"),
        state=TaskState.PENDING,
        queue_name="pixav:upload",
        local_path="/tmp/photo.jpg",
        share_url=None,
    )


@pytest.mark.asyncio
async def test_process_task_happy_path(
    service: PixelInjectorService,
    sample_task: Task,
    redroid_session: RedroidSession,
    mock_redroid: AsyncMock,
    mock_uploader: AsyncMock,
    mock_verifier: AsyncMock,
) -> None:
    result = await service.process_task(sample_task)

    mock_redroid.create.assert_called_once_with(str(sample_task.id))
    mock_redroid.wait_ready.assert_called_once_with("container-123", timeout=120)
    mock_uploader.push_file.assert_called_once_with(redroid_session, "/tmp/photo.jpg")
    mock_uploader.trigger_upload.assert_called_once_with(redroid_session, "/sdcard/DCIM/test.jpg")
    mock_verifier.wait_for_share_url.assert_called_once_with(redroid_session, timeout=300)
    mock_verifier.validate_share_url.assert_called_once_with("https://photos.app.goo.gl/test123")
    mock_redroid.destroy.assert_called_once_with("container-123")

    assert result.state == TaskState.COMPLETE
    assert result.share_url == "https://photos.app.goo.gl/test123"
    assert result.id == sample_task.id


@pytest.mark.asyncio
async def test_process_task_redroid_create_fails(
    service: PixelInjectorService,
    sample_task: Task,
    mock_redroid: AsyncMock,
) -> None:
    mock_redroid.create.side_effect = RedroidError("failed to create container")

    result = await service.process_task(sample_task)

    assert result.state == TaskState.FAILED
    assert result.share_url is None
    assert result.error_message is not None
    mock_redroid.destroy.assert_not_called()


@pytest.mark.asyncio
async def test_process_task_upload_fails(
    service: PixelInjectorService,
    sample_task: Task,
    mock_redroid: AsyncMock,
    mock_uploader: AsyncMock,
) -> None:
    mock_uploader.trigger_upload.side_effect = UploadError("upload failed")

    result = await service.process_task(sample_task)

    assert result.state == TaskState.FAILED
    assert result.share_url is None
    assert result.error_message is not None
    mock_redroid.destroy.assert_called_once_with("container-123")


@pytest.mark.asyncio
async def test_process_task_verification_fails(
    service: PixelInjectorService,
    sample_task: Task,
    mock_redroid: AsyncMock,
    mock_verifier: AsyncMock,
) -> None:
    mock_verifier.validate_share_url.return_value = False

    result = await service.process_task(sample_task)

    assert result.state == TaskState.FAILED
    assert result.share_url is None
    assert result.error_message is not None
    mock_redroid.destroy.assert_called_once_with("container-123")


@pytest.mark.asyncio
async def test_process_task_always_destroys_container(
    service: PixelInjectorService,
    sample_task: Task,
    mock_redroid: AsyncMock,
    mock_uploader: AsyncMock,
) -> None:
    mock_uploader.push_file.side_effect = UploadError("push failed")

    result = await service.process_task(sample_task)

    mock_redroid.destroy.assert_called_once_with("container-123")
    assert result.state == TaskState.FAILED


@pytest.mark.asyncio
async def test_process_task_wait_ready_timeout(
    service: PixelInjectorService,
    sample_task: Task,
    mock_redroid: AsyncMock,
) -> None:
    mock_redroid.wait_ready.return_value = False

    result = await service.process_task(sample_task)

    assert result.state == TaskState.FAILED
    assert result.share_url is None
    mock_redroid.destroy.assert_called_once_with("container-123")


@pytest.mark.asyncio
async def test_process_task_verification_error(
    service: PixelInjectorService,
    sample_task: Task,
    mock_redroid: AsyncMock,
    mock_verifier: AsyncMock,
) -> None:
    mock_verifier.wait_for_share_url.side_effect = VerificationError("timeout waiting for share url")

    result = await service.process_task(sample_task)

    assert result.state == TaskState.FAILED
    assert result.share_url is None
    mock_redroid.destroy.assert_called_once_with("container-123")


@pytest.mark.asyncio
async def test_process_task_missing_local_path_fails_fast(
    service: PixelInjectorService,
    sample_task: Task,
    mock_redroid: AsyncMock,
) -> None:
    missing_path_task = sample_task.model_copy(update={"local_path": None})

    result = await service.process_task(missing_path_task)

    assert result.state == TaskState.FAILED
    assert result.error_message == "local_path is required for upload tasks"
    mock_redroid.create.assert_not_called()


@pytest.mark.asyncio
async def test_process_task_timeout_marks_failed(
    mock_redroid: AsyncMock,
    mock_uploader: AsyncMock,
    mock_verifier: AsyncMock,
    sample_task: Task,
) -> None:
    service = PixelInjectorService(
        redroid=mock_redroid,
        uploader=mock_uploader,
        verifier=mock_verifier,
        task_timeout_seconds=1,
    )

    async def _slow_trigger(*_args: object, **_kwargs: object) -> None:
        await asyncio.sleep(2)

    mock_uploader.trigger_upload.side_effect = _slow_trigger

    result = await service.process_task(sample_task)

    assert result.state == TaskState.FAILED
    assert result.error_message == "upload timed out after 1s"
