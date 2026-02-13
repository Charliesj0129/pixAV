"""Tests for MaxwellOrchestrator."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from pixav.maxwell_core.orchestrator import MaxwellOrchestrator
from pixav.shared.enums import TaskState
from pixav.shared.models import Task


@pytest.fixture
def mock_scheduler() -> AsyncMock:
    s = AsyncMock()
    s.active_count.return_value = 3
    return s


@pytest.fixture
def mock_dispatcher() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def mock_monitor() -> AsyncMock:
    m = AsyncMock()
    m.check_pressure.return_value = True
    m.all_pressures.return_value = {"pixav:download": {"depth": 5, "ok": True, "warn": False, "critical": False}}
    return m


@pytest.fixture
def mock_cleaner() -> AsyncMock:
    c = AsyncMock()
    c.cleanup.return_value = 0
    c.cleanup_expired_videos.return_value = 0
    return c


@pytest.fixture
def mock_task_repo() -> AsyncMock:
    r = AsyncMock()
    r.count_by_state.return_value = 0
    r.list_pending.return_value = []
    return r


@pytest.fixture
def mock_video_repo() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def orchestrator(
    mock_scheduler: AsyncMock,
    mock_dispatcher: AsyncMock,
    mock_monitor: AsyncMock,
    mock_cleaner: AsyncMock,
    mock_task_repo: AsyncMock,
    mock_video_repo: AsyncMock,
) -> MaxwellOrchestrator:
    return MaxwellOrchestrator(
        scheduler=mock_scheduler,
        dispatcher=mock_dispatcher,
        monitor=mock_monitor,
        cleaner=mock_cleaner,
        task_repo=mock_task_repo,
        video_repo=mock_video_repo,
    )


class TestMaxwellOrchestrator:
    async def test_tick_no_pending(
        self,
        orchestrator: MaxwellOrchestrator,
        mock_cleaner: AsyncMock,
        mock_monitor: AsyncMock,
        mock_task_repo: AsyncMock,
    ) -> None:
        mock_task_repo.count_by_state.return_value = 0

        stats = await orchestrator.tick()

        assert stats["orphans_cleaned"] == 0
        mock_cleaner.cleanup.assert_awaited_once()
        mock_monitor.check_pressure.assert_awaited_once()

    async def test_tick_backpressured(
        self,
        orchestrator: MaxwellOrchestrator,
        mock_monitor: AsyncMock,
        mock_cleaner: AsyncMock,
    ) -> None:
        mock_monitor.check_pressure.return_value = False

        stats = await orchestrator.tick()

        assert stats["skipped_pressure"] == 1
        mock_cleaner.cleanup.assert_awaited_once()

    async def test_tick_with_orphans(
        self,
        orchestrator: MaxwellOrchestrator,
        mock_cleaner: AsyncMock,
    ) -> None:
        mock_cleaner.cleanup.return_value = 5

        stats = await orchestrator.tick()
        assert stats["orphans_cleaned"] == 5

    async def test_tick_dispatches_pending_tasks(
        self,
        orchestrator: MaxwellOrchestrator,
        mock_task_repo: AsyncMock,
        mock_dispatcher: AsyncMock,
    ) -> None:
        pending = Task(
            id=uuid.UUID("00000000-0000-0000-0000-000000000201"),
            video_id=uuid.UUID("00000000-0000-0000-0000-000000000202"),
            state=TaskState.PENDING,
            queue_name="pixav:download",
        )
        mock_task_repo.count_by_state.return_value = 1
        mock_task_repo.list_pending.return_value = [pending]

        stats = await orchestrator.tick()

        assert stats["dispatched"] == 1
        mock_dispatcher.dispatch.assert_awaited_once_with(str(pending.id), "pixav:download")
        mock_task_repo.update_state.assert_awaited_once_with(pending.id, TaskState.DOWNLOADING)

    async def test_run_gc(
        self,
        orchestrator: MaxwellOrchestrator,
        mock_cleaner: AsyncMock,
    ) -> None:
        mock_cleaner.cleanup.return_value = 2
        mock_cleaner.cleanup_expired_videos.return_value = 3

        result = await orchestrator.run_gc()

        assert result["orphans_cleaned"] == 2
        assert result["videos_expired"] == 3

    async def test_health(
        self,
        orchestrator: MaxwellOrchestrator,
        mock_scheduler: AsyncMock,
        mock_monitor: AsyncMock,
    ) -> None:
        result = await orchestrator.health()

        assert result["active_accounts"] == 3
        assert "pixav:download" in result["queues"]
