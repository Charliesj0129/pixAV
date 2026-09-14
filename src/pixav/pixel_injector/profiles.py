"""Android device profiles for Redroid containers.

The spoofing details live in YAML, not in Python. pixAV only knows a profile
name; what makes a container look like a Pixel XL is data.

Two facts make an image-level spoof sufficient, so no Magisk/Zygisk/LSPosed
chain is involved:

1. redroid accepts arbitrary ``ro.xxx`` property overrides as container command
   arguments, which covers ``Build.MANUFACTURER``/``BRAND``/``FINGERPRINT``.
   ``Build.MODEL`` is baked into ``/product/etc/build.prop`` because redroid's
   second-stage init cannot preserve whitespace in a container argument.
2. AOSP's ``SystemConfig`` reads ``<feature name="..."/>`` declarations from
   ``/system/etc/sysconfig`` and ``/system/etc/permissions`` into the set that
   ``PackageManager.hasSystemFeature()`` answers from, which covers the
   ``PIXEL_*_EXPERIENCE`` features. That file is baked into the image.

Those are the two layers Google Photos checks for the original-quality
entitlement, and both are image-level facts here — which also means
``getprop`` and ``pm list features`` are ground truth for verification rather
than a proxy for what one hooked process sees.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class ReadinessCheck(BaseModel):
    """One command whose output must contain ``contains`` before a container is ready."""

    model_config = {"frozen": True}

    command: str
    contains: str


class AndroidProfile(BaseModel):
    """A named Android device identity plus the evidence that it took effect."""

    model_config = {"frozen": True}

    name: str
    # Pin by digest in production: a floating tag silently changes the device
    # identity underneath a running fleet.
    image: str
    args: tuple[str, ...] = ()
    readiness: tuple[ReadinessCheck, ...] = Field(default_factory=tuple)


# Pixel XL, codename `marlin`. It is the Pixel generation that still carries the
# unlimited *original quality* Google Photos entitlement; Pixel 2-5 get storage-
# saver quality and Pixel 6 onward get nothing.
#
# Deliberately partial: a real Pixel XL shipped Android 10, while this image is
# Android 13/14. `ro.build.version.sdk` cannot be lied about without breaking the
# platform and every app on it, so the identity is consistent in vendor/model and
# inconsistent in OS version. If that inconsistency turns out to matter, the
# fallback is a Photos-scoped runtime hook, not a deeper set of property lies.
_BUILTIN_PROFILES: dict[str, AndroidProfile] = {
    "gphotos_pixel_xl_v1": AndroidProfile(
        name="gphotos_pixel_xl_v1",
        image="redroid/redroid:13.0.0-latest",
        args=(
            "androidboot.redroid_gpu_mode=guest",
            "ro.product.brand=google",
            "ro.product.manufacturer=Google",
            "ro.product.name=marlin",
            "ro.product.device=marlin",
            "ro.build.fingerprint=google/marlin/marlin:10/QP1A.191005.007.A3/5972272:user/release-keys",
        ),
        readiness=(
            ReadinessCheck(command="getprop sys.boot_completed", contains="1"),
            ReadinessCheck(command="getprop ro.product.model", contains="Pixel XL"),
            ReadinessCheck(
                command="getprop ro.build.fingerprint",
                contains="google/marlin/marlin:10/QP1A.191005.007.A3/5972272:user/release-keys",
            ),
        ),
    ),
}


def load_profiles(path: str | Path | None) -> dict[str, AndroidProfile]:
    """Load profiles from YAML, falling back to the built-in defaults.

    A missing file is not an error: it means the operator has not yet built a
    golden image and the built-in profile applies.
    """
    profiles = dict(_BUILTIN_PROFILES)
    if path is None:
        return profiles

    profile_path = Path(path)
    if not profile_path.is_file():
        logger.info("no android profile file at %s, using built-in profiles", profile_path)
        return profiles

    raw = yaml.safe_load(profile_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"android profile file {profile_path} must contain a mapping of profile names")

    for name, body in raw.items():
        if not isinstance(body, dict):
            raise ValueError(f"android profile {name!r} must be a mapping")
        profiles[str(name)] = AndroidProfile.model_validate({"name": str(name), **body})
    logger.info("loaded %d android profile(s) from %s", len(raw), profile_path)
    return profiles


def get_profile(name: str, *, path: str | Path | None = None) -> AndroidProfile:
    """Resolve one profile by name."""
    profiles = load_profiles(path)
    try:
        return profiles[name]
    except KeyError as exc:
        known = ", ".join(sorted(profiles)) or "<none>"
        raise ValueError(f"unknown android profile {name!r}; known profiles: {known}") from exc
