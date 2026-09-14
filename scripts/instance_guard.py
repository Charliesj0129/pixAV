"""Backwards-compatible import path; the guards live in :mod:`pixav.shared.instance`.

Seven operator scripts and several tests import this module directly, and one of
those callers is a CLI that a supervisor re-runs from disk between parts. Leaving
the path valid lets the guards move into the package without touching any of them.
"""

from pixav.shared.instance import (
    METADATA_SUFFIX,
    database_identity,
    metadata_path,
    read_backup_identity,
    redis_identity,
    require_same_instance,
    write_backup_metadata,
)

__all__ = [
    "METADATA_SUFFIX",
    "database_identity",
    "metadata_path",
    "read_backup_identity",
    "redis_identity",
    "require_same_instance",
    "write_backup_metadata",
]
