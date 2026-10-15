"""Validated wire contracts for one retained Windows Android runtime."""

from __future__ import annotations

import hashlib
import json
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from pixav.pixel_injector.windows_environment import WindowsEnvironment


def canonical_digest(value: dict) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


class NativeProcess(BaseModel):
    model_config = {"frozen": True, "extra": "forbid"}
    pid: int = Field(gt=0, strict=True)
    created_filetime: str = Field(pattern=r"^[0-9]{17,19}$")
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    original_process_absent: Literal[False] = False


class NativeProcesses(BaseModel):
    model_config = {"frozen": True, "extra": "forbid"}
    adb: NativeProcess
    emulator: NativeProcess
    qemu: NativeProcess


class NativeIdentity(BaseModel):
    model_config = {"frozen": True, "extra": "forbid", "populate_by_name": True}
    schema_id: Literal["windows-native-control-v1"] = Field(alias="schema")
    sdk_root: str = Field(min_length=3, max_length=512, pattern=r"^[A-Za-z]:\\")
    serial: str = Field(pattern=r"^emulator-[0-9]{4,5}$")
    avd_name: str = Field(pattern=r"^[A-Za-z0-9._-]+$")
    boot_id: UUID
    processes: NativeProcesses
    result: Literal["NATIVE_IDENTITY_OBSERVED"] = "NATIVE_IDENTITY_OBSERVED"
    environment: WindowsEnvironment | None = None

    @model_validator(mode="after")
    def validate_serial(self) -> NativeIdentity:
        port = int(self.serial.split("-")[1])
        if port < 5554 or port > 5682 or port % 2:
            raise ValueError("invalid native emulator port")
        if self.environment is not None and (
            self.environment.boot_id != self.boot_id or self.environment.avd_name != self.avd_name
        ):
            raise ValueError("environment evidence belongs to a different boot or AVD")
        return self

    def payload(self) -> dict:
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)


class WindowsHostManifest(BaseModel):
    model_config = {"frozen": True, "extra": "forbid"}
    runtime_id: UUID
    runtime_generation: int = Field(gt=0, strict=True)
    device_key: str = Field(min_length=1, max_length=256)
    identity_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    tools_id: str = Field(min_length=1, max_length=128)
    native: NativeIdentity

    @model_validator(mode="after")
    def verify_digest(self) -> WindowsHostManifest:
        if canonical_digest(self.native.payload()) != self.identity_sha256:
            raise ValueError("native identity digest mismatch")
        return self


class HostUIRequest(BaseModel):
    model_config = {"frozen": True, "extra": "forbid"}
    runtime_id: UUID
    runtime_generation: int = Field(gt=0, strict=True)
    identity_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    session_id: UUID
    session_token: UUID = Field(repr=False)
    operation_id: UUID
    generation: int = Field(gt=0, strict=True)
    cold: bool = False
    role: Literal["upload", "cold_read", "observe"] = "upload"

    def public_binding(self) -> dict:
        return self.model_dump(mode="json", exclude={"session_token", "cold"})


class HostHaltReceipt(BaseModel):
    model_config = {"frozen": True, "extra": "forbid", "populate_by_name": True}
    schema_id: Literal["windows-ui-halt-v1"] = Field(default="windows-ui-halt-v1", alias="schema")
    runtime_id: UUID
    runtime_generation: int = Field(gt=0)
    session_id: UUID
    operation_id: UUID
    generation: int = Field(gt=0)
    role: Literal["upload", "cold_read", "observe"] = "upload"
    device_key: str
    tools_id: str
    identity_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    native_writers_stopped: Literal[True]
    halt_mode: Literal["quiesce", "cold"]
    receipt_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
