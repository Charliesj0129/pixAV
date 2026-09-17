"""Redroid container management implementation using Docker SDK."""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import Any, cast

from docker.errors import APIError, NotFound

import docker
from pixav.pixel_injector.profiles import AndroidProfile, ReadinessCheck, get_profile
from pixav.pixel_injector.session import RedroidSession
from pixav.shared.exceptions import RedroidError

logger = logging.getLogger(__name__)


class DockerRedroidManager:
    """Docker-based implementation of RedroidManager protocol.

    Manages Redroid Android container lifecycle using Docker SDK.
    """

    def __init__(
        self,
        image: str,
        *,
        adb_host: str = "127.0.0.1",
        adb_port_start: int = 5555,
        network: str | None = None,
        profile: AndroidProfile | None = None,
    ) -> None:
        # The profile owns the device identity. When one is supplied its image
        # wins, so the identity and the image it was verified against can never
        # drift apart.
        self._profile = profile
        self.image = profile.image if profile is not None else image
        self._adb_host = adb_host
        self._adb_port = adb_port_start
        self._network = network
        self._docker: Any | None = None

    @classmethod
    def from_profile_name(
        cls,
        profile_name: str,
        *,
        profiles_path: str | None = None,
        adb_host: str = "127.0.0.1",
        adb_port_start: int = 5555,
        network: str | None = None,
    ) -> DockerRedroidManager:
        """Build a manager from a named profile in the profile file."""
        profile = get_profile(profile_name, path=profiles_path)
        return cls(
            profile.image,
            adb_host=adb_host,
            adb_port_start=adb_port_start,
            network=network,
            profile=profile,
        )

    def _client(self) -> Any:
        if self._docker is None:
            self._docker = cast(Any, docker).from_env()
        return self._docker

    async def create(self, task_id: str) -> RedroidSession:
        """Create a new Redroid container for the given task.

        Args:
            task_id: Unique identifier for the upload task.

        Returns:
            Active Redroid session with container and ADB endpoint.

        Raises:
            RedroidError: If container creation fails.
        """
        name = f"pixav-redroid-{task_id[:8]}"
        loop = asyncio.get_running_loop()

        try:
            kwargs: dict[str, Any] = {
                "name": name,
                "detach": True,
                "privileged": True,
                # Let Docker assign a random host port
                "ports": {"5555/tcp": None},
                "labels": {"pixav.task_id": task_id},
            }
            if self._network:
                kwargs["network"] = self._network
            # redroid takes `ro.xxx=value` overrides as container arguments.
            # Without this the profile is inert: the container boots with the
            # stock AOSP identity no matter what the profile declares.
            if self._profile is not None and self._profile.args:
                kwargs["command"] = list(self._profile.args)

            container = await loop.run_in_executor(
                None,
                partial(
                    self._client().containers.run,
                    self.image,
                    **kwargs,
                ),
            )
            cid = container.id
            adb_port = await _wait_for_adb_port(container)
            if adb_port is None:
                # A guessed fallback can attach ADB to an unrelated process or
                # leave this task hanging for the full ADB timeout. The port is
                # dynamically published, so fail closed and remove the session
                # when Docker never reports the binding.
                await loop.run_in_executor(None, partial(container.remove, force=True))
                raise RedroidError(f"Docker did not publish an ADB port for container {name}")
            session = RedroidSession(
                task_id=task_id,
                container_id=cid,
                adb_host=self._adb_host,
                adb_port=adb_port,
            )
            logger.info("created redroid container %s (%s) for task %s", name, cid[:12], task_id)
            return session
        except APIError as exc:
            raise RedroidError(f"failed to create container {name}: {exc}") from exc

    async def cleanup_orphans(self) -> int:
        """Remove containers left by an earlier worker process."""
        loop = asyncio.get_running_loop()

        def _cleanup() -> int:
            containers = self._client().containers.list(all=True, filters={"label": "pixav.task_id"})
            for container in containers:
                _stop_and_remove(container)
            return len(containers)

        try:
            removed = await loop.run_in_executor(None, _cleanup)
            if removed:
                logger.warning("removed %d orphaned pixAV Redroid container(s)", removed)
            return removed
        except APIError as exc:
            raise RedroidError(f"failed to clean orphaned Redroid containers: {exc}") from exc

    async def destroy(self, container_id: str) -> None:
        """Destroy a Redroid container.

        Args:
            container_id: ID of container to destroy.

        Raises:
            RedroidError: If container destruction fails.
        """
        loop = asyncio.get_running_loop()

        def _remove() -> None:
            container = self._client().containers.get(container_id)
            _stop_and_remove(container)

        try:
            await loop.run_in_executor(None, _remove)
            logger.info("destroyed container %s", container_id[:12])
        except NotFound:
            logger.warning("container %s already removed", container_id[:12])
        except APIError as exc:
            raise RedroidError(f"failed to destroy container {container_id[:12]}: {exc}") from exc

    async def wait_ready(self, container_id: str, timeout: int = 120) -> bool:
        """Wait until Android has actually booted inside the container.

        Docker's own health signal is useless here: stock Redroid images declare
        no HEALTHCHECK, so ``Health.Status`` is ``none`` from the instant the
        container starts. Treating that as ready declared a container usable
        while Android was still booting.

        Readiness is therefore evidence from inside the guest — by default
        ``sys.boot_completed``, plus whatever else the profile asserts, which is
        how a device identity is proven rather than assumed.

        Args:
            container_id: ID of container to wait for.
            timeout: Maximum seconds to wait.

        Returns:
            True if the container became ready, False on timeout or a terminal
            container state.
        """
        loop = asyncio.get_running_loop()
        elapsed = 0
        poll_interval = 3
        checks = self._readiness_checks()

        while elapsed < timeout:
            try:
                container = await loop.run_in_executor(
                    None,
                    partial(self._client().containers.get, container_id),
                )
                status = container.status
                if status in ("exited", "dead"):
                    logger.error("container %s in terminal state: %s", container_id[:12], status)
                    return False
                if status == "running" and await self._checks_pass(container, checks):
                    logger.info("container %s is ready", container_id[:12])
                    return True
            except (NotFound, APIError) as exc:
                logger.debug("waiting for container %s: %s", container_id[:12], exc)

            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

        logger.warning("container %s readiness timed out after %ds", container_id[:12], timeout)
        return False

    def _readiness_checks(self) -> tuple[ReadinessCheck, ...]:
        """Profile checks, or the minimum boot check when no profile is set."""
        if self._profile is not None and self._profile.readiness:
            return self._profile.readiness
        return (ReadinessCheck(command="getprop sys.boot_completed", contains="1"),)

    async def _checks_pass(self, container: Any, checks: tuple[ReadinessCheck, ...]) -> bool:
        """Run every readiness command inside the guest and match its output."""
        loop = asyncio.get_running_loop()
        for check in checks:
            try:
                result = await loop.run_in_executor(
                    None,
                    partial(container.exec_run, check.command),
                )
            except (APIError, OSError) as exc:
                # Android's init has not brought up the shell yet; keep waiting
                # rather than declaring the container broken.
                logger.debug("readiness command %r not answerable yet: %s", check.command, exc)
                return False

            exit_code = getattr(result, "exit_code", None)
            output = getattr(result, "output", b"")
            if isinstance(output, bytes):
                output = output.decode("utf-8", errors="replace")
            if exit_code != 0 or check.contains not in output:
                logger.debug("readiness check not yet satisfied: %r", check.command)
                return False
        return True


async def _wait_for_adb_port(container: Any, *, timeout: float = 5.0) -> int | None:
    """Wait for Docker's asynchronous host-port publication.

    ``containers.run(detach=True)`` may return before ``NetworkSettings.Ports``
    contains the dynamically assigned host port. This is observable after a
    Docker Desktop/WSL restart and cannot safely fall back to a fixed port.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        try:
            await loop.run_in_executor(None, container.reload)
            bindings = container.attrs.get("NetworkSettings", {}).get("Ports", {}).get("5555/tcp", [])
            if bindings:
                host_port = bindings[0].get("HostPort")
                if host_port is not None:
                    return int(host_port)
        except (APIError, KeyError, OSError, TypeError, ValueError) as exc:
            logger.debug("ADB port binding not ready for %s: %s", container.id[:12], exc)

        if loop.time() >= deadline:
            return None
        await asyncio.sleep(0.1)


def _stop_and_remove(container: Any) -> None:
    """Gracefully stop a Redroid process tree before exact-container removal.

    ``remove(force=True)`` immediately sends SIGKILL.  During the Phase 0
    2 GiB experiment that left an Android child as a zombie and made Docker
    refuse removal.  Docker's ``--init`` cannot be used here because Android
    init must remain PID 1.  Ask Android init to power down its own process tree
    first, then retain a bounded Docker stop and force-removal fallback.
    """
    status = getattr(container, "status", None)
    if status not in {"created", "exited", "dead"}:
        android_shutdown_requested = False
        try:
            result = container.exec_run(["setprop", "sys.powerctl", "shutdown"])
            if getattr(result, "exit_code", 1) != 0:
                logger.warning("Android shutdown request failed; using bounded Docker stop")
            else:
                android_shutdown_requested = True
        except (APIError, OSError) as exc:
            logger.warning("Android shutdown request failed; using bounded Docker stop: %s", exc)
        if android_shutdown_requested:
            try:
                container.wait(timeout=30)
            except Exception as exc:  # Docker SDK surfaces an HTTP read timeout here.
                logger.debug("Android shutdown did not finish within 30s: %s", exc)
        try:
            container.reload()
            status = getattr(container, "status", None)
        except (APIError, OSError):
            status = None
        if status not in {"created", "exited", "dead"}:
            try:
                container.stop(timeout=10)
            except APIError as exc:
                logger.warning("bounded Redroid stop failed; forcing exact-container removal: %s", exc)
    container.remove(force=True)
