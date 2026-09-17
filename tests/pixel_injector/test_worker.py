"""Tests for pixel_injector worker persistence behavior."""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from pixav.config import Settings
from pixav.pixel_injector.worker import _release_upload_lock, _renew_upload_lock, run_from_settings, run_worker
from pixav.shared.enums import TaskState
from pixav.shared.models import Task


def unmanaged_task_repo() -> AsyncMock:
    """A task repository for a video the managed authority has not admitted."""
    repo = AsyncMock()
    repo.is_managed_video.return_value = False
    return repo


class TestPixelInjectorWorker:
    async def test_run_worker_persists_success(self) -> None:
        video_id = uuid.uuid4()
        task_id = uuid.uuid4()
        queue = AsyncMock()
        queue.name = "pixav:upload"
        queue.pop_claim.side_effect = [
            (
                {
                    "task_id": str(task_id),
                    "video_id": str(video_id),
                    "local_path": "/tmp/video.mp4",
                    "queue_name": "pixav:upload",
                },
                "receipt-1",
            ),
            None,
        ]

        stop_event = asyncio.Event()
        service = AsyncMock()

        async def _process(task: Task, account=None) -> Task:
            stop_event.set()
            return task.model_copy(
                update={
                    "state": TaskState.COMPLETE,
                    "share_url": "https://photos.app.goo.gl/abc123",
                }
            )

        service.process_task.side_effect = _process
        task_repo = AsyncMock()
        task_repo.is_managed_video.return_value = False
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

    async def test_managed_video_is_refused_before_any_upload_bdd_019(self) -> None:
        """The legacy uploader stands down rather than race the authority."""
        video_id = uuid.uuid4()
        task_id = uuid.uuid4()
        queue = AsyncMock()
        queue.pop_claim.side_effect = [
            (
                {
                    "id": str(task_id),
                    "video_id": str(video_id),
                    "local_path": "/tmp/video.mp4",
                    "queue_name": "pixav:upload",
                },
                "receipt-1",
            ),
            None,
        ]
        service = AsyncMock()
        task_repo = AsyncMock()
        task_repo.is_managed_video.return_value = True

        await run_worker(
            queue=queue,
            service=service,
            task_repo=task_repo,
            video_repo=AsyncMock(),
            poll_timeout=0,
            max_tasks=1,
        )

        service.process_task.assert_not_awaited()
        task_repo.update_state.assert_not_awaited()
        queue.ack.assert_awaited_once_with("receipt-1")

    async def test_run_worker_persists_failure(self) -> None:
        video_id = uuid.uuid4()
        task_id = uuid.uuid4()
        queue = AsyncMock()
        queue.name = "pixav:upload"
        queue.pop_claim.side_effect = [
            (
                {
                    "task_id": str(task_id),
                    "video_id": str(video_id),
                    "local_path": "/tmp/video.mp4",
                    "queue_name": "pixav:upload",
                    "max_retries": 0,
                },
                "receipt-1",
            ),
            None,
        ]

        stop_event = asyncio.Event()
        service = AsyncMock()

        async def _process(task: Task, account=None) -> Task:
            stop_event.set()
            return task.model_copy(
                update={
                    "state": TaskState.FAILED,
                    "error_message": "adb failed",
                }
            )

        service.process_task.side_effect = _process
        task_repo = AsyncMock()
        task_repo.is_managed_video.return_value = False
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

        queue.pop_claim.assert_not_awaited()
        service.process_task.assert_not_awaited()

    async def test_run_worker_requeues_when_lock_busy(self) -> None:
        video_id = uuid.uuid4()
        task_id = uuid.uuid4()
        queue = AsyncMock()
        queue.name = "pixav:upload"
        queue.pop_claim.side_effect = [
            (
                {
                    "task_id": str(task_id),
                    "video_id": str(video_id),
                    "local_path": "/tmp/video.mp4",
                    "queue_name": "pixav:upload",
                },
                "receipt-1",
            ),
            None,
        ]

        stop_event = asyncio.Event()
        service = AsyncMock()
        redis_client = AsyncMock()
        redis_client.get.return_value = None
        redis_client.set.return_value = False

        async def _nack(*_args, **_kwargs) -> bool:
            stop_event.set()
            return True

        queue.nack.side_effect = _nack

        await run_worker(
            queue=queue,
            service=service,
            redis_client=redis_client,
            poll_timeout=0,
            stop_event=stop_event,
        )

        queue.nack.assert_awaited()
        service.process_task.assert_not_awaited()

    async def test_run_worker_updates_account_usage_on_success(self) -> None:
        video_id = uuid.uuid4()
        task_id = uuid.uuid4()
        account_id = uuid.uuid4()
        queue = AsyncMock()
        queue.name = "pixav:upload"
        queue.pop_claim.side_effect = [
            (
                {
                    "task_id": str(task_id),
                    "video_id": str(video_id),
                    "account_id": str(account_id),
                    "local_path": "/tmp/video.mp4",
                    "queue_name": "pixav:upload",
                },
                "receipt-1",
            ),
            None,
        ]

        stop_event = asyncio.Event()
        service = AsyncMock()

        async def _process(task: Task, account=None) -> Task:
            stop_event.set()
            return task.model_copy(
                update={
                    "state": TaskState.COMPLETE,
                    "share_url": "https://photos.app.goo.gl/abc123",
                }
            )

        service.process_task.side_effect = _process
        task_repo = AsyncMock()
        task_repo.is_managed_video.return_value = False
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

    async def test_run_worker_drops_duplicate_terminal_task(self) -> None:
        video_id = uuid.uuid4()
        task_id = uuid.uuid4()
        queue = AsyncMock()
        queue.name = "pixav:upload"
        queue.pop_claim.side_effect = [
            (
                {
                    "task_id": str(task_id),
                    "video_id": str(video_id),
                    "local_path": "/tmp/video.mp4",
                    "queue_name": "pixav:upload",
                },
                "receipt-1",
            ),
            None,
        ]

        stop_event = asyncio.Event()
        service = AsyncMock()
        task_repo = AsyncMock()
        task_repo.is_managed_video.return_value = False
        task_repo.find_by_id.return_value = Task(
            id=task_id,
            video_id=video_id,
            state=TaskState.COMPLETE,
            queue_name="pixav:upload",
        )

        async def _stop_soon() -> None:
            await asyncio.sleep(0.05)
            stop_event.set()

        stopper = asyncio.create_task(_stop_soon())
        try:
            await run_worker(
                queue=queue,
                service=service,
                task_repo=task_repo,
                poll_timeout=0,
                stop_event=stop_event,
            )
        finally:
            await stopper

        service.process_task.assert_not_awaited()
        queue.ack.assert_awaited()

    async def test_release_upload_lock_uses_lua_compare_and_delete(self) -> None:
        redis_client = AsyncMock()

        await _release_upload_lock(
            redis_client,
            lock_key="pixav:upload:lock",
            lock_token="token-123",
        )

        redis_client.eval.assert_awaited_once()
        args = redis_client.eval.call_args[0]
        assert "redis.call('get'" in args[0]
        assert args[1] == 1
        assert args[2] == "pixav:upload:lock"
        assert args[3] == "token-123"
        redis_client.get.assert_not_awaited()
        redis_client.delete.assert_not_awaited()

    async def test_release_upload_lock_falls_back_when_eval_unavailable(self) -> None:
        redis_client = AsyncMock()
        redis_client.eval.side_effect = RuntimeError("EVAL disabled")
        redis_client.get.return_value = "token-123"

        await _release_upload_lock(
            redis_client,
            lock_key="pixav:upload:lock",
            lock_token="token-123",
        )

        redis_client.get.assert_awaited_once_with("pixav:upload:lock")
        redis_client.delete.assert_awaited_once_with("pixav:upload:lock")

    async def test_renew_upload_lock_uses_lua_compare_and_expire(self) -> None:
        redis_client = AsyncMock()
        redis_client.eval.return_value = 1

        renewed = await _renew_upload_lock(
            redis_client,
            lock_key="pixav:upload:lock",
            lock_token="token-123",
            ttl_seconds=7200,
        )

        assert renewed is True
        redis_client.eval.assert_awaited_once()
        args = redis_client.eval.call_args[0]
        assert "redis.call('expire'" in args[0]
        assert args[2] == "pixav:upload:lock"
        assert args[3] == "token-123"
        assert args[4] == "7200"

    async def test_renew_upload_lock_falls_back_when_eval_unavailable(self) -> None:
        redis_client = AsyncMock()
        redis_client.eval.side_effect = RuntimeError("EVAL disabled")
        redis_client.get.return_value = "token-123"
        redis_client.expire.return_value = True

        renewed = await _renew_upload_lock(
            redis_client,
            lock_key="pixav:upload:lock",
            lock_token="token-123",
            ttl_seconds=60,
        )

        assert renewed is True
        redis_client.get.assert_awaited_once_with("pixav:upload:lock")
        redis_client.expire.assert_awaited_once_with("pixav:upload:lock", 60)

    async def test_guarded_one_shot_exits_after_exact_payload(self) -> None:
        task_id = uuid.uuid4()
        video_id = uuid.uuid4()
        queue = AsyncMock()
        queue.name = "pixav:upload"
        queue.requeue_inflight.return_value = 0
        queue.pop_claim.return_value = (
            {
                "task_id": str(task_id),
                "video_id": str(video_id),
                "local_path": "/tmp/video.mp4",
                "queue_name": "pixav:upload",
            },
            "receipt-1",
        )
        service = AsyncMock()
        service.process_task.return_value = Task(
            id=task_id,
            video_id=video_id,
            state=TaskState.COMPLETE,
            queue_name="pixav:upload",
            local_path="/tmp/video.mp4",
            share_url=f"pixav-local://{video_id}",
        )

        await run_worker(
            queue=queue,
            service=service,
            task_repo=unmanaged_task_repo(),
            video_repo=AsyncMock(),
            poll_timeout=0,
            max_tasks=1,
            expected_task_id=task_id,
            expected_video_id=video_id,
        )

        queue.ack.assert_awaited_once_with("receipt-1")
        assert queue.pop_claim.await_count == 1

    async def test_guarded_one_shot_restores_mismatched_head(self) -> None:
        expected_task_id = uuid.uuid4()
        expected_video_id = uuid.uuid4()
        queue = AsyncMock()
        queue.name = "pixav:upload"
        queue.requeue_inflight.return_value = 0
        queue.pop_claim.return_value = (
            {"task_id": str(uuid.uuid4()), "video_id": str(expected_video_id)},
            "receipt-1",
        )
        service = AsyncMock()

        with pytest.raises(RuntimeError, match="upload queue head changed"):
            await run_worker(
                queue=queue,
                service=service,
                poll_timeout=0,
                max_tasks=1,
                expected_task_id=expected_task_id,
                expected_video_id=expected_video_id,
            )

        queue.nack.assert_awaited_once_with("receipt-1", requeue=True, front=True)
        queue.ack.assert_not_awaited()
        service.process_task.assert_not_awaited()

    async def test_settings_worker_checks_database_identity_before_wiring_service(self) -> None:
        pool = AsyncMock()
        pool.fetchval.return_value = "other-cluster"
        redis = AsyncMock()
        settings = Settings(vpn_egress_echo_url="")

        with (
            patch("pixav.pixel_injector.worker.create_pool", new=AsyncMock(return_value=pool)),
            patch("pixav.pixel_injector.worker.create_redis", new=AsyncMock(return_value=redis)),
            patch("pixav.pixel_injector.worker.LocalPixelInjectorService") as local_service,
        ):
            with pytest.raises(RuntimeError, match="database identity mismatch"):
                await run_from_settings(settings, expected_db_identity="expected-cluster", max_tasks=1)

        local_service.assert_not_called()
        redis.aclose.assert_awaited_once()
        pool.close.assert_awaited_once()
