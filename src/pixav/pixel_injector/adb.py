"""ADB connection management for Redroid containers."""

from __future__ import annotations

import asyncio
import logging

from pixav.shared.exceptions import AdbError

logger = logging.getLogger(__name__)


class AdbConnection:
    """Manages ADB connection to Redroid Android containers.

    Uses ``adb`` CLI through async subprocess calls.
    """

    def __init__(self, *, adb_bin: str = "adb", timeout: int = 30) -> None:
        self._adb_bin = adb_bin
        self._timeout = timeout
        self._target: str | None = None

    async def connect(self, host: str, port: int) -> None:
        """Connect to ADB daemon on container.

        Args:
            host: Container hostname or IP.
            port: ADB port (typically 5555).

        Raises:
            AdbError: If connection fails.
        """
        self._target = f"{host}:{port}"
        stdout, stderr, rc = await self._run("connect", self._target)
        if rc != 0 or "cannot" in stdout.lower():
            raise AdbError(f"ADB connect failed to {self._target}: {stdout} {stderr}")
        logger.info("ADB connected to %s", self._target)

    async def push(self, local: str, remote: str) -> None:
        """Push file to container via ADB.

        Args:
            local: Local file path.
            remote: Remote destination path in container.

        Raises:
            AdbError: If push fails or no active connection.
        """
        target = self._target_or_raise()
        stdout, stderr, rc = await self._run("-s", target, "push", local, remote)
        if rc != 0:
            raise AdbError(f"ADB push failed: {stderr}")
        logger.info("pushed %s → %s on %s", local, remote, target)

    async def shell(self, cmd: str) -> str:
        """Execute shell command in container.

        Args:
            cmd: Shell command to execute.

        Returns:
            Command output (stdout).

        Raises:
            AdbError: If command fails or no active connection.
        """
        target = self._target_or_raise()
        stdout, stderr, rc = await self._run("-s", target, "shell", cmd)
        if rc != 0:
            raise AdbError(f"ADB shell failed (rc={rc}): {stderr}")
        return stdout

    def _target_or_raise(self) -> str:
        if self._target is None:
            raise AdbError("not connected — call connect() first")
        return self._target

    async def _run(self, *args: str) -> tuple[str, str, int]:
        """Run an ADB command and return (stdout, stderr, returncode)."""
        cmd = [self._adb_bin, *args]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=self._timeout)
        except asyncio.TimeoutError as exc:
            raise AdbError(f"ADB command timed out: {' '.join(cmd)}") from exc
        except FileNotFoundError as exc:
            raise AdbError(f"adb binary not found: {self._adb_bin}") from exc

        return (
            stdout_b.decode(errors="replace").strip(),
            stderr_b.decode(errors="replace").strip(),
            proc.returncode or 0,
        )
