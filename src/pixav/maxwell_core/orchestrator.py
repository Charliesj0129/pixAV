"""Maxwell-Core orchestrator — the central pipeline coordinator."""

from __future__ import annotations

import logging

from pixav.maxwell_core.backpressure import QueueDepthMonitor
from pixav.maxwell_core.dispatcher import RedisTaskDispatcher
from pixav.maxwell_core.gc import OrphanTaskCleaner
from pixav.maxwell_core.scheduler import LruAccountScheduler
from pixav.shared.enums import TaskState
from pixav.shared.repository import TaskRepository, VideoRepository

logger = logging.getLogger(__name__)


class MaxwellOrchestrator:
    """Central pipeline coordinator.

    Ties together scheduling, dispatching, backpressure, and GC into a
    single tick-based loop that checks for pending work and dispatches it.
    """

    def __init__(
        self,
        *,
        scheduler: LruAccountScheduler,
        dispatcher: RedisTaskDispatcher,
        monitor: QueueDepthMonitor,
        cleaner: OrphanTaskCleaner,
        task_repo: TaskRepository,
        video_repo: VideoRepository,
        download_queue_name: str = "pixav:download",
        upload_queue_name: str = "pixav:upload",
        no_account_policy: str = "wait",
        batch_size: int = 5,
    ) -> None:
        self._scheduler = scheduler
        self._dispatcher = dispatcher
        self._monitor = monitor
        self._cleaner = cleaner
        self._task_repo = task_repo
        self._video_repo = video_repo
        self._download_q = download_queue_name
        self._upload_q = upload_queue_name
        self._no_account_policy = no_account_policy
        self._batch_size = batch_size

    async def tick(self) -> dict[str, int]:
        """Run one scheduling cycle.

        Returns:
            Summary dict with counts: dispatched, skipped, orphans_cleaned.
        """
        stats: dict[str, int] = {
            "dispatched": 0,
            "skipped_pressure": 0,
            "orphans_cleaned": 0,
            "waiting_no_account": 0,
            "failed_no_account": 0,
        }

        # 1. GC pass — clean orphaned tasks
        stats["orphans_cleaned"] = await self._cleaner.cleanup()

        # 2. Find pending tasks from DB (up to batch_size)
        pending_count = await self._task_repo.count_by_state(TaskState.PENDING)
        if pending_count == 0:
            logger.debug("no pending tasks")
            return stats

        # 3. Dispatch pending tasks
        pending_tasks = await self._task_repo.list_pending(self._batch_size)
        for task in pending_tasks:
            queue_name = task.queue_name or self._download_q
            next_state = TaskState.DOWNLOADING
            if queue_name == self._upload_q:
                next_state = TaskState.UPLOADING

            try:
                queue_ok = await self._monitor.check_pressure(queue_name)
                if not queue_ok:
                    stats["skipped_pressure"] += 1
                    continue

                # For upload tasks, assign an account via LRU scheduling
                account_id: str | None = None
                if queue_name == self._upload_q:
                    try:
                        account_id = await self._scheduler.next_account()
                        await self._task_repo.assign_account(task.id, account_id)
                    except RuntimeError as exc:
                        if self._no_account_policy == "fail":
                            await self._task_repo.update_state(task.id, TaskState.FAILED, error_message=str(exc))
                            logger.warning("no active accounts — task %s moved to failed", task.id)
                            stats["failed_no_account"] += 1
                        else:
                            logger.info("no active accounts — task %s remains pending", task.id)
                            stats["waiting_no_account"] += 1
                        continue

                await self._dispatcher.dispatch(str(task.id), queue_name)
                await self._task_repo.update_state(task.id, next_state)
                stats["dispatched"] += 1

                # Mark account as used after successful dispatch
                if account_id is not None:
                    await self._scheduler.mark_used(account_id)
                    logger.info("dispatched upload task %s → account %s", task.id, account_id)
            except Exception as exc:
                logger.warning("failed to dispatch task %s to %s: %s", task.id, queue_name, exc)

        logger.info("tick complete: %s", stats)
        return stats

    async def run_gc(self) -> dict[str, int]:
        """Run garbage collection independently.

        Returns:
            Summary dict with cleanup counts.
        """
        orphans = await self._cleaner.cleanup()
        expired = await self._cleaner.cleanup_expired_videos()
        return {"orphans_cleaned": orphans, "videos_expired": expired}

    async def health(self) -> dict[str, object]:
        """Return orchestrator health status.

        Returns:
            Dict with active accounts, queue pressures.
        """
        active = await self._scheduler.active_count()
        pressures = await self._monitor.all_pressures()
        return {
            "active_accounts": active,
            "queues": pressures,
        }
