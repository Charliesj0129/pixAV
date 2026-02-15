"""Core service orchestrating pixel_injector upload workflow."""

from __future__ import annotations

import asyncio
import logging
import os

from pixav.pixel_injector.interfaces import FileUploader, RedroidManager, UploadVerifier
from pixav.pixel_injector.session import RedroidSession
from pixav.shared.enums import TaskState
from pixav.shared.exceptions import RedroidError, UploadError, VerificationError
from pixav.shared.models import Task

logger = logging.getLogger(__name__)


class PixelInjectorService:
    """Orchestrate create -> upload -> verify -> destroy for one task."""

    def __init__(
        self,
        redroid: RedroidManager,
        uploader: FileUploader,
        verifier: UploadVerifier,
        *,
        ready_timeout_seconds: int = 120,
        verify_timeout_seconds: int = 300,
        task_timeout_seconds: int = 3600,
    ) -> None:
        self.redroid = redroid
        self.uploader = uploader
        self.verifier = verifier
        self._ready_timeout_seconds = ready_timeout_seconds
        self._verify_timeout_seconds = verify_timeout_seconds
        self._task_timeout_seconds = task_timeout_seconds

    async def process_task(self, task: Task) -> Task:
        """Process an upload task and return an updated immutable task model."""
        session: RedroidSession | None = None
        task_id = str(task.id)

        if not task.local_path:
            logger.error("task %s is missing local_path", task_id)
            return task.model_copy(
                update={
                    "state": TaskState.FAILED,
                    "error_message": "local_path is required for upload tasks",
                }
            )
        local_path = task.local_path

        async def _execute_pipeline() -> Task:
            nonlocal session
            logger.info("creating redroid container for task %s", task_id)
            session = await self.redroid.create(task_id)

            logger.info(
                "waiting for container %s (adb=%s:%d)",
                session.container_id,
                session.adb_host,
                session.adb_port,
            )
            ready = await self.redroid.wait_ready(session.container_id, timeout=self._ready_timeout_seconds)
            if not ready:
                raise RedroidError(f"container {session.container_id} did not become ready")

            logger.info("pushing %s into container %s", local_path, session.container_id)
            remote_path = await self.uploader.push_file(session, local_path)

            logger.info("triggering upload for %s in %s", remote_path, session.container_id)
            await self.uploader.trigger_upload(session, remote_path)

            logger.info("waiting for share url from %s", session.container_id)
            share_url = await self.verifier.wait_for_share_url(session, timeout=self._verify_timeout_seconds)
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

        try:
            return await asyncio.wait_for(_execute_pipeline(), timeout=self._task_timeout_seconds)
        except asyncio.TimeoutError:
            logger.error("task %s timed out after %ds", task_id, self._task_timeout_seconds)
            return task.model_copy(
                update={
                    "state": TaskState.FAILED,
                    "error_message": f"upload timed out after {self._task_timeout_seconds}s",
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
            if session is not None:
                try:
                    logger.info("destroying container %s for task %s", session.container_id, task_id)
                    await self.redroid.destroy(session.container_id)
                except Exception as exc:  # pragma: no cover - defensive cleanup log
                    logger.error("failed to destroy container %s: %s", session.container_id, exc)


class LocalPixelInjectorService:
    """Dev/test uploader: marks upload complete without Redroid/ADB automation.

    This is intended for end-to-end pipeline verification on a single host.
    It emits a synthetic share_url that can be consumed by strm_resolver.
    """

    def __init__(self, *, share_scheme: str = "pixav-local://") -> None:
        scheme = share_scheme.strip()
        if not scheme:
            scheme = "pixav-local://"
        self._share_scheme = scheme

    async def process_task(self, task: Task) -> Task:
        if not task.local_path:
            return task.model_copy(
                update={
                    "state": TaskState.FAILED,
                    "error_message": "local_path is required for upload tasks",
                }
            )
        if not os.path.isfile(task.local_path):
            return task.model_copy(
                update={
                    "state": TaskState.FAILED,
                    "error_message": f"local_path does not exist: {task.local_path}",
                }
            )

        share_url = f"{self._share_scheme}{task.video_id}"
        return task.model_copy(
            update={
                "state": TaskState.COMPLETE,
                "share_url": share_url,
                "error_message": None,
            }
        )
