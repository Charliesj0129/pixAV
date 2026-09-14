"""Constants and small helpers every stage of the isolated single-film run shares."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pixav.pixel_injector.canary import CanaryBlockedError

PROJECT = "pixav-first-4k"
ROOT = Path(__file__).resolve().parents[3]
"""Repository root, three packages up from ``src/pixav/first_4k``.

The run reads ``config/`` and ``docker-compose.first-4k.yml`` from here and
writes evidence under ``WORK``. None of that ships in the wheel, so the path
resolves through this file's location; ``tests/first_4k`` pins it.
"""
WORK = ROOT / ".verify/first-4k"
DSN_ENV = "PIXAV_FIRST_4K_DSN"
DSN = os.environ.get(DSN_ENV, "").strip()
"""Isolated PostgreSQL DSN, supplied by the environment.

It carries the isolated database's password, so it is never a literal in the
tree. ``scripts/first_4k_movie.py`` still holds one while a run is in flight;
stage B removes it once the entry point moves here.
"""
REDIS = "redis://127.0.0.1:26379/0"
TOOLS = "pixav-first-4k-tools:1"
MEDIA = "pixav-photos-canary:maestro-2.10.0"
# Bumped whenever the selection policy changes, so an in-flight run is
# recognised as stale instead of silently reusing the old board's shortlist.
SELECTION_VERSION = 3
SUCCESS_STATUSES = {
    "PREFLIGHT_READY",
    "BOARDS_READY",
    "RESET_COMPLETE",
    "LIVE_VERIFIED",
    "PLAYABLE_QUOTA_WAIT",
    "STATUS",
    "NO_RUN",
    "RECOVERY_DRILL_COMPLETE",
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def container(client: Any, service: str) -> Any:
    item = client.containers.get(f"{PROJECT}-{service}-1")
    if (
        item.labels.get("com.docker.compose.project") != PROJECT
        or item.labels.get("com.docker.compose.service") != service
    ):
        raise CanaryBlockedError("isolated Compose identity mismatch")
    return item
