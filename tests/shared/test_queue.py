"""Tests for the Redis-backed TaskQueue."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from pixav.shared.queue import TaskQueue


@pytest.fixture()
def mock_redis() -> AsyncMock:
    return AsyncMock()


@pytest.fixture()
def queue(mock_redis: AsyncMock) -> TaskQueue:
    return TaskQueue(redis=mock_redis, queue_name="pixav:test")


class TestTaskQueue:
    async def test_push_serializes_and_rpushes(self, queue: TaskQueue, mock_redis: AsyncMock) -> None:
        mock_redis.rpush.return_value = 1
        payload = {"task_id": "abc", "action": "upload"}

        length = await queue.push(payload)

        mock_redis.rpush.assert_awaited_once_with("pixav:test", json.dumps(payload))
        assert length == 1

    async def test_pop_returns_parsed_payload(self, queue: TaskQueue, mock_redis: AsyncMock) -> None:
        expected = {"task_id": "abc"}
        mock_redis.blpop.return_value = ("pixav:test", json.dumps(expected))

        result = await queue.pop(timeout=5)

        mock_redis.blpop.assert_awaited_once_with(["pixav:test"], timeout=5)
        assert result == expected

    async def test_pop_returns_none_on_timeout(self, queue: TaskQueue, mock_redis: AsyncMock) -> None:
        mock_redis.blpop.return_value = None

        result = await queue.pop(timeout=1)

        assert result is None

    async def test_length_returns_llen(self, queue: TaskQueue, mock_redis: AsyncMock) -> None:
        mock_redis.llen.return_value = 42

        result = await queue.length()

        mock_redis.llen.assert_awaited_once_with("pixav:test")
        assert result == 42

    def test_name_property(self, queue: TaskQueue) -> None:
        assert queue.name == "pixav:test"
