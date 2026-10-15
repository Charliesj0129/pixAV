"""Authority-only verification of stopped Windows UI resources.

Worker receipts request reconciliation. Fresh host observations and the exact
Docker daemon/container must both prove cessation before the DB fence moves.
This module never retries an upload or changes its operation/generation.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from docker.errors import NotFound

from pixav.pixel_injector.canary import OWNER_LABEL
from pixav.pixel_injector.windows_control import WindowsHostClient
from pixav.pixel_injector.windows_host_models import HostHaltReceipt, HostUIRequest

logger = logging.getLogger(__name__)


class WindowsUIReconciler:
    def __init__(
        self, pool: Any, docker: Any, control: WindowsHostClient, *, docker_daemon_id: str, cold_image: str = ""
    ) -> None:
        if not docker_daemon_id:
            raise ValueError("Windows reconciliation requires the original Docker daemon identity")
        self.pool, self.docker, self.control, self.docker_daemon_id = pool, docker, control, docker_daemon_id
        self.cold_image = cold_image

    async def tick(self) -> int:
        identity = await asyncio.to_thread(self.docker.info)
        if identity.get("ID") != self.docker_daemon_id:
            return 0
        rows = await self.pool.fetch("""SELECT s.*,r.tools_id,r.tools_image,r.identity_sha256
            FROM photos_ui_sessions s JOIN upload_runtimes r
              ON r.runtime_id=s.runtime_id AND r.generation=s.runtime_generation
            WHERE r.kind='windows_android' AND (s.state='HALT_REQUIRED'
              OR (s.state='ACTIVE' AND s.lease_until<=clock_timestamp()))
            ORDER BY s.created_at LIMIT 10""")
        completed = 0
        for row in rows:
            try:
                completed += int(await self._reconcile(row))
            except Exception:
                # Keep the fence and original request. No provider/host payload
                # or credential-bearing exception is emitted to logs.
                logger.warning("Windows UI halt reconciliation remains pending")
        return completed

    async def _reconcile(self, row: Any) -> bool:
        if row["role"] == "cold_read" and not await self._readers_stopped(row):
            return False
        request = HostUIRequest(
            runtime_id=row["runtime_id"],
            runtime_generation=row["runtime_generation"],
            identity_sha256=row["identity_sha256"],
            session_id=row["id"],
            session_token=row["token"],
            operation_id=row["operation_id"],
            generation=row["generation"],
            role=row["role"],
        )
        try:
            runner = await asyncio.to_thread(self.docker.containers.get, row["tools_id"])
        except NotFound:
            runner = None
        if runner is not None:
            await asyncio.to_thread(runner.reload)
            if (
                runner.id != row["tools_id"]
                or runner.labels.get(OWNER_LABEL) != str(row["runtime_owner"])
                or runner.labels.get("pixav.runtime.id") != str(row["runtime_id"])
                or runner.labels.get("pixav.runtime.generation") != str(row["runtime_generation"])
                or runner.attrs.get("Config", {}).get("Image") != row["tools_image"]
            ):
                return False
            if runner.status != "exited":
                await asyncio.to_thread(runner.stop, timeout=10)
                await asyncio.to_thread(runner.reload)
            if runner.status != "exited":
                return False
        # An expired owner may have died before requesting halt. Stop only its
        # bound original native resource, then independently observe it again.
        try:
            raw = await asyncio.to_thread(self.control.call, "verify-halt", request)
        except Exception:
            await asyncio.to_thread(self.control.call, "halt", request.model_copy(update={"cold": True}))
            raw = await asyncio.to_thread(self.control.call, "verify-halt", request)
        receipt = HostHaltReceipt.model_validate(raw)
        expected = request.public_binding()
        actual = receipt.model_dump(mode="json")
        if (
            any(actual.get(key) != value for key, value in expected.items())
            or receipt.tools_id != row["tools_id"]
            or receipt.device_key != row["device_key"]
        ):
            return False
        evidence = {**receipt.model_dump(mode="json", by_alias=True), "runner_stopped": True}
        if (await asyncio.to_thread(self.docker.info)).get("ID") != self.docker_daemon_id:
            return False
        return bool(
            await self.pool.fetchval(
                "SELECT record_verified_photos_ui_halt($1,$2,$3::jsonb)", row["id"], row["token"], json.dumps(evidence)
            )
        )

    # Keep the independent reader ownership/retirement checks explicit.
    async def _readers_stopped(self, row: Any) -> bool:  # noqa: C901
        if not self.cold_image:
            return False
        readers = await asyncio.to_thread(
            self.docker.containers.list, all=True, filters={"label": ["pixav.photos_ui.session_id=" + str(row["id"])]}
        )
        for reader in readers:
            await asyncio.to_thread(reader.reload)
            if (
                reader.labels.get(OWNER_LABEL) != str(row["runtime_owner"])
                or reader.labels.get("pixav.photos_canary.role") not in {"cold-readback", "playback-materialize"}
                or reader.attrs.get("Config", {}).get("Image") != self.cold_image
            ):
                return False
            if reader.status == "running":
                await asyncio.to_thread(reader.stop, timeout=10)
                await asyncio.to_thread(reader.reload)
            if reader.status not in {"exited", "created", "dead"}:
                return False
            # Retire this exact Docker object before handing off the resource.
            # A late start RPC must encounter a missing ID, not a stopped object.
            # Bind-mounted work/evidence is retained; no volume removal is used.
            await asyncio.to_thread(reader.remove)
            if not await self._known_child_absent(reader.id):
                return False
        report = await self.pool.fetchval(
            "SELECT evidence FROM photos_ui_halt_requests WHERE session_id=$1 ORDER BY created_at DESC LIMIT 1",
            row["id"],
        )
        if isinstance(report, str):
            report = json.loads(report)
        child = report.get("cold_child", {}) if isinstance(report, dict) else {}
        # A hard crash during Docker create may leave a late external result.
        # Stopping discovered readers does not prove an unknown create terminal.
        if child.get("phase") not in {"NOT_STARTED", "CREATED", "REMOVED"}:
            return False
        known_id = child.get("container_id")
        if child.get("phase") != "NOT_STARTED" and not known_id:
            return False
        if known_id and not readers:
            return await self._known_child_absent(known_id)
        return not readers or all(reader.id == known_id for reader in readers)

    async def _known_child_absent(self, identity: str) -> bool:
        try:
            await asyncio.to_thread(self.docker.containers.get, identity)
        except NotFound:
            return True
        return False
