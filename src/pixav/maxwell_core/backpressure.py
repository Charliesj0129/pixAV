"""Queue depth monitoring for backpressure control."""

from __future__ import annotations

import logging

from pixav.shared.queue import TaskQueue

logger = logging.getLogger(__name__)

# Default thresholds
_DEFAULT_WARN = 50
_DEFAULT_CRITICAL = 100


class QueueDepthMonitor:
    """Monitor queue depths and enforce backpressure.

    Implements the ``BackpressureMonitor`` protocol.

    Returns ``True`` (OK to dispatch) when queue depth is below the
    critical threshold, ``False`` when backpressured.
    """

    def __init__(
        self,
        *,
        queues: dict[str, TaskQueue],
        warn_threshold: int = _DEFAULT_WARN,
        critical_threshold: int = _DEFAULT_CRITICAL,
    ) -> None:
        self._queues = queues
        self._warn = warn_threshold
        self._critical = critical_threshold

    async def check_pressure(self, queue_name: str) -> bool:
        """Check if a queue is under acceptable pressure.

        Args:
            queue_name: Name of the queue to check.

        Returns:
            True if queue depth < critical threshold (safe to dispatch).
            False if the queue is backpressured.
        """
        queue = self._queues.get(queue_name)
        if queue is None:
            logger.warning("unknown queue %s, assuming OK", queue_name)
            return True

        depth = await queue.length()

        if depth >= self._critical:
            logger.warning("queue %s backpressured: depth=%d (critical=%d)", queue_name, depth, self._critical)
            return False

        if depth >= self._warn:
            logger.info("queue %s elevated: depth=%d (warn=%d)", queue_name, depth, self._warn)

        return True

    async def all_pressures(self) -> dict[str, dict[str, int | bool]]:
        """Return pressure status for all monitored queues.

        Returns:
            Dict mapping queue_name -> {depth, ok, warn, critical}.
        """
        result: dict[str, dict[str, int | bool]] = {}
        for name, queue in self._queues.items():
            depth = await queue.length()
            result[name] = {
                "depth": depth,
                "ok": depth < self._critical,
                "warn": depth >= self._warn,
                "critical": depth >= self._critical,
            }
        return result
