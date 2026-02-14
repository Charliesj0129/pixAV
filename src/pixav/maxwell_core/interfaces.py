"""Interfaces for Maxwell-Core module."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class TaskScheduler(Protocol):
    """Protocol for task scheduling."""

    async def next_account(self) -> str:
        """Get the next account ID for task processing.

        Returns:
            Account ID to process next.
        """
        ...


@runtime_checkable
class TaskDispatcher(Protocol):
    """Protocol for task dispatching."""

    async def dispatch(self, task_id: str, queue_name: str) -> None:
        """Dispatch a task to a named queue.

        Args:
            task_id: Unique identifier for the task.
            queue_name: Name of the queue to dispatch to.
        """
        ...


@runtime_checkable
class BackpressureMonitor(Protocol):
    """Protocol for monitoring queue backpressure."""

    async def check_pressure(self, queue_name: str) -> bool:
        """Check if a queue is under acceptable pressure.

        Args:
            queue_name: Name of the queue to check.

        Returns:
            True if queue is under acceptable pressure (OK to dispatch), False otherwise.
        """
        ...
