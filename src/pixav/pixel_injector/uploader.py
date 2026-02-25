"""File upload implementation using ADB + UIAutomator."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pixav.pixel_injector.adb import AdbConnection
from pixav.pixel_injector.session import RedroidSession
from pixav.shared.exceptions import UploadError

if TYPE_CHECKING:
    from pixav.shared.models import Account

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

    async def login(self, session: RedroidSession, account: Account) -> None:
        """Perform automated login for the given account using ADB keyevents."""
        import asyncio
        password = account.password
        if not password:
            raise UploadError("account password not provided")

        try:
            await self._ensure_connected(session)
            
            logger.info("launching Google Accounts login for %s in %s", account.email, session.container_id[:12])
            await self._adb.shell("am start -a android.settings.ADD_ACCOUNT_SETTINGS -e account_types com.google")
            await asyncio.sleep(8)
            
            logger.info("inputting email for %s", session.container_id[:12])
            email_escaped = account.email.replace("'", "'\\''")
            await self._adb.shell(f"input text '{email_escaped}'")
            await asyncio.sleep(1)
            await self._adb.shell("input keyevent 66")  # ENTER
            await asyncio.sleep(8)
            
            logger.info("inputting password for %s", session.container_id[:12])
            pwd_escaped = password.replace("'", "'\\''")
            await self._adb.shell(f"input text '{pwd_escaped}'")
            await asyncio.sleep(1)
            await self._adb.shell("input keyevent 66")  # ENTER
            await asyncio.sleep(8)
            
            logger.info("accepting terms for %s", session.container_id[:12])
            # Navigate to 'I agree' button and press Enter
            await self._adb.shell("input keyevent 61")  # TAB
            await asyncio.sleep(0.5)
            await self._adb.shell("input keyevent 61")  # TAB
            await asyncio.sleep(0.5)
            await self._adb.shell("input keyevent 61")  # TAB
            await asyncio.sleep(0.5)
            await self._adb.shell("input keyevent 66")  # ENTER
            
            # Wait for sync screens
            await asyncio.sleep(10)
            
            logger.info("returning to home screen %s", session.container_id[:12])
            await self._adb.shell("input keyevent 3")  # HOME
            
        except Exception as exc:
            raise UploadError(f"failed to execute login automation in {session.container_id}: {exc}") from exc

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
            scan_cmd = f'am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d "file://{remote_path}"'
            await self._adb.shell(scan_cmd)
            logger.info("triggered media scan for %s in %s", remote_path, session.container_id[:12])
        except Exception as exc:
            raise UploadError(f"failed to trigger upload in {session.container_id}: {exc}") from exc
