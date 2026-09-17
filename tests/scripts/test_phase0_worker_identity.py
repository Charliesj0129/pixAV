"""Restart between preflight and worker construction must fail before mutation."""

from unittest.mock import AsyncMock, patch

import pytest

from pixav.config import Settings
from pixav.media_loader import worker as download
from pixav.pixel_injector import worker as upload


@pytest.mark.parametrize("module,runner", [(download, download.run_loop), (upload, upload.run_from_settings)])
async def test_worker_rejects_changed_redis_before_recovery(module, runner):
    pool, redis = AsyncMock(), AsyncMock()
    redis.info.return_value = {"run_id": "restarted"}
    with (
        patch.object(module, "create_pool", new=AsyncMock(return_value=pool)),
        patch.object(module, "create_redis", new=AsyncMock(return_value=redis)),
        patch.object(module, "TaskQueue") as queue,
    ):
        with pytest.raises(RuntimeError, match="Redis identity changed"):
            await runner(Settings(), max_tasks=1, expected_redis_identity="preflight")
    queue.assert_not_called()
    pool.close.assert_awaited_once()
    redis.aclose.assert_awaited_once()
