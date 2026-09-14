"""Tests for Android device profiles."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from pixav.pixel_injector.profiles import AndroidProfile, get_profile, load_profiles

_PIXEL_XL_PRODUCT_PROP = Path("docker/android/pixel-xl-product-build.prop")


class TestBuiltinProfiles:
    def test_pixel_xl_profile_declares_the_identity_google_photos_reads(self) -> None:
        profile = get_profile("gphotos_pixel_xl_v1", path=None)
        args = dict(arg.split("=", 1) for arg in profile.args if "=" in arg)

        # Layer 1 of the entitlement check: Build.MANUFACTURER/BRAND/MODEL.
        assert args["ro.product.brand"] == "google"
        assert args["ro.product.manufacturer"] == "Google"
        # Redroid splits container arguments on whitespace. The model therefore
        # has to be baked into the image rather than passed as ``Pixel XL``.
        assert "ro.product.model" not in args
        assert "ro.product.product.model=Pixel XL" in _PIXEL_XL_PRODUCT_PROP.read_text(encoding="utf-8")
        # Codename is marlin; sailfish is the smaller Pixel.
        assert args["ro.product.device"] == "marlin"
        assert args["ro.build.fingerprint"] == ("google/marlin/marlin:10/QP1A.191005.007.A3/5972272:user/release-keys")

    def test_sdk_level_is_deliberately_not_spoofed(self) -> None:
        """A real Pixel XL shipped Android 10; lying about the SDK breaks the platform."""
        profile = get_profile("gphotos_pixel_xl_v1", path=None)

        assert not any(arg.startswith("ro.build.version.sdk") for arg in profile.args)

    def test_readiness_proves_the_spoof_rather_than_assuming_it(self) -> None:
        profile = get_profile("gphotos_pixel_xl_v1", path=None)
        commands = [check.command for check in profile.readiness]

        assert "getprop sys.boot_completed" in commands
        assert "getprop ro.build.fingerprint" in commands

    def test_unknown_profile_names_the_known_ones(self) -> None:
        with pytest.raises(ValueError, match="gphotos_pixel_xl_v1"):
            get_profile("does_not_exist", path=None)


class TestYamlProfiles:
    def test_missing_file_falls_back_to_builtins(self, tmp_path) -> None:
        profiles = load_profiles(tmp_path / "absent.yml")

        assert "gphotos_pixel_xl_v1" in profiles

    def test_file_profile_overrides_the_builtin_of_the_same_name(self, tmp_path) -> None:
        path = tmp_path / "profiles.yml"
        path.write_text(
            """
gphotos_pixel_xl_v1:
  image: pixav/redroid-gphotos-pixelxl@sha256:abc123
  args:
    - ro.product.model=Pixel XL
  readiness:
    - command: pm list features
      contains: PIXEL_2016_EXPERIENCE
""",
            encoding="utf-8",
        )

        profile = load_profiles(path)["gphotos_pixel_xl_v1"]

        assert profile.image == "pixav/redroid-gphotos-pixelxl@sha256:abc123"
        assert profile.readiness[0].contains == "PIXEL_2016_EXPERIENCE"

    def test_malformed_file_is_rejected_rather_than_silently_ignored(self, tmp_path) -> None:
        path = tmp_path / "profiles.yml"
        path.write_text("- just\n- a\n- list\n", encoding="utf-8")

        with pytest.raises(ValueError, match="mapping"):
            load_profiles(path)

    def test_profiles_are_immutable(self) -> None:
        profile = AndroidProfile(name="x", image="y")

        with pytest.raises(ValidationError):
            profile.image = "z"  # type: ignore[misc]


def test_shipped_profile_file_is_valid() -> None:
    """The file in the repo must actually load, not just look right."""
    profiles = load_profiles("config/android_profiles.yml")

    assert "gphotos_pixel_xl_v1" in profiles
