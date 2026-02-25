"""Tests for media_loader worker loop and payload parsing."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, Mock, patch

import pytest

from pixav.config import Settings
from pixav.media_loader.worker import _parse_int, _parse_uuid, run_loop
from pixav.shared.enums import TaskState
from pixav.shared.exceptions import DownloadError
from pixav.shared.models import Task


def _settings() -> Settings:
    return Settings(
        queue_download="pixav:download",
        queue_download_dlq="pixav:download:dlq",
        queue_upload="pixav:upload",
        download_max_retries=5,
    )


def _wire_common_patches(
    *,
    pool: AsyncMock,
    redis: AsyncMock,
    download_queue: AsyncMock,
    dlq_queue: AsyncMock,
    qbit_client: AsyncMock,
    service: AsyncMock,
):
    return patch.multiple(
        "pixav.media_loader.worker",
        create_pool=AsyncMock(return_value=pool),
        create_redis=AsyncMock(return_value=redis),
        TaskQueue=Mock(side_effect=[download_queue, dlq_queue]),
        QBitClient=Mock(return_value=qbit_client),
        FFmpegRemuxer=Mock(return_value=object()),
        StashMetadataScraper=Mock(return_value=object()),
        MediaLoaderService=Mock(return_value=service),
    )


class TestMediaLoaderWorker:
    async def test_run_loop_claims_and_acks_payload(self) -> None:
        video_id = uuid.uuid4()
        task_id = uuid.uuid4()
        pool = AsyncMock()
        redis = AsyncMock()
        download_queue = AsyncMock()
        download_queue.name = "pixav:download"
        download_queue.requeue_inflight.return_value = 2
        download_queue.pop_claim.side_effect = [
            ({"task_id": str(task_id), "video_id": str(video_id)}, "receipt-1"),
            KeyboardInterrupt(),
        ]
        dlq_queue = AsyncMock()
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
            dlq_queue=dlq_queue,
            qbit_client=qbit_client,
            service=service,
        ):
            with pytest.raises(KeyboardInterrupt):
                await run_loop(_settings())

        service.process_task.assert_awaited_once()
        download_queue.ack.assert_awaited_once_with("receipt-1")
        download_queue.nack.assert_not_awaited()
        redis.aclose.assert_awaited_once()
        pool.close.assert_awaited_once()

    async def test_run_loop_drops_invalid_payload_and_acks(self) -> None:
        pool = AsyncMock()
        redis = AsyncMock()
        download_queue = AsyncMock()
        download_queue.name = "pixav:download"
        download_queue.requeue_inflight.return_value = 0
        download_queue.pop_claim.side_effect = [
            ({"video_id": "bad-uuid"}, "receipt-1"),
            KeyboardInterrupt(),
        ]
        dlq_queue = AsyncMock()
        qbit_client = AsyncMock()
        qbit_client.health_check.return_value = "4.6.0"
        service = AsyncMock()

        with _wire_common_patches(
            pool=pool,
            redis=redis,
            download_queue=download_queue,
            dlq_queue=dlq_queue,
            qbit_client=qbit_client,
            service=service,
        ):
            with pytest.raises(KeyboardInterrupt):
                await run_loop(_settings())

        service.process_task.assert_not_awaited()
        download_queue.ack.assert_awaited_once_with("receipt-1")

    async def test_run_loop_nacks_on_unexpected_loop_error(self) -> None:
        video_id = uuid.uuid4()
        task_id = uuid.uuid4()
        pool = AsyncMock()
        redis = AsyncMock()
        download_queue = AsyncMock()
        download_queue.name = "pixav:download"
        download_queue.requeue_inflight.return_value = 0
        download_queue.pop_claim.side_effect = [
            ({"task_id": str(task_id), "video_id": str(video_id)}, "receipt-1"),
            KeyboardInterrupt(),
        ]
        download_queue.ack.side_effect = RuntimeError("ack failed")
        dlq_queue = AsyncMock()
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
                dlq_queue=dlq_queue,
                qbit_client=qbit_client,
                service=service,
            ),
            patch("pixav.media_loader.worker.asyncio.sleep", new=AsyncMock()),
        ):
            with pytest.raises(KeyboardInterrupt):
                await run_loop(_settings())

        download_queue.nack.assert_awaited_once_with("receipt-1", requeue=True)

    async def test_run_loop_returns_when_qbit_health_check_fails(self) -> None:
        pool = AsyncMock()
        redis = AsyncMock()
        download_queue = AsyncMock()
        download_queue.name = "pixav:download"
        dlq_queue = AsyncMock()
        qbit_client = AsyncMock()
        qbit_client.health_check.side_effect = DownloadError("qbit down")
        service = AsyncMock()

        with _wire_common_patches(
            pool=pool,
            redis=redis,
            download_queue=download_queue,
            dlq_queue=dlq_queue,
            qbit_client=qbit_client,
            service=service,
        ):
            await run_loop(_settings())

        service.process_task.assert_not_awaited()
        redis.aclose.assert_awaited_once()
        pool.close.assert_awaited_once()

    def test_parse_uuid(self) -> None:
        assert _parse_uuid("bad-uuid") is None
        assert _parse_uuid(str(uuid.uuid4())) is not None

    def test_parse_int(self) -> None:
        assert _parse_int("10", default=1, minimum=0) == 10
        assert _parse_int("x", default=3, minimum=0) == 3
        assert _parse_int("-5", default=3, minimum=0) == 0
