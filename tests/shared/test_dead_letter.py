from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from pixav.shared.dead_letter import DeadLetterStore


class _Pipeline:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def __getattr__(self, name: str):
        def _record(*args, **_kwargs):
            self.calls.append((name, args))
            return self

        return _record

    async def execute(self) -> list[int]:
        return [1 for _ in self.calls]


async def test_dlq_upserts_by_task_id_and_sets_ttl() -> None:
    redis = MagicMock()
    pipelines: list[_Pipeline] = []

    def _pipeline(*, transaction: bool) -> _Pipeline:
        assert transaction is True
        pipe = _Pipeline()
        pipelines.append(pipe)
        return pipe

    redis.pipeline.side_effect = _pipeline
    redis.zcard = AsyncMock(return_value=1)
    redis.zrangebyscore = AsyncMock(return_value=[])
    store = DeadLetterStore(redis, retention_days=30, max_items_per_stage=1000)

    await store.put("upload", {"task_id": "same-task", "error_message": "first"})
    await store.put("upload", {"task_id": "same-task", "error_message": "latest"})

    hset_calls = [call for pipe in pipelines for call in pipe.calls if call[0] == "hset"]
    assert len(hset_calls) == 2
    assert all(call[1][1] == "same-task" for call in hset_calls)
    assert any(call[0] == "expire" and call[1][1] == 30 * 86400 for call in pipelines[0].calls)


async def test_dlq_trims_oldest_over_limit() -> None:
    redis = MagicMock()
    pipelines: list[_Pipeline] = []
    redis.pipeline.side_effect = lambda **_kwargs: pipelines.append(_Pipeline()) or pipelines[-1]
    redis.zcard = AsyncMock(side_effect=[3, 2])
    redis.zrange = AsyncMock(return_value=["old-task"])
    redis.zrangebyscore = AsyncMock(return_value=[])
    store = DeadLetterStore(redis, max_items_per_stage=2)

    depth = await store.put("download", {"task_id": "new-task"})

    assert depth == 2
    assert any(call[0] == "hdel" and call[1][1] == "old-task" for call in pipelines[-1].calls)


async def test_dlq_remove_drops_both_index_and_payload() -> None:
    redis = MagicMock()
    pipelines: list[_Pipeline] = []
    redis.pipeline.side_effect = lambda **_kwargs: pipelines.append(_Pipeline()) or pipelines[-1]
    redis.zcard = AsyncMock(return_value=0)
    store = DeadLetterStore(redis)

    removed = await store.remove("upload", "task-1")

    assert removed is True
    assert ("hdel", ("pixav:dlq:upload:items", "task-1")) in pipelines[-1].calls
    assert ("zrem", ("pixav:dlq:upload:index", "task-1")) in pipelines[-1].calls


async def test_dlq_remove_returns_false_when_absent() -> None:
    redis = MagicMock()

    class _EmptyPipeline(_Pipeline):
        async def execute(self) -> list[int]:
            return [0 for _ in self.calls]

    redis.pipeline.side_effect = lambda **_kwargs: _EmptyPipeline()
    redis.zcard = AsyncMock(return_value=0)

    assert await DeadLetterStore(redis).remove("upload", "missing-task") is False


async def test_dlq_list_returns_newest_first_and_skips_corrupt_payloads() -> None:
    redis = MagicMock()
    redis.zrevrange = AsyncMock(return_value=["newest", "corrupt", "oldest"])
    redis.zrangebyscore = AsyncMock(return_value=[])
    redis.hmget = AsyncMock(
        return_value=[
            '{"task_id": "newest"}',
            "not-json",
            '{"task_id": "oldest"}',
        ]
    )
    store = DeadLetterStore(redis)

    items = await store.list("upload", limit=10)

    assert [item["task_id"] for item in items] == ["newest", "oldest"]
    redis.zrevrange.assert_awaited_once_with("pixav:dlq:upload:index", 0, 9)


async def test_dlq_list_returns_empty_when_index_is_empty() -> None:
    redis = MagicMock()
    redis.zrangebyscore = AsyncMock(return_value=[])
    redis.zrevrange = AsyncMock(return_value=[])
    redis.hmget = AsyncMock()

    assert await DeadLetterStore(redis).list("download") == []
    redis.hmget.assert_not_awaited()


async def test_dlq_put_requires_task_id() -> None:
    with pytest.raises(ValueError, match="task_id"):
        await DeadLetterStore(MagicMock()).put("upload", {"error_message": "no id"})


async def test_dlq_prunes_entries_older_than_retention_even_when_stage_is_active() -> None:
    redis = MagicMock()
    pipelines: list[_Pipeline] = []
    redis.pipeline.side_effect = lambda **_kwargs: pipelines.append(_Pipeline()) or pipelines[-1]
    redis.zrangebyscore = AsyncMock(return_value=["expired-task"])
    redis.zcard = AsyncMock(return_value=1)
    store = DeadLetterStore(redis, retention_days=30)

    await store.put("upload", {"task_id": "new-task"})

    redis.zrangebyscore.assert_awaited_once()
    assert any(call[0] == "zrem" and call[1][1] == "expired-task" for call in pipelines[-1].calls)
    assert any(call[0] == "hdel" and call[1][1] == "expired-task" for call in pipelines[-1].calls)
