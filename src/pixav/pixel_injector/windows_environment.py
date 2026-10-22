"""Frozen environment evidence required before Windows managed admission.

Process/boot identity can reconcile an old runtime. New managed effects also
require the installed software, AVD directory/configuration and Photos-only
environment to be pinned; a Pixel name alone cannot supply this evidence.
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Annotated, BinaryIO, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator


class EnvironmentFile(BaseModel):
    model_config = {"frozen": True, "extra": "forbid"}
    path: str = Field(min_length=4, max_length=1024)
    size_bytes: int = Field(gt=0, strict=True)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("path")
    @classmethod
    def local_absolute_file(cls, value: str) -> str:
        path = PureWindowsPath(value)
        if not path.is_absolute() or path.drive.startswith("\\") or ".." in path.parts:
            raise ValueError("environment file must be an exact local absolute path")
        return value


class _WindowsEnvironment(BaseModel):
    model_config = {"frozen": True, "extra": "forbid", "populate_by_name": True}
    profile: Literal["gphotos_pixel_xl_infinity_v1"]
    boot_id: UUID
    observed_at: datetime
    host_machine_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    avd_name: str = Field(pattern=r"^[A-Za-z0-9._-]+$")
    avd_path: str
    avd_directory_device: int = Field(ge=0, strict=True)
    avd_directory_inode: int = Field(gt=0, strict=True)
    avd_config: EnvironmentFile
    hardware_config: EnvironmentFile
    private_ramdisk: EnvironmentFile
    system_files: tuple[EnvironmentFile, ...] = Field(min_length=2)
    emulator_version: Literal["35.2.10"]
    platform_tools_version: Literal["35.0.2"]
    system_image_package: Literal["system-images;android-35;google_apis_playstore;x86_64"]
    system_image_revision: Literal[9]
    magisk_version_code: Literal[30700]
    framework_version_code: Literal[3080]
    framework_api: Literal[102]
    module_package: Literal["io.github.samson910022.pixelifyphotos"]
    module_version: Literal["1.4.1"]
    module_apk_sha256: Literal["6cac6601adbfb2e27ff4596c8d38735a7a7d0cbf228f646c5e73dbfc8b74203a"]
    photos_version: str = Field(min_length=1, max_length=128)
    photos_apk_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    scoped_packages: tuple[Literal["com.google.android.apps.photos"], ...] = Field(min_length=1, max_length=1)
    scope_user_id: Literal[0]
    pixel_xl_build_verified: Literal[True]
    hook_receipt_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def verify_paths_and_evidence(self) -> _WindowsEnvironment:
        root = PureWindowsPath(self.avd_path)
        if not root.is_absolute() or root.drive.startswith("\\") or ".." in root.parts:
            raise ValueError("AVD directory must be an exact local absolute path")
        if any(PureWindowsPath(pin.path).parent != root for pin in (self.avd_config, self.hardware_config)):
            raise ValueError("AVD configuration is outside the bound instance")
        if (
            PureWindowsPath(self.avd_config.path).name != "config.ini"
            or PureWindowsPath(self.hardware_config.path).name != "hardware-qemu.ini"
        ):
            raise ValueError("exact AVD configuration files required")
        names = [PureWindowsPath(pin.path).name.lower() for pin in self.system_files]
        if len(set(pin.path.lower() for pin in self.system_files)) != len(self.system_files):
            raise ValueError("duplicate system image evidence")
        if not {"system.img", "vendor.img", "kernel-ranchu", "ramdisk.img", "source.properties"}.issubset(names):
            raise ValueError("complete system image evidence required")
        if self.observed_at.tzinfo is None:
            raise ValueError("environment observation requires an explicit clock offset")
        return self


class WindowsEnvironment(_WindowsEnvironment):
    """Original single-APK contract; its canonical payload remains unchanged."""

    schema_id: Literal["windows-pixel-environment-v1"] = Field(alias="schema")


class AndroidApk(BaseModel):
    model_config = {"frozen": True, "extra": "forbid"}
    path: str = Field(pattern=r"^/[A-Za-z0-9_./=+~-]+\.apk$")
    size_bytes: int = Field(gt=0, strict=True)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("path")
    @classmethod
    def exact_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if ".." in path.parts or str(path) != value:
            raise ValueError("exact installed APK path required")
        return value


class _WindowsSplitEnvironment(_WindowsEnvironment):
    """Pin the installed base and every split without accepting later updates."""

    photos_apks: tuple[AndroidApk, ...] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def complete_photos_apks(self) -> _WindowsSplitEnvironment:
        paths = [PurePosixPath(apk.path) for apk in self.photos_apks]
        bases = [apk for apk in self.photos_apks if PurePosixPath(apk.path).name == "base.apk"]
        if (
            len(set(paths)) != len(paths)
            or len({path.parent for path in paths}) != 1
            or len(bases) != 1
            or bases[0].sha256 != self.photos_apk_sha256
        ):
            raise ValueError("complete unambiguous Photos APK set required")
        return self


class WindowsEnvironmentV2(_WindowsSplitEnvironment):
    """Retain the original split-APK payload and implicit software launch."""

    schema_id: Literal["windows-pixel-environment-v2"] = Field(alias="schema")


class WindowsEnvironmentV3(_WindowsSplitEnvironment):
    """Explicit host-GPU launch; its identity requires a new registration."""

    schema_id: Literal["windows-pixel-environment-v3"] = Field(alias="schema")
    emulator_cpu_cores: int = Field(strict=True, ge=4, le=4)
    emulator_memory_mib: int = Field(strict=True, ge=4096, le=4096)
    emulator_gpu: Literal["host"]


WindowsEnvironmentContract = Annotated[
    WindowsEnvironment | WindowsEnvironmentV2 | WindowsEnvironmentV3, Field(discriminator="schema_id")
]


class EnvironmentFiles:
    """Keep verified software/config files open without Windows write/delete sharing."""

    def __init__(self, environment: WindowsEnvironmentContract, sdk_root: str) -> None:
        if os.name != "nt":
            raise RuntimeError("environment file seals require the Windows host")
        import winreg

        self.environment = environment
        self._files: list[tuple[EnvironmentFile, BinaryIO, tuple[int, ...]]] = []
        # The Linux type-checking host omits Windows-only standard-library APIs.
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as key:  # type: ignore[attr-defined]
            machine, _ = winreg.QueryValueEx(key, "MachineGuid")  # type: ignore[attr-defined]
        if hashlib.sha256(str(machine).encode()).hexdigest() != environment.host_machine_sha256:
            raise ValueError("environment belongs to a different Windows host")
        sdk = PureWindowsPath(sdk_root)
        image = sdk / "system-images/android-35/google_apis_playstore/x86_64"
        if any(PureWindowsPath(pin.path).parent != image for pin in environment.system_files):
            raise ValueError("system image file outside the bound SDK")
        try:
            self.verify_directory()
            for pin in (
                environment.avd_config,
                environment.hardware_config,
                environment.private_ramdisk,
                *environment.system_files,
            ):
                handle = self._open_locked(pin.path)
                try:
                    digest = hashlib.sha256()
                    while chunk := handle.read(8 * 1024 * 1024):
                        digest.update(chunk)
                    stat = os.fstat(handle.fileno())
                    if stat.st_size != pin.size_bytes or digest.hexdigest() != pin.sha256:
                        raise ValueError("environment file content changed")
                    self._files.append((pin, handle, self._fingerprint(stat)))
                except BaseException:
                    handle.close()
                    raise
            self.verify_hardware(image)
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _open_locked(path: str) -> BinaryIO:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        create = kernel.CreateFileW
        create.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        create.restype = wintypes.HANDLE
        handle = create(path, 0x80000000, 1, None, 3, 0x08000000, None)  # READ / FILE_SHARE_READ / OPEN_EXISTING
        if handle == ctypes.c_void_p(-1).value:
            raise OSError("environment file cannot be sealed")
        close = kernel.CloseHandle
        close.argtypes = [wintypes.HANDLE]
        close.restype = wintypes.BOOL
        try:
            descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)  # type: ignore[attr-defined]
        except BaseException:
            close(handle)
            raise
        try:
            return os.fdopen(descriptor, "rb")
        except BaseException:
            os.close(descriptor)
            raise

    @staticmethod
    def _fingerprint(stat) -> tuple[int, ...]:
        return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns

    def verify_directory(self) -> None:
        stat = Path(self.environment.avd_path).stat()
        if (stat.st_dev, stat.st_ino) != (self.environment.avd_directory_device, self.environment.avd_directory_inode):
            raise ValueError("AVD physical directory changed")

    def verify_hardware(self, image: PureWindowsPath) -> None:
        environment = self.environment
        values = {}
        for line in Path(environment.hardware_config.path).read_text(encoding="utf-8").splitlines():
            name, separator, value = line.partition("=")
            if separator:
                values[name.strip()] = value.strip()
        root = PureWindowsPath(environment.avd_path)
        expected = {
            "disk.dataPartition.path": root / "userdata-qemu.img",
            "disk.cachePartition.path": root / "cache.img",
            "disk.encryptionKeyPartition.path": root / "encryptionkey.img",
            "disk.ramdisk.path": PureWindowsPath(environment.private_ramdisk.path),
            "kernel.path": image / "kernel-ranchu",
            "disk.systemPartition.initPath": image / "system.img",
            "disk.vendorPartition.initPath": image / "vendor.img",
        }
        if values.get("avd.name") != environment.avd_name or any(
            PureWindowsPath(values.get(name, "")) != path for name, path in expected.items()
        ):
            raise ValueError("running hardware configuration belongs to a different instance or image")
        if isinstance(environment, WindowsEnvironmentV3):
            launch = {
                "hw.cpu.ncore": str(environment.emulator_cpu_cores),
                "hw.ramSize": str(environment.emulator_memory_mib),
                "hw.gpu.mode": environment.emulator_gpu,
                "hw.gpu.enabled": "true",
            }
            if any(values.get(name) != value for name, value in launch.items()):
                raise ValueError("running hardware launch configuration differs from the pinned profile")

    def verify(self) -> None:
        self.verify_directory()
        if any(self._fingerprint(Path(pin.path).stat()) != fingerprint for pin, _handle, fingerprint in self._files):
            raise ValueError("environment file identity changed")

    def close(self) -> None:
        for _pin, handle, _fingerprint in self._files:
            handle.close()
        self._files.clear()
