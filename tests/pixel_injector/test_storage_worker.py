"""Assembly rules for the managed storage worker."""

from __future__ import annotations

from pathlib import Path

import pytest

from pixav.config import Settings
from pixav.pixel_injector.storage_worker import _owner, build_worker

OWNER = "66666666-6666-4666-8666-666666666666"


def settings(**changes) -> Settings:
    return Settings(**{**dict(pixel_injector_mode="redroid"), **changes})


def test_worker_is_assembled_for_the_pixel_compatible_environment_bdd_043():
    worker = build_worker(object(), settings(), lambda account_id: None, owner="owner-1")
    assert worker.uploader is not None and worker.readback is not None


@pytest.mark.parametrize("mode", ["local", "", "direct"])
def test_other_upload_environments_refuse_to_start_bdd_043_044(mode):
    """Starting on a substituted environment would fake the durability evidence."""
    with pytest.raises(RuntimeError):
        build_worker(object(), settings(pixel_injector_mode=mode), lambda account_id: None, owner="owner-1")


def test_omitting_a_legacy_runtime_cannot_provision_retired_redroid(monkeypatch):
    monkeypatch.setattr("docker.from_env", lambda: pytest.fail("retired guest must not be provisioned"), raising=False)
    with pytest.raises(RuntimeError, match="retired"):
        build_worker(object(), settings(), owner="owner-1")


def test_windows_worker_requires_registered_runtime_and_mutual_tls():
    with pytest.raises(RuntimeError, match="registered runtime"):
        build_worker(object(), settings(pixel_injector_mode="windows_android"), owner=OWNER)
    with pytest.raises(RuntimeError, match="transport is not configured"):
        build_worker(
            object(), settings(pixel_injector_mode="windows_android", storage_windows_runtime_id=OWNER), owner=OWNER
        )


def test_windows_worker_selects_registered_factory_without_guest_fallback(monkeypatch):
    from types import SimpleNamespace

    from pixav.pixel_injector.windows_storage import WindowsSegmentUploader

    native = SimpleNamespace(owner=OWNER, staging_root=Path("synthetic"))
    monkeypatch.setattr("pixav.pixel_injector.storage_worker.windows_runtime", lambda *a, **kw: native)
    worker = build_worker(object(), settings(pixel_injector_mode="windows_android"), owner=OWNER)
    assert isinstance(worker.uploader, WindowsSegmentUploader)
    assert worker.readback._windows_runtime is native


def test_the_readback_is_configured_with_its_own_isolated_image(monkeypatch):
    """The browser must not run in the image that can see the staged bytes."""
    monkeypatch.setattr("docker.from_env", lambda: object(), raising=False)

    worker = build_worker(object(), settings(), lambda account_id: None, owner="owner-1")

    assert worker.readback._image == settings().storage_readback_image
    assert not hasattr(worker.readback, "_source_root")


def test_the_worker_owner_must_be_a_stable_uuid():
    """A fresh identity each start would provision a second signed-in guest."""
    with pytest.raises(RuntimeError, match="PIXAV_STORAGE_WORKER_OWNER"):
        _owner(settings(storage_worker_owner=""))
    with pytest.raises(RuntimeError, match="PIXAV_STORAGE_WORKER_OWNER"):
        _owner(settings(storage_worker_owner="not-a-uuid"))
    assert _owner(settings(storage_worker_owner=OWNER)) == OWNER


@pytest.mark.parametrize(
    "changes",
    [
        {"storage_guest_cpus": 0},
        {"storage_tools_cpus": -1},
        {"storage_guest_cpus": float("inf")},
        {"storage_tools_memory_mb": 0},
    ],
)
def test_runtime_resource_limits_cannot_be_unbounded(changes):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        settings(**changes)
