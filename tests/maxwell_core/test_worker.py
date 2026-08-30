"""Tests for maxwell_core worker queue ingestion."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, call, patch

from pixav.maxwell_core.worker import _publish_queue_depths, ingest_crawl_queue
from pixav.shared.enums import VideoStatus
from pixav.shared.models import Video


class TestIngestCrawlQueue:
    async def test_creates_pending_task_from_valid_payload(self) -> None:
        video_id = uuid.uuid4()
        crawl_queue = AsyncMock()
        crawl_queue.pop_claim.side_effect = [
            ({"video_id": str(video_id), "magnet_uri": "magnet:?xt=urn:btih:abc"}, "receipt-1"),
            None,
        ]

        task_repo = AsyncMock()
        task_repo.has_open_task.return_value = False

        video_repo = AsyncMock()
        video_repo.find_by_id.return_value = Video(
            id=video_id,
            title="E2E Video",
            magnet_uri="magnet:?xt=urn:btih:abc",
            status=VideoStatus.DISCOVERED,
        )

        created = await ingest_crawl_queue(
            crawl_queue=crawl_queue,
            task_repo=task_repo,
            video_repo=video_repo,
            download_queue_name="pixav:download",
            batch_size=5,
        )

        assert created == 1
        task_repo.insert.assert_awaited_once()
        inserted = task_repo.insert.call_args[0][0]
        assert inserted.video_id == video_id
        assert inserted.queue_name == "pixav:download"

    async def test_skips_invalid_video_id(self) -> None:
        crawl_queue = AsyncMock()
        crawl_queue.pop_claim.side_effect = [({"video_id": "bad-uuid"}, "receipt-1"), None]

        task_repo = AsyncMock()
        video_repo = AsyncMock()

        created = await ingest_crawl_queue(
            crawl_queue=crawl_queue,
            task_repo=task_repo,
            video_repo=video_repo,
            download_queue_name="pixav:download",
        )

        assert created == 0
        task_repo.insert.assert_not_awaited()

    async def test_skips_missing_video(self) -> None:
        video_id = uuid.uuid4()
        crawl_queue = AsyncMock()
        crawl_queue.pop_claim.side_effect = [({"video_id": str(video_id)}, "receipt-1"), None]

        task_repo = AsyncMock()
        video_repo = AsyncMock()
        video_repo.find_by_id.return_value = None

        created = await ingest_crawl_queue(
            crawl_queue=crawl_queue,
            task_repo=task_repo,
            video_repo=video_repo,
            download_queue_name="pixav:download",
        )

        assert created == 0
        task_repo.insert.assert_not_awaited()

    async def test_skips_when_open_task_exists(self) -> None:
        video_id = uuid.uuid4()
        crawl_queue = AsyncMock()
        crawl_queue.pop_claim.side_effect = [({"video_id": str(video_id)}, "receipt-1"), None]

        task_repo = AsyncMock()
        task_repo.has_open_task.return_value = True
        video_repo = AsyncMock()
        video_repo.find_by_id.return_value = Video(
            id=video_id,
            title="Dup",
            status=VideoStatus.DISCOVERED,
        )

        created = await ingest_crawl_queue(
            crawl_queue=crawl_queue,
            task_repo=task_repo,
            video_repo=video_repo,
            download_queue_name="pixav:download",
        )

        assert created == 0
        task_repo.insert.assert_not_awaited()


class TestPublishQueueDepths:
    """The tick must publish a Prometheus gauge for every pipeline queue."""

    @staticmethod
    def _queue(name: str, depth: int) -> AsyncMock:
        queue = AsyncMock()
        queue.name = name
        queue.total_depth.return_value = depth
        return queue

    async def test_publishes_depth_for_every_queue(self) -> None:
        crawl_queue = self._queue("pixav:crawl", 3)
        download_queue = self._queue("pixav:download", 7)
        upload_queue = self._queue("pixav:upload", 0)

        with patch("pixav.maxwell_core.worker.set_queue_depth") as set_depth:
            await _publish_queue_depths(
                crawl_queue,
                {"pixav:download": download_queue, "pixav:upload": upload_queue},
            )

        assert set_depth.call_args_list == [
            call("pixav:crawl", 3),
            call("pixav:download", 7),
            call("pixav:upload", 0),
        ]

    async def test_queue_error_does_not_break_the_tick(self) -> None:
        healthy = self._queue("pixav:download", 2)
        broken = self._queue("pixav:crawl", 0)
        broken.total_depth.side_effect = RuntimeError("redis down")

        with patch("pixav.maxwell_core.worker.set_queue_depth") as set_depth:
            await _publish_queue_depths(broken, {"pixav:download": healthy})

        set_depth.assert_called_once_with("pixav:download", 2)
