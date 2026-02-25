"""Tests for shared/health_server.py — worker + health server runner."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from pixav.shared.health import create_health_app
from pixav.shared.health_server import run_with_health


async def _long_running() -> None:
    """Simulate a long-running server coroutine."""
    await asyncio.sleep(60)


async def _instant_worker() -> None:
    """Worker that returns immediately."""


class TestRunWithHealth:
    async def test_cancels_server_when_worker_finishes(self) -> None:
        """When the worker coroutine finishes, the pending server task is cancelled."""
        mock_server = MagicMock()
        mock_server.serve = _long_running
        mock_config = MagicMock()

        health_app = create_health_app("test")

        with patch("pixav.shared.health_server.uvicorn.Server", return_value=mock_server):
            with patch("pixav.shared.health_server.uvicorn.Config", return_value=mock_config):
                # Should complete quickly: worker finishes instantly, server gets cancelled
                await run_with_health(
                    worker_coro=_instant_worker(),
                    health_app=health_app,
                    host="127.0.0.1",
                    port=19999,
                )

    async def test_raises_exception_from_worker(self) -> None:
        """If the worker raises, run_with_health should propagate the exception."""

        async def failing_worker() -> None:
            raise RuntimeError("worker crashed")

        mock_server = MagicMock()
        mock_server.serve = _long_running
        mock_config = MagicMock()

        health_app = create_health_app("test")

        with patch("pixav.shared.health_server.uvicorn.Server", return_value=mock_server):
            with patch("pixav.shared.health_server.uvicorn.Config", return_value=mock_config):
                with pytest.raises(RuntimeError, match="worker crashed"):
                    await run_with_health(
                        worker_coro=failing_worker(),
                        health_app=health_app,
                        host="127.0.0.1",
                        port=19998,
                    )
