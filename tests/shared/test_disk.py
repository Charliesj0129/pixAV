from __future__ import annotations

import json
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
    payload = json.loads(redis.set.await_args.args[1])
    assert payload["paused"] is True
    assert payload["below_threshold"] is True
    assert redis.set.await_args.kwargs == {"nx": True}
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

    redis.get.return_value = '{"paused":true,"reason":"another operator"}'
    with patch("pixav.shared.disk.shutil.disk_usage", return_value=SimpleNamespace(total=1000, free=500)):
        with pytest.raises(RuntimeError, match="owner"):
            await guard.resume()
    redis.delete.assert_not_awaited()


async def test_disk_resume_cannot_delete_replacement_pause(tmp_path):
    from pixav.shared.disk import PhysicalCapacity

    redis = AsyncMock()
    redis.get.return_value = None
    token = "synthetic-owner-token-with-at-least-32-bytes"
    probe = AsyncMock(return_value=PhysicalCapacity("D:\\", 1000, 50))
    guard = DownloadSpaceGuard(
        redis,
        path=str(tmp_path),
        pause_key="pause",
        min_free_bytes=100,
        min_free_percent=10,
        owner_token=token,
        physical_probe=probe,
    )
    with patch("pixav.shared.disk.shutil.disk_usage", return_value=SimpleNamespace(total=1000, free=500)):
        assert (await guard.check_and_latch()).paused
        record = redis.set.await_args.args[1]
        assert token not in record
        redis.get.return_value = record
        probe.return_value = PhysicalCapacity("D:\\", 1000, 500)
        redis.eval.return_value = 0
        with pytest.raises(RuntimeError, match="changed"):
            await guard.resume()
        redis.eval.return_value = 1
        assert not (await guard.resume()).paused
    redis.delete.assert_not_awaited()
    assert redis.eval.await_args.args[1:] == (1, "pause", record)


@pytest.mark.parametrize("error", [OSError("synthetic private failure"), TimeoutError()])
async def test_physical_capacity_failure_latches_even_with_virtual_space(tmp_path, error):
    redis = AsyncMock()
    redis.get.return_value = None
    guard = DownloadSpaceGuard(
        redis,
        path=str(tmp_path),
        pause_key="pause",
        min_free_bytes=100,
        min_free_percent=10,
        physical_probe=AsyncMock(side_effect=error),
    )
    with patch("pixav.shared.disk.shutil.disk_usage", return_value=SimpleNamespace(total=1000, free=900)):
        status = await guard.check_and_latch()
        assert status.paused and status.below_threshold
        assert "private" not in status.reason
        with pytest.raises(RuntimeError):
            await guard.resume()
    redis.delete.assert_not_awaited()


async def test_status_is_read_only_and_does_not_clear_existing_pause(tmp_path):
    redis = AsyncMock()
    redis.get.return_value = '{"paused":true}'
    guard = DownloadSpaceGuard(redis, path=str(tmp_path), pause_key="pause", min_free_bytes=0, min_free_percent=0)
    assert (await guard.status()).paused
    redis.set.assert_not_awaited()
    redis.eval.assert_not_awaited()
    redis.delete.assert_not_awaited()


@pytest.mark.parametrize("defect", ["mode", "symlink", "short"])
def test_pause_token_file_refuses_unsafe_reference(tmp_path, defect):
    from pixav.shared.disk import read_pause_token

    path = tmp_path / "token"
    path.write_text("synthetic-persistent-owner-token-123456789")
    path.chmod(0o600)
    if defect == "mode":
        path.chmod(0o644)
    elif defect == "symlink":
        link = tmp_path / "link"
        link.symlink_to(path)
        path = link
    else:
        path.write_text("short")
    with pytest.raises((OSError, ValueError)):
        read_pause_token(str(path))


def test_windows_guard_cannot_silently_omit_physical_probe():
    from pixav.config import Settings
    from pixav.shared.disk import build_download_space_guard

    with pytest.raises(ValueError, match="persistent pause"):
        build_download_space_guard(Settings(pixel_injector_mode="windows_android"), AsyncMock())
