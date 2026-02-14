"""Core service orchestrating pixel_injector upload workflow."""

from __future__ import annotations

import logging

from pixav.pixel_injector.interfaces import FileUploader, RedroidManager, UploadVerifier
from pixav.shared.enums import TaskState
from pixav.shared.exceptions import RedroidError, UploadError, VerificationError
from pixav.shared.models import Task

logger = logging.getLogger(__name__)


class PixelInjectorService:
    """Orchestrate create -> upload -> verify -> destroy for one task."""

    def __init__(self, redroid: RedroidManager, uploader: FileUploader, verifier: UploadVerifier) -> None:
        self.redroid = redroid
        self.uploader = uploader
        self.verifier = verifier

    async def process_task(self, task: Task) -> Task:
        """Process an upload task and return an updated immutable task model."""
        container_id: str | None = None
        task_id = str(task.id)

        if not task.local_path:
            logger.error("task %s is missing local_path", task_id)
            return task.model_copy(
                update={
                    "state": TaskState.FAILED,
                    "error_message": "local_path is required for upload tasks",
                }
            )

        try:
            logger.info("creating redroid container for task %s", task_id)
            container_id = await self.redroid.create(task_id)

            logger.info("waiting for container %s", container_id)
            ready = await self.redroid.wait_ready(container_id, timeout=120)
            if not ready:
                raise RedroidError(f"container {container_id} did not become ready")

            logger.info("pushing %s into container %s", task.local_path, container_id)
            remote_path = await self.uploader.push_file(container_id, task.local_path)

            logger.info("triggering upload for %s in %s", remote_path, container_id)
            await self.uploader.trigger_upload(container_id, remote_path)

            logger.info("waiting for share url from %s", container_id)
            share_url = await self.verifier.wait_for_share_url(container_id, timeout=300)
            is_valid = await self.verifier.validate_share_url(share_url)
            if not is_valid:
                raise VerificationError(f"share url validation failed: {share_url}")

            logger.info("task %s completed", task_id)
            return task.model_copy(
                update={
                    "state": TaskState.COMPLETE,
                    "share_url": share_url,
                    "error_message": None,
                }
            )
        except (RedroidError, UploadError, VerificationError) as exc:
            logger.error("task %s failed: %s", task_id, exc)
            return task.model_copy(
                update={
                    "state": TaskState.FAILED,
                    "error_message": str(exc),
                }
            )
        finally:
            if container_id is not None:
                try:
                    logger.info("destroying container %s for task %s", container_id, task_id)
                    await self.redroid.destroy(container_id)
                except Exception as exc:  # pragma: no cover - defensive cleanup log
                    logger.error("failed to destroy container %s: %s", container_id, exc)
