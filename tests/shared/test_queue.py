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

    async def test_pop_claim_moves_payload_to_processing(self, queue: TaskQueue, mock_redis: AsyncMock) -> None:
        expected = {"task_id": "abc"}
        raw = json.dumps(expected)
        mock_redis.blmove.return_value = raw

        claimed = await queue.pop_claim(timeout=7)

        assert claimed == (expected, raw)
        mock_redis.blmove.assert_awaited_once_with("pixav:test", "pixav:test:processing", 7, "LEFT", "RIGHT")

    async def test_ack_removes_from_processing(self, queue: TaskQueue, mock_redis: AsyncMock) -> None:
        mock_redis.lrem.return_value = 1

        ok = await queue.ack("receipt")

        assert ok is True
        mock_redis.lrem.assert_awaited_once_with("pixav:test:processing", 1, "receipt")

    async def test_nack_requeues_payload(self, queue: TaskQueue, mock_redis: AsyncMock) -> None:
        mock_redis.lrem.return_value = 1

        ok = await queue.nack("receipt", requeue=True)

        assert ok is True
        mock_redis.rpush.assert_awaited_once_with("pixav:test", "receipt")

    async def test_requeue_inflight_moves_items_back(self, queue: TaskQueue, mock_redis: AsyncMock) -> None:
        mock_redis.rpoplpush.side_effect = ["raw-1", "raw-2", None]

        moved = await queue.requeue_inflight(max_items=10)

        assert moved == 2

    async def test_length_returns_llen(self, queue: TaskQueue, mock_redis: AsyncMock) -> None:
        mock_redis.llen.return_value = 42

        result = await queue.length()

        mock_redis.llen.assert_awaited_once_with("pixav:test")
        assert result == 42

    async def test_processing_length_returns_processing_llen(self, queue: TaskQueue, mock_redis: AsyncMock) -> None:
        mock_redis.llen.return_value = 3

        result = await queue.processing_length()

        mock_redis.llen.assert_awaited_once_with("pixav:test:processing")
        assert result == 3

    async def test_total_depth_sums_queue_and_processing(self, queue: TaskQueue, mock_redis: AsyncMock) -> None:
        mock_redis.llen.side_effect = [4, 2]

        result = await queue.total_depth()

        assert result == 6

    def test_name_property(self, queue: TaskQueue) -> None:
        assert queue.name == "pixav:test"


class _FakeRedisLists:
    """Minimal in-memory Redis list stand-in for ordering assertions."""

    def __init__(self) -> None:
        self.lists: dict[str, list[str]] = {}

    async def rpush(self, key: str, value: str) -> int:
        bucket = self.lists.setdefault(key, [])
        bucket.append(value)
        return len(bucket)

    async def lpush(self, key: str, value: str) -> int:
        bucket = self.lists.setdefault(key, [])
        bucket.insert(0, value)
        return len(bucket)

    async def blmove(self, src: str, dst: str, timeout: int, src_side: str, dst_side: str) -> str | None:
        bucket = self.lists.get(src) or []
        if not bucket:
            return None
        value = bucket.pop(0) if src_side == "LEFT" else bucket.pop()
        target = self.lists.setdefault(dst, [])
        target.append(value) if dst_side == "RIGHT" else target.insert(0, value)
        return value

    async def rpoplpush(self, src: str, dst: str) -> str | None:
        bucket = self.lists.get(src) or []
        if not bucket:
            return None
        value = bucket.pop()
        self.lists.setdefault(dst, []).insert(0, value)
        return value

    async def lrem(self, key: str, count: int, value: str) -> int:
        bucket = self.lists.get(key) or []
        if value not in bucket:
            return 0
        bucket.remove(value)
        return 1


class TestTaskQueueOrdering:
    """Regression tests: the durable queue must be FIFO, not LIFO."""

    @pytest.fixture()
    def fifo_queue(self) -> TaskQueue:
        return TaskQueue(redis=_FakeRedisLists(), queue_name="pixav:fifo")  # type: ignore[arg-type]

    async def test_pop_claim_returns_oldest_payload_first(self, fifo_queue: TaskQueue) -> None:
        for seq in range(3):
            await fifo_queue.push({"seq": seq})

        seen = []
        for _ in range(3):
            claimed = await fifo_queue.pop_claim(timeout=1)
            assert claimed is not None
            payload, receipt = claimed
            seen.append(payload["seq"])
            await fifo_queue.ack(receipt)

        assert seen == [0, 1, 2]

    async def test_nack_front_is_claimed_before_queued_work(self, fifo_queue: TaskQueue) -> None:
        await fifo_queue.push({"seq": 0})
        await fifo_queue.push({"seq": 1})

        claimed = await fifo_queue.pop_claim(timeout=1)
        assert claimed is not None
        _payload, receipt = claimed
        assert await fifo_queue.nack(receipt, requeue=True, front=True) is True

        retried = await fifo_queue.pop_claim(timeout=1)
        assert retried is not None
        assert retried[0]["seq"] == 0

    async def test_nack_back_is_claimed_after_queued_work(self, fifo_queue: TaskQueue) -> None:
        await fifo_queue.push({"seq": 0})
        await fifo_queue.push({"seq": 1})

        claimed = await fifo_queue.pop_claim(timeout=1)
        assert claimed is not None
        _payload, receipt = claimed
        assert await fifo_queue.nack(receipt, requeue=True, front=False) is True

        following = await fifo_queue.pop_claim(timeout=1)
        assert following is not None
        assert following[0]["seq"] == 1

    async def test_requeue_inflight_restores_original_order(self, fifo_queue: TaskQueue) -> None:
        for seq in range(3):
            await fifo_queue.push({"seq": seq})
        for _ in range(2):
            assert await fifo_queue.pop_claim(timeout=1) is not None

        assert await fifo_queue.requeue_inflight(max_items=10) == 2

        seen = []
        for _ in range(3):
            claimed = await fifo_queue.pop_claim(timeout=1)
            assert claimed is not None
            seen.append(claimed[0]["seq"])

        assert seen == [0, 1, 2]
