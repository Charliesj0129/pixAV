"""File upload implementation using ADB + UIAutomator."""

from __future__ import annotations

import logging

from pixav.pixel_injector.adb import AdbConnection
from pixav.pixel_injector.session import RedroidSession
from pixav.shared.exceptions import UploadError

logger = logging.getLogger(__name__)

# Default remote path in Android container for media
_REMOTE_MEDIA_DIR = "/sdcard/DCIM/Camera"


class UIAutomatorUploader:
    """ADB+UIAutomator-based implementation of FileUploader protocol.

    Pushes files into the Redroid container via ADB, then triggers
    a media scan so Google Photos picks up the file.
    """

    def __init__(
        self,
        adb: AdbConnection,
    ) -> None:
        self._adb = adb

    async def _ensure_connected(self, session: RedroidSession) -> None:
        """Ensure adb has an active target before issuing commands."""
        await self._adb.connect(session.adb_host, session.adb_port)

    async def push_file(self, session: RedroidSession, local_path: str) -> str:
        """Push a file to the container via ADB.

        Args:
            session: Target Redroid session.
            local_path: Path to local file.

        Returns:
            Remote path where file was pushed.

        Raises:
            UploadError: If push fails.
        """
        import os

        filename = os.path.basename(local_path)
        remote_path = f"{_REMOTE_MEDIA_DIR}/{filename}"

        try:
            await self._ensure_connected(session)
            await self._adb.push(local_path, remote_path)
        except Exception as exc:
            raise UploadError(f"failed to push {local_path} to {session.container_id}: {exc}") from exc

        logger.info("pushed %s → %s in container %s", local_path, remote_path, session.container_id[:12])
        return remote_path

    async def trigger_upload(self, session: RedroidSession, remote_path: str) -> None:
        """Trigger media scan and Google Photos upload for a file.

        Sends a media scanner broadcast so Google Photos discovers the file.

        Args:
            session: Target Redroid session.
            remote_path: Path to file within container.

        Raises:
            UploadError: If triggering upload fails.
        """
        try:
            await self._ensure_connected(session)
            # Trigger Android media scanner
            scan_cmd = f"am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE " f'-d "file://{remote_path}"'
            await self._adb.shell(scan_cmd)
            logger.info("triggered media scan for %s in %s", remote_path, session.container_id[:12])
        except Exception as exc:
            raise UploadError(f"failed to trigger upload in {session.container_id}: {exc}") from exc
