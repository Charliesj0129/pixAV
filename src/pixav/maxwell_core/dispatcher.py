"""Redis-based task dispatching."""

from __future__ import annotations

import logging
import uuid

from pixav.shared.queue import TaskQueue
from pixav.shared.repository import TaskRepository

logger = logging.getLogger(__name__)


class RedisTaskDispatcher:
    """Dispatch tasks to the appropriate Redis queue.

    Implements the ``TaskDispatcher`` protocol.

    Looks up the task in DB, determines the target queue based on state,
    serialises the payload, and pushes to the queue.
    """

    def __init__(
        self,
        *,
        task_repo: TaskRepository,
        queues: dict[str, TaskQueue],
    ) -> None:
        self._task_repo = task_repo
        self._queues = queues  # queue_name -> TaskQueue

    async def dispatch(self, task_id: str, queue_name: str) -> None:
        """Dispatch a task to the specified Redis queue.

        Args:
            task_id: UUID string of the task.
            queue_name: Target queue name (e.g. ``pixav:download``).

        Raises:
            ValueError: If the queue_name is not registered.
        """
        queue = self._queues.get(queue_name)
        if queue is None:
            raise ValueError(f"unknown queue: {queue_name}. Known: {list(self._queues.keys())}")

        payload: dict[str, str | int] = {
            "task_id": task_id,
            "queue_name": queue_name,
        }

        # Optionally enrich with video_id from DB
        task = await self._task_repo.find_by_id(uuid.UUID(task_id))
        if task is not None:
            payload["video_id"] = str(task.video_id)
            payload["retries"] = task.retries
            payload["max_retries"] = task.max_retries
            if task.account_id is not None:
                payload["account_id"] = str(task.account_id)

        await queue.push(payload)
        logger.info("dispatched task %s → %s", task_id, queue_name)

    async def dispatch_batch(self, task_ids: list[str], queue_name: str) -> int:
        """Dispatch multiple tasks to the same queue.

        Args:
            task_ids: List of task UUID strings.
            queue_name: Target queue name.

        Returns:
            Number of tasks successfully dispatched.
        """
        dispatched = 0
        for tid in task_ids:
            try:
                await self.dispatch(tid, queue_name)
                dispatched += 1
            except Exception as exc:
                logger.warning("failed to dispatch %s: %s", tid, exc)
        return dispatched
