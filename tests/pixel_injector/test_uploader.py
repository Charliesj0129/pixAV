"""Tests for UIAutomatorUploader."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from pixav.pixel_injector.session import RedroidSession
from pixav.pixel_injector.uploader import UIAutomatorUploader
from pixav.shared.exceptions import UploadError


@pytest.fixture
def mock_adb() -> AsyncMock:
    adb = AsyncMock()
    adb.connect.return_value = None
    adb.push.return_value = None
    adb.shell.return_value = "Broadcasting: Intent..."
    return adb


@pytest.fixture
def uploader(mock_adb: AsyncMock) -> UIAutomatorUploader:
    return UIAutomatorUploader(adb=mock_adb)


@pytest.fixture
def session() -> RedroidSession:
    return RedroidSession(
        task_id="task-1",
        container_id="container-123",
        adb_host="127.0.0.1",
        adb_port=32768,
    )


class TestUIAutomatorUploader:
    async def test_push_file_success(
        self,
        uploader: UIAutomatorUploader,
        mock_adb: AsyncMock,
        session: RedroidSession,
    ) -> None:
        result = await uploader.push_file(session, "/data/remuxed/video.mp4")

        assert result == "/sdcard/DCIM/Camera/video.mp4"
        mock_adb.connect.assert_awaited_once_with("127.0.0.1", 32768)
        mock_adb.push.assert_awaited_once_with("/data/remuxed/video.mp4", "/sdcard/DCIM/Camera/video.mp4")

    async def test_push_file_failure(
        self,
        uploader: UIAutomatorUploader,
        mock_adb: AsyncMock,
        session: RedroidSession,
    ) -> None:
        mock_adb.push.side_effect = Exception("adb error")

        with pytest.raises(UploadError, match="failed to push"):
            await uploader.push_file(session, "/data/video.mp4")

    async def test_trigger_upload(
        self,
        uploader: UIAutomatorUploader,
        mock_adb: AsyncMock,
        session: RedroidSession,
    ) -> None:
        await uploader.trigger_upload(session, "/sdcard/DCIM/Camera/video.mp4")

        mock_adb.connect.assert_awaited_once_with("127.0.0.1", 32768)
        mock_adb.shell.assert_awaited_once()
        cmd = mock_adb.shell.call_args[0][0]
        assert "MEDIA_SCANNER_SCAN_FILE" in cmd
        assert "/sdcard/DCIM/Camera/video.mp4" in cmd

    async def test_trigger_upload_failure(
        self,
        uploader: UIAutomatorUploader,
        mock_adb: AsyncMock,
        session: RedroidSession,
    ) -> None:
        mock_adb.shell.side_effect = Exception("shell error")

        with pytest.raises(UploadError, match="failed to trigger"):
            await uploader.trigger_upload(session, "/sdcard/video.mp4")
