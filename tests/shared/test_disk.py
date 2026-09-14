from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from pixav.shared.disk import DownloadSpaceGuard


async def test_low_disk_latches_and_does_not_auto_resume(tmp_path) -> None:
    redis = AsyncMock()
    redis.get.side_effect = [None, '{"reason":"latched"}']
    guard = DownloadSpaceGuard(
        redis,
        path=str(tmp_path),
        pause_key="pause",
        min_free_bytes=100,
        min_free_percent=10,
    )
    with patch("pixav.shared.disk.shutil.disk_usage", return_value=SimpleNamespace(total=1000, free=50)):
        first = await guard.check_and_latch()
        second = await guard.check_and_latch()
    assert first.paused is True
    assert second.paused is True
    redis.set.assert_awaited_once()
    redis.delete.assert_not_awaited()


async def test_resume_revalidates_space(tmp_path) -> None:
    redis = AsyncMock()
    guard = DownloadSpaceGuard(
        redis,
        path=str(tmp_path),
        pause_key="pause",
        min_free_bytes=100,
        min_free_percent=10,
    )
    with patch("pixav.shared.disk.shutil.disk_usage", return_value=SimpleNamespace(total=1000, free=50)):
        with pytest.raises(RuntimeError):
            await guard.resume()
    redis.delete.assert_not_awaited()

    with patch("pixav.shared.disk.shutil.disk_usage", return_value=SimpleNamespace(total=1000, free=500)):
        await guard.resume()
    redis.delete.assert_awaited_once_with("pause")
