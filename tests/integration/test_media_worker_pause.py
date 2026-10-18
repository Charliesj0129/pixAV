"""Managed media queue gates with real Redis and synthetic activities only."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pixav.config import Settings
from pixav.media_loader.worker import _run_managed_media
from pixav.shared.queue import TaskQueue

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("pause_source", ["system", "disk", "during-capacity"])
async def test_paused_media_preserves_exact_queue_bytes(integration_redis, monkeypatch, pause_source):
    redis, key = integration_redis
    settings = Settings(system_pause_key=f"{key}:unrelated")
    queue = TaskQueue(redis, key)
    pending = [json.dumps({"synthetic_sequence": 2}).encode()]
    inflight = [json.dumps({"synthetic_sequence": value}).encode() for value in [0, 1]]
    await redis.rpush(key, *pending)
    await redis.rpush(queue.processing_name, *inflight)
    if pause_source == "system":
        await redis.set(settings.system_pause_key, '{"paused":true}')
    observed = asyncio.Event()
    read = redis.get
    count = 0

    async def read_pause(selected_key):
        nonlocal count
        result = await read(selected_key)
        count += 1
        if count >= (3 if pause_source == "during-capacity" else 2):
            observed.set()
        return result

    async def capacity():
        if pause_source == "during-capacity":
            await redis.set(settings.system_pause_key, '{"paused":true}')
        return SimpleNamespace(paused=pause_source == "disk")

    monkeypatch.setattr(redis, "get", read_pause)
    worker = AsyncMock()
    disk = AsyncMock()
    disk.check_and_latch.side_effect = capacity
    task = asyncio.create_task(_run_managed_media(worker, queue, redis, settings, disk))
    try:
        await asyncio.wait_for(observed.wait(), timeout=3)
        await asyncio.sleep(0.05)
        assert not task.done()
        assert await redis.lrange(key, 0, -1) == pending
        assert await redis.lrange(queue.processing_name, 0, -1) == inflight
        worker.run_one.assert_not_awaited()
        if pause_source == "system":
            disk.check_and_latch.assert_not_awaited()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_two_consumers_recover_fifo_once_without_recovering_their_sibling(integration_redis, monkeypatch):
    redis, key = integration_redis
    queue = TaskQueue(redis, key)
    settings = Settings(system_pause_key=f"{key}:unrelated")
    pending = json.dumps({"synthetic_sequence": 2}).encode()
    inflight = [json.dumps({"synthetic_sequence": value}).encode() for value in [0, 1]]
    await redis.rpush(key, pending)
    await redis.rpush(queue.processing_name, *inflight)
    recovered = 0
    recover = queue.requeue_inflight

    async def observe_recovery():
        nonlocal recovered
        recovered += 1
        return await recover()

    monkeypatch.setattr(queue, "requeue_inflight", observe_recovery)
    claimed = []
    both_claimed = asyncio.Event()
    hold = asyncio.Event()
    cancelled = []

    class HoldActivity:
        async def run_one(self, queue, *, download_paused):
            assert not download_paused
            payload = await queue.pop_claim(timeout=1)
            assert payload is not None
            claimed.append(payload[0])
            if len(claimed) == 2:
                both_claimed.set()
            try:
                await hold.wait()
            finally:
                cancelled.append(True)

    disk = AsyncMock()
    disk.check_and_latch.return_value = SimpleNamespace(paused=False)
    task = asyncio.create_task(_run_managed_media(HoldActivity(), queue, redis, settings, disk))
    try:
        await asyncio.wait_for(both_claimed.wait(), timeout=3)
        assert recovered == 1
        assert sorted(row["synthetic_sequence"] for row in claimed) == [0, 1]
        assert await redis.lrange(key, 0, -1) == [pending]
        assert await redis.lrange(queue.processing_name, 0, -1) == inflight
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert len(cancelled) == 2
    assert await redis.lrange(queue.processing_name, 0, -1) == inflight


async def test_pause_after_recovery_blocks_both_consumers_before_claim(integration_redis, monkeypatch):
    redis, key = integration_redis
    queue = TaskQueue(redis, key)
    settings = Settings(system_pause_key=f"{key}:unrelated")
    inflight = [json.dumps({"synthetic_sequence": value}).encode() for value in [0, 1]]
    await redis.rpush(queue.processing_name, *inflight)
    recovered = asyncio.Event()
    recover = queue.requeue_inflight

    async def recover_then_pause():
        result = await recover()
        await redis.set(settings.system_pause_key, '{"paused":true}')
        recovered.set()
        return result

    monkeypatch.setattr(queue, "requeue_inflight", recover_then_pause)
    worker = AsyncMock()
    disk = AsyncMock()
    disk.check_and_latch.return_value = SimpleNamespace(paused=False)
    task = asyncio.create_task(_run_managed_media(worker, queue, redis, settings, disk))
    try:
        await asyncio.wait_for(recovered.wait(), timeout=3)
        await asyncio.sleep(0.05)
        worker.run_one.assert_not_awaited()
        assert await redis.lrange(key, 0, -1) == inflight
        assert await redis.lrange(queue.processing_name, 0, -1) == []
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
