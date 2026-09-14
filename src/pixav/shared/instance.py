"""Prove which PostgreSQL and Redis a destructive script is about to touch.

The host's ``localhost:5432`` and the compose network's ``postgres:5432`` may be
different instances, and a DSN assembled from environment variables can point
at either depending on where the process runs. A cleanup that reads its plan
from one instance and executes it against another produces exactly the outcome
the dry-run was supposed to prevent, while the dry-run output still looks
plausible.

``pg_control_system().system_identifier`` is the right anchor: it is generated
at initdb and does not change with the database name, the port, the container
name, or a restore into a differently named database. Redis ``run_id`` is
weaker — it changes on every restart — so it is only used to detect that a run
spans two different servers, never as a durable identity.

A custom-format ``pg_dump`` does not carry the identifier, so
``scripts/backup_postgres.py`` writes it into a sidecar beside the dump. That is
what makes "the full backup belongs to the instance I am about to modify"
checkable at all.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

from pixav.shared.backup_files import create_backup_file

METADATA_SUFFIX = ".meta.json"


class _Fetcher(Protocol):
    async def fetchval(self, query: str, *args: Any) -> Any: ...


async def database_identity(connection: _Fetcher) -> str:
    """Return the cluster's initdb-assigned system identifier."""
    value = await connection.fetchval("SELECT system_identifier FROM pg_control_system()")
    return str(value)


async def redis_identity(redis: Any) -> str:
    """Return the Redis server's current run id (changes on restart)."""
    info = await redis.info("server")
    return str(info.get("run_id", ""))


def metadata_path(backup: Path) -> Path:
    """Return the sidecar path for a backup file."""
    return backup.with_name(backup.name + METADATA_SUFFIX)


def write_backup_metadata(backup: Path, *, system_identifier: str, database: str) -> Path:
    """Record which instance a backup came from, beside the backup."""
    target = metadata_path(backup)
    payload = {
        "schema_version": 1,
        "backup": backup.name,
        "system_identifier": system_identifier,
        "database": database,
    }
    # Same owner-only treatment as the dump: this names the production cluster.
    with create_backup_file(target) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return target


def read_backup_identity(backup: Path) -> str | None:
    """Return the system identifier recorded for ``backup``, if any.

    Returns None for a backup taken before sidecars existed, so the caller can
    decide whether to refuse or to require an explicit override — the two
    outcomes must not be confused with a *mismatch*, which is never overridable.
    """
    sidecar = metadata_path(backup)
    if not sidecar.is_file():
        return None
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    recorded = payload.get("system_identifier") if isinstance(payload, dict) else None
    return str(recorded) if recorded else None


def require_same_instance(*, backup: Path, live_identifier: str, allow_unverified: bool) -> None:
    """Refuse to apply unless the backup provably came from the live instance."""
    recorded = read_backup_identity(backup)
    if recorded is None:
        if allow_unverified:
            return
        raise RuntimeError(
            f"backup {backup.name} has no {METADATA_SUFFIX} sidecar recording its source instance; "
            "re-take it with scripts/backup_postgres.py, or pass --allow-unverified-backup"
        )
    if recorded != live_identifier:
        raise RuntimeError(
            f"backup {backup.name} came from PostgreSQL system identifier {recorded}, "
            f"but this run is connected to {live_identifier}"
        )
