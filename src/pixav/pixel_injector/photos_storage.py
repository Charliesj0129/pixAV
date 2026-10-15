"""Google Photos adapters for the managed storage activities.

Upload drives the configured Pixel-compatible guest through Maestro; the
read-back re-acquires the bytes through an ordinary browser session that has
never seen the staging file. The two never share a filesystem view, which is
what makes the read-back independent evidence rather than a local echo.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import socket
import tempfile
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from pixav.pixel_injector.maestro_parts import MaestroPartUploader, MaestroPartVerifier
from pixav.pixel_injector.photos_auth import AccountSessionRequiredError, read_account_secret
from pixav.pixel_injector.segment_staging import release_segment, stage_segment
from pixav.pixel_injector.session import RedroidSession
from pixav.shared.cold_receipts import AUTHENTICATED_RECEIPT, validate_account_cold_receipt
from pixav.shared.exceptions import ReadbackUnavailableError, UploadError, VerificationError
from pixav.shared.host_paths import host_path
from pixav.shared.models import Account
from pixav.shared.photos_ui_ownership import photos_ui_ownership
from pixav.shared.photos_ui_session import current_photos_ui_lease
from pixav.shared.storage_models import RemoteAssetSegment

logger = logging.getLogger(__name__)

# Only this mode provides the upload environment the contract names. Any other
# value must fail the activity rather than quietly substitute a direct upload.
PIXEL_COMPATIBLE_MODE = "redroid"

# The read-back container mounts only its fresh workspace. Code ships in the image.
COLD_WORK_MOUNT = "/work"
COLD_SOURCE_MOUNT = "/app/src"
COLD_READBACK_LABEL = "cold-readback"
COLD_MOUNTS = (f"{COLD_WORK_MOUNT}:rw",)


class ColdProcessError(VerificationError):
    """The isolated process reported a failure; only its exception type is known.

    The type is the one detail the child is allowed to say, so it is kept as
    data for callers that classify failures, never as a free-form message.
    """

    def __init__(self, error_type: str) -> None:
        super().__init__(f"cold read-back failed: {error_type}")
        self.error_type = error_type


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
        observation_clock: Callable[[], Awaitable[datetime]] | None = None,
        ui_pool: Any = None,
        existing_runtime: Callable[[uuid.UUID], Any] | None = None,
    ) -> None:
        self._runtime = runtime
        self._existing_runtime = existing_runtime
        self._owner = owner
        self._flows = flows
        self._mode = mode
        self._observation_clock = observation_clock
        self._staging_root = staging_root
        self._ui_pool = ui_pool

    async def upload(self, segment: RemoteAssetSegment, account: Account, journal) -> tuple[str, dict]:
        if self._ui_pool is None:
            raise UploadError("Photos upload requires shared UI ownership")
        async with photos_ui_ownership(self._ui_pool, account.id, self._owner) as check:
            return await self._upload_owned(segment, account, journal, check)

    async def _upload_owned(self, segment: RemoteAssetSegment, account: Account, journal, check) -> tuple[str, dict]:
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
            observation_clock=self._observation_clock,
            observation_binding={"account_id": str(account.id), "owner": self._owner, "profile": "gphotos_pixel_xl_v1"},
        )
        verifier = MaestroPartVerifier(uploader)
        uploader.check = check
        session = RedroidSession(self._owner, guest.id, "127.0.0.1", 5555)
        await uploader.login(session, account)
        remote = await uploader.push_file(session, str(staged / segment.filename))
        await uploader.trigger_upload(session, remote)
        share_url = await verifier.wait_for_share_url(session, timeout=21600)
        if not await verifier.validate_share_url(share_url):
            raise UploadError("Photos sharing location unavailable")
        return share_url, uploader.backup_evidence

    async def share_existing(self, segment: RemoteAssetSegment, account: Account, journal) -> tuple[str, dict]:
        """Share an already selected original under the original activity lease."""
        if self._mode != PIXEL_COMPATIBLE_MODE or self._ui_pool is None or self._existing_runtime is None:
            raise UploadError("existing item requires Pixel runtime and shared UI ownership")
        review = segment.recovery.get("upload_outcome_review", {}).get("evidence", {})
        if review.get("guest_owner") != self._owner or review.get("account_id") != str(account.id):
            raise UploadError("existing item runtime binding changed")
        async with photos_ui_ownership(self._ui_pool, account.id, self._owner) as check:
            guest, runner = await self._existing_runtime(account.id)

            async def save(value: dict) -> None:
                await journal("upload_intent", value)

            uploader = MaestroPartUploader(
                guest,
                runner,
                self._owner,
                segment,
                dict(segment.recovery),
                save,
                self._flows,
                observation_clock=self._observation_clock,
            )
            uploader.check = check
            session = RedroidSession(self._owner, guest.id, "127.0.0.1", 5555)
            return await MaestroPartVerifier(uploader).share_selected_existing(session, account)

    async def release(self, segment: RemoteAssetSegment) -> None:
        """Drop the staged link once the authority has committed the usage."""
        await asyncio.to_thread(release_segment, segment, root=self._staging_root)


class PhotosColdReadback:
    """Re-acquires a segment from Photos in a container that holds nothing else.

    Running the browser inside the worker would put it on the same filesystem as
    the staged upload artifact, and a receipt produced there could not tell a
    provider download from a local copy. So the read-back is an ephemeral
    container with an empty per-segment work directory it writes
    into; the executable code comes from its image. No staging root, no guest ``/data``, no
    database credential and no queue are reachable from it, which is what makes
    ``cold_inputs: provider-only`` a fact rather than an assertion.
    """

    # The module the isolated container runs, and the label it carries. Other
    # cold processes reuse the same isolation by naming their own entry point.
    ENTRY = "pixav.pixel_injector.segment_readback"
    LABEL = COLD_READBACK_LABEL

    def __init__(
        self,
        root: Path,
        *,
        client_factory: Callable[[], Any] | None = None,
        image: str,
        host_project_root: str = "",
        project_root: Path | None = None,
        timeout_seconds: int = 21600,
        authentication_secret: str = "",
        ui_pool: Any = None,
        owner: str = "",
        windows_runtime: Any = None,
    ) -> None:
        self._root = root
        self._client_factory = client_factory
        self._image = image
        self._host_project_root = host_project_root
        self._project_root = project_root
        self._timeout_seconds = timeout_seconds
        self._authentication_secret = authentication_secret
        self._ui_pool = ui_pool
        self._owner = owner
        self._windows_runtime = windows_runtime

    @asynccontextmanager
    async def _ownership(self, account_id):
        if self._windows_runtime is not None:
            async with self._windows_runtime.cold(account_id) as lease:
                yield lease.check
        else:
            owner = await self._ui_pool.fetchval("SELECT owner FROM guest_runtimes WHERE account_id=$1", account_id)
            if str(owner) != self._owner:
                raise AccountSessionRequiredError("cold guest ownership changed")
            async with photos_ui_ownership(self._ui_pool, account_id, self._owner) as check:
                yield check

    @property
    def requires_fresh_work(self) -> bool:
        return bool(self._authentication_secret)

    async def run_authenticated(self, manifest: dict, destination: Path, timeout_seconds: int) -> dict:
        """Cookie input stays in memory; cancellation waits for exact child removal."""
        parts = manifest.get("segments", [manifest.get("segment")])
        accounts = {part.get("account_id") for part in parts if isinstance(part, dict)}
        if len(accounts) != 1 or None in accounts or self._ui_pool is None:
            raise AccountSessionRequiredError("cold account binding unavailable")
        account_id = uuid.UUID(str(next(iter(accounts))))
        secret = read_account_secret(Path(self._authentication_secret), account_id, self._owner)
        email = await self._ui_pool.fetchval("SELECT email FROM accounts WHERE id=$1", account_id)
        if not email or hashlib.sha256(email.strip().lower().encode()).hexdigest() != secret.account_email_sha256:
            raise AccountSessionRequiredError("cold account identity changed")
        stopped = threading.Event()
        async with self._ownership(account_id) as check:
            check()
            payload = json.dumps({**manifest, "authentication": secret.model_dump(mode="json")}).encode()
            task = asyncio.create_task(
                asyncio.to_thread(self._run, destination, payload, timeout_seconds=timeout_seconds, stopped=stopped)
            )
            try:
                receipt = await asyncio.shield(task)
                check()
                obtained = receipt.get("segments", []) if "segments" in manifest else [receipt]
                if len(obtained) != len(parts):
                    raise VerificationError("authenticated cold receipt incomplete")
                for part, report in zip(parts, obtained, strict=True):
                    binding = report.get("authentication", {})
                    if (
                        report.get("receipt_version") != AUTHENTICATED_RECEIPT
                        or binding.get("owner") != self._owner
                        or binding.get("account_email_sha256") != secret.account_email_sha256
                    ):
                        raise VerificationError("authenticated cold receipt binding changed")
                    try:
                        validate_account_cold_receipt(RemoteAssetSegment.model_validate(part), report)
                    except ValueError:
                        raise VerificationError("authenticated cold receipt invalid") from None
                return receipt
            except asyncio.CancelledError:
                stopped.set()
                await self._wait_cancelled_child(task)
                raise

    @staticmethod
    async def _wait_cancelled_child(task: asyncio.Task) -> None:
        while not task.done():
            try:
                await asyncio.shield(task)
            except (asyncio.CancelledError, Exception):
                if task.done():
                    break

    def _client(self) -> Any:
        if self._windows_runtime is not None:
            return self._windows_runtime.docker
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
        # Each activity must use a new browser/workspace, including retries.
        # Retain previous provider downloads as evidence, but never echo them
        # back as a fresh cold acquisition.
        return Path(tempfile.mkdtemp(prefix="session-", dir=destination))

    @staticmethod
    def manifest(segment: RemoteAssetSegment) -> dict:
        """What the cold process is allowed to know: the provider location and
        the facts the bytes must satisfy. Never a local path or an account."""
        redacted = segment.model_copy(update={"local_path": "", "account_id": None, "recovery": {}, "verification": {}})
        return {"version": 1, "segment": json.loads(redacted.model_dump_json())}

    async def read_back(self, segment: RemoteAssetSegment) -> dict:
        destination = self._work_dir(segment)
        manifest = self.manifest(segment)
        try:
            if self.requires_fresh_work:
                manifest["segment"]["account_id"] = str(segment.account_id) if segment.account_id else None
                receipt = await self.run_authenticated(manifest, destination, self._timeout_seconds)
            else:
                receipt = await asyncio.to_thread(self._run, destination, json.dumps(manifest).encode())
        except ColdProcessError as exc:
            if exc.error_type in {"AccountSessionRequiredError", "AccountSessionMismatchError"}:
                raise AccountSessionRequiredError("Photos session requires operator login") from None
            if exc.error_type == "OriginalMismatchError":
                raise VerificationError("cold read-back original does not match the manifest") from exc
            raise ReadbackUnavailableError(exc.error_type) from exc
        return {
            **receipt,
            "cold_inputs": "provider-only",
            "cold_mounts": list(COLD_MOUNTS),
            "provider_asset_id": str(segment.asset_id),
            "provider_segment_index": segment.segment_index,
            "provider_share_sha256": hashlib.sha256((segment.share_url or "").encode()).hexdigest(),
        }

    def _create_cold_child(self, client, destination, stopped, lease):
        labels = {"pixav.photos_canary.role": self.LABEL}
        extra = {}
        if lease is not None:
            labels.update(
                {"pixav.photos_ui.session_id": str(lease.ticket.id), "pixav.photos_canary.owner": self._owner}
            )
            extra["name"] = "pixav-cold-" + lease.ticket.id.hex
            lease.child_phase = "CREATE_REQUESTED"
        try:
            container = client.containers.create(
                self._image,
                command=["python", "-m", self.ENTRY],
                stdin_open=True,
                user=f"{os.getuid()}:{os.getgid()}",
                labels=labels,
                environment={"PYTHONPATH": COLD_SOURCE_MOUNT, "HOME": COLD_WORK_MOUNT},
                volumes={
                    self._host(destination): {"bind": COLD_WORK_MOUNT, "mode": "rw"},
                },
                working_dir=COLD_WORK_MOUNT,
                **({"mem_limit": "4g", "nano_cpus": 2_000_000_000} if stopped is not None else {}),
                **extra,
            )
        except BaseException:
            if lease is not None:
                lease.child_phase = "CREATE_UNKNOWN"
            raise
        if lease is not None:
            lease.child_container_id = container.id
            lease.child_phase = "CREATED"
        return container

    def _run(
        self,
        destination: Path,
        payload: bytes,
        *,
        timeout_seconds: int | None = None,
        stopped: threading.Event | None = None,
    ) -> dict:
        client = self._client()
        lease = current_photos_ui_lease()
        container = self._create_cold_child(client, destination, stopped, lease)
        try:
            self._assert_isolated(container)
            stream = container.attach_socket(params={"stdin": 1, "stream": 1})
            try:
                stream._sock.sendall(payload)
                stream._sock.shutdown(socket.SHUT_WR)
                container.start()
                if stopped is None:
                    status = container.wait(timeout=timeout_seconds or self._timeout_seconds)
                else:
                    deadline = time.monotonic() + (timeout_seconds or self._timeout_seconds)
                    while True:
                        if stopped.is_set():
                            raise VerificationError("cold UI ownership lost")
                        if time.monotonic() >= deadline:
                            raise TimeoutError("cold activity deadline expired")
                        container.reload()
                        if container.attrs["State"]["Status"] != "running":
                            status = {"StatusCode": container.attrs["State"]["ExitCode"]}
                            break
                        stopped.wait(0.25)
            finally:
                stream.close()
            return self._receipt(container, status)
        finally:
            container.remove(force=True)
            if lease is not None:
                lease.child_phase = "REMOVED"

    @staticmethod
    def _assert_isolated(container: Any) -> None:
        """The container must carry only the work mount and no others."""
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
        if not isinstance(report, dict):
            report = {}
        if code != 0 or report.get("status") == "BLOCKED":
            # The child only ever reports an exception type, so nothing here can
            # carry a share location, a path or a provider payload.
            raise ColdProcessError(str(report.get("error_type", "NO_REPORT")))
        return report
