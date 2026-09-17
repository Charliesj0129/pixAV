"""The retained upload guest as a journalled fact of the managed pipeline.

Creating a Pixel-compatible guest is an external side effect with the same
hazards as an upload: it can succeed while the process that asked for it dies.
So the intent is written to PostgreSQL first, the container's identity is
written as soon as it exists, and a runtime whose intent is unresolved is handed
to an operator instead of being recreated — a second guest could be carrying a
signed-in account or an upload in flight.

The guest is retained across executions because it holds a Google session.
Switching accounts therefore retires the runtime explicitly rather than signing
a second account into the same device, which would make the original-quality
backup attribution impossible to prove.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from pathlib import Path
from typing import Any

import asyncpg

from pixav.pixel_injector.canary import OWNER_LABEL, TOOLS_IMAGE, CanaryBlockedError, owned_containers
from pixav.pixel_injector.profiles import AndroidProfile, get_profile
from pixav.shared.host_paths import host_path

logger = logging.getLogger(__name__)

GUEST_STAGING_MOUNT = "/parts"
GUEST_DATA_MOUNT = "/data"
BOOT_DEADLINE_SECONDS = 240
_ROLES = ("guest", "tools")


class ManagedRuntime:
    """Provision, verify and retire the one guest this worker owns."""

    def __init__(
        self,
        pool: asyncpg.Pool,
        client: Any,
        *,
        owner: str,
        staging_root: Path,
        guest_data_root: Path,
        profile_name: str,
        profiles_path: str | None = None,
        tools_image: str = TOOLS_IMAGE,
        host_project_root: str = "",
        project_root: Path | None = None,
    ) -> None:
        self.pool = pool
        self.client = client
        self.owner = owner
        self.staging_root = staging_root
        self.guest_data_root = guest_data_root
        self.profile_name = profile_name
        self.profiles_path = profiles_path
        self.tools_image = tools_image
        # A bind-mount source is resolved by the Docker daemon on its own host.
        # When this worker is itself containerised, handing it the path this
        # process sees makes the daemon create an empty host directory and mount
        # that instead of the staged bytes — with no error anywhere.
        self.host_project_root = host_project_root
        self.project_root = project_root

    def host_source(self, path: Path) -> str:
        """The path the Docker daemon must be given for a bind-mount source."""
        return str(host_path(path, host_project_root=self.host_project_root, project_root=self.project_root))

    # ── journal ──────────────────────────────────────────────────────────────

    def _profile(self) -> AndroidProfile:
        return get_profile(self.profile_name, path=self.profiles_path)

    async def _journal(self, state: str, **fields: Any) -> None:
        """Persist what is about to happen before it happens."""
        report = {
            "owner": self.owner,
            "state": state,
            "profile": self.profile_name,
            "guest_image": self._profile().image,
            "tools_image": self.tools_image,
            "staging_root": str(self.staging_root),
            **{key: (str(value) if value is not None else None) for key, value in fields.items()},
        }
        accepted = await self.pool.fetchval("SELECT journal_runtime($1::jsonb)", json.dumps(report))
        if not accepted:
            raise CanaryBlockedError("runtime journal rejected; an operator is holding this runtime")

    async def _record(self) -> dict | None:
        row = await self.pool.fetchrow("SELECT * FROM guest_runtimes WHERE owner=$1", self.owner)
        return dict(row) if row else None

    # ── acquire ──────────────────────────────────────────────────────────────

    async def acquire(self, account_id: uuid.UUID | None = None) -> tuple[Any, Any]:
        """Return the retained guest and tools containers for this account."""
        record = await self._record()
        containers = await asyncio.to_thread(owned_containers, self.client, self.owner)

        if record is not None and record["state"] == "REVIEW_REQUIRED":
            raise CanaryBlockedError("retained runtime is held for operator review")

        if record is not None and record["state"] in {"INTENT", "GUEST_CREATED"} and containers:
            # A creation was interrupted. The containers that exist may already
            # hold credentials or a partial upload, so nothing here replaces them.
            await self._journal("REVIEW_REQUIRED", review_reason="interrupted runtime creation")
            raise CanaryBlockedError("interrupted runtime creation; inspect the retained containers")

        if record is not None and record["state"] == "READY":
            signed_in = str(record["account_id"]) if record["account_id"] else None
            if account_id is not None and signed_in != str(account_id):
                await self._retire(containers)
                record, containers = await self._record(), []
            else:
                return await self._retained(record)

        if record is not None and record["state"] == "RETIRING":
            await self._retire(containers)
            record, containers = await self._record(), []

        if containers:
            # Containers with this owner label exist that no journal explains.
            await self._journal("REVIEW_REQUIRED", review_reason="unjournalled owned containers")
            raise CanaryBlockedError("owned containers exist without a runtime journal")

        await self._provision(account_id)
        return await self._retained(await self._record())

    async def _provision(self, account_id: uuid.UUID | None) -> None:
        profile = self._profile()
        await self._journal("INTENT", account_id=account_id)
        self.guest_data_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.staging_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        guest = await asyncio.to_thread(
            lambda: self.client.containers.run(
                profile.image,
                command=list(profile.args),
                name=f"pixav-managed-guest-{self.owner}",
                detach=True,
                privileged=True,
                labels={OWNER_LABEL: self.owner, "pixav.photos_canary.role": "guest"},
                volumes={self.host_source(self.guest_data_root): {"bind": GUEST_DATA_MOUNT, "mode": "rw"}},
                ports={"5555/tcp": ("127.0.0.1", None)},
            )
        )
        await self._journal("GUEST_CREATED", guest_id=guest.id, account_id=account_id)
        tools = await asyncio.to_thread(
            lambda: self.client.containers.run(
                self.tools_image,
                name=f"pixav-managed-tools-{self.owner}",
                detach=True,
                network_mode=f"container:{guest.id}",
                labels={OWNER_LABEL: self.owner, "pixav.photos_canary.role": "tools"},
                volumes={self.host_source(self.staging_root): {"bind": GUEST_STAGING_MOUNT, "mode": "ro"}},
                tmpfs={"/tmp": "rw,exec,nosuid,nodev,mode=1777"},  # noqa: S108 - private container tmpfs
            )
        )
        await self._journal("READY", guest_id=guest.id, tools_id=tools.id, account_id=account_id)

    # ── verification ─────────────────────────────────────────────────────────

    def _verify(self, item: Any, *, role: str, image: str, destination: str, writable: bool) -> None:
        """Every recorded property must still hold, or this is not our runtime."""
        if (
            item.labels.get(OWNER_LABEL) != self.owner
            or item.labels.get("pixav.photos_canary.role") != role
            or "pixav.task_id" in item.labels
        ):
            raise CanaryBlockedError("retained runtime ownership mismatch")
        if item.attrs.get("Config", {}).get("Image") not in (None, image):
            raise CanaryBlockedError("retained runtime image changed")
        # The daemon reports the source as it resolved it, so the comparison has
        # to be against the host path this worker asked for, not its own view.
        source = self.host_source(self.guest_data_root if role == "guest" else self.staging_root)
        mounts = item.attrs.get("Mounts", [])
        expected = [m for m in mounts if m["Destination"] == destination]
        if len(expected) != 1 or str(expected[0]["Source"]) != source or expected[0].get("RW") != writable:
            raise CanaryBlockedError("retained runtime mount mismatch")
        if any(m.get("Type") == "bind" and m["Destination"] != destination for m in mounts):
            raise CanaryBlockedError("unexpected retained runtime bind mount")

    async def _retained(self, record: dict | None) -> tuple[Any, Any]:
        """Attach to the exact recorded containers; never create or replace."""
        if record is None or record["state"] != "READY":
            raise CanaryBlockedError("no usable retained runtime")
        profile = self._profile()
        items = []
        for role, key, image, destination, writable in (
            ("guest", "guest_id", profile.image, GUEST_DATA_MOUNT, True),
            ("tools", "tools_id", self.tools_image, GUEST_STAGING_MOUNT, False),
        ):
            if not record[key]:
                raise CanaryBlockedError("retained runtime is missing a container identity")
            item = await asyncio.to_thread(self.client.containers.get, record[key])
            self._verify(item, role=role, image=image, destination=destination, writable=writable)
            items.append(item)
        if items[1].attrs["HostConfig"]["NetworkMode"] != "container:" + items[0].id:
            raise CanaryBlockedError("retained tools namespace changed")
        for item in items:
            if item.status != "running":
                await asyncio.to_thread(item.start)
        await self._await_boot(items[0])
        return items[0], items[1]

    async def _await_boot(self, guest: Any) -> None:
        for _ in range(BOOT_DEADLINE_SECONDS // 2):
            result = await asyncio.to_thread(guest.exec_run, ["getprop", "sys.boot_completed"])
            if result.exit_code == 0 and result.output.strip() == b"1":
                return
            await asyncio.sleep(2)
        raise CanaryBlockedError("retained guest boot deadline exceeded")

    # ── retirement ───────────────────────────────────────────────────────────

    async def _retire(self, containers: list[Any]) -> None:
        """Stop and remove this worker's guest before a different account signs in."""
        await self._journal("RETIRING")
        for item in sorted(containers, key=lambda c: c.labels.get("pixav.photos_canary.role") != "tools"):
            await asyncio.to_thread(item.stop, timeout=30)
            await asyncio.to_thread(item.remove, force=False)
        forgotten = await self.pool.fetchval("SELECT forget_runtime_containers($1)", uuid.UUID(self.owner))
        if not forgotten:
            raise CanaryBlockedError("runtime retirement was not recorded")
        logger.info("retired the managed upload runtime for owner %s", self.owner)
