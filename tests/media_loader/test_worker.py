"""Tests for media_loader worker loop and payload parsing."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from pixav.config import Settings
from pixav.media_loader.worker import _parse_int, _parse_uuid, _record_task_outcome, run_loop
from pixav.shared.enums import TaskState
from pixav.shared.exceptions import DownloadError
from pixav.shared.models import Task
from pixav.shared.pause import is_paused_value


class _StopWorker(BaseException):
    """Exit the infinite worker loop without pytest's KeyboardInterrupt handling."""


def _settings(**changes) -> Settings:
    return Settings(
        **changes,
        queue_download="pixav:download",
        queue_download_dlq="pixav:download:dlq",
        queue_upload="pixav:upload",
        download_max_retries=5,
        download_min_free_bytes=0,
        download_min_free_percent=0,
        vpn_egress_echo_url="",
    )


def _wire_common_patches(
    *,
    pool: AsyncMock,
    redis: AsyncMock,
    download_queue: AsyncMock,
    qbit_client: AsyncMock,
    service: AsyncMock,
    disk_guard: AsyncMock | None = None,
):
    if disk_guard is None:
        disk_guard = AsyncMock()
        disk_guard.check_and_latch.return_value = SimpleNamespace(paused=False, reason=None)
    return patch.multiple(
        "pixav.media_loader.worker",
        create_pool=AsyncMock(return_value=pool),
        create_redis=AsyncMock(return_value=redis),
        TaskQueue=Mock(return_value=download_queue),
        QBitClient=Mock(return_value=qbit_client),
        FFmpegRemuxer=Mock(return_value=object()),
        StashMetadataScraper=Mock(return_value=object()),
        MediaLoaderService=Mock(return_value=service),
        DownloadSpaceGuard=Mock(return_value=disk_guard),
    )


class TestMediaLoaderWorker:
    async def test_run_loop_claims_and_acks_payload(self) -> None:
        video_id = uuid.uuid4()
        task_id = uuid.uuid4()
        pool = AsyncMock()
        redis = AsyncMock()
        redis.get.return_value = None
        download_queue = AsyncMock()
        download_queue.name = "pixav:download"
        download_queue.requeue_inflight.return_value = 2
        download_queue.pop_claim.side_effect = [
            ({"task_id": str(task_id), "video_id": str(video_id)}, "receipt-1"),
            _StopWorker(),
        ]
        qbit_client = AsyncMock()
        qbit_client.health_check.return_value = "4.6.0"
        service = AsyncMock()
        service.process_task.return_value = Task(
            id=task_id,
            video_id=video_id,
            state=TaskState.PENDING,
            queue_name="pixav:upload",
        )

        with _wire_common_patches(
            pool=pool,
            redis=redis,
            download_queue=download_queue,
            qbit_client=qbit_client,
            service=service,
        ):
            with pytest.raises(_StopWorker):
                await run_loop(_settings())

        service.process_task.assert_awaited_once()
        download_queue.ack.assert_awaited_once_with("receipt-1")
        download_queue.nack.assert_not_awaited()
        redis.aclose.assert_awaited_once()
        pool.close.assert_awaited_once()
        qbit_client.aclose.assert_awaited_once()

    async def test_run_loop_drops_invalid_payload_and_acks(self) -> None:
        pool = AsyncMock()
        redis = AsyncMock()
        redis.get.return_value = None
        download_queue = AsyncMock()
        download_queue.name = "pixav:download"
        download_queue.requeue_inflight.return_value = 0
        download_queue.pop_claim.side_effect = [
            ({"video_id": "bad-uuid"}, "receipt-1"),
            _StopWorker(),
        ]
        qbit_client = AsyncMock()
        qbit_client.health_check.return_value = "4.6.0"
        service = AsyncMock()

        with _wire_common_patches(
            pool=pool,
            redis=redis,
            download_queue=download_queue,
            qbit_client=qbit_client,
            service=service,
        ):
            with pytest.raises(_StopWorker):
                await run_loop(_settings())

        service.process_task.assert_not_awaited()
        download_queue.ack.assert_awaited_once_with("receipt-1")

    async def test_run_loop_nacks_on_unexpected_loop_error(self) -> None:
        video_id = uuid.uuid4()
        task_id = uuid.uuid4()
        pool = AsyncMock()
        redis = AsyncMock()
        redis.get.return_value = None
        download_queue = AsyncMock()
        download_queue.name = "pixav:download"
        download_queue.requeue_inflight.return_value = 0
        download_queue.pop_claim.side_effect = [
            ({"task_id": str(task_id), "video_id": str(video_id)}, "receipt-1"),
            _StopWorker(),
        ]
        download_queue.ack.side_effect = RuntimeError("ack failed")
        qbit_client = AsyncMock()
        qbit_client.health_check.return_value = "4.6.0"
        service = AsyncMock()
        service.process_task.return_value = Task(
            id=task_id,
            video_id=video_id,
            state=TaskState.PENDING,
            queue_name="pixav:upload",
        )

        with (
            _wire_common_patches(
                pool=pool,
                redis=redis,
                download_queue=download_queue,
                qbit_client=qbit_client,
                service=service,
            ),
            patch("pixav.media_loader.worker.asyncio.sleep", new=AsyncMock()),
        ):
            with pytest.raises(_StopWorker):
                await run_loop(_settings())

        download_queue.nack.assert_awaited_once_with("receipt-1", requeue=True)

    async def test_run_loop_returns_when_qbit_health_check_fails(self) -> None:
        pool = AsyncMock()
        redis = AsyncMock()
        redis.get.return_value = None
        download_queue = AsyncMock()
        download_queue.name = "pixav:download"
        qbit_client = AsyncMock()
        qbit_client.health_check.side_effect = DownloadError("qbit down")
        service = AsyncMock()

        with _wire_common_patches(
            pool=pool,
            redis=redis,
            download_queue=download_queue,
            qbit_client=qbit_client,
            service=service,
        ):
            await run_loop(_settings())

        service.process_task.assert_not_awaited()
        qbit_client.aclose.assert_awaited_once()
        redis.aclose.assert_awaited_once()
        pool.close.assert_awaited_once()

    async def test_managed_activity_worker_starts_without_a_torrent_client(self) -> None:
        """Preparation needs ffmpeg and a local artifact, not a swarm.

        Refusing to start would also refuse every ``prepare`` activity. A
        ``download`` activity still fails through the torrent client itself and
        is reported as infrastructure for the authority to reconcile.
        """
        pool = AsyncMock()
        redis = AsyncMock()
        redis.get.return_value = None
        download_queue = AsyncMock()
        download_queue.name = "pixav:download"
        qbit_client = AsyncMock()
        qbit_client.health_check.side_effect = DownloadError("qbit down")
        service = AsyncMock()
        activity = AsyncMock()
        # The managed loop swallows per-activity errors on purpose, so the stop
        # has to come from outside it.
        redis.get.side_effect = [None, None, _StopWorker()]

        with (
            _wire_common_patches(
                pool=pool,
                redis=redis,
                download_queue=download_queue,
                qbit_client=qbit_client,
                service=service,
            ),
            patch("pixav.media_loader.activity.MediaActivityWorker", return_value=activity),
            patch("pixav.shared.workflow.require_workflow_role", new=AsyncMock()),
            patch("pixav.media_loader.worker.asyncio.sleep", new=AsyncMock()),
        ):
            with pytest.raises(_StopWorker):
                await run_loop(_settings(managed_media_workflow=True))

        assert activity.run_one.await_count == 2
        service.process_task.assert_not_awaited()
        qbit_client.aclose.assert_awaited_once()

    def test_parse_uuid(self) -> None:
        assert _parse_uuid("bad-uuid") is None
        assert _parse_uuid(str(uuid.uuid4())) is not None

    def test_parse_int(self) -> None:
        assert _parse_int("10", default=1, minimum=0) == 10
        assert _parse_int("x", default=3, minimum=0) == 3
        assert _parse_int("-5", default=3, minimum=0) == 0

    def test_parse_system_pause(self) -> None:
        assert is_paused_value("true") is True
        assert is_paused_value("0") is False
        assert is_paused_value(None) is False
        assert is_paused_value('{"paused": true, "token": "owned"}') is True

    async def test_one_shot_limit_exits_after_one_ack(self) -> None:
        video_id = uuid.uuid4()
        task_id = uuid.uuid4()
        pool = AsyncMock()
        redis = AsyncMock()
        redis.get.return_value = None
        download_queue = AsyncMock()
        download_queue.name = "pixav:download"
        download_queue.requeue_inflight.return_value = 0
        download_queue.pop_claim.return_value = (
            {"task_id": str(task_id), "video_id": str(video_id)},
            "receipt-1",
        )
        qbit_client = AsyncMock()
        qbit_client.health_check.return_value = "5.2.3"
        service = AsyncMock()
        service.process_task.return_value = Task(
            id=task_id,
            video_id=video_id,
            state=TaskState.PENDING,
            queue_name="pixav:upload",
        )

        with _wire_common_patches(
            pool=pool,
            redis=redis,
            download_queue=download_queue,
            qbit_client=qbit_client,
            service=service,
        ):
            await run_loop(_settings(), max_tasks=1)

        download_queue.ack.assert_awaited_once_with("receipt-1")
        assert download_queue.pop_claim.await_count == 1

    async def test_expected_database_identity_refuses_wrong_cluster(self) -> None:
        pool = AsyncMock()
        pool.fetchval.return_value = "other-cluster"
        redis = AsyncMock()
        qbit_client = AsyncMock()
        with _wire_common_patches(
            pool=pool,
            redis=redis,
            download_queue=AsyncMock(),
            qbit_client=qbit_client,
            service=AsyncMock(),
        ):
            with pytest.raises(RuntimeError, match="database identity mismatch"):
                await run_loop(_settings(), expected_db_identity="expected-cluster")

        qbit_client.health_check.assert_not_awaited()

    async def test_guarded_one_shot_restores_mismatched_head_and_exits(self) -> None:
        expected_task_id = uuid.uuid4()
        expected_video_id = uuid.uuid4()
        observed_task_id = uuid.uuid4()
        pool = AsyncMock()
        redis = AsyncMock()
        redis.get.return_value = None
        download_queue = AsyncMock()
        download_queue.name = "pixav:download"
        download_queue.requeue_inflight.return_value = 0
        download_queue.pop_claim.return_value = (
            {"task_id": str(observed_task_id), "video_id": str(expected_video_id)},
            "receipt-1",
        )
        qbit_client = AsyncMock()
        qbit_client.health_check.return_value = "5.2.3"
        service = AsyncMock()

        with _wire_common_patches(
            pool=pool,
            redis=redis,
            download_queue=download_queue,
            qbit_client=qbit_client,
            service=service,
        ):
            with pytest.raises(RuntimeError, match="download queue head changed"):
                await run_loop(
                    _settings(),
                    max_tasks=1,
                    expected_task_id=expected_task_id,
                    expected_video_id=expected_video_id,
                )

        download_queue.nack.assert_awaited_once_with("receipt-1", requeue=True, front=True)
        download_queue.ack.assert_not_awaited()
        service.process_task.assert_not_awaited()

    async def test_guarded_one_shot_fails_when_qbit_health_check_fails(self) -> None:
        pool = AsyncMock()
        redis = AsyncMock()
        qbit_client = AsyncMock()
        qbit_client.health_check.side_effect = DownloadError("qbit down")

        with _wire_common_patches(
            pool=pool,
            redis=redis,
            download_queue=AsyncMock(),
            qbit_client=qbit_client,
            service=AsyncMock(),
        ):
            with pytest.raises(RuntimeError, match="qBittorrent health check failed"):
                await run_loop(_settings(), max_tasks=1)


class TestRecordTaskOutcome:
    """Terminal task states must map onto the shared Prometheus counters."""

    def test_complete_counts_as_processed(self) -> None:
        with patch("pixav.media_loader.worker.record_task_processed") as processed:
            _record_task_outcome(TaskState.COMPLETE)
        processed.assert_called_once_with("media_loader")

    def test_failed_counts_as_failed(self) -> None:
        with patch("pixav.media_loader.worker.record_task_failed") as failed:
            _record_task_outcome(TaskState.FAILED)
        failed.assert_called_once_with("media_loader")

    @pytest.mark.parametrize("state", [TaskState.PENDING, TaskState.DOWNLOADING, TaskState.REMUXING])
    def test_non_terminal_states_count_as_retried(self, state: TaskState) -> None:
        with patch("pixav.media_loader.worker.record_task_retried") as retried:
            _record_task_outcome(state)
        retried.assert_called_once_with("media_loader")


class TestDownloadPauseGate:
    async def test_paused_disk_blocks_claims(self) -> None:
        """A latched disk pause must stop new downloads without killing the loop."""
        pool = AsyncMock()
        redis = AsyncMock()
        redis.get.return_value = None
        download_queue = AsyncMock()
        download_queue.name = "pixav:download"
        download_queue.requeue_inflight.return_value = 0
        qbit_client = AsyncMock()
        qbit_client.health_check.return_value = "4.6.0"

        disk_guard = AsyncMock()
        disk_guard.check_and_latch.side_effect = [
            SimpleNamespace(paused=True, reason="disk space below download safety threshold"),
            _StopWorker(),
        ]

        with _wire_common_patches(
            pool=pool,
            redis=redis,
            download_queue=download_queue,
            qbit_client=qbit_client,
            service=AsyncMock(),
            disk_guard=disk_guard,
        ):
            with patch("pixav.media_loader.worker.asyncio.sleep", AsyncMock()) as sleep:
                with pytest.raises(_StopWorker):
                    await run_loop(_settings())

        download_queue.pop_claim.assert_not_awaited()
        sleep.assert_awaited()
        # Teardown still runs, so upload/janitor/monitoring stay unaffected.
        redis.aclose.assert_awaited_once()
        pool.close.assert_awaited_once()

    async def test_system_pause_blocks_claims_but_keeps_worker_alive(self) -> None:
        pool = AsyncMock()
        redis = AsyncMock()
        redis.get.side_effect = ["true", _StopWorker()]
        download_queue = AsyncMock()
        download_queue.name = "pixav:download"
        download_queue.requeue_inflight.return_value = 0
        qbit_client = AsyncMock()
        qbit_client.health_check.return_value = "5.2.3"

        with _wire_common_patches(
            pool=pool,
            redis=redis,
            download_queue=download_queue,
            qbit_client=qbit_client,
            service=AsyncMock(),
        ):
            with patch("pixav.media_loader.worker.asyncio.sleep", AsyncMock()):
                with pytest.raises(_StopWorker):
                    await run_loop(_settings())

        download_queue.pop_claim.assert_not_awaited()
