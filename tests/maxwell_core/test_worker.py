"""Tests for maxwell_core worker queue ingestion."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

from pixav.maxwell_core.worker import ingest_crawl_queue
from pixav.shared.enums import VideoStatus
from pixav.shared.models import Video


class TestIngestCrawlQueue:
    async def test_creates_pending_task_from_valid_payload(self) -> None:
        video_id = uuid.uuid4()
        crawl_queue = AsyncMock()
        crawl_queue.pop.side_effect = [
            {"video_id": str(video_id), "magnet_uri": "magnet:?xt=urn:btih:abc"},
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
        crawl_queue.pop.side_effect = [{"video_id": "bad-uuid"}, None]

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
        crawl_queue.pop.side_effect = [{"video_id": str(video_id)}, None]

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
        crawl_queue.pop.side_effect = [{"video_id": str(video_id)}, None]

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
