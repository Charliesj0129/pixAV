"""Redroid container management implementation using Docker SDK."""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import Any, cast

from docker.errors import APIError, NotFound

import docker
from pixav.pixel_injector.session import RedroidSession
from pixav.shared.exceptions import RedroidError

logger = logging.getLogger(__name__)

# Default device capabilities for Redroid
_DEFAULT_PROPS = {
    "ro.product.model": "Pixel 6",
    "ro.product.brand": "google",
}


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
    ) -> None:
        self.image = image
        self._adb_host = adb_host
        self._adb_port = adb_port_start
        self._network = network
        self._docker: Any | None = None

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

            container = await loop.run_in_executor(
                None,
                partial(
                    self._client().containers.run,
                    self.image,
                    **kwargs,
                ),
            )
            cid = container.id
            adb_port = _extract_adb_port(container, fallback=self._adb_port, container_name=name)
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

    async def destroy(self, container_id: str) -> None:
        """Destroy a Redroid container.

        Args:
            container_id: ID of container to destroy.

        Raises:
            RedroidError: If container destruction fails.
        """
        loop = asyncio.get_running_loop()
        try:
            container = await loop.run_in_executor(
                None,
                partial(self._client().containers.get, container_id),
            )
            await loop.run_in_executor(
                None,
                partial(container.remove, force=True),
            )
            logger.info("destroyed container %s", container_id[:12])
        except NotFound:
            logger.warning("container %s already removed", container_id[:12])
        except APIError as exc:
            raise RedroidError(f"failed to destroy container {container_id[:12]}: {exc}") from exc

    async def wait_ready(self, container_id: str, timeout: int = 120) -> bool:
        """Wait for container to be ready (ADB responsive).

        Args:
            container_id: ID of container to wait for.
            timeout: Maximum seconds to wait.

        Returns:
            True if container became ready, False if timeout.
        """
        loop = asyncio.get_running_loop()
        elapsed = 0
        poll_interval = 3

        while elapsed < timeout:
            try:
                container = await loop.run_in_executor(
                    None,
                    partial(self._client().containers.get, container_id),
                )
                status = container.status
                if status == "running":
                    # Check if ADB port is responsive via container health
                    health = container.attrs.get("State", {}).get("Health", {})
                    health_status = health.get("Status", "none")
                    if health_status in ("healthy", "none"):
                        # 'none' means no healthcheck configured — assume ready
                        logger.info("container %s is ready", container_id[:12])
                        return True
                elif status in ("exited", "dead"):
                    logger.error("container %s in terminal state: %s", container_id[:12], status)
                    return False
            except (NotFound, APIError) as exc:
                logger.debug("waiting for container %s: %s", container_id[:12], exc)

            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

        logger.warning("container %s readiness timed out after %ds", container_id[:12], timeout)
        return False


def _extract_adb_port(container: Any, *, fallback: int, container_name: str) -> int:
    """Resolve mapped host port for container ADB tcp/5555."""
    try:
        container.reload()
        bindings = container.attrs.get("NetworkSettings", {}).get("Ports", {}).get("5555/tcp", [])
        if bindings:
            host_port = bindings[0].get("HostPort")
            if host_port is not None:
                return int(host_port)
    except Exception as exc:  # pragma: no cover - defensive logging only
        logger.warning("failed to read dynamic port for %s: %s", container_name, exc)

    logger.warning("could not read dynamic port for %s, falling back to %d", container_name, fallback)
    return fallback
