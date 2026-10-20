"""Read-only identity and stopped-process checks for the selected downloader.

A torrent's paused state is not process quiescence. This adapter requires the
exact container to have stopped after an actual mount observation in its last
running session. It never pauses, starts, removes or replaces a downloader.
"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from pixav.shared.cleanup_operation import CleanupDownloaderMount, read_downloader_mount
from pixav.shared.cleanup_recovery import CleanupApprovalError, CleanupRuntimeBinding, EvidenceFile
from pixav.shared.media_file import open_media_directory


class MaintenanceGuard(Protocol):
    async def check(self) -> CleanupRuntimeBinding: ...


def _root_identity(path: Path) -> tuple[int, int]:
    # Opening the directory's descriptor creates neither a sentinel nor a lock
    # file, and rejects symlinks in every component of the path.
    with open_media_directory(path / ".pixav-cleanup-identity") as directory:
        info = os.fstat(directory.descriptor)
        return info.st_dev, info.st_ino


def _downloader_mount(attributes: dict, destination: str) -> dict:
    matching = [m for m in attributes["Mounts"] if m["Destination"] == destination]
    if len(matching) != 1 or matching[0]["Type"] != "bind":
        raise CleanupApprovalError("CLEANUP_DOWNLOADER_BIND_REQUIRED")
    return matching[0]


def _container_identity(attributes: dict) -> tuple:
    return attributes["Id"], attributes["Image"], attributes["Created"], attributes["State"]["StartedAt"]


def capture_downloader_mount(
    client: Any, container_id: str, destination: str, staging_root: Path, runtime: CleanupRuntimeBinding
) -> CleanupDownloaderMount:
    """Observe a live mount; the returned evidence does not authorize unlink."""
    try:
        if not re.fullmatch(r"[a-f0-9]{64}", container_id) or client.info()["ID"] != runtime.docker_daemon_id:
            raise CleanupApprovalError("CLEANUP_DOWNLOADER_IDENTITY_MISMATCH")
        container = client.containers.get(container_id)
        container.reload()
        before = container.attrs
        labels, state = before["Config"].get("Labels") or {}, before["State"]
        if labels.get("com.docker.compose.service") != "qbittorrent" or not labels.get("com.docker.compose.project"):
            raise CleanupApprovalError("CLEANUP_DOWNLOADER_ROLE_MISMATCH")
        if not state["Running"] or state.get("Paused") or state.get("Restarting"):
            raise CleanupApprovalError("CLEANUP_DOWNLOADER_LIVE_MOUNT_REQUIRED")
        identity = _container_identity(before)
        project = labels["com.docker.compose.project"]
        mount = dict(_downloader_mount(before, destination))
        host = _root_identity(Path(mount["Source"]))
        if host != _root_identity(staging_root):
            raise CleanupApprovalError("CLEANUP_DOWNLOADER_ROOT_MISMATCH")
        observed = container.exec_run(["stat", "-c", "%d %i", destination])
        if observed.exit_code or not re.fullmatch(rb"[0-9]+ [0-9]+\s*", observed.output):
            raise CleanupApprovalError("CLEANUP_DOWNLOADER_MOUNT_UNCONFIRMED")
        if tuple(map(int, observed.output.split())) != host:
            raise CleanupApprovalError("CLEANUP_DOWNLOADER_MOUNT_MISMATCH")
        container.reload()
        after = container.attrs
        if (
            identity != _container_identity(after)
            or not after["State"]["Running"]
            or after["State"].get("Paused")
            or _downloader_mount(after, destination) != mount
        ):
            raise CleanupApprovalError("CLEANUP_DOWNLOADER_CHANGED_DURING_CAPTURE")
        return CleanupDownloaderMount(
            version="cleanup-downloader-mount-v1",
            runtime=runtime,
            container_id=container_id,
            image_id=identity[1],
            compose_project=project,
            compose_service="qbittorrent",
            created_at=identity[2],
            started_at=identity[3],
            mount_destination=destination,
            mount_source=mount["Source"],
            root_device=host[0],
            root_inode=host[1],
            observed_at=datetime.now(timezone.utc),
        )
    except CleanupApprovalError:
        raise
    except Exception:
        raise CleanupApprovalError("CLEANUP_DOWNLOADER_CAPTURE_UNCONFIRMED") from None


class StoppedDownloaderGuard:
    def __init__(self, client: Any, maintenance: MaintenanceGuard, receipt: EvidenceFile, staging_root: Path):
        self.client, self.maintenance = client, maintenance
        self.receipt, self.staging_root = receipt, staging_root

    def _check_stopped(self, binding: CleanupDownloaderMount, runtime: CleanupRuntimeBinding) -> None:
        if (binding.runtime.docker_context, binding.runtime.docker_daemon_id) != (
            runtime.docker_context,
            runtime.docker_daemon_id,
        ) or self.client.info()["ID"] != runtime.docker_daemon_id:
            raise CleanupApprovalError("CLEANUP_DOWNLOADER_RUNTIME_MISMATCH")
        containers = self.client.containers.list(
            all=True,
            filters={
                "label": [
                    "com.docker.compose.project=" + binding.compose_project,
                    "com.docker.compose.service=qbittorrent",
                ]
            },
        )
        if len(containers) != 1 or containers[0].id != binding.container_id:
            raise CleanupApprovalError("CLEANUP_DOWNLOADER_CONTAINER_MISMATCH")
        container = containers[0]
        container.reload()
        attributes = container.attrs
        state = attributes["State"]
        if (
            state["Status"] != "exited"
            or state["Running"]
            or state.get("Paused")
            or state.get("Restarting")
            or state.get("Dead")
            or state.get("Pid", -1) != 0
            or attributes["HostConfig"]["RestartPolicy"]["Name"] != "no"
        ):
            raise CleanupApprovalError("CLEANUP_DOWNLOADER_NOT_QUIESCENT")
        if _container_identity(attributes) != (
            binding.container_id,
            binding.image_id,
            binding.created_at,
            binding.started_at,
        ):
            raise CleanupApprovalError("CLEANUP_DOWNLOADER_RUNTIME_REPLACED")
        mount = _downloader_mount(attributes, binding.mount_destination)
        expected = binding.root_device, binding.root_inode
        if (
            mount["Source"] != binding.mount_source
            or _root_identity(Path(mount["Source"])) != expected
            or _root_identity(self.staging_root) != expected
        ):
            raise CleanupApprovalError("CLEANUP_DOWNLOADER_ROOT_MISMATCH")

    async def check(self) -> CleanupRuntimeBinding:
        runtime = await self.maintenance.check()
        try:
            binding = await asyncio.to_thread(read_downloader_mount, self.receipt)
            await asyncio.to_thread(self._check_stopped, binding, runtime)
        except CleanupApprovalError:
            raise
        except Exception:
            raise CleanupApprovalError("CLEANUP_DOWNLOADER_QUIESCENCE_UNCONFIRMED") from None
        if await self.maintenance.check() != runtime:
            raise CleanupApprovalError("CLEANUP_RUNTIME_CHANGED")
        return runtime
