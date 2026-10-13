from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from pixav.pixel_injector.android_target import AndroidTarget
from pixav.pixel_injector.canary import CanaryBlockedError


@pytest.mark.parametrize(
    "serial,host",
    [
        ("emulator-5554", None),
        ("", "host.docker.internal"),
        ("emulator-5555", "host.docker.internal"),
        ("emulator-5554;id", "host.docker.internal"),
        ("emulator-5554", "tcp:host.docker.internal:5037"),
        ("emulator-5554", "bad host"),
        ("emulator-5554", "bad..host"),
        ("emulator-5554", "-bad-host"),
        (None, "host.docker.internal"),
        ("emulator-5554", 123),
    ],
)
def test_invalid_or_implicit_targets_are_rejected(serial, host):
    with pytest.raises(ValueError):
        AndroidTarget(serial, host)


def test_target_cannot_change_after_validation():
    target = AndroidTarget("emulator-5554", "host.docker.internal")
    with pytest.raises(FrozenInstanceError):
        target.serial = "emulator-5556"


def test_missing_external_runner_configuration_fails_closed():
    target = AndroidTarget("emulator-5554", "host.docker.internal")
    with pytest.raises(CanaryBlockedError, match="configuration unavailable"):
        target.require_runner(SimpleNamespace())
