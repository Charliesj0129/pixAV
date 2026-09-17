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


def test_omitting_a_runtime_uses_the_journalled_managed_guest(monkeypatch):
    """No caller has to supply an environment for the real runner to be correct."""
    built: dict = {}

    class Runtime:
        def __init__(self, pool, client, **kwargs):
            built.update(kwargs)

        async def acquire(self, account_id=None):  # pragma: no cover - never called here
            raise AssertionError("acquire is not part of assembly")

    monkeypatch.setattr("pixav.pixel_injector.storage_worker.ManagedRuntime", Runtime)
    monkeypatch.setattr("docker.from_env", lambda: object(), raising=False)

    worker = build_worker(object(), settings(), owner="owner-1")

    assert worker.uploader is not None
    assert built["staging_root"] == Path(settings().storage_staging_dir)
    assert built["profile_name"] == settings().redroid_profile


def test_the_readback_is_configured_with_its_own_isolated_image(monkeypatch):
    """The browser must not run in the image that can see the staged bytes."""
    monkeypatch.setattr("docker.from_env", lambda: object(), raising=False)

    worker = build_worker(object(), settings(), lambda account_id: None, owner="owner-1")

    assert worker.readback._image == settings().storage_readback_image
    assert worker.readback._source_root == Path("src").resolve()


def test_the_worker_owner_must_be_a_stable_uuid():
    """A fresh identity each start would provision a second signed-in guest."""
    with pytest.raises(RuntimeError, match="PIXAV_STORAGE_WORKER_OWNER"):
        _owner(settings(storage_worker_owner=""))
    with pytest.raises(RuntimeError, match="PIXAV_STORAGE_WORKER_OWNER"):
        _owner(settings(storage_worker_owner="not-a-uuid"))
    assert _owner(settings(storage_worker_owner=OWNER)) == OWNER
