"""Translate a worker-visible path into the path the Docker daemon will see.

A worker that creates sibling containers hands bind-mount sources to the daemon,
and the daemon resolves them on the *host*. When the worker itself runs in a
container, ``/app/data/storage-staging`` means nothing to the host: Docker does
not report an error for it, it silently creates an empty host directory and the
sibling mounts that instead of the staged bytes.

``PIXAV_HOST_PROJECT_ROOT`` names where the worker's own project root lives on
the host. Empty means the worker runs on the host already and no translation is
needed, which is the case for a ``uv run`` process.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePath

__all__ = ["host_path"]


def _absolute(path: str | PurePath) -> Path:
    """Absolutise without following symlinks, so prefixes stay comparable."""
    return Path(os.path.abspath(str(path)))


def host_path(path: str | PurePath, *, host_project_root: str, project_root: str | PurePath | None = None) -> Path:
    """Return ``path`` as the Docker daemon's host filesystem sees it.

    Paths outside the project root are returned unchanged: a deployment that
    mounts an absolute host directory straight into the worker already shares
    that path with the daemon, and rewriting it would break it.
    """
    absolute = _absolute(path)
    if not host_project_root.strip():
        return absolute
    root = _absolute(project_root if project_root is not None else Path.cwd())
    if absolute == root:
        return _absolute(host_project_root)
    if root in absolute.parents:
        return _absolute(host_project_root) / absolute.relative_to(root)
    return absolute
