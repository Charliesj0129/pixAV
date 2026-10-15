"""Attach to a registered external device; never provision or replace one."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from pixav.pixel_injector.android_target import AndroidTarget
from pixav.pixel_injector.canary import CanaryBlockedError
from pixav.pixel_injector.session import WindowsAndroidSession
from pixav.pixel_injector.windows_control import BoundWindowsControl, WindowsHostClient
from pixav.pixel_injector.windows_device import WindowsAndroidDevice
from pixav.pixel_injector.windows_host_models import NativeIdentity, canonical_digest
from pixav.shared.photos_ui_session import PhotosUILease, photos_ui_session


class WindowsManagedRuntime:
    def __init__(
        self,
        pool: Any,
        docker: Any,
        control: WindowsHostClient,
        *,
        runtime_id: uuid.UUID,
        generation: int,
        owner: uuid.UUID,
        staging_root: Path,
        profile: str,
        server_host: str = "host.docker.internal",
        docker_daemon_id: str = "",
    ) -> None:
        self.pool, self.docker, self.control = pool, docker, control
        self.runtime_id, self.generation, self.owner = runtime_id, generation, owner
        self.staging_root, self.profile, self.server_host = staging_root, profile, server_host
        self.docker_daemon_id = docker_daemon_id

    async def binding(self, account_id: uuid.UUID) -> WindowsAndroidSession:
        row = await self.pool.fetchrow(
            """SELECT * FROM upload_runtimes WHERE runtime_id=$1 AND generation=$2
            AND owner=$3 AND account_id=$4 AND kind='windows_android' AND state='READY'""",
            self.runtime_id,
            self.generation,
            self.owner,
            account_id,
        )
        if row is None or row["profile"] != self.profile or Path(row["staging_root"]) != self.staging_root:
            raise CanaryBlockedError("registered Windows runtime binding unavailable")
        raw = json.loads(row["identity"]) if isinstance(row["identity"], str) else row["identity"]
        try:
            identity = NativeIdentity.model_validate(raw)
            if identity.environment is None or identity.environment.profile != row["profile"]:
                raise ValueError("managed admission requires complete environment evidence")
            if canonical_digest(identity.payload()) != row["identity_sha256"]:
                raise ValueError("identity digest changed")
            return WindowsAndroidSession(
                runtime_id=row["runtime_id"],
                generation=row["generation"],
                owner=row["owner"],
                account_id=row["account_id"],
                device_key=row["device_key"],
                identity_sha256=row["identity_sha256"],
                avd_name=identity.avd_name,
                boot_id=identity.boot_id,
                tools_id=row["tools_id"],
                tools_image=row["tools_image"],
                target=AndroidTarget(identity.serial, self.server_host),
            )
        except (TypeError, ValueError):
            raise CanaryBlockedError("Windows runtime identity invalid; reconciliation required") from None

    @asynccontextmanager
    async def acquire(
        self, account_id: uuid.UUID, *, role: Literal["upload", "cold_read", "observe"] = "upload"
    ) -> AsyncIterator[tuple[WindowsAndroidDevice, PhotosUILease]]:
        session = await self.binding(account_id)
        identity = await asyncio.to_thread(self.docker.info)
        if not self.docker_daemon_id or identity.get("ID") != self.docker_daemon_id:
            raise CanaryBlockedError("Windows tools Docker daemon identity changed")
        try:
            runner = await asyncio.to_thread(self.docker.containers.get, session.tools_id)
        except Exception:
            raise CanaryBlockedError("registered Windows tools missing; reconcile original runtime") from None
        device = None
        native = None
        failed = True

        async def halt(ticket) -> dict:
            if device is None or native is None:
                raise CanaryBlockedError("Windows runtime claim outcome unconfirmed")
            if failed:
                await device.halt()
            else:
                await device.quiesce()
            return native.last_halt

        async with photos_ui_session(self.pool, session, role=role, halt=halt) as lease:
            native = BoundWindowsControl(self.control, session, lease.ticket)

            async def stop(_session) -> None:
                await asyncio.to_thread(native.halt, cold=True)

            async def quiesce(_session) -> None:
                await asyncio.to_thread(native.halt, cold=False)

            device = WindowsAndroidDevice(
                session, runner, verify_native=native.verify, stop_native=stop, quiesce_native=quiesce
            )
            device.ui_session_id = str(lease.ticket.id)
            await asyncio.to_thread(device._runner_identity)
            session.target.require_runner(runner)
            await asyncio.to_thread(native.claim)
            if runner.status == "exited":
                await asyncio.to_thread(runner.start)
            await asyncio.to_thread(device.verify, session)
            lease.check()
            yield device, lease
            lease.check()
            failed = False

    @asynccontextmanager
    async def cold(self, account_id: uuid.UUID) -> AsyncIterator[PhotosUILease]:
        # A browser cold read must survive an absent native guest/tools. Its
        # original account/runtime journal still binds the persistent fence.
        session = await self.binding(account_id)
        identity = await asyncio.to_thread(self.docker.info)
        if not self.docker_daemon_id or identity.get("ID") != self.docker_daemon_id:
            raise CanaryBlockedError("Windows cold Docker daemon identity changed")
        native = None
        lease: PhotosUILease

        async def halt(ticket) -> dict:
            if native is None:
                raise CanaryBlockedError("Windows cold resource claim unconfirmed")
            evidence = await asyncio.to_thread(native.halt, cold=False)
            return {**evidence, "cold_child": {"phase": lease.child_phase, "container_id": lease.child_container_id}}

        async with photos_ui_session(self.pool, session, role="cold_read", halt=halt) as lease:
            native = BoundWindowsControl(self.control, session, lease.ticket, role="cold_read")
            await asyncio.to_thread(native.claim)
            lease.check()
            yield lease
            lease.check()
