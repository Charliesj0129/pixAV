"""Explicit ADB transport selection for retained and external Android devices.

A serial selects a connection. Runtime identity, ownership and fencing remain
the caller's responsibility; this value cannot establish those facts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from pixav.pixel_injector.canary import CanaryBlockedError


@dataclass(frozen=True, slots=True)
class AndroidTarget:
    """Select the exact emulator and the Windows server owning its connection."""

    serial: str
    server_host: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.serial, str) or (self.server_host is not None and not isinstance(self.server_host, str)):
            raise ValueError("invalid Android transport fields")
        if self.server_host is None:
            if self.serial != "127.0.0.1:5555":
                raise ValueError("external Android targets require an explicit ADB server")
            return
        if not re.fullmatch(r"emulator-[0-9]{4,5}", self.serial):
            raise ValueError("Windows Android requires an exact emulator serial")
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", self.server_host):
            raise ValueError("invalid Windows ADB server host")
        if any(
            not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
            for label in self.server_host.split(".")
        ):
            raise ValueError("invalid Windows ADB server host")
        port = int(self.serial.split("-", 1)[1])
        if port < 5554 or port > 5682 or port % 2:
            raise ValueError("invalid Android emulator console port")

    @property
    def maestro_arguments(self) -> list[str]:
        host = ["--host", self.server_host] if self.server_host is not None else []
        return [*host, "--device", self.serial]

    def require_runner(self, runner: Any) -> None:
        """Reject routing changes before staging a flow or providing credentials."""
        attrs = getattr(runner, "attrs", None)
        if not isinstance(attrs, dict):
            if self.server_host is not None:
                raise CanaryBlockedError("external Android runner configuration unavailable")
            return
        config = attrs.get("Config", {})
        if not isinstance(config, dict):
            raise CanaryBlockedError("Android runner configuration invalid")
        values = config.get("Env", [])
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise CanaryBlockedError("Android runner environment invalid")
        sockets = [value.split("=", 1)[1] for value in values if value.startswith("ADB_SERVER_SOCKET=")]
        serials = [value.split("=", 1)[1] for value in values if value.startswith("ANDROID_SERIAL=")]
        if self.server_host is None:
            if sockets or (serials and serials != [self.serial]):
                raise CanaryBlockedError("container Android target does not match the tools environment")
            return
        if sockets != [f"tcp:{self.server_host}:5037"] or serials != [self.serial]:
            raise CanaryBlockedError("external Android target does not match the tools environment")


CONTAINER_ANDROID_TARGET = AndroidTarget("127.0.0.1:5555")
