"""Paused or capacity-latched managed workers leave activity queues untouched."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from pixav.config import Settings
from pixav.pixel_injector import playback_worker, storage_worker


@pytest.mark.parametrize("module", [storage_worker, playback_worker])
@pytest.mark.parametrize("system_paused,disk_paused", [(True, False), (False, True), (False, False)])
async def test_pause_prevents_recovery_and_new_activity_claims(monkeypatch, module, system_paused, disk_paused):
    redis = AsyncMock()
    # The third case models a pause arriving during the physical-capacity RPC.
    redis.get.side_effect = ['{"paused":true}'] if system_paused else [None, '{"paused":true}']
    queue = Mock(requeue_inflight=AsyncMock())
    worker = SimpleNamespace(run_one=AsyncMock(), uploader=None, materializer=None)
    guard = SimpleNamespace(check_and_latch=AsyncMock(return_value=SimpleNamespace(paused=disk_paused)))
    monkeypatch.setattr(module, "require_workflow_role", AsyncMock())
    monkeypatch.setattr(module, "build_worker", lambda *args, **kwargs: worker)
    monkeypatch.setattr(module, "TaskQueue", lambda **kwargs: queue)
    monkeypatch.setattr(module, "build_download_space_guard", lambda *args: guard, raising=False)

    async def stop_after_poll(seconds):
        raise module.asyncio.CancelledError

    monkeypatch.setattr(module.asyncio, "sleep", stop_after_poll)
    run = getattr(module, "run_storage_activity_worker", None) or module.run_playback_activity_worker
    kwargs = {"owner": "synthetic-owner"} if module is storage_worker else {}
    with pytest.raises(module.asyncio.CancelledError):
        await run(object(), redis, Settings(), **kwargs)
    queue.requeue_inflight.assert_not_awaited()
    worker.run_one.assert_not_awaited()
    if system_paused:
        guard.check_and_latch.assert_not_awaited()
    else:
        guard.check_and_latch.assert_awaited_once()


@pytest.mark.parametrize("module", [storage_worker, playback_worker])
async def test_first_unpaused_poll_recovers_fifo_once_before_claiming(monkeypatch, module):
    redis = AsyncMock()
    redis.get.side_effect = ['{"paused":true}', None, None, None, None]
    events = []

    async def recover():
        events.append("recover")

    async def claim(queue):
        events.append("claim")

    queue = Mock(requeue_inflight=AsyncMock(side_effect=recover))
    worker = SimpleNamespace(run_one=AsyncMock(side_effect=claim), uploader=None, materializer=None)
    guard = SimpleNamespace(check_and_latch=AsyncMock(return_value=SimpleNamespace(paused=False)))
    monkeypatch.setattr(module, "require_workflow_role", AsyncMock())
    monkeypatch.setattr(module, "build_worker", lambda *args, **kwargs: worker)
    monkeypatch.setattr(module, "TaskQueue", lambda **kwargs: queue)
    monkeypatch.setattr(module, "build_download_space_guard", lambda *args: guard, raising=False)
    polls = 0

    async def stop_after_three_polls(seconds):
        nonlocal polls
        polls += 1
        if polls == 3:
            raise module.asyncio.CancelledError

    monkeypatch.setattr(module.asyncio, "sleep", stop_after_three_polls)
    run = getattr(module, "run_storage_activity_worker", None) or module.run_playback_activity_worker
    kwargs = {"owner": "synthetic-owner"} if module is storage_worker else {}
    with pytest.raises(module.asyncio.CancelledError):
        await run(object(), redis, Settings(), **kwargs)
    assert events == ["recover", "claim", "claim"]
    assert guard.check_and_latch.await_count == 2
