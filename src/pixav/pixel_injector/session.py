"""Immutable runtime sessions; an external serial never denotes a container."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from uuid import UUID

from pixav.pixel_injector.android_target import AndroidTarget


@dataclass(frozen=True, slots=True)
class RedroidSession:
    """Connection context returned by Redroid manager for one task."""

    task_id: str
    container_id: str
    adb_host: str
    adb_port: int


@dataclass(frozen=True, slots=True)
class WindowsAndroidSession:
    """A registered external device generation and its exact tools binding.

    The native observer must still verify the registered identity before every
    effect. Constructing this value grants neither UI ownership nor readiness.
    """

    runtime_id: UUID
    generation: int
    owner: UUID
    account_id: UUID = field(repr=False)
    device_key: str
    identity_sha256: str
    avd_name: str
    boot_id: UUID
    tools_id: str
    tools_image: str
    target: AndroidTarget

    def __post_init__(self) -> None:
        if not all(isinstance(value, UUID) for value in (self.runtime_id, self.owner, self.account_id, self.boot_id)):
            raise ValueError("invalid external runtime identity")
        if type(self.generation) is not int or self.generation <= 0:
            raise ValueError("invalid external runtime generation")
        if not isinstance(self.target, AndroidTarget) or self.target.server_host is None:
            raise ValueError("Windows runtime requires an external Android target")
        if not isinstance(self.identity_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", self.identity_sha256):
            raise ValueError("invalid external runtime identity digest")
        if not isinstance(self.tools_image, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", self.tools_image):
            raise ValueError("Windows tools require an immutable image identity")
        if not isinstance(self.avd_name, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", self.avd_name):
            raise ValueError("invalid external AVD name")
        if any(not isinstance(value, str) or not value.strip() for value in (self.device_key, self.tools_id)):
            raise ValueError("external runtime binding incomplete")
