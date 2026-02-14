"""Tests for RedisTaskDispatcher."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from pixav.maxwell_core.dispatcher import RedisTaskDispatcher
from pixav.shared.enums import TaskState
from pixav.shared.models import Task


@pytest.fixture
def mock_task_repo() -> AsyncMock:
    repo = AsyncMock()
    repo.find_by_id.return_value = Task(
        id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        video_id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
        state=TaskState.PENDING,
        queue_name="pixav:download",
    )
    return repo


@pytest.fixture
def mock_queue() -> AsyncMock:
    q = AsyncMock()
    q.push.return_value = 1
    return q


@pytest.fixture
def dispatcher(mock_task_repo: AsyncMock, mock_queue: AsyncMock) -> RedisTaskDispatcher:
    return RedisTaskDispatcher(
        task_repo=mock_task_repo,
        queues={"pixav:download": mock_queue},
    )


class TestRedisTaskDispatcher:
    async def test_dispatch_success(
        self,
        dispatcher: RedisTaskDispatcher,
        mock_queue: AsyncMock,
        mock_task_repo: AsyncMock,
    ) -> None:
        tid = "00000000-0000-0000-0000-000000000001"
        await dispatcher.dispatch(tid, "pixav:download")

        mock_queue.push.assert_awaited_once()
        payload = mock_queue.push.call_args[0][0]
        assert payload["task_id"] == tid
        assert payload["video_id"] == "00000000-0000-0000-0000-000000000002"

    async def test_dispatch_unknown_queue_raises(self, dispatcher: RedisTaskDispatcher) -> None:
        with pytest.raises(ValueError, match="unknown queue"):
            await dispatcher.dispatch("some-id", "pixav:nonexistent")

    async def test_dispatch_task_not_in_db(
        self,
        dispatcher: RedisTaskDispatcher,
        mock_task_repo: AsyncMock,
        mock_queue: AsyncMock,
    ) -> None:
        mock_task_repo.find_by_id.return_value = None

        await dispatcher.dispatch("00000000-0000-0000-0000-000000000099", "pixav:download")

        # Still dispatches, just without video_id enrichment
        mock_queue.push.assert_awaited_once()
        payload = mock_queue.push.call_args[0][0]
        assert "video_id" not in payload

    async def test_dispatch_batch(
        self,
        dispatcher: RedisTaskDispatcher,
        mock_queue: AsyncMock,
    ) -> None:
        ids = [str(uuid.uuid4()) for _ in range(3)]
        count = await dispatcher.dispatch_batch(ids, "pixav:download")

        assert count == 3
        assert mock_queue.push.await_count == 3

    async def test_dispatch_batch_partial_failure(
        self,
        dispatcher: RedisTaskDispatcher,
        mock_queue: AsyncMock,
    ) -> None:
        mock_queue.push.side_effect = [1, Exception("fail"), 1]
        ids = [str(uuid.uuid4()) for _ in range(3)]
        count = await dispatcher.dispatch_batch(ids, "pixav:download")

        assert count == 2
