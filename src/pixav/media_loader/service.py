"""Media-Loader service for torrent processing pipeline."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from pixav.media_loader.interfaces import MetadataScraper, Remuxer, TorrentClient
from pixav.shared.enums import TaskState, VideoStatus
from pixav.shared.models import Task
from pixav.shared.queue import TaskQueue
from pixav.shared.repository import TaskRepository, VideoRepository

logger = logging.getLogger(__name__)


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
        upload_queue_name: str = "pixav:upload",
        retry_queue: TaskQueue | None = None,
        dlq_queue: TaskQueue | None = None,
        output_dir: str = "./data/remuxed",
        mode: str = "full",
    ) -> None:
        self._client = client
        self._remuxer = remuxer
        self._scraper = scraper
        self._video_repo = video_repo
        self._task_repo = task_repo
        self._upload_queue_name = upload_queue_name
        self._retry_queue = retry_queue
        self._dlq_queue = dlq_queue
        self._output_dir = output_dir
        self._mode = mode.strip().lower()

    async def process_task(self, task: Task) -> Task:
        """Process a single download task from the crawl queue."""
        video = await self._video_repo.find_by_id(task.video_id)
        if not video:
            return self._fail_task(task, f"video {task.video_id} not found in DB", fatal=True)
        if not video.magnet_uri:
            return self._fail_task(task, "video has no magnet_uri", fatal=True)

        # Fast path: resume a previously downloaded/remuxed video.
        if video.local_path and os.path.isfile(video.local_path):
            await self._video_repo.update_status(task.video_id, VideoStatus.DOWNLOADED)
            return await self._route_to_upload(task, video, video.local_path)

        try:
            if self._mode == "verify":
                return await self._process_verify(task, video.magnet_uri, video)

            # 1. Download
            torrent_hash, download_path = await self._download(task, video.magnet_uri)

            # 2. Remux
            from pixav.media_loader.remuxer import FFmpegRemuxer

            output_path = FFmpegRemuxer.make_output_path(download_path, self._output_dir)
            await self._remux(task, download_path, output_path)

            # 2a. Cleanup
            await self._cleanup(torrent_hash)

            # 3. Metadata
            metadata_json = await self._scrape_metadata(video.title)

            # 4. Update & Route
            await self._video_repo.update_download_result(
                task.video_id, local_path=output_path, metadata_json=metadata_json
            )
            return await self._route_to_upload(task, video, output_path)

        except Exception as exc:
            return await self._handle_processing_error(task, exc)

    async def _process_verify(self, task: Task, magnet_uri: str, video: Any) -> Task:
        """Verify qBittorrent connectivity without performing a full download."""
        await self._task_repo.update_state(task.id, TaskState.DOWNLOADING)
        await self._video_repo.update_status(task.video_id, VideoStatus.DOWNLOADING)

        torrent_hash = await self._client.add_magnet(magnet_uri)
        await self._cleanup(torrent_hash)

        # Create a small placeholder file so downstream upload/stream stages can run.
        out_path = str(Path(self._output_dir) / f"verify-{task.id}.mp4")
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_bytes(b"0" * 1024)

        await self._video_repo.update_download_result(task.video_id, local_path=out_path, metadata_json=None)
        return await self._route_to_upload(task, video, out_path)

    async def _download(self, task: Task, magnet_uri: str) -> tuple[str, str]:
        """Execute download phase."""
        await self._task_repo.update_state(task.id, TaskState.DOWNLOADING)
        await self._video_repo.update_status(task.video_id, VideoStatus.DOWNLOADING)

        torrent_hash = await self._client.add_magnet(magnet_uri)
        download_path = await self._client.wait_complete(torrent_hash)
        return torrent_hash, download_path

    async def _remux(self, task: Task, input_path: str, output_path: str) -> None:
        """Execute remux phase."""
        await self._task_repo.update_state(task.id, TaskState.REMUXING)
        await self._remuxer.remux(input_path, output_path)

    async def _cleanup(self, torrent_hash: str) -> None:
        """Execute cleanup phase."""
        try:
            await self._client.delete_torrent(torrent_hash, delete_files=True)
        except Exception as exc:
            logger.warning("failed to delete torrent %s: %s", torrent_hash, exc)

    async def _scrape_metadata(self, title: str) -> str | None:
        """Execute metadata scraping phase (best-effort)."""
        if not self._scraper:
            return None
        try:
            metadata = await self._scraper.scrape(title)
            return json.dumps(metadata)
        except Exception as exc:
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

    def _fail_task(self, task: Task, msg: str, fatal: bool = False) -> Task:
        """Immediate failure helper (non-async for pre-checks)."""
        # Note: In a real app we might want to update DB here, but strict
        # signature expects Task return. The calling code handles state if needed.
        # But for 'fatal' pre-checks (no video/magnet), we usually just return FAILED state.
        return task.model_copy(update={"state": TaskState.FAILED, "error_message": msg})

    async def _handle_processing_error(self, task: Task, exc: Exception) -> Task:
        """Handle exceptions with retry/DLQ logic."""
        error_msg = f"{type(exc).__name__}: {exc}"
        next_retry = task.retries + 1

        if self._retry_queue and next_retry <= task.max_retries:
            await self._task_repo.set_retry(task.id, next_retry, state=TaskState.PENDING, error_message=error_msg)
            await self._video_repo.update_status(task.video_id, VideoStatus.DISCOVERED)
            await self._retry_queue.push(
                {
                    "task_id": str(task.id),
                    "video_id": str(task.video_id),
                    "queue_name": task.queue_name or "pixav:download",
                    "retries": next_retry,
                    "max_retries": task.max_retries,
                }
            )
            logger.warning(
                "task %s failed (attempt %d/%d), requeued: %s",
                task.id,
                next_retry,
                task.max_retries,
                error_msg,
            )
            return task.model_copy(
                update={"state": TaskState.PENDING, "retries": next_retry, "error_message": error_msg}
            )

        # Fatal / Exhausted
        logger.error("task %s failed permanently: %s", task.id, error_msg)
        await self._task_repo.update_state(task.id, TaskState.FAILED, error_message=error_msg)
        await self._video_repo.update_status(task.video_id, VideoStatus.FAILED)

        if self._dlq_queue:
            await self._dlq_queue.push(
                {
                    "task_id": str(task.id),
                    "video_id": str(task.video_id),
                    "stage": "download",
                    "attempts": task.retries,
                    "max_retries": task.max_retries,
                    "error_message": error_msg,
                }
            )
        return task.model_copy(update={"state": TaskState.FAILED, "error_message": error_msg})
