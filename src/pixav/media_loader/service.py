"""Media-Loader service for torrent processing pipeline."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pixav.media_loader.interfaces import MetadataScraper, Remuxer, TorrentClient
from pixav.media_loader.metadata import probe_media
from pixav.shared.dead_letter import DeadLetterStore
from pixav.shared.enums import TaskState, VideoStatus
from pixav.shared.exceptions import SourceUnavailableError
from pixav.shared.models import Task
from pixav.shared.phase0_timing import phase0_span
from pixav.shared.repository import SourceCandidateRepository, TaskRepository, VideoRepository
from pixav.shared.retry import DEFAULT_RETRY_BACKOFF_SECONDS, retry_deadline
from pixav.sht_probe.scoring import QualityScorer

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class MediaLoaderService:
    """Orchestrates the download → remux → metadata → route-to-upload pipeline.

    Flow per task:
    1. Fetch video from DB (get magnet_uri)
    2. Add magnet to torrent client → wait for download
    3. Remux downloaded file to MP4
    4. (Optional) Scrape metadata from Stash
    5. Update video in DB (local_path, metadata, status)
    6. Route task to upload stage (`pending` + queue=`pixav:upload`)
    """

    def __init__(
        self,
        *,
        client: TorrentClient,
        remuxer: Remuxer,
        scraper: MetadataScraper | None = None,
        video_repo: VideoRepository,
        task_repo: TaskRepository,
        candidate_repo: SourceCandidateRepository | None = None,
        upload_queue_name: str = "pixav:upload",
        dlq_store: DeadLetterStore | None = None,
        source_cooldown_hours: int = 6,
        retry_backoff_seconds: tuple[int, ...] = DEFAULT_RETRY_BACKOFF_SECONDS,
        failure_retention_days: int = 7,
        output_dir: str = "./data/remuxed",
        mode: str = "full",
    ) -> None:
        self._client = client
        self._remuxer = remuxer
        self._scraper = scraper
        self._video_repo = video_repo
        self._task_repo = task_repo
        self._candidate_repo = candidate_repo
        self._upload_queue_name = upload_queue_name
        self._source_cooldown_hours = source_cooldown_hours
        self._dlq_store = dlq_store
        self._retry_backoff_seconds = retry_backoff_seconds
        self._failure_retention_days = failure_retention_days
        self._output_dir = output_dir
        self._mode = mode.strip().lower()

    async def process_task(self, task: Task) -> Task:
        """Process a single download task from the crawl queue."""
        if await self._task_repo.is_managed_video(task.video_id):
            raise ValueError("legacy media task refused: video belongs to managed execution")
        video = await self._video_repo.find_by_id(task.video_id)
        if not video:
            return await self._fail_task(task, f"video {task.video_id} not found in DB")
        if not video.magnet_uri:
            return await self._fail_task(task, "video has no magnet_uri", persist_video_failure=True)

        torrent_hash: str | None = None
        cleaned_up = False

        try:
            if self._mode == "verify":
                return await self._process_verify(task, video.magnet_uri, video)

            if video.local_path:
                from pixav.media_loader.preparation import PreparationPolicy, inspect_media

                facts = await inspect_media(video.local_path)
                PreparationPolicy().validate_output(facts, facts)
                return await self._route_to_upload(task, video, video.local_path)

            # 1. Download
            await self._task_repo.update_state(task.id, TaskState.DOWNLOADING)
            await self._video_repo.update_status(task.video_id, VideoStatus.DOWNLOADING)
            # Keep ownership of the hash in this frame before polling. If
            # wait_complete() raises, assigning a tuple returned by a helper
            # never happens and the exception path cannot know which torrent to
            # remove — the same lost-on-unwind leak fixed for metadata probes.
            with phase0_span(task.id, task.video_id, "download"):
                torrent_hash = await self._client.add_magnet(video.magnet_uri)
                download_path = await self._client.wait_complete(torrent_hash)

            # 2. Remux
            from pixav.media_loader.remuxer import FFmpegRemuxer, select_media_input

            media_input = select_media_input(download_path)
            output_path = FFmpegRemuxer.make_output_path(
                media_input,
                self._output_dir,
                unique_key=str(task.video_id),
            )
            output_path = await self._remux(task, media_input, output_path)

            # 2a. Cleanup
            await self._cleanup(torrent_hash)
            cleaned_up = True

            # 3. Metadata + 4. Update & Route
            await self._persist_download(task, video, download_path, output_path, torrent_hash)
            return await self._route_to_upload(task, video, output_path)

        except SourceUnavailableError as exc:
            # Not a transient failure: this magnet's swarm cannot deliver. Walking
            # the retry backoff against it would burn six attempts over ~1.5 days
            # on a source that is structurally dead, so cool the candidate down
            # and move to the next source for the same media item.
            if torrent_hash and not cleaned_up:
                await self._cleanup(torrent_hash)
            return await self._handle_source_unavailable(task, video.magnet_uri, exc)

        except Exception as exc:
            # If we already created a torrent but failed before the normal cleanup
            # point (e.g. remux crash), attempt best-effort cleanup to avoid
            # leaving qBittorrent state/files behind.
            if torrent_hash and not cleaned_up:
                await self._cleanup(torrent_hash)
            return await self._handle_processing_error(task, exc)

    async def _persist_download(
        self,
        task: Task,
        video: Any,
        download_path: str,
        output_path: str,
        torrent_hash: str,
    ) -> None:
        """Probe the result, score it, and record which source delivered it."""
        media_metadata = await probe_media(output_path)
        fallback_title = _fallback_title(video.title, download_path, output_path)
        stash_metadata = await self._scrape_metadata(fallback_title)
        torrent_name = Path(download_path).name.strip()
        metadata: dict[str, Any] = {
            "torrent": {"name": torrent_name or None, "info_hash": torrent_hash},
            "media": media_metadata,
        }
        if stash_metadata is not None:
            metadata["stash"] = stash_metadata
        scoring_title = _technical_scoring_title(fallback_title, media_metadata)
        score = QualityScorer().score(
            scoring_title,
            size_bytes=int(media_metadata.get("size_bytes") or 0),
        )

        await self._video_repo.update_download_result(
            task.video_id,
            local_path=output_path,
            metadata_json=json.dumps(metadata),
            title=fallback_title,
            quality_score=score,
        )
        if self._candidate_repo is not None:
            await self._candidate_repo.mark_succeeded(task.video_id, video.magnet_uri)

    async def _process_verify(self, task: Task, magnet_uri: str, video: Any) -> Task:
        """Verify qBittorrent connectivity without performing a full download."""
        await self._client.health_check()
        return await self._fail_task(task, "verify is connectivity-only; no upload artifact created")

    async def _remux(self, task: Task, input_path: str, output_path: str) -> str:
        """Execute remux phase."""
        await self._task_repo.update_state(task.id, TaskState.REMUXING)
        with phase0_span(task.id, task.video_id, "remux"):
            from pixav.media_loader.preparation import prepare_media

            artifact = await prepare_media(input_path, output_path, self._remuxer)
            return artifact.path

    async def _cleanup(self, torrent_hash: str) -> None:
        """Execute cleanup phase."""
        # Legacy calls have no durable ownership identity. Preserve state and
        # files until reconciliation can establish ownership.
        logger.info("retaining torrent for reconciliation: %s", torrent_hash)

    async def _scrape_metadata(self, title: str) -> dict[str, Any] | None:
        """Execute metadata scraping phase (best-effort)."""
        if not self._scraper:
            return None
        try:
            metadata = await self._scraper.scrape(title)
            return metadata
        except Exception as exc:
            from pixav.shared.metrics import record_stash_failure

            record_stash_failure()
            logger.warning("metadata scrape failed for %s: %s", title, exc)
            return None

    async def _route_to_upload(self, task: Task, video: Any, output_path: str) -> Task:
        """Route successful task to upload queue."""
        await self._task_repo.route_to_queue(
            task.id,
            queue_name=self._upload_queue_name,
            state=TaskState.PENDING,
        )
        logger.info(
            "task %s routed to upload queue %s: %s → %s",
            task.id,
            self._upload_queue_name,
            video.magnet_uri[:40],
            output_path,
        )
        return task.model_copy(
            update={
                "state": TaskState.PENDING,
                "queue_name": self._upload_queue_name,
                "local_path": output_path,
            }
        )

    async def _fail_task(self, task: Task, msg: str, *, persist_video_failure: bool = False) -> Task:
        """Persist and return an immediate FAILED task result."""
        await self._task_repo.update_state(task.id, TaskState.FAILED, error_message=msg)
        if persist_video_failure:
            await self._video_repo.update_status(task.video_id, VideoStatus.FAILED)
        return task.model_copy(update={"state": TaskState.FAILED, "error_message": msg})

    async def _handle_source_unavailable(self, task: Task, magnet_uri: str, exc: Exception) -> Task:
        """Cool the dead source down and switch the media item to its next source."""
        error_msg = f"{type(exc).__name__}: {exc}"

        if self._candidate_repo is None:
            # No candidate table wired in: still refuse the retry ladder, because
            # retrying a dead swarm on the same magnet cannot succeed.
            logger.warning("task %s source unavailable (no candidate repo): %s", task.id, error_msg)
            return await self._fail_task(task, error_msg, persist_video_failure=True)

        await self._candidate_repo.mark_unavailable(
            task.video_id,
            magnet_uri,
            reason=error_msg,
            cooldown_hours=self._source_cooldown_hours,
        )

        nxt = await self._candidate_repo.next_candidate(task.video_id, exclude_magnet=magnet_uri)
        if nxt is None:
            logger.error("task %s: every source candidate is unavailable: %s", task.id, error_msg)
            failed = await self._fail_task(task, error_msg, persist_video_failure=True)
            if self._dlq_store:
                await self._dlq_store.put(
                    "download",
                    {
                        "task_id": str(task.id),
                        "video_id": str(task.video_id),
                        "stage": "download",
                        "attempts": task.retries,
                        "max_retries": task.max_retries,
                        "error_message": error_msg,
                        "reason": "source_candidates_exhausted",
                        "trace_id": task.trace_id,
                    },
                )
            return failed

        # Switching sources is not a retry of the same work, so the attempt count
        # is left alone and the task becomes due immediately.
        await self._video_repo.update_source(
            task.video_id,
            magnet_uri=nxt.magnet_uri,
            info_hash=nxt.info_hash,
        )
        await self._task_repo.set_retry(
            task.id,
            task.retries,
            state=TaskState.PENDING,
            error_message=error_msg,
            retry_not_before=_utc_now(),
        )
        await self._video_repo.update_status(task.video_id, VideoStatus.DISCOVERED)
        logger.warning("task %s switched to next source candidate: %s", task.id, error_msg)
        return task.model_copy(update={"state": TaskState.PENDING, "error_message": error_msg})

    async def _handle_processing_error(self, task: Task, exc: Exception) -> Task:
        """Handle exceptions with retry/DLQ logic."""
        error_msg = f"{type(exc).__name__}: {exc}"
        next_retry = task.retries + 1

        if next_retry <= task.max_retries:
            due = retry_deadline(
                next_retry,
                backoff_seconds=self._retry_backoff_seconds,
            )
            await self._task_repo.set_retry(
                task.id,
                next_retry,
                state=TaskState.PENDING,
                error_message=error_msg,
                retry_not_before=due,
            )
            await self._video_repo.update_status(task.video_id, VideoStatus.DISCOVERED)
            logger.warning(
                "task %s failed (retry %d/%d), due at %s: %s",
                task.id,
                next_retry,
                task.max_retries,
                due.isoformat(),
                error_msg,
            )
            return task.model_copy(
                update={"state": TaskState.PENDING, "retries": next_retry, "error_message": error_msg}
            )

        # Fatal / Exhausted
        logger.error("task %s failed permanently: %s", task.id, error_msg)
        await self._task_repo.update_state(task.id, TaskState.FAILED, error_message=error_msg)
        await self._video_repo.update_status(task.video_id, VideoStatus.FAILED)
        schedule_cleanup = getattr(self._video_repo, "schedule_terminal_cleanup", None)
        if callable(schedule_cleanup):
            await schedule_cleanup(task.video_id, retention_days=self._failure_retention_days)

        payload = {
            "task_id": str(task.id),
            "video_id": str(task.video_id),
            "stage": "download",
            "attempts": task.retries,
            "max_retries": task.max_retries,
            "error_message": error_msg,
            "trace_id": task.trace_id,
        }
        if self._dlq_store:
            await self._dlq_store.put("download", payload)
        return task.model_copy(update={"state": TaskState.FAILED, "error_message": error_msg})


def _fallback_title(current_title: str, *paths: str) -> str:
    if current_title.strip() and current_title.strip().lower() != "untitled":
        return current_title.strip()
    for path in paths:
        stem = Path(path).stem.strip()
        if stem and stem.lower() not in {"untitled", "video"}:
            return stem
    return current_title.strip() or "Untitled"


def _technical_scoring_title(title: str, media: dict[str, Any]) -> str:
    parts = [title]
    height = int(media.get("height") or 0)
    if height >= 2160:
        parts.append("2160p")
    elif height >= 1080:
        parts.append("1080p")
    elif height >= 720:
        parts.append("720p")
    codec = str(media.get("codec") or "")
    if codec:
        parts.append(codec)
    parts.append(".mp4")
    return " ".join(parts)
