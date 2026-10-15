"""Full environment evidence cannot be replaced by a device/model name."""

from copy import deepcopy
from datetime import datetime, timezone

import pytest

from pixav.pixel_injector.windows_environment import WindowsEnvironment


def environment_data(boot_id, avd_name="synthetic"):
    def pin(path):
        return {"path": path, "size_bytes": 1, "sha256": "a" * 64}

    return {
        "schema": "windows-pixel-environment-v1",
        "profile": "gphotos_pixel_xl_infinity_v1",
        "boot_id": str(boot_id),
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "host_machine_sha256": "b" * 64,
        "avd_name": avd_name,
        "avd_path": r"D:\Synthetic\Avd",
        "avd_directory_device": 1,
        "avd_directory_inode": 2,
        "avd_config": pin(r"D:\Synthetic\Avd\config.ini"),
        "hardware_config": pin(r"D:\Synthetic\Avd\hardware-qemu.ini"),
        "private_ramdisk": pin(r"D:\Synthetic\Image\ramdisk.img"),
        "system_files": [
            pin(r"D:\Synthetic\Sdk\system-images\system.img"),
            pin(r"D:\Synthetic\Sdk\system-images\vendor.img"),
            pin(r"D:\Synthetic\Sdk\system-images\kernel-ranchu"),
            pin(r"D:\Synthetic\Sdk\system-images\ramdisk.img"),
            pin(r"D:\Synthetic\Sdk\system-images\source.properties"),
        ],
        "emulator_version": "35.2.10",
        "platform_tools_version": "35.0.2",
        "system_image_package": "system-images;android-35;google_apis_playstore;x86_64",
        "system_image_revision": 9,
        "magisk_version_code": 30700,
        "framework_version_code": 3080,
        "framework_api": 102,
        "module_package": "io.github.samson910022.pixelifyphotos",
        "module_version": "1.4.1",
        "module_apk_sha256": "6cac6601adbfb2e27ff4596c8d38735a7a7d0cbf228f646c5e73dbfc8b74203a",
        "photos_version": "synthetic-pinned-version",
        "photos_apk_sha256": "c" * 64,
        "scoped_packages": ["com.google.android.apps.photos"],
        "scope_user_id": 0,
        "pixel_xl_build_verified": True,
        "hook_receipt_sha256": "d" * 64,
    }


@pytest.mark.parametrize(
    "defect",
    [
        "broad-scope",
        "user",
        "module",
        "revision",
        "config-path",
        "hardware-path",
        "vendor-missing",
        "duplicate-image",
        "emulator",
        "naive-clock",
    ],
)
def test_registration_rejects_incomplete_or_substituted_environment(defect):
    import uuid

    data = deepcopy(environment_data(uuid.uuid4()))
    if defect == "broad-scope":
        data["scoped_packages"].append("com.google.android.gms")
    elif defect == "user":
        data["scope_user_id"] = 1
    elif defect == "module":
        data["module_apk_sha256"] = "f" * 64
    elif defect == "revision":
        data["system_image_revision"] = 10
    elif defect == "config-path":
        data["avd_config"]["path"] = r"D:\Other\config.ini"
    elif defect == "hardware-path":
        data["hardware_config"]["path"] = r"D:\Other\hardware-qemu.ini"
    elif defect == "vendor-missing":
        data["system_files"] = data["system_files"][:1]
    elif defect == "duplicate-image":
        data["system_files"].append(data["system_files"][0])
    elif defect == "emulator":
        data["emulator_version"] = "99.0.0"
    else:
        data["observed_at"] = "2026-10-15T00:00:00"
    with pytest.raises(ValueError):
        WindowsEnvironment.model_validate(data)


def test_registration_retains_complete_immutable_environment():
    import uuid

    model = WindowsEnvironment.model_validate(environment_data(uuid.uuid4()))
    assert isinstance(model.system_files, tuple)
    assert model.scoped_packages == ("com.google.android.apps.photos",)
    with pytest.raises(ValueError):
        model.scope_user_id = 1
