"""Shared runtime session model for one Redroid upload attempt."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RedroidSession:
    """Connection context returned by Redroid manager for one task."""

    task_id: str
    container_id: str
    adb_host: str
    adb_port: int
