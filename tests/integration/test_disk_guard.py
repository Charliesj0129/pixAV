"""A real Redis latch preserves ownership through both write races."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from pixav.shared.disk import DownloadSpaceGuard

pytestmark = pytest.mark.integration


async def test_disk_latch_never_overwrites_or_resumes_replacement(integration_redis, tmp_path):
    redis, key = integration_redis
    foreign = b'{"paused":true,"owner_sha256":"another-owner"}'

    async def raced_set(*args, **kwargs):
        await redis.set(key, foreign)
        return await redis.set(*args, **kwargs)

    guard = DownloadSpaceGuard(
        SimpleNamespace(get=redis.get, set=raced_set, eval=redis.eval),
        path=str(tmp_path),
        pause_key=key,
        min_free_bytes=100,
        min_free_percent=10,
        owner_token="synthetic-persistent-owner-token-123456789",
    )
    with patch("pixav.shared.disk.shutil.disk_usage", return_value=SimpleNamespace(total=1000, free=50)):
        assert (await guard.check_and_latch()).paused
    assert await redis.get(key) == foreign

    # Replace only this test's exact synthetic value, then create an owned latch.
    assert (
        await redis.eval(
            "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) else return 0 end",
            1,
            key,
            foreign,
        )
        == 1
    )
    guard._redis = redis
    with patch("pixav.shared.disk.shutil.disk_usage", return_value=SimpleNamespace(total=1000, free=50)):
        assert (await guard.check_and_latch()).paused

    async def raced_eval(*args):
        await redis.set(key, foreign)
        return await redis.eval(*args)

    guard._redis = SimpleNamespace(get=redis.get, eval=raced_eval)
    with patch("pixav.shared.disk.shutil.disk_usage", return_value=SimpleNamespace(total=1000, free=500)):
        with pytest.raises(RuntimeError, match="changed"):
            await guard.resume()
    assert await redis.get(key) == foreign


@pytest.mark.parametrize("kind", ["storage", "playback"])
async def test_paused_worker_preserves_real_fifo_and_inflight_bytes(integration_redis, monkeypatch, kind):
    from pixav.config import Settings
    from pixav.pixel_injector import playback_worker, storage_worker
    from pixav.shared.queue import TaskQueue

    redis, key = integration_redis
    module = storage_worker if kind == "storage" else playback_worker
    queue = TaskQueue(redis=redis, queue_name=key)
    pause = f"{key}:unrelated"
    await redis.set(pause, '{"paused":true,"owner_sha256":"synthetic-owner"}')
    await redis.rpush(key, b'{"ordinal":3}', b'{"ordinal":4}')
    await redis.rpush(queue.processing_name, b'{"ordinal":1}', b'{"ordinal":2}')
    before = [await redis.lrange(name, 0, -1) for name in [key, queue.processing_name]]
    worker = SimpleNamespace(run_one=AsyncMock(), uploader=None, materializer=None)
    guard = Mock(check_and_latch=AsyncMock(side_effect=AssertionError("paused worker must not probe")))
    monkeypatch.setattr(module, "require_workflow_role", AsyncMock())
    monkeypatch.setattr(module, "build_worker", lambda *args, **kwargs: worker)
    monkeypatch.setattr(module, "build_download_space_guard", lambda *args: guard)
    monkeypatch.setattr(module, "TaskQueue", lambda **kwargs: queue)

    async def stop_after_poll(seconds):
        raise module.asyncio.CancelledError

    monkeypatch.setattr(module.asyncio, "sleep", stop_after_poll)
    run = getattr(module, "run_storage_activity_worker", None) or module.run_playback_activity_worker
    kwargs = {"owner": "synthetic-owner"} if kind == "storage" else {}
    with pytest.raises(module.asyncio.CancelledError):
        await run(object(), redis, Settings(system_pause_key=pause), **kwargs)
    assert [await redis.lrange(name, 0, -1) for name in [key, queue.processing_name]] == before
    worker.run_one.assert_not_awaited()
