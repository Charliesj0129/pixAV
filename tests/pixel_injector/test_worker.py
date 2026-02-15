"""Tests for pixel_injector worker persistence behavior."""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, patch

from pixav.pixel_injector.worker import run_worker
from pixav.shared.enums import TaskState
from pixav.shared.models import Task


class TestPixelInjectorWorker:
    async def test_run_worker_persists_success(self) -> None:
        video_id = uuid.uuid4()
        task_id = uuid.uuid4()
        queue = AsyncMock()
        queue.name = "pixav:upload"
        queue.pop.side_effect = [
            {
                "task_id": str(task_id),
                "video_id": str(video_id),
                "local_path": "/tmp/video.mp4",
                "queue_name": "pixav:upload",
            },
            None,
        ]

        stop_event = asyncio.Event()
        service = AsyncMock()

        async def _process(task: Task) -> Task:
            stop_event.set()
            return task.model_copy(
                update={
                    "state": TaskState.COMPLETE,
                    "share_url": "https://photos.app.goo.gl/abc123",
                }
            )

        service.process_task.side_effect = _process
        task_repo = AsyncMock()
        video_repo = AsyncMock()

        await run_worker(
            queue=queue,
            service=service,
            task_repo=task_repo,
            video_repo=video_repo,
            poll_timeout=0,
            stop_event=stop_event,
        )

        task_repo.update_state.assert_any_await(task_id, TaskState.UPLOADING)
        task_repo.update_state.assert_any_await(task_id, TaskState.COMPLETE)
        video_repo.update_upload_result.assert_awaited_once_with(
            video_id,
            share_url="https://photos.app.goo.gl/abc123",
        )

    async def test_run_worker_persists_failure(self) -> None:
        video_id = uuid.uuid4()
        task_id = uuid.uuid4()
        queue = AsyncMock()
        queue.name = "pixav:upload"
        queue.pop.side_effect = [
            {
                "task_id": str(task_id),
                "video_id": str(video_id),
                "local_path": "/tmp/video.mp4",
                "queue_name": "pixav:upload",
                "max_retries": 0,
            },
            None,
        ]

        stop_event = asyncio.Event()
        service = AsyncMock()

        async def _process(task: Task) -> Task:
            stop_event.set()
            return task.model_copy(
                update={
                    "state": TaskState.FAILED,
                    "error_message": "adb failed",
                }
            )

        service.process_task.side_effect = _process
        task_repo = AsyncMock()
        video_repo = AsyncMock()

        await run_worker(
            queue=queue,
            service=service,
            task_repo=task_repo,
            video_repo=video_repo,
            poll_timeout=0,
            stop_event=stop_event,
        )

        task_repo.update_state.assert_any_await(task_id, TaskState.UPLOADING)
        task_repo.update_state.assert_any_await(task_id, TaskState.FAILED, error_message="adb failed")
        video_repo.update_status.assert_any_await(video_id, "uploading")
        video_repo.update_status.assert_any_await(video_id, "failed")

    async def test_run_worker_respects_pause_key(self) -> None:
        queue = AsyncMock()
        queue.name = "pixav:upload"

        stop_event = asyncio.Event()
        service = AsyncMock()
        redis_client = AsyncMock()
        redis_client.get.return_value = "1"

        async def _stop_soon() -> None:
            await asyncio.sleep(0.05)
            stop_event.set()

        stopper = asyncio.create_task(_stop_soon())
        try:
            await run_worker(
                queue=queue,
                service=service,
                redis_client=redis_client,
                pause_key="system:pause",
                poll_timeout=0,
                stop_event=stop_event,
            )
        finally:
            await stopper

        queue.pop.assert_not_awaited()
        service.process_task.assert_not_awaited()

    async def test_run_worker_requeues_when_lock_busy(self) -> None:
        video_id = uuid.uuid4()
        task_id = uuid.uuid4()
        queue = AsyncMock()
        queue.name = "pixav:upload"
        queue.pop.side_effect = [
            {
                "task_id": str(task_id),
                "video_id": str(video_id),
                "local_path": "/tmp/video.mp4",
                "queue_name": "pixav:upload",
            },
            None,
        ]

        stop_event = asyncio.Event()
        service = AsyncMock()
        redis_client = AsyncMock()
        redis_client.get.return_value = None
        redis_client.set.return_value = False

        async def _push(payload: dict[str, str]) -> None:
            stop_event.set()
            return None

        queue.push.side_effect = _push

        await run_worker(
            queue=queue,
            service=service,
            redis_client=redis_client,
            poll_timeout=0,
            stop_event=stop_event,
        )

        queue.push.assert_awaited()
        service.process_task.assert_not_awaited()

    async def test_run_worker_updates_account_usage_on_success(self) -> None:
        video_id = uuid.uuid4()
        task_id = uuid.uuid4()
        account_id = uuid.uuid4()
        queue = AsyncMock()
        queue.name = "pixav:upload"
        queue.pop.side_effect = [
            {
                "task_id": str(task_id),
                "video_id": str(video_id),
                "account_id": str(account_id),
                "local_path": "/tmp/video.mp4",
                "queue_name": "pixav:upload",
            },
            None,
        ]

        stop_event = asyncio.Event()
        service = AsyncMock()

        async def _process(task: Task) -> Task:
            stop_event.set()
            return task.model_copy(
                update={
                    "state": TaskState.COMPLETE,
                    "share_url": "https://photos.app.goo.gl/abc123",
                }
            )

        service.process_task.side_effect = _process
        task_repo = AsyncMock()
        video_repo = AsyncMock()
        account_repo = AsyncMock()

        with patch("os.path.getsize", return_value=987654):
            await run_worker(
                queue=queue,
                service=service,
                task_repo=task_repo,
                video_repo=video_repo,
                account_repo=account_repo,
                poll_timeout=0,
                stop_event=stop_event,
            )

        account_repo.apply_upload_usage.assert_awaited_once_with(account_id, 987654)
