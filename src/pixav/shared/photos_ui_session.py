"""Persistent Photos resource ownership under the original activity lease.

Exiting a scope never frees the device. Only an authority that independently
observes the original writers stopped can clear this fence. Database loss also
leaves the persistent row in place while the caller halts its exact writers.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID, uuid4

from pixav.pixel_injector.canary import CanaryBlockedError
from pixav.pixel_injector.session import WindowsAndroidSession
from pixav.shared.activity_context import require_activity_context
from pixav.shared.photos_ui_ownership import PhotosUIConnection, photos_ui_ownership


@dataclass(frozen=True, slots=True)
class PhotosUITicket:
    id: UUID
    token: UUID = field(repr=False)


class PhotosUILease:
    def __init__(self, guard: PhotosUIConnection, ticket: PhotosUITicket) -> None:
        self.guard, self.ticket = guard, ticket
        self._valid_until = 0.0
        self._lost = False
        self.child_phase = "NOT_STARTED"
        self.child_container_id: str | None = None

    def check(self) -> None:
        if self._lost or time.monotonic() >= self._valid_until:
            raise CanaryBlockedError("Photos UI session lease lost; retain persistent fence")
        self.guard()

    async def _deadline(self) -> None:
        started = time.monotonic()
        remaining = await self.guard.connection.fetchval(
            """SELECT EXTRACT(EPOCH FROM (lease_until-clock_timestamp()))
            FROM photos_ui_sessions WHERE id=$1 AND state='ACTIVE'""",
            self.ticket.id,
        )
        # The DB clock sets the deadline; monotonic time only measures its
        # remaining duration. Subtract a margin and the query round trip.
        if remaining is None or float(remaining) <= 1:
            self._lost = True
            raise CanaryBlockedError("Photos UI session deadline unavailable")
        self._valid_until = started + float(remaining) - 1

    async def _heartbeat(self, owner: asyncio.Task, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                accepted = await self.guard.connection.fetchval(
                    "SELECT heartbeat_photos_ui_session($1,$2)", self.ticket.id, self.ticket.token
                )
                if not accepted:
                    raise CanaryBlockedError("Photos UI heartbeat rejected")
                await self._deadline()
            except Exception:
                self._lost = True
                owner.cancel("Photos UI persistent lease lost")
                return

    async def _halt(self, halt: Callable[[PhotosUITicket], Awaitable[dict]], failed: bool) -> None:
        reason = "UI_GUARD_LOST" if failed else "UI_SCOPE_ENDED"
        evidence: dict = {}
        cancelled = False
        halt_task: asyncio.Future[dict] = asyncio.ensure_future(halt(self.ticket))
        try:
            while not halt_task.done():
                try:
                    await asyncio.shield(halt_task)
                except asyncio.CancelledError:
                    cancelled = True
            evidence = halt_task.result()
        except BaseException:
            reason = "NATIVE_HALT_UNCONFIRMED"
            raise
        finally:
            # This request never releases a slot. A disconnected DB may
            # refuse it; the original ACTIVE fence must then survive.
            if self.guard.connected():
                recorded = await self.guard.connection.fetchval(
                    "SELECT request_photos_ui_halt($1,$2,$3::jsonb)",
                    self.ticket.id,
                    self.ticket.token,
                    json.dumps({"id": str(uuid4()), "reason": reason, "evidence": evidence}),
                )
                if not recorded:
                    raise CanaryBlockedError("Photos UI halt request rejected; retain persistent fence")
        if cancelled:
            raise asyncio.CancelledError


_CURRENT_UI: ContextVar[PhotosUILease | None] = ContextVar("pixav_photos_ui", default=None)


def current_photos_ui_lease() -> PhotosUILease | None:
    return _CURRENT_UI.get()


@asynccontextmanager
async def photos_ui_session(
    pool: Any,
    runtime: WindowsAndroidSession,
    *,
    role: Literal["upload", "cold_read", "observe"],
    halt: Callable[[PhotosUITicket], Awaitable[dict]],
    heartbeat_seconds: float = 10,
) -> AsyncIterator[PhotosUILease]:
    context = require_activity_context()
    request = context.request
    account_matches = request.account_id == runtime.account_id
    if role == "cold_read" and request.stage == "verify" and request.account_id is None:
        account_matches = bool(
            await pool.fetchval(
                """SELECT EXISTS(SELECT 1 FROM remote_asset_segments segment
                JOIN operation_intents operation ON operation.id=$4 AND operation.execution_id=$5
                  AND operation.stage='verify'
                WHERE segment.asset_id=$1 AND segment.segment_index=$2 AND segment.account_id=$3
                  AND operation.identity='verify:'||segment.asset_id::text||':'||segment.segment_index::text)""",
                request.asset_id,
                request.segment_index,
                runtime.account_id,
                request.operation_id,
                request.execution_id,
            )
        )
    if role == "cold_read" and request.stage == "playback" and request.account_id is None:
        account_matches = bool(
            await pool.fetchval(
                """SELECT EXISTS(SELECT 1 FROM playable_assets p JOIN remote_asset_segments s
            ON s.asset_id=p.remote_asset_id WHERE p.id=$1 AND s.account_id=$2)""",
                request.playable_asset_id,
                runtime.account_id,
            )
        )
    # The persistent claim additionally requires an exact authority-accepted
    # original review before admitting a cold reader in the upload stage.
    if not account_matches or (
        (role == "upload") != (request.stage == "upload") and not (role == "cold_read" and request.stage == "upload")
    ):
        raise CanaryBlockedError("Photos UI activity/account binding changed")
    if not 0 < heartbeat_seconds <= 15:
        raise ValueError("Photos UI heartbeat interval must be at most 15 seconds")
    owner = asyncio.current_task()
    if owner is None:
        raise RuntimeError("Photos UI session requires an activity task")
    ticket = PhotosUITicket(uuid4(), uuid4())
    async with photos_ui_ownership(pool, runtime.account_id, str(runtime.owner)) as guard:
        report = {
            "id": str(ticket.id),
            "token": str(ticket.token),
            "runtime_id": str(runtime.runtime_id),
            "runtime_generation": runtime.generation,
            "runtime_owner": str(runtime.owner),
            "account_id": str(runtime.account_id),
            "role": role,
            "execution_id": str(request.execution_id),
            "attempt_id": str(request.attempt_id),
            "operation_id": str(request.operation_id),
            "generation": request.generation,
            "owner": str(request.owner),
            "worker_token": str(context.worker_token),
        }
        accepted = await guard.connection.fetchval("SELECT acquire_photos_ui_session($1::jsonb)", json.dumps(report))
        if not accepted:
            raise CanaryBlockedError("Photos UI persistent resource claim rejected")
        lease = PhotosUILease(guard, ticket)
        heartbeat = None
        previous = None
        failed = True
        try:
            await lease._deadline()
            heartbeat = asyncio.create_task(lease._heartbeat(owner, heartbeat_seconds))
            lease.check()
            previous = _CURRENT_UI.set(lease)
            yield lease
            lease.check()
            failed = False
        finally:
            if previous is not None:
                _CURRENT_UI.reset(previous)
            lease._lost = True
            if heartbeat is not None:
                heartbeat.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat
            await lease._halt(halt, failed)
