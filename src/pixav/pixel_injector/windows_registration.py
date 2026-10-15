"""Enroll one existing Windows environment after fresh read-only observations.

This never provisions a guest/tools, starts a writer, replays an operation or
advances an execution. The only apply effects are the atomic registry/audit rows.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, model_validator

from pixav.pixel_injector.android_target import AndroidTarget
from pixav.pixel_injector.canary import OWNER_LABEL
from pixav.pixel_injector.windows_control import WindowsHostClient
from pixav.pixel_injector.windows_host_models import HostReadinessReceipt, HostReadinessRequest, WindowsHostManifest
from pixav.shared.instance import database_identity
from pixav.shared.photos_ui_ownership import photos_ui_ownership

_PARTS_STAT = "import json,os; s=os.stat('/parts'); print(json.dumps({'device':s.st_dev,'inode':s.st_ino}))"


class RegistrationError(ValueError):
    """A bounded operator code, never a provider/DB/credential-bearing error."""


class WindowsRegistration(BaseModel):
    model_config = {"frozen": True, "extra": "forbid"}
    host: WindowsHostManifest
    owner: UUID
    account_id: UUID = Field(repr=False)
    tools_image: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    docker_daemon_id: str = Field(min_length=1, max_length=128)
    staging_root: str
    staging_host_root: str
    staging_device: int = Field(ge=0, strict=True)
    staging_inode: int = Field(gt=0, strict=True)
    native_script_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def complete_environment_and_paths(self) -> WindowsRegistration:
        if self.host.native.environment is None:
            raise ValueError("complete Windows environment required")
        for value in (self.staging_root, self.staging_host_root):
            path = PurePosixPath(value)
            if not path.is_absolute() or ".." in path.parts or str(path) != value:
                raise ValueError("exact absolute staging path required")
        return self

    def runtime_values(self) -> tuple:
        host = self.host
        if host.native.environment is None:
            raise RegistrationError("WINDOWS_ENVIRONMENT_INCOMPLETE")
        return (
            host.runtime_id,
            host.runtime_generation,
            self.owner,
            self.account_id,
            host.device_key,
            host.native.environment.profile,
            json.dumps(host.native.payload()),
            host.identity_sha256,
            host.tools_id,
            self.tools_image,
            self.staging_root,
        )


def inspect_tools(docker: Any, spec: WindowsRegistration) -> None:
    """Validate the existing immutable object and actual bind; read-only exec only."""
    if docker.info().get("ID") != spec.docker_daemon_id:
        raise RegistrationError("WINDOWS_DOCKER_IDENTITY_CHANGED")
    runner = docker.containers.get(spec.host.tools_id)
    runner.reload()
    attrs, labels = runner.attrs, runner.labels
    expected = {
        OWNER_LABEL: str(spec.owner),
        "pixav.photos_canary.role": "tools",
        "pixav.runtime.id": str(spec.host.runtime_id),
        "pixav.runtime.generation": str(spec.host.runtime_generation),
    }
    if (
        runner.id != spec.host.tools_id
        or runner.status != "running"
        or any(labels.get(k) != v for k, v in expected.items())
        or "pixav.task_id" in labels
        or attrs.get("Config", {}).get("Image") != spec.tools_image
    ):
        raise RegistrationError("WINDOWS_TOOLS_BINDING_CHANGED")
    AndroidTarget(spec.host.native.serial, "host.docker.internal").require_runner(runner)
    config = attrs.get("HostConfig", {})
    if (
        not config.get("ReadonlyRootfs")
        or config.get("Privileged")
        or config.get("NetworkMode") == "host"
        or config.get("PidMode") == "host"
        or config.get("IpcMode") == "host"
        or config.get("CapAdd")
        or config.get("Devices")
        or config.get("DeviceRequests")
        or "ALL" not in config.get("CapDrop", [])
        or "no-new-privileges:true" not in config.get("SecurityOpt", [])
        or config.get("PortBindings")
    ):
        raise RegistrationError("WINDOWS_TOOLS_ISOLATION_INVALID")
    binds = [m for m in attrs.get("Mounts", []) if m.get("Type") != "tmpfs"]
    if (
        len(binds) != 1
        or binds[0].get("Type") != "bind"
        or binds[0].get("Destination") != "/parts"
        or binds[0].get("Source") != spec.staging_host_root
        or binds[0].get("RW") is not False
    ):
        raise RegistrationError("WINDOWS_STAGING_BIND_CHANGED")
    root = Path(spec.staging_host_root)
    if root.resolve() != root or not root.is_dir():
        raise RegistrationError("WINDOWS_STAGING_PATH_INVALID")
    stat = root.stat()
    if (stat.st_dev, stat.st_ino) != (spec.staging_device, spec.staging_inode):
        raise RegistrationError("WINDOWS_STAGING_PHYSICAL_IDENTITY_CHANGED")
    probe = runner.exec_run(["python", "-c", _PARTS_STAT])
    if probe.exit_code != 0 or len(probe.output) > 1024:
        raise RegistrationError("WINDOWS_STAGING_PROBE_UNCONFIRMED")
    try:
        actual = json.loads(probe.output)
    except (ValueError, UnicodeError):
        raise RegistrationError("WINDOWS_STAGING_PROBE_UNCONFIRMED") from None
    if actual != {"device": spec.staging_device, "inode": spec.staging_inode}:
        raise RegistrationError("WINDOWS_STAGING_CONTAINER_IDENTITY_CHANGED")


class WindowsRuntimeRegistry:
    def __init__(self, pool: Any, docker: Any, control: WindowsHostClient, *, db_identity: str, database: str) -> None:
        self.pool, self.docker, self.control = pool, docker, control
        self.db_identity, self.database = db_identity, database

    async def _instance(self, conn: Any) -> None:
        if (
            await database_identity(conn) != self.db_identity
            or await conn.fetchval("SELECT current_database()") != self.database
        ):
            raise RegistrationError("WINDOWS_REGISTRY_DATABASE_IDENTITY_CHANGED")

    async def _rows(self, conn: Any, spec: WindowsRegistration) -> Any:
        await self._instance(conn)
        environment = spec.host.native.environment
        if environment is None:
            raise RegistrationError("WINDOWS_ENVIRONMENT_INCOMPLETE")
        if not await conn.fetchval("SELECT EXISTS(SELECT 1 FROM accounts WHERE id=$1)", spec.account_id):
            raise RegistrationError("WINDOWS_ACCOUNT_NOT_REGISTERED")
        if await conn.fetchval(
            """SELECT EXISTS(SELECT 1 FROM photos_ui_sessions s JOIN upload_runtimes r
          ON r.runtime_id=s.runtime_id AND r.generation=s.runtime_generation
          WHERE s.state IN ('ACTIVE','HALT_REQUIRED') AND
          (s.account_id=$1 OR s.runtime_owner=$2 OR s.device_key=$3 OR
            (r.identity->'environment'->>'host_machine_sha256'=$4 AND
             (r.identity->>'serial'=$5 OR
              (r.identity->'environment'->>'avd_directory_device'=$6 AND
               r.identity->'environment'->>'avd_directory_inode'=$7)))))""",
            spec.account_id,
            spec.owner,
            spec.host.device_key,
            environment.host_machine_sha256,
            spec.host.native.serial,
            str(environment.avd_directory_device),
            str(environment.avd_directory_inode),
        ):
            raise RegistrationError("WINDOWS_PREVIOUS_UI_OWNER_UNRECONCILED")
        row = await conn.fetchrow(
            "SELECT * FROM upload_runtimes WHERE runtime_id=$1 AND generation=$2",
            spec.host.runtime_id,
            spec.host.runtime_generation,
        )
        if row:
            actual = (
                row["runtime_id"],
                row["generation"],
                row["owner"],
                row["account_id"],
                row["device_key"],
                row["profile"],
                json.loads(row["identity"]) if isinstance(row["identity"], str) else row["identity"],
                row["identity_sha256"],
                row["tools_id"],
                row["tools_image"],
                row["staging_root"],
            )
            expected = list(spec.runtime_values())
            expected[6] = json.loads(expected[6])
            if actual != tuple(expected) or row["state"] not in {"REGISTERED", "READY"}:
                raise RegistrationError("WINDOWS_REGISTRY_GENERATION_CONFLICT")
            if row["state"] == "READY":
                await self._enrollment(conn, spec)
        if await conn.fetchval(
            """SELECT EXISTS(SELECT 1 FROM upload_runtimes
          WHERE kind='windows_android' AND state='READY'
            AND (runtime_id<>$1 OR generation<>$2)
            AND (device_key=$3 OR account_id=$4 OR owner=$5
              OR (identity->'environment'->>'host_machine_sha256'=$6 AND
                  (identity->>'serial'=$7 OR
                   (identity->'environment'->>'avd_directory_device'=$8 AND
                    identity->'environment'->>'avd_directory_inode'=$9)))))""",
            spec.host.runtime_id,
            spec.host.runtime_generation,
            spec.host.device_key,
            spec.account_id,
            spec.owner,
            environment.host_machine_sha256,
            spec.host.native.serial,
            str(environment.avd_directory_device),
            str(environment.avd_directory_inode),
        ):
            raise RegistrationError("WINDOWS_PREVIOUS_RUNTIME_REQUIRES_RETIREMENT")
        return row

    async def _enrollment(self, conn: Any, spec: WindowsRegistration) -> None:
        audit = await conn.fetchrow(
            "SELECT * FROM windows_runtime_enrollments WHERE runtime_id=$1 AND generation=$2",
            spec.host.runtime_id,
            spec.host.runtime_generation,
        )
        if audit is None:
            raise RegistrationError("WINDOWS_ENROLLMENT_BINDING_CHANGED")
        receipt = (
            json.loads(audit["native_receipt"]) if isinstance(audit["native_receipt"], str) else audit["native_receipt"]
        )
        if (
            audit["identity_sha256"] != spec.host.identity_sha256
            or audit["docker_daemon_id"] != spec.docker_daemon_id
            or (audit["staging_device"], audit["staging_inode"]) != (spec.staging_device, spec.staging_inode)
            or receipt.get("script_sha256") != spec.native_script_sha256
        ):
            raise RegistrationError("WINDOWS_ENROLLMENT_BINDING_CHANGED")

    async def _observe(self, spec: WindowsRegistration) -> HostReadinessReceipt:
        await asyncio.to_thread(inspect_tools, self.docker, spec)
        host = spec.host
        subject = HostReadinessRequest(
            runtime_id=host.runtime_id,
            runtime_generation=host.runtime_generation,
            identity_sha256=host.identity_sha256,
            nonce=uuid4(),
        )
        receipt = await asyncio.to_thread(self.control.readiness, subject)
        if (
            receipt.device_key != host.device_key
            or receipt.tools_id != host.tools_id
            or receipt.script_sha256 != spec.native_script_sha256
        ):
            raise RegistrationError("WINDOWS_NATIVE_READINESS_BINDING_CHANGED")
        await asyncio.to_thread(inspect_tools, self.docker, spec)
        return receipt

    async def enroll(
        self, spec: WindowsRegistration, *, apply: bool = False, operator: str = "", reason: str = ""
    ) -> dict:
        async with self.pool.acquire() as conn:
            await self._rows(conn, spec)
        if not apply:
            await self._observe(spec)
            async with self.pool.acquire() as conn:
                await self._rows(conn, spec)
            return {"mode": "DRY_RUN", "eligible": True, "registry_rows_written": 0, "execution_changed": False}
        if not operator.strip() or not reason.strip():
            raise RegistrationError("WINDOWS_REGISTRATION_OPERATOR_REASON_REQUIRED")
        async with photos_ui_ownership(self.pool, spec.account_id, str(spec.owner)) as guard:
            async with guard.connection.transaction():
                conn = guard.connection
                if not await conn.fetchval(
                    "SELECT pg_try_advisory_xact_lock(hashtextextended('pixav.windows.enrollment',0))"
                ):
                    raise RegistrationError("WINDOWS_REGISTRATION_BUSY")
                existing = await self._rows(conn, spec)
                receipt = await self._observe(spec)
                guard()
                await self._rows(conn, spec)
                if existing and existing["state"] == "READY":
                    return {"mode": "APPLY", "state": "READY", "already_registered": True, "execution_changed": False}
                await conn.execute(
                    """INSERT INTO upload_runtimes(runtime_id,generation,owner,kind,account_id,device_key,profile,
                  identity,identity_sha256,tools_id,tools_image,staging_root,state)
                  VALUES($1,$2,$3,'windows_android',$4,$5,$6,$7::jsonb,$8,$9,$10,$11,'REGISTERED')
                  ON CONFLICT(runtime_id,generation) DO NOTHING""",
                    *spec.runtime_values(),
                )
                await conn.execute(
                    """INSERT INTO windows_runtime_enrollments(runtime_id,generation,identity_sha256,
                  docker_daemon_id,staging_device,staging_inode,native_receipt,operator,reason)
                  VALUES($1,$2,$3,$4,$5,$6,$7::jsonb,$8,$9)""",
                    spec.host.runtime_id,
                    spec.host.runtime_generation,
                    spec.host.identity_sha256,
                    spec.docker_daemon_id,
                    spec.staging_device,
                    spec.staging_inode,
                    receipt.model_dump_json(),
                    operator,
                    reason,
                )
                await conn.execute(
                    "UPDATE upload_runtimes SET state='READY',review_reason=NULL WHERE runtime_id=$1 AND generation=$2",
                    spec.host.runtime_id,
                    spec.host.runtime_generation,
                )
                return {"mode": "APPLY", "state": "READY", "already_registered": False, "execution_changed": False}
