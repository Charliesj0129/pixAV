"""A containerised worker must hand the Docker daemon host paths, not its own."""

from __future__ import annotations

from pathlib import Path

from pixav.shared.host_paths import host_path

HOST_ROOT = "/home/operator/pixAV"


def test_a_path_under_the_project_root_is_rewritten_for_the_daemon():
    result = host_path("/app/data/storage-staging", host_project_root=HOST_ROOT, project_root="/app")

    assert result == Path(HOST_ROOT) / "data/storage-staging"


def test_the_project_root_itself_maps_to_the_host_root():
    assert host_path("/app", host_project_root=HOST_ROOT, project_root="/app") == Path(HOST_ROOT)


def test_an_empty_setting_means_the_worker_already_runs_on_the_host(tmp_path):
    """A ``uv run`` process shares the daemon's filesystem; rewriting would break it."""
    assert host_path(tmp_path / "staging", host_project_root="", project_root="/app") == tmp_path / "staging"


def test_a_relative_path_is_made_absolute_before_comparison(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    assert (
        host_path("data/staging", host_project_root=HOST_ROOT, project_root=tmp_path)
        == Path(HOST_ROOT) / "data/staging"
    )


def test_a_path_outside_the_project_root_is_left_alone():
    """An absolute host directory mounted into the worker is already shared."""
    result = host_path("/mnt/media/library", host_project_root=HOST_ROOT, project_root="/app")

    assert result == Path("/mnt/media/library")


def test_a_sibling_of_the_project_root_is_not_treated_as_inside_it():
    result = host_path("/app-backup/data", host_project_root=HOST_ROOT, project_root="/app")

    assert result == Path("/app-backup/data")
