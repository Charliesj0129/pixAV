"""Protocol interfaces for pixel_injector dependency injection."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pixav.pixel_injector.session import RedroidSession
from pixav.shared.models import Task


@runtime_checkable
class RedroidManager(Protocol):
    """Protocol for managing Redroid container lifecycle."""

    async def create(self, task_id: str) -> RedroidSession:
        """Create a new Redroid container for the given task.

        Args:
            task_id: Unique identifier for the upload task

        Returns:
            Runtime session info (container + ADB endpoint)

        Raises:
            RedroidError: If container creation fails
        """
        ...

    async def destroy(self, container_id: str) -> None:
        """Destroy a Redroid container.

        Args:
            container_id: ID of container to destroy

        Raises:
            RedroidError: If container destruction fails
        """
        ...

    async def wait_ready(self, container_id: str, timeout: int = 120) -> bool:
        """Wait for container to be ready for use.

        Args:
            container_id: ID of container to wait for
            timeout: Maximum seconds to wait

        Returns:
            True if container became ready, False if timeout

        Raises:
            RedroidError: If error occurs while waiting
        """
        ...


@runtime_checkable
class FileUploader(Protocol):
    """Protocol for uploading files to Redroid container."""

    async def push_file(self, session: RedroidSession, local_path: str) -> str:
        """Push a file to the container.

        Args:
            session: Active Redroid session
            local_path: Path to local file

        Returns:
            Remote path where file was pushed

        Raises:
            UploadError: If file push fails
        """
        ...

    async def trigger_upload(self, session: RedroidSession, remote_path: str) -> None:
        """Trigger Google Photos upload for a file in the container.

        Args:
            session: Active Redroid session
            remote_path: Path to file within container

        Raises:
            UploadError: If upload trigger fails
        """
        ...


@runtime_checkable
class UploadVerifier(Protocol):
    """Protocol for verifying Google Photos uploads."""

    async def wait_for_share_url(self, session: RedroidSession, timeout: int = 300) -> str:
        """Wait for and extract the Google Photos share URL.

        Args:
            session: Active Redroid session
            timeout: Maximum seconds to wait

        Returns:
            Share URL string

        Raises:
            VerificationError: If share URL not found or timeout
        """
        ...

    async def validate_share_url(self, share_url: str) -> bool:
        """Validate that a share URL is accessible.

        Args:
            share_url: URL to validate

        Returns:
            True if URL is valid and accessible, False otherwise
        """
        ...


@runtime_checkable
class PixelInjector(Protocol):
    """Protocol for upload orchestration services (real or local/dev)."""

    async def process_task(self, task: Task) -> Task: ...
