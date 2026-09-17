"""Bounded, task-deduplicated Redis dead-letter index."""

from __future__ import annotations

import json
import time
from typing import Any, cast

import redis.asyncio as aioredis

from pixav.shared.metrics import record_dlq_terminal, set_dlq_depth


class DeadLetterStore:
    """Keep the latest terminal failure per task for a bounded period.

    PostgreSQL ``tasks`` remains the permanent record. Redis is an operational
    index: a hash provides task-id upserts and a sorted set provides ordering
    and deterministic trimming.
    """

    def __init__(
        self,
        redis: aioredis.Redis,
        *,
        namespace: str = "pixav:dlq",
        retention_days: int = 30,
        max_items_per_stage: int = 1000,
    ) -> None:
        self._redis = redis
        self._namespace = namespace.rstrip(":")
        self._ttl_seconds = max(1, retention_days) * 86400
        self._max_items = max(1, max_items_per_stage)

    def _items_key(self, stage: str) -> str:
        return f"{self._namespace}:{stage}:items"

    def _index_key(self, stage: str) -> str:
        return f"{self._namespace}:{stage}:index"

    async def _remove_ids(self, stage: str, task_ids: list[str]) -> None:
        if not task_ids:
            return
        client = cast(Any, self._redis)
        trim = client.pipeline(transaction=True)
        trim.zrem(self._index_key(stage), *task_ids)
        trim.hdel(self._items_key(stage), *task_ids)
        await trim.execute()

    async def _prune_expired(self, stage: str, *, now: float | None = None) -> None:
        """Enforce retention per task even when the stage remains active."""
        client = cast(Any, self._redis)
        cutoff = (time.time() if now is None else now) - self._ttl_seconds
        stale = list(await client.zrangebyscore(self._index_key(stage), "-inf", cutoff))
        await self._remove_ids(stage, stale)

    async def put(self, stage: str, payload: dict[str, Any]) -> int:
        task_id = str(payload.get("task_id", "")).strip()
        if not task_id:
            raise ValueError("DLQ payload requires task_id")
        normalized = dict(payload)
        normalized["stage"] = stage
        normalized.setdefault("failed_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        items_key = self._items_key(stage)
        index_key = self._index_key(stage)
        client = cast(Any, self._redis)
        pipe = client.pipeline(transaction=True)
        now = time.time()
        pipe.hset(items_key, task_id, json.dumps(normalized, sort_keys=True))
        pipe.zadd(index_key, {task_id: now})
        pipe.expire(items_key, self._ttl_seconds)
        pipe.expire(index_key, self._ttl_seconds)
        await pipe.execute()

        await self._prune_expired(stage, now=now)
        depth = int(await client.zcard(index_key))
        overflow = depth - self._max_items
        if overflow > 0:
            stale = await client.zrange(index_key, 0, overflow - 1)
            await self._remove_ids(stage, list(stale))
            depth = int(await client.zcard(index_key))
        set_dlq_depth(stage, depth)
        record_dlq_terminal(stage)
        return depth

    async def remove(self, stage: str, task_id: str) -> bool:
        client = cast(Any, self._redis)
        pipe = client.pipeline(transaction=True)
        pipe.hdel(self._items_key(stage), task_id)
        pipe.zrem(self._index_key(stage), task_id)
        removed = await pipe.execute()
        depth = int(await client.zcard(self._index_key(stage)))
        set_dlq_depth(stage, depth)
        return bool(sum(int(value) for value in removed[:2]))

    async def list(self, stage: str, *, limit: int = 1000) -> list[dict[str, Any]]:
        client = cast(Any, self._redis)
        await self._prune_expired(stage)
        ids = await client.zrevrange(self._index_key(stage), 0, max(0, limit - 1))
        if not ids:
            return []
        raw_items = await client.hmget(self._items_key(stage), ids)
        result: list[dict[str, Any]] = []
        for raw in raw_items:
            if not raw:
                continue
            try:
                value = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(value, dict):
                result.append(value)
        return result
