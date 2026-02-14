"""Redroid container management implementation using Docker SDK."""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import Any, cast

from docker.errors import APIError, NotFound

import docker
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
        adb_port_start: int = 5555,
        network: str = "pixav",
    ) -> None:
        self.image = image
        self._adb_port = adb_port_start
        self._network = network
        self._docker: Any | None = None

    def _client(self) -> Any:
        if self._docker is None:
            self._docker = cast(Any, docker).from_env()
        return self._docker

    async def create(self, task_id: str) -> str:
        """Create a new Redroid container for the given task.

        Args:
            task_id: Unique identifier for the upload task.

        Returns:
            Container ID string.

        Raises:
            RedroidError: If container creation fails.
        """
        name = f"pixav-redroid-{task_id[:8]}"
        loop = asyncio.get_running_loop()

        try:
            container = await loop.run_in_executor(
                None,
                partial(
                    self._client().containers.run,
                    self.image,
                    name=name,
                    detach=True,
                    privileged=True,
                    ports={"5555/tcp": self._adb_port},
                    network=self._network,
                    labels={"pixav.task_id": task_id},
                ),
            )
            cid = container.id
            logger.info("created redroid container %s (%s) for task %s", name, cid[:12], task_id)
            return cid
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
