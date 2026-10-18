"""A real Redis latch preserves ownership through both write races."""

from types import SimpleNamespace
from unittest.mock import patch

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
