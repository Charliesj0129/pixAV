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


def split_environment_data(boot_id, avd_name="synthetic"):
    data = environment_data(boot_id, avd_name)
    data["schema"] = "windows-pixel-environment-v2"
    data["photos_apks"] = [
        {"path": "/data/app/synthetic.photos/base.apk", "size_bytes": 256, "sha256": data["photos_apk_sha256"]},
        {"path": "/data/app/synthetic.photos/split_config.en.apk", "size_bytes": 128, "sha256": "e" * 64},
    ]
    return data


def test_original_environment_canonical_payload_is_not_upgraded():
    import uuid

    from pydantic import TypeAdapter

    from pixav.pixel_injector.windows_environment import WindowsEnvironmentContract
    from pixav.pixel_injector.windows_host_models import canonical_digest

    original = environment_data(uuid.uuid4())
    original["observed_at"] = "2026-10-15T00:00:00Z"
    parsed = TypeAdapter(WindowsEnvironmentContract).validate_python(original)
    payload = parsed.model_dump(mode="json", by_alias=True)
    assert payload == original
    assert canonical_digest(payload) == canonical_digest(original)
    assert "photos_apks" not in payload


def test_native_identity_retains_the_entire_split_apk_set():
    import uuid

    from pixav.pixel_injector.windows_host_models import NativeIdentity

    boot = uuid.uuid4()
    data = split_environment_data(boot)
    process = {"pid": 100, "created_filetime": "130000000000000000", "sha256": "a" * 64}
    native = NativeIdentity.model_validate(
        {
            "schema": "windows-native-control-v1",
            "sdk_root": r"D:\Synthetic\Sdk",
            "serial": "emulator-5554",
            "avd_name": "synthetic",
            "boot_id": str(boot),
            "processes": {"adb": process, "emulator": {**process, "pid": 104}, "qemu": {**process, "pid": 108}},
            "environment": data,
        }
    )
    assert native.payload()["environment"]["photos_apks"] == data["photos_apks"]
    assert isinstance(native.environment.photos_apks, tuple)


@pytest.mark.parametrize(
    "defect", ["missing-base", "duplicate", "base-digest", "other-directory", "unsafe-path", "empty"]
)
def test_split_environment_refuses_incomplete_or_ambiguous_apks(defect):
    import uuid

    from pixav.pixel_injector.windows_environment import WindowsEnvironmentV2

    data = split_environment_data(uuid.uuid4())
    if defect == "missing-base":
        data["photos_apks"] = data["photos_apks"][1:]
    elif defect == "duplicate":
        data["photos_apks"].append(data["photos_apks"][0])
    elif defect == "base-digest":
        data["photos_apks"][0]["sha256"] = "f" * 64
    elif defect == "other-directory":
        data["photos_apks"][1]["path"] = "/data/app/other/split_config.en.apk"
    elif defect == "unsafe-path":
        data["photos_apks"][1]["path"] = "/data/app/synthetic.photos/../split.apk"
    else:
        data["photos_apks"] = []
    with pytest.raises(ValueError):
        WindowsEnvironmentV2.model_validate(data)
