#!/usr/bin/env python3
"""Create a PostgreSQL custom-format backup without shell redirection.

The backup is streamed from ``pg_dump`` inside the running PostgreSQL
container into a new, never-overwritten file under ``backups/``.

A dump of this database contains ``accounts.password``, so the file is created
owner-only from the very first byte. Creating it 0644 and chmod-ing afterwards
would leave the credentials world-readable for the whole duration of the dump.
"""

from __future__ import annotations

import argparse
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from pixav.config import get_settings

if __package__:
    from scripts.backup_files import create_backup_file
    from scripts.instance_guard import write_backup_metadata
else:  # Support the documented ``python scripts/backup_postgres.py`` form.
    from backup_files import create_backup_file
    from instance_guard import write_backup_metadata

_SAFE_CONTAINER_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")


def build_command(*, container: str, user: str, database: str) -> list[str]:
    """Build the argument-only docker/pg_dump command."""
    if not _SAFE_CONTAINER_NAME.fullmatch(container):
        raise ValueError("container name contains unsupported characters")
    return [
        "docker",
        "exec",
        container,
        "pg_dump",
        "--username",
        user,
        "--dbname",
        database,
        "--format=custom",
        "--no-owner",
        "--no-acl",
    ]


def build_identity_command(*, container: str, user: str, database: str) -> list[str]:
    """Build the command that reads the cluster's system identifier."""
    if not _SAFE_CONTAINER_NAME.fullmatch(container):
        raise ValueError("container name contains unsupported characters")
    return [
        "docker",
        "exec",
        container,
        "psql",
        "--username",
        user,
        "--dbname",
        database,
        "--tuples-only",
        "--no-align",
        "--command",
        "SELECT system_identifier FROM pg_control_system()",
    ]


def read_system_identifier(*, container: str, user: str, database: str) -> str:
    """Return the identifier of the cluster this backup was taken from."""
    command = build_identity_command(container=container, user=user, database=database)
    completed = subprocess.run(command, capture_output=True, check=False)
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"could not read the cluster system identifier: {detail}")
    identifier = completed.stdout.decode("utf-8", errors="replace").strip()
    if not identifier:
        raise RuntimeError("cluster system identifier came back empty")
    return identifier


def default_output_path() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path("backups") / f"pixav-{stamp}.dump"


def run_backup(*, container: str, output: Path) -> Path:
    """Run pg_dump and return the new backup path."""
    settings = get_settings()
    command = build_command(container=container, user=settings.db_user, database=settings.db_name)
    output.parent.mkdir(parents=True, exist_ok=True)

    try:
        with create_backup_file(output, binary=True) as destination:
            completed = subprocess.run(command, stdout=destination, stderr=subprocess.PIPE, check=False)
    except FileExistsError as exc:
        raise RuntimeError(f"refusing to overwrite existing backup: {output}") from exc

    if completed.returncode != 0:
        output.unlink(missing_ok=True)
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"pg_dump failed ({completed.returncode}): {detail}")
    if output.stat().st_size == 0:
        output.unlink(missing_ok=True)
        raise RuntimeError("pg_dump produced an empty backup")

    # A custom-format dump carries no clue about which cluster produced it, so a
    # destructive script cannot otherwise tell "the backup I was handed" from
    # "a backup of some other instance with the same database name".
    write_backup_metadata(
        output,
        system_identifier=read_system_identifier(container=container, user=settings.db_user, database=settings.db_name),
        database=settings.db_name,
    )
    return output.resolve()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", default="pixav-postgres")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    try:
        output = run_backup(container=args.container, output=args.output or default_output_path())
    except (OSError, RuntimeError, ValueError) as exc:
        parser.exit(2, f"backup refused: {exc}\n")
    print(output)


if __name__ == "__main__":
    main()
