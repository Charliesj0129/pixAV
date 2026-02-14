"""Tests for QueueDepthMonitor."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from pixav.maxwell_core.backpressure import QueueDepthMonitor


@pytest.fixture
def mock_queue() -> AsyncMock:
    q = AsyncMock()
    q.length.return_value = 0
    return q


@pytest.fixture
def monitor(mock_queue: AsyncMock) -> QueueDepthMonitor:
    return QueueDepthMonitor(
        queues={"pixav:download": mock_queue},
        warn_threshold=10,
        critical_threshold=20,
    )


class TestQueueDepthMonitor:
    async def test_low_depth_ok(self, monitor: QueueDepthMonitor, mock_queue: AsyncMock) -> None:
        mock_queue.length.return_value = 5
        assert await monitor.check_pressure("pixav:download") is True

    async def test_warn_level_still_ok(self, monitor: QueueDepthMonitor, mock_queue: AsyncMock) -> None:
        mock_queue.length.return_value = 15
        assert await monitor.check_pressure("pixav:download") is True

    async def test_critical_level_backpressured(self, monitor: QueueDepthMonitor, mock_queue: AsyncMock) -> None:
        mock_queue.length.return_value = 25
        assert await monitor.check_pressure("pixav:download") is False

    async def test_exactly_critical_threshold(self, monitor: QueueDepthMonitor, mock_queue: AsyncMock) -> None:
        mock_queue.length.return_value = 20
        assert await monitor.check_pressure("pixav:download") is False

    async def test_unknown_queue_returns_true(self, monitor: QueueDepthMonitor) -> None:
        # Unknown queue defaults to OK (safe fallback)
        assert await monitor.check_pressure("pixav:unknown") is True

    async def test_all_pressures(self, monitor: QueueDepthMonitor, mock_queue: AsyncMock) -> None:
        mock_queue.length.return_value = 15
        result = await monitor.all_pressures()

        assert "pixav:download" in result
        info = result["pixav:download"]
        assert info["depth"] == 15
        assert info["ok"] is True
        assert info["warn"] is True
        assert info["critical"] is False
