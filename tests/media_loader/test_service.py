"""Tests for MediaLoaderService."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from pixav.media_loader.service import (
    MediaLoaderService,
    _fallback_title,
    _technical_scoring_title,
)
from pixav.shared.enums import TaskState, VideoStatus
from pixav.shared.exceptions import DownloadError, RemuxError, SourceUnavailableError
from pixav.shared.models import SourceCandidate, Task, Video


@pytest.fixture(autouse=True)
def _stable_media_selection():
    """Service tests isolate orchestration; remuxer tests cover filesystem selection."""

    async def prepare(input_path, output_path, remuxer):
        await remuxer.remux(input_path, output_path)
        return SimpleNamespace(path=output_path)

    with (
        patch("pixav.media_loader.remuxer.select_media_input", return_value="/downloads/video.mkv"),
        patch("pixav.media_loader.preparation.prepare_media", side_effect=prepare),
        patch(
            "pixav.media_loader.service.probe_media", return_value={"size_bytes": 100, "height": 1080, "codec": "h264"}
        ),
    ):
        yield


@pytest.fixture
def mock_client() -> AsyncMock:
    client = AsyncMock()
    client.add_magnet.return_value = "hash123"
    client.wait_complete.return_value = "/downloads/video.mkv"
    return client


@pytest.fixture
def mock_remuxer() -> AsyncMock:
    remuxer = AsyncMock()
    remuxer.remux.return_value = None
    return remuxer


@pytest.fixture
def mock_scraper() -> AsyncMock:
    scraper = AsyncMock()
    scraper.scrape.return_value = {"found": True, "title": "Test", "tags": ["tag1"]}
    return scraper


@pytest.fixture
def mock_video_repo() -> AsyncMock:
    repo = AsyncMock()
    repo.find_by_id.return_value = Video(
        id=uuid.UUID("00000000-0000-0000-0000-000000000010"),
        title="Test Video",
        magnet_uri="magnet:?xt=urn:btih:abc123",
        status=VideoStatus.DISCOVERED,
    )
    repo.update_status.return_value = None
    return repo


@pytest.fixture
def mock_task_repo() -> AsyncMock:
    repo = AsyncMock()
    repo.is_managed_video.return_value = False
    repo.update_state.return_value = None
    repo.route_to_queue.return_value = None
    repo.set_retry.return_value = None
    return repo


@pytest.fixture
def sample_task() -> Task:
    return Task(
        id=uuid.UUID("00000000-0000-0000-0000-000000000100"),
        video_id=uuid.UUID("00000000-0000-0000-0000-000000000010"),
        state=TaskState.PENDING,
        queue_name="pixav:download",
        max_retries=3,
    )


@pytest.fixture
def service(
    mock_client: AsyncMock,
    mock_remuxer: AsyncMock,
    mock_scraper: AsyncMock,
    mock_video_repo: AsyncMock,
    mock_task_repo: AsyncMock,
) -> MediaLoaderService:
    return MediaLoaderService(
        client=mock_client,
        remuxer=mock_remuxer,
        scraper=mock_scraper,
        video_repo=mock_video_repo,
        task_repo=mock_task_repo,
        upload_queue_name="pixav:upload",
        output_dir="/data/remuxed",
    )


class TestMediaLoaderService:
    async def test_managed_video_rejects_legacy_side_effects_bdd_019(
        self, service, sample_task, mock_task_repo, mock_client, mock_video_repo
    ):
        mock_task_repo.is_managed_video.return_value = True
        with pytest.raises(ValueError, match="managed execution"):
            await service.process_task(sample_task)
        mock_client.add_magnet.assert_not_awaited()
        mock_task_repo.update_state.assert_not_awaited()
        mock_video_repo.find_by_id.assert_not_awaited()

    async def test_process_task_happy_path(
        self,
        service: MediaLoaderService,
        sample_task: Task,
        mock_client: AsyncMock,
        mock_remuxer: AsyncMock,
        mock_scraper: AsyncMock,
        mock_video_repo: AsyncMock,
        mock_task_repo: AsyncMock,
    ) -> None:
        result = await service.process_task(sample_task)

        assert result.state == TaskState.PENDING
        assert result.queue_name == "pixav:upload"
        assert result.local_path is not None

        mock_client.add_magnet.assert_awaited_once()
        mock_client.wait_complete.assert_awaited_once_with("hash123")
        mock_remuxer.remux.assert_awaited_once()
        mock_scraper.scrape.assert_awaited_once_with("Test Video")
        mock_video_repo.update_download_result.assert_awaited_once()
        metadata = json.loads(mock_video_repo.update_download_result.await_args.kwargs["metadata_json"])
        assert metadata["torrent"] == {"name": "video.mkv", "info_hash": "hash123"}
        assert "media" in metadata
        assert metadata["stash"]["found"] is True
        mock_task_repo.route_to_queue.assert_awaited_once_with(
            sample_task.id,
            queue_name="pixav:upload",
            state=TaskState.PENDING,
        )
        mock_client.delete_torrent.assert_not_awaited()

    async def test_process_task_cleanup_failure_non_fatal(
        self,
        service: MediaLoaderService,
        sample_task: Task,
        mock_client: AsyncMock,
    ) -> None:
        mock_client.delete_torrent.side_effect = Exception("delete failed")

        result = await service.process_task(sample_task)

        assert result.state == TaskState.PENDING
        mock_client.delete_torrent.assert_not_awaited()

    async def test_process_task_video_not_found(
        self,
        service: MediaLoaderService,
        sample_task: Task,
        mock_video_repo: AsyncMock,
        mock_task_repo: AsyncMock,
    ) -> None:
        mock_video_repo.find_by_id.return_value = None

        result = await service.process_task(sample_task)

        assert result.state == TaskState.FAILED
        assert "not found" in (result.error_message or "")
        mock_task_repo.update_state.assert_awaited_once()
        mock_video_repo.update_status.assert_not_awaited()

    async def test_process_task_no_magnet(
        self,
        service: MediaLoaderService,
        sample_task: Task,
        mock_video_repo: AsyncMock,
        mock_task_repo: AsyncMock,
    ) -> None:
        mock_video_repo.find_by_id.return_value = Video(
            title="No Magnet",
            magnet_uri=None,
            status=VideoStatus.DISCOVERED,
        )

        result = await service.process_task(sample_task)

        assert result.state == TaskState.FAILED
        assert "no magnet_uri" in (result.error_message or "")
        mock_task_repo.update_state.assert_awaited_once()
        mock_video_repo.update_status.assert_awaited_once_with(sample_task.video_id, VideoStatus.FAILED)

    async def test_process_task_download_fails(
        self,
        service: MediaLoaderService,
        sample_task: Task,
        mock_client: AsyncMock,
        mock_task_repo: AsyncMock,
        mock_video_repo: AsyncMock,
    ) -> None:
        mock_client.add_magnet.side_effect = DownloadError("torrent client down")

        result = await service.process_task(sample_task)

        assert result.state == TaskState.PENDING
        assert "DownloadError" in (result.error_message or "")
        mock_task_repo.set_retry.assert_awaited_once()
        mock_video_repo.update_status.assert_any_await(sample_task.video_id, VideoStatus.DISCOVERED)
        mock_client.delete_torrent.assert_not_awaited()

    async def test_process_task_remux_fails(
        self,
        service: MediaLoaderService,
        sample_task: Task,
        mock_remuxer: AsyncMock,
        mock_client: AsyncMock,
    ) -> None:
        mock_remuxer.remux.side_effect = RemuxError("ffmpeg crashed")

        result = await service.process_task(sample_task)

        assert result.state == TaskState.PENDING
        assert "RemuxError" in (result.error_message or "")
        mock_client.delete_torrent.assert_not_awaited()

    async def test_process_task_metadata_failure_non_fatal(
        self,
        service: MediaLoaderService,
        sample_task: Task,
        mock_scraper: AsyncMock,
    ) -> None:
        mock_scraper.scrape.side_effect = Exception("stash down")

        result = await service.process_task(sample_task)

        assert result.state == TaskState.PENDING
        assert result.queue_name == "pixav:upload"

    async def test_process_task_without_scraper(
        self,
        mock_client: AsyncMock,
        mock_remuxer: AsyncMock,
        mock_video_repo: AsyncMock,
        mock_task_repo: AsyncMock,
        sample_task: Task,
    ) -> None:
        service_no_scraper = MediaLoaderService(
            client=mock_client,
            remuxer=mock_remuxer,
            scraper=None,
            video_repo=mock_video_repo,
            task_repo=mock_task_repo,
            upload_queue_name="pixav:upload",
        )

        result = await service_no_scraper.process_task(sample_task)
        assert result.state == TaskState.PENDING
        assert result.queue_name == "pixav:upload"

    async def test_process_task_requeues_when_retry_enabled(
        self,
        mock_client: AsyncMock,
        mock_remuxer: AsyncMock,
        mock_video_repo: AsyncMock,
        mock_task_repo: AsyncMock,
        sample_task: Task,
    ) -> None:
        mock_client.add_magnet.side_effect = DownloadError("transient outage")

        retry_service = MediaLoaderService(
            client=mock_client,
            remuxer=mock_remuxer,
            scraper=None,
            video_repo=mock_video_repo,
            task_repo=mock_task_repo,
            upload_queue_name="pixav:upload",
        )

        result = await retry_service.process_task(sample_task)

        assert result.state == TaskState.PENDING
        assert result.retries == 1
        # The retry is durable: it lives in PostgreSQL as a due time, never as an
        # immediate Redis re-push that Maxwell would dispatch a second time.
        mock_task_repo.set_retry.assert_awaited_once()
        due = mock_task_repo.set_retry.await_args.kwargs["retry_not_before"]
        assert due > datetime.now(timezone.utc)
        mock_video_repo.update_status.assert_any_await(sample_task.video_id, VideoStatus.DISCOVERED)

    async def test_process_task_exhausted_retries_goes_to_dlq(
        self,
        mock_client: AsyncMock,
        mock_remuxer: AsyncMock,
        mock_video_repo: AsyncMock,
        mock_task_repo: AsyncMock,
        sample_task: Task,
    ) -> None:
        dlq_store = AsyncMock()

        mock_client.add_magnet.side_effect = DownloadError("permanent failure")
        exhausted = sample_task.model_copy(update={"retries": sample_task.max_retries})

        service = MediaLoaderService(
            client=mock_client,
            remuxer=mock_remuxer,
            scraper=None,
            video_repo=mock_video_repo,
            task_repo=mock_task_repo,
            upload_queue_name="pixav:upload",
            dlq_store=dlq_store,
        )
        result = await service.process_task(exhausted)

        assert result.state == TaskState.FAILED
        mock_task_repo.update_state.assert_any_await(
            exhausted.id,
            TaskState.FAILED,
            error_message=result.error_message,
        )
        dlq_store.put.assert_awaited_once()


class TestTitleFallback:
    """Untitled videos must recover a real title from the files on disk."""

    def test_keeps_a_real_existing_title(self) -> None:
        assert _fallback_title("SSIS-123 Real Title", "/downloads/junk.mkv") == "SSIS-123 Real Title"

    def test_falls_back_to_filename_stem(self) -> None:
        assert _fallback_title("Untitled", "/downloads/SSIS-123.mkv") == "SSIS-123"

    def test_skips_placeholder_stems(self) -> None:
        assert _fallback_title("", "/downloads/video.mp4", "/remuxed/SSIS-456.mp4") == "SSIS-456"

    def test_returns_untitled_when_nothing_is_usable(self) -> None:
        assert _fallback_title("", "/downloads/untitled.mkv") == "Untitled"


class TestTechnicalScoringTitle:
    """ffprobe data feeds the scorer the resolution/codec tokens it looks for."""

    @pytest.mark.parametrize(
        ("height", "expected"),
        [(2160, "2160p"), (1080, "1080p"), (720, "720p")],
    )
    def test_height_maps_to_resolution_token(self, height: int, expected: str) -> None:
        scoring_title = _technical_scoring_title("SSIS-123", {"height": height, "codec": "h264"})
        assert expected in scoring_title
        assert "h264" in scoring_title
        assert scoring_title.endswith(".mp4")

    def test_omits_tokens_when_probe_returned_nothing(self) -> None:
        assert _technical_scoring_title("SSIS-123", {}) == "SSIS-123 .mp4"


class TestSourceUnavailable:
    """A dead swarm invalidates the source, not the media item."""

    @pytest.fixture
    def mock_candidate_repo(self) -> AsyncMock:
        repo = AsyncMock()
        repo.mark_unavailable.return_value = None
        repo.mark_succeeded.return_value = None
        repo.next_candidate.return_value = None
        return repo

    def _service(self, mock_client, mock_remuxer, mock_video_repo, mock_task_repo, candidate_repo, dlq=None):
        return MediaLoaderService(
            client=mock_client,
            remuxer=mock_remuxer,
            scraper=None,
            video_repo=mock_video_repo,
            task_repo=mock_task_repo,
            candidate_repo=candidate_repo,
            upload_queue_name="pixav:upload",
            dlq_store=dlq,
            source_cooldown_hours=6,
        )

    async def test_dead_source_is_cooled_down_not_retried_on_the_backoff_ladder(
        self,
        mock_client: AsyncMock,
        mock_remuxer: AsyncMock,
        mock_video_repo: AsyncMock,
        mock_task_repo: AsyncMock,
        mock_candidate_repo: AsyncMock,
        sample_task: Task,
    ) -> None:
        mock_client.wait_complete.side_effect = SourceUnavailableError("classified invalid torrent identity")
        mock_candidate_repo.next_candidate.return_value = None

        service = self._service(mock_client, mock_remuxer, mock_video_repo, mock_task_repo, mock_candidate_repo)
        result = await service.process_task(sample_task)

        mock_candidate_repo.mark_unavailable.assert_awaited_once()
        assert mock_candidate_repo.mark_unavailable.await_args.kwargs["cooldown_hours"] == 6
        # Never walks the six-stage backoff: retrying a dead swarm cannot succeed.
        mock_task_repo.set_retry.assert_not_awaited()
        # The hash was already admitted to qBittorrent before polling found the
        # dead swarm; exception unwinding must not lose it and leak the torrent.
        mock_client.delete_torrent.assert_not_awaited()
        assert result.state == TaskState.FAILED

    async def test_switches_to_the_next_candidate_without_consuming_a_retry(
        self,
        mock_client: AsyncMock,
        mock_remuxer: AsyncMock,
        mock_video_repo: AsyncMock,
        mock_task_repo: AsyncMock,
        mock_candidate_repo: AsyncMock,
        sample_task: Task,
    ) -> None:
        mock_client.wait_complete.side_effect = SourceUnavailableError("classified invalid torrent identity")
        mock_candidate_repo.next_candidate.return_value = SourceCandidate(
            video_id=sample_task.video_id,
            magnet_uri="magnet:?xt=urn:btih:beef",
            info_hash="beef",
        )

        service = self._service(mock_client, mock_remuxer, mock_video_repo, mock_task_repo, mock_candidate_repo)
        result = await service.process_task(sample_task)

        mock_video_repo.update_source.assert_awaited_once()
        assert mock_video_repo.update_source.await_args.kwargs["magnet_uri"] == "magnet:?xt=urn:btih:beef"
        # Re-queued immediately, and the attempt count is untouched: switching
        # sources is new work, not a retry of the same work.
        assert mock_task_repo.set_retry.await_args.args[1] == sample_task.retries
        assert result.state == TaskState.PENDING

    async def test_exhausted_candidates_reach_the_dlq_with_a_distinct_reason(
        self,
        mock_client: AsyncMock,
        mock_remuxer: AsyncMock,
        mock_video_repo: AsyncMock,
        mock_task_repo: AsyncMock,
        mock_candidate_repo: AsyncMock,
        sample_task: Task,
    ) -> None:
        mock_client.wait_complete.side_effect = SourceUnavailableError("classified invalid torrent identity")
        mock_candidate_repo.next_candidate.return_value = None
        dlq = AsyncMock()

        service = self._service(mock_client, mock_remuxer, mock_video_repo, mock_task_repo, mock_candidate_repo, dlq)
        await service.process_task(sample_task)

        dlq.put.assert_awaited_once()
        assert dlq.put.await_args.args[1]["reason"] == "source_candidates_exhausted"

    async def test_torrent_client_outage_still_retries_normally(
        self,
        mock_client: AsyncMock,
        mock_remuxer: AsyncMock,
        mock_video_repo: AsyncMock,
        mock_task_repo: AsyncMock,
        mock_candidate_repo: AsyncMock,
        sample_task: Task,
    ) -> None:
        """A plain DownloadError is transient and must keep the retry ladder."""
        mock_client.add_magnet.side_effect = DownloadError("qBittorrent is down")

        service = self._service(mock_client, mock_remuxer, mock_video_repo, mock_task_repo, mock_candidate_repo)
        result = await service.process_task(sample_task)

        mock_candidate_repo.mark_unavailable.assert_not_awaited()
        mock_task_repo.set_retry.assert_awaited_once()
        assert result.retries == 1

    async def test_the_winning_candidate_is_recorded_on_success(
        self,
        service: MediaLoaderService,
        mock_client: AsyncMock,
        mock_remuxer: AsyncMock,
        mock_video_repo: AsyncMock,
        mock_task_repo: AsyncMock,
        mock_candidate_repo: AsyncMock,
        sample_task: Task,
    ) -> None:
        svc = self._service(mock_client, mock_remuxer, mock_video_repo, mock_task_repo, mock_candidate_repo)
        await svc.process_task(sample_task)

        mock_candidate_repo.mark_succeeded.assert_awaited_once()
