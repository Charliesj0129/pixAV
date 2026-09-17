"""ADB connection management for Redroid containers."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from pixav.shared.exceptions import AdbError

logger = logging.getLogger(__name__)


class AdbConnection:
    """Manages ADB connection to Redroid Android containers.

    Uses ``adb`` CLI through async subprocess calls.
    """

    def __init__(self, *, adb_bin: str = "adb", timeout: int = 120) -> None:
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

        # Wait for device to come fully online
        await self._run("-s", self._target, "wait-for-device")

        # Also wait for sys.boot_completed = 1
        for _ in range(60):
            try:
                boot_stdout, _, _ = await self._run("-s", self._target, "shell", "getprop sys.boot_completed")
                if boot_stdout.strip() == "1":
                    break
            except Exception as e:
                logger.debug("ADB wait for boot failure: %s", e)
            await asyncio.sleep(1)

        logger.info("ADB connected to %s", self._target)

    async def push(self, local: str, remote: str, *, timeout: int | None = None) -> None:
        """Push file to container via ADB.

        Args:
            local: Local file path.
            remote: Remote destination path in container.

        Raises:
            AdbError: If push fails or no active connection.
        """
        target = self._target_or_raise()
        stdout, stderr, rc = await self._run("-s", target, "push", local, remote, timeout=timeout)
        if rc != 0:
            raise AdbError(f"ADB push failed: {stderr}")
        logger.info("pushed %s → %s on %s", local, remote, target)

    async def pull(self, remote: str, local: str, *, timeout: int | None = None) -> None:
        """Pull a file from the connected Android guest.

        This is used by the manual Pixel experiment to preserve screenshots as
        evidence. Keeping it beside :meth:`push` also makes the script fail with
        a domain error when no ADB target exists, rather than an AttributeError.
        """
        target = self._target_or_raise()
        _stdout, stderr, rc = await self._run("-s", target, "pull", remote, local, timeout=timeout)
        if rc != 0:
            raise AdbError(f"ADB pull failed: {stderr}")
        logger.info("pulled %s → %s from %s", remote, local, target)

    async def shell(
        self,
        cmd: str,
        *,
        sensitive: bool = False,
        timeout: int | None = None,
    ) -> str:
        """Execute shell command in container.

        Args:
            cmd: Shell command to execute.
            sensitive: Send the command over stdin rather than process argv.
                Use this for commands containing credentials so they cannot be
                observed through the host process list or echoed in timeout
                errors.

        Returns:
            Command output (stdout).

        Raises:
            AdbError: If command fails or no active connection.
        """
        target = self._target_or_raise()
        if sensitive:
            stdout, stderr, rc = await self._run_sensitive_shell(target, cmd, timeout=timeout)
        else:
            stdout, stderr, rc = await self._run("-s", target, "shell", cmd, timeout=timeout)
        if rc != 0:
            detail = "redacted" if sensitive else stderr
            raise AdbError(f"ADB shell failed (rc={rc}): {detail}")
        return stdout

    def _target_or_raise(self) -> str:
        if self._target is None:
            raise AdbError("not connected — call connect() first")
        return self._target

    async def _run(self, *args: str, timeout: int | None = None) -> tuple[str, str, int]:
        """Run an ADB command and return (stdout, stderr, returncode)."""
        cmd = [self._adb_bin, *args]
        effective_timeout = self._timeout if timeout is None else timeout
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=effective_timeout)
        except asyncio.TimeoutError as exc:
            await _kill_and_reap(proc)
            raise AdbError(f"ADB command timed out: {' '.join(cmd)}") from exc
        except FileNotFoundError as exc:
            raise AdbError(f"adb binary not found: {self._adb_bin}") from exc

        return (
            stdout_b.decode(errors="replace").strip(),
            stderr_b.decode(errors="replace").strip(),
            proc.returncode or 0,
        )

    async def _run_sensitive_shell(
        self,
        target: str,
        cmd: str,
        *,
        timeout: int | None = None,
    ) -> tuple[str, str, int]:
        """Execute a remote shell command without placing it in process argv."""
        effective_timeout = self._timeout if timeout is None else timeout
        try:
            proc = await asyncio.create_subprocess_exec(
                self._adb_bin,
                "-s",
                target,
                "shell",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(f"{cmd}\n".encode()),
                timeout=effective_timeout,
            )
        except asyncio.TimeoutError as exc:
            await _kill_and_reap(proc)
            raise AdbError("ADB sensitive shell command timed out") from exc
        except FileNotFoundError as exc:
            raise AdbError(f"adb binary not found: {self._adb_bin}") from exc

        return (
            stdout_b.decode(errors="replace").strip(),
            stderr_b.decode(errors="replace").strip(),
            proc.returncode or 0,
        )


async def _kill_and_reap(proc: asyncio.subprocess.Process) -> None:
    """Do not leak an ADB subprocess after its command timeout."""
    with suppress(ProcessLookupError):
        proc.kill()
    with suppress(Exception):
        await proc.communicate()
