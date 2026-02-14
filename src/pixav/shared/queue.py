"""Redis-backed task queue using RPUSH / BLPOP / LLEN."""

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

    async def push(self, payload: dict[str, Any]) -> int:
        """Append a JSON-encoded payload to the queue. Returns new queue length."""
        raw = json.dumps(payload)
        length = cast(int, await cast(Any, self._redis).rpush(self._queue_name, raw))
        logger.debug("pushed to %s (len=%d)", self._queue_name, length)
        return length

    async def pop(self, timeout: int = 0) -> dict[str, Any] | None:
        """Blocking pop from the queue. Returns parsed payload or None on timeout."""
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
