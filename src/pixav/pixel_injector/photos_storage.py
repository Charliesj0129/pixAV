"""Google Photos adapters for the managed storage activities.

Upload drives the configured Pixel-compatible guest through Maestro; the
read-back re-acquires the bytes through an ordinary browser session that has
never seen the staging file. The two never share a filesystem view, which is
what makes the read-back independent evidence rather than a local echo.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from pixav.pixel_injector.maestro_parts import MaestroPartUploader, MaestroPartVerifier
from pixav.pixel_injector.segment_staging import release_segment, stage_segment
from pixav.pixel_injector.session import RedroidSession
from pixav.shared.exceptions import UploadError, VerificationError
from pixav.shared.host_paths import host_path
from pixav.shared.models import Account
from pixav.shared.storage_models import RemoteAssetSegment

logger = logging.getLogger(__name__)

# Only this mode provides the upload environment the contract names. Any other
# value must fail the activity rather than quietly substitute a direct upload.
PIXEL_COMPATIBLE_MODE = "redroid"

# The read-back container sees exactly these two paths and nothing else.
COLD_WORK_MOUNT = "/work"
COLD_SOURCE_MOUNT = "/app/src"
COLD_READBACK_LABEL = "cold-readback"
COLD_MOUNTS = (f"{COLD_SOURCE_MOUNT}:ro", f"{COLD_WORK_MOUNT}:rw")


class MaestroSegmentUploader:
    """Places a segment through the configured Pixel-compatible guest."""

    # The tools container mounts the staging root here, read-only. Each asset
    # owns one subdirectory so a segment is addressable by its identity alone.
    GUEST_STAGING_ROOT = "/parts"

    def __init__(
        self,
        runtime: Callable[[uuid.UUID], Any],
        *,
        owner: str,
        flows: Path,
        mode: str,
        staging_root: Path,
    ) -> None:
        self._runtime = runtime
        self._owner = owner
        self._flows = flows
        self._mode = mode
        self._staging_root = staging_root

    async def upload(self, segment: RemoteAssetSegment, account: Account, journal) -> tuple[str, dict]:
        if self._mode != PIXEL_COMPATIBLE_MODE:
            # Recording a remote success from a substituted path would make the
            # durability evidence describe an environment that never ran.
            raise UploadError("configured upload environment is not the Pixel-compatible one")
        # Link the prepared bytes under the name the guest will verify, before
        # asking for a runtime: a missing artifact must not cost a container.
        staged = await asyncio.to_thread(stage_segment, segment, root=self._staging_root)
        guest, runner = await self._runtime(account.id)
        recovery = dict(segment.recovery)

        async def save(value: dict) -> None:
            await journal("upload_intent", value)

        uploader = MaestroPartUploader(
            guest,
            runner,
            self._owner,
            segment,
            recovery,
            save,
            self._flows,
            source_root=f"{self.GUEST_STAGING_ROOT}/{segment.asset_id}",
        )
        verifier = MaestroPartVerifier(uploader)
        session = RedroidSession(self._owner, guest.id, "127.0.0.1", 5555)
        await uploader.login(session, account)
        remote = await uploader.push_file(session, str(staged / segment.filename))
        await uploader.trigger_upload(session, remote)
        share_url = await verifier.wait_for_share_url(session, timeout=21600)
        if not await verifier.validate_share_url(share_url):
            raise UploadError("Photos sharing location unavailable")
        return share_url, uploader.backup_evidence

    async def release(self, segment: RemoteAssetSegment) -> None:
        """Drop the staged link once the authority has committed the usage."""
        await asyncio.to_thread(release_segment, segment, root=self._staging_root)


class PhotosColdReadback:
    """Re-acquires a segment from Photos in a container that holds nothing else.

    Running the browser inside the worker would put it on the same filesystem as
    the staged upload artifact, and a receipt produced there could not tell a
    provider download from a local copy. So the read-back is an ephemeral
    container with two mounts: an empty per-segment work directory it writes
    into, and the source tree read-only. No staging root, no guest ``/data``, no
    database credential and no queue are reachable from it, which is what makes
    ``cold_inputs: provider-only`` a fact rather than an assertion.
    """

    def __init__(
        self,
        root: Path,
        *,
        client_factory: Callable[[], Any] | None = None,
        image: str,
        source_root: Path,
        host_project_root: str = "",
        project_root: Path | None = None,
        timeout_seconds: int = 21600,
    ) -> None:
        self._root = root
        self._client_factory = client_factory
        self._image = image
        self._source_root = source_root
        self._host_project_root = host_project_root
        self._project_root = project_root
        self._timeout_seconds = timeout_seconds

    def _client(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory()
        import docker

        return cast(Any, docker).from_env()

    def _host(self, path: Path) -> str:
        return str(host_path(path, host_project_root=self._host_project_root, project_root=self._project_root))

    def _work_dir(self, segment: RemoteAssetSegment) -> Path:
        destination = self._root / str(segment.asset_id) / str(segment.segment_index)
        if destination.is_symlink() or any(parent.is_symlink() for parent in destination.parents):
            raise VerificationError("symlink in the read-back path")
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        return destination

    @staticmethod
    def manifest(segment: RemoteAssetSegment) -> dict:
        """What the cold process is allowed to know: the provider location and
        the facts the bytes must satisfy. Never a local path or an account."""
        redacted = segment.model_copy(update={"local_path": "", "account_id": None, "recovery": {}, "verification": {}})
        return {"version": 1, "segment": json.loads(redacted.model_dump_json())}

    async def read_back(self, segment: RemoteAssetSegment) -> dict:
        destination = self._work_dir(segment)
        payload = json.dumps(self.manifest(segment)).encode()
        receipt = await asyncio.to_thread(self._run, destination, payload)
        return {**receipt, "cold_inputs": "provider-only", "cold_mounts": list(COLD_MOUNTS)}

    def _run(self, destination: Path, payload: bytes) -> dict:
        client = self._client()
        container = client.containers.create(
            self._image,
            command=["python", "-m", "pixav.pixel_injector.segment_readback"],
            stdin_open=True,
            user=f"{os.getuid()}:{os.getgid()}",
            labels={"pixav.photos_canary.role": COLD_READBACK_LABEL},
            environment={"PYTHONPATH": COLD_SOURCE_MOUNT, "HOME": COLD_WORK_MOUNT},
            volumes={
                self._host(self._source_root): {"bind": COLD_SOURCE_MOUNT, "mode": "ro"},
                self._host(destination): {"bind": COLD_WORK_MOUNT, "mode": "rw"},
            },
            working_dir=COLD_WORK_MOUNT,
        )
        try:
            self._assert_isolated(container)
            stream = container.attach_socket(params={"stdin": 1, "stream": 1})
            try:
                stream._sock.sendall(payload)
                stream._sock.shutdown(socket.SHUT_WR)
                container.start()
                status = container.wait(timeout=self._timeout_seconds)
            finally:
                stream.close()
            return self._receipt(container, status)
        finally:
            container.remove(force=True)

    @staticmethod
    def _assert_isolated(container: Any) -> None:
        """The container must carry the two mounts above and no others."""
        container.reload()
        binds = [
            f"{mount.get('Destination')}:{'rw' if mount.get('RW') else 'ro'}"
            for mount in container.attrs.get("Mounts", [])
        ]
        if sorted(binds) != sorted(COLD_MOUNTS):
            raise VerificationError("cold read-back container has unexpected mounts")

    @staticmethod
    def _receipt(container: Any, status: Any) -> dict:
        code = status.get("StatusCode") if isinstance(status, dict) else status
        raw = container.logs(stdout=True, stderr=False)
        text = raw.decode("utf-8", errors="replace").strip().splitlines()
        try:
            report = json.loads(text[-1]) if text else {}
        except ValueError:
            report = {}
        if code != 0 or not isinstance(report, dict) or report.get("status") == "BLOCKED":
            # The child only ever reports an exception type, so nothing here can
            # carry a share location, a path or a provider payload.
            raise VerificationError(f"cold read-back failed: {report.get('error_type', 'NO_REPORT')}")
        return report
