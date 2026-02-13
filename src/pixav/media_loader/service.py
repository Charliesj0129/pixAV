"""Media-Loader service for torrent processing pipeline."""

from __future__ import annotations

import logging

from pixav.media_loader.interfaces import MetadataScraper, Remuxer, TorrentClient
from pixav.shared.enums import TaskState, VideoStatus
from pixav.shared.models import Task
from pixav.shared.queue import TaskQueue
from pixav.shared.repository import TaskRepository, VideoRepository

logger = logging.getLogger(__name__)


class MediaLoaderService:
    """Orchestrates the download → remux → metadata → enqueue-upload pipeline.

    Flow per task:
    1. Fetch video from DB (get magnet_uri)
    2. Add magnet to torrent client → wait for download
    3. Remux downloaded file to MP4
    4. (Optional) Scrape metadata from Stash
    5. Update video in DB (local_path, metadata, status)
    6. Push to upload queue for Pixel-Injector
    """

    def __init__(
        self,
        *,
        client: TorrentClient,
        remuxer: Remuxer,
        scraper: MetadataScraper | None = None,
        video_repo: VideoRepository,
        task_repo: TaskRepository,
        upload_queue: TaskQueue,
        output_dir: str = "./data/remuxed",
    ) -> None:
        self._client = client
        self._remuxer = remuxer
        self._scraper = scraper
        self._video_repo = video_repo
        self._task_repo = task_repo
        self._upload_queue = upload_queue
        self._output_dir = output_dir

    async def process_task(self, task: Task) -> Task:
        """Process a single download task from the crawl queue.

        Args:
            task: Task containing video_id and magnet_uri info.

        Returns:
            Updated task with final state (COMPLETE or FAILED).
        """
        video = await self._video_repo.find_by_id(task.video_id)
        if video is None:
            return task.model_copy(
                update={
                    "state": TaskState.FAILED,
                    "error_message": f"video {task.video_id} not found in DB",
                }
            )

        if not video.magnet_uri:
            return task.model_copy(
                update={
                    "state": TaskState.FAILED,
                    "error_message": "video has no magnet_uri",
                }
            )

        try:
            # 1. Download
            await self._task_repo.update_state(task.id, TaskState.DOWNLOADING)
            await self._video_repo.update_status(task.video_id, VideoStatus.DOWNLOADING)

            torrent_hash = await self._client.add_magnet(video.magnet_uri)
            download_path = await self._client.wait_complete(torrent_hash)

            # 2. Remux
            await self._task_repo.update_state(task.id, TaskState.REMUXING)
            from pixav.media_loader.remuxer import FFmpegRemuxer

            output_path = FFmpegRemuxer.make_output_path(download_path, self._output_dir)
            await self._remuxer.remux(download_path, output_path)

            # 3. Metadata (optional, best-effort)
            if self._scraper:
                try:
                    await self._scraper.scrape(video.title)
                except Exception as exc:
                    logger.warning("metadata scrape failed for %s: %s", video.title, exc)

            # 4. Update video in DB
            await self._video_repo.update_status(task.video_id, VideoStatus.DOWNLOADED)
            # Note: to update local_path and metadata, we'd need to extend
            # the repository or use raw SQL. For now we update status only.

            # 5. Push to upload queue
            await self._upload_queue.push(
                {
                    "task_id": str(task.id),
                    "video_id": str(task.video_id),
                    "local_path": output_path,
                    "title": video.title,
                }
            )

            await self._task_repo.update_state(task.id, TaskState.COMPLETE)
            logger.info("task %s complete: %s → %s", task.id, video.magnet_uri[:40], output_path)

            return task.model_copy(
                update={
                    "state": TaskState.COMPLETE,
                    "local_path": output_path,
                }
            )

        except Exception as exc:
            error_msg = f"{type(exc).__name__}: {exc}"
            logger.error("task %s failed: %s", task.id, error_msg)
            await self._task_repo.update_state(task.id, TaskState.FAILED, error_message=error_msg)
            await self._video_repo.update_status(task.video_id, VideoStatus.FAILED)

            return task.model_copy(
                update={
                    "state": TaskState.FAILED,
                    "error_message": error_msg,
                }
            )
