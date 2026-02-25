"""Redis-backed task queue with optional durable claim/ack semantics."""

from __future__ import annotations

import json
import logging
from typing import Any, cast

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)


class TaskQueue:
    """Simple Redis list-backed queue for inter-module communication."""

    def __init__(self, redis: aioredis.Redis, queue_name: str) -> None:
        self._redis = redis
        self._queue_name = queue_name

    @property
    def name(self) -> str:
        return self._queue_name

    @property
    def processing_name(self) -> str:
        """Name of the in-flight processing list for durable consumers."""
        return f"{self._queue_name}:processing"

    async def push(self, payload: dict[str, Any]) -> int:
        """Append a JSON-encoded payload to the queue. Returns new queue length."""
        raw = json.dumps(payload)
        length = cast(int, await cast(Any, self._redis).rpush(self._queue_name, raw))
        logger.debug("pushed to %s (len=%d)", self._queue_name, length)
        return length

    async def pop_claim(self, timeout: int = 0) -> tuple[dict[str, Any], str] | None:
        """Durably claim one payload using BRPOPLPUSH.

        The message is atomically moved from ``name`` to ``processing_name`` and
        must be completed with ``ack()`` or ``nack()`` by the consumer.
        """
        raw = cast(
            str | bytes | None,
            await cast(Any, self._redis).brpoplpush(
                self._queue_name,
                self.processing_name,
                timeout=timeout,
            ),
        )
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            payload: dict[str, Any] = json.loads(raw)
        except json.JSONDecodeError:
            # Malformed payloads should not poison durable workers forever.
            await self.ack(raw)
            raise
        return payload, raw

    async def ack(self, receipt: str) -> bool:
        """Acknowledge a claimed payload by removing it from processing list."""
        removed = cast(int, await cast(Any, self._redis).lrem(self.processing_name, 1, receipt))
        return removed > 0

    async def nack(self, receipt: str, *, requeue: bool = True, front: bool = False) -> bool:
        """Reject a claimed payload and optionally requeue it."""
        removed = cast(int, await cast(Any, self._redis).lrem(self.processing_name, 1, receipt))
        if removed <= 0:
            return False
        if requeue:
            if front:
                await cast(Any, self._redis).lpush(self._queue_name, receipt)
            else:
                await cast(Any, self._redis).rpush(self._queue_name, receipt)
        return True

    async def requeue_inflight(self, max_items: int = 500) -> int:
        """Move stuck in-flight payloads back to the main queue."""
        moved = 0
        for _ in range(max_items):
            raw = cast(
                str | bytes | None,
                await cast(Any, self._redis).rpoplpush(self.processing_name, self._queue_name),
            )
            if raw is None:
                break
            moved += 1
        return moved

    async def pop(self, timeout: int = 0) -> dict[str, Any] | None:
        """Legacy destructive pop. Prefer ``pop_claim`` for durable workers."""
        result = cast(
            tuple[str | bytes, str | bytes] | None,
            await cast(Any, self._redis).blpop([self._queue_name], timeout=timeout),
        )
        if result is None:
            return None
        _key, raw = result
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        payload: dict[str, Any] = json.loads(raw)
        return payload

    async def length(self) -> int:
        """Return the current depth of the queue."""
        n = cast(int, await cast(Any, self._redis).llen(self._queue_name))
        return n
