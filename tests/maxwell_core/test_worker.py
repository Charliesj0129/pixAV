"""Tests for maxwell_core worker queue ingestion."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, call, patch

from pixav.maxwell_core.worker import (
    _open_reverifications,
    _publish_execution_metrics,
    _publish_queue_depths,
    ingest_crawl_queue,
)
from pixav.shared import metrics
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


class TestManagedObservationIngestion:
    """BDD-008: the authority discards a payload it cannot read, and counts it."""

    @staticmethod
    def _observation(provider_id: str, letter: str) -> dict:
        from pixav.sht_probe.policy import SourcePolicy

        candidate = SourcePolicy().normalize(
            {
                "provider": "synthetic",
                "provider_id": provider_id,
                "title": "synthetic 1080p .mp4",
                "magnet_uri": "magnet:?xt=urn:btih:" + letter * 40,
            }
        )
        return candidate.model_dump(mode="json")

    @staticmethod
    def _errors() -> float:
        return metrics.source_adapter_errors.labels(reason="INVALID_PROVIDER_PAYLOAD")._value.get()

    async def test_an_unreadable_observation_is_counted_and_the_rest_survive_bdd_008(self) -> None:
        from pixav.sht_probe.policy import SourcePolicy

        crawl_queue = AsyncMock()
        crawl_queue.pop_claim.side_effect = [
            ({"schema": "source-observation-v1", "observation": {"provider": "synthetic"}}, "receipt-1"),
            ({"schema": "source-observation-v1", "observation": self._observation("two", "b")}, "receipt-2"),
            None,
        ]
        managed = AsyncMock()
        managed.source_policy = SourcePolicy()
        before = self._errors()

        created = await ingest_crawl_queue(
            crawl_queue=crawl_queue,
            task_repo=AsyncMock(),
            video_repo=AsyncMock(),
            download_queue_name="pixav:download",
            managed_workflow=managed,
        )

        assert created == 1, "the readable observation still becomes work"
        assert self._errors() == before + 1
        managed.ingest_observation.assert_awaited_once()
        # Both queue items are settled: the bad one is acknowledged, not requeued
        # forever, because redelivering it cannot make it parse.
        assert crawl_queue.ack.await_count == 2
        crawl_queue.nack.assert_not_awaited()


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


def _series(name: str) -> dict[str, float]:
    rendered = metrics.generate_latest(metrics._REGISTRY).decode()
    return {
        line.split(" ")[0]: float(line.split(" ")[1])
        for line in rendered.splitlines()
        if line.startswith(name) and not line.startswith("#")
    }


class TestPublishExecutionMetrics:
    async def test_waiting_states_are_published_separately_bdd_124(self) -> None:
        """A quota wait is a healthy pipeline, not a failure to page on."""
        managed = AsyncMock()
        managed.observe_states.return_value = {
            "waiting_quota": 2,
            "source_unavailable": 1,
            "user_action_required": 3,
            "terminal": {"infrastructure": 4},
        }

        await _publish_execution_metrics(managed)

        assert _series("pixav_executions_waiting_quota")["pixav_executions_waiting_quota"] == 2
        assert _series("pixav_executions_source_unavailable")["pixav_executions_source_unavailable"] == 1
        assert _series("pixav_executions_user_action_required")["pixav_executions_user_action_required"] == 3
        assert _series("pixav_executions_terminal") == {'pixav_executions_terminal{failure_class="infrastructure"}': 4}

    async def test_source_exhaustion_is_observable_bdd_123(self) -> None:
        managed = AsyncMock()
        managed.observe_states.return_value = {
            "waiting_quota": 0,
            "source_unavailable": 5,
            "user_action_required": 0,
            "terminal": {},
        }

        await _publish_execution_metrics(managed)

        assert _series("pixav_executions_source_unavailable")["pixav_executions_source_unavailable"] == 5

    async def test_a_metrics_failure_never_breaks_the_tick(self) -> None:
        managed = AsyncMock()
        managed.observe_states.side_effect = RuntimeError("database is away")

        await _publish_execution_metrics(managed)

    async def test_an_unmanaged_deployment_publishes_nothing(self) -> None:
        await _publish_execution_metrics(None)


class TestReverificationBacklog:
    """A durable copy nobody has re-read is an assumption, and it is visible."""

    @staticmethod
    def _managed(due: int = 0) -> AsyncMock:
        managed = AsyncMock()
        managed.observe_states.return_value = {
            "waiting_quota": 0,
            "source_unavailable": 0,
            "user_action_required": 0,
            "terminal": {},
        }
        managed.storage.due_reverification.return_value = due
        managed.storage.reverify_due.return_value = []
        return managed

    async def test_the_overdue_backlog_is_published_bdd_055(self) -> None:
        managed = self._managed(due=4)

        await _publish_execution_metrics(managed, reverify_interval_days=30)

        managed.storage.due_reverification.assert_awaited_once_with(interval_days=30)
        assert _series("pixav_remote_assets_due_reverification")["pixav_remote_assets_due_reverification"] == 4

    async def test_re_verification_is_opt_out_bdd_055(self) -> None:
        managed = self._managed()

        await _open_reverifications(managed, interval_days=0)

        managed.storage.reverify_due.assert_not_awaited()

    async def test_a_reverification_failure_never_breaks_the_tick_bdd_055(self) -> None:
        """Losing one assurance cycle changes nothing that was already proven."""
        managed = self._managed()
        managed.storage.reverify_due.side_effect = RuntimeError("database is away")

        await _open_reverifications(managed, interval_days=30)
