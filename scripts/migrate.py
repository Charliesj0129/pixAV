#!/usr/bin/env python3
"""Minimal SQL migration runner — applies numbered .sql files in order."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path

import asyncpg

from pixav.config import get_settings

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def select_pending(filenames: list[str], applied: set[str], until: str | None = None) -> list[str]:
    """Return the migrations to run, in order, stopping after ``until``.

    ``until`` exists for expand/contract deployments. A schema change is often
    split into an additive half that is safe to apply while the old code is
    still serving traffic, and a contracting half that is not. Without a stop
    point the only way to apply the safe half alone is to run raw SQL by hand
    and hand-write the ``_migrations`` row, which is exactly the moment an
    operator mistypes and leaves the ledger disagreeing with the schema.

    An ``until`` that names no migration is an error rather than a no-op: a
    typo must not silently apply everything.
    """
    ordered = sorted(filenames)
    if until is not None:
        if until not in ordered:
            raise ValueError(f"--until names no migration: {until}")
        ordered = ordered[: ordered.index(until) + 1]
    return [name for name in ordered if name not in applied]


async def run_migrations(dsn: str, *, until: str | None = None) -> list[str]:
    """Apply every pending migration up to ``until``; return what was applied."""
    conn: asyncpg.Connection = await asyncpg.connect(dsn)
    try:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS _migrations (
                filename TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """)

        applied: set[str] = {row["filename"] for row in await conn.fetch("SELECT filename FROM _migrations")}
        available = [path.name for path in MIGRATIONS_DIR.glob("*.sql")]
        pending = select_pending(available, applied, until)

        if until is not None:
            logger.info("stopping after %s", until)

        for name in pending:
            logger.info("apply %s", name)
            sql = (MIGRATIONS_DIR / name).read_text(encoding="utf-8")
            await conn.execute(sql)
            await conn.execute("INSERT INTO _migrations (filename) VALUES ($1)", name)

        logger.info("migrations complete (%d applied)", len(pending))
        return pending
    finally:
        await conn.close()


def main() -> None:
    """Apply migrations against PIXAV_DSN, or the configured database.

    The fallback goes through ``Settings`` rather than a literal DSN so the
    runner picks up PIXAV_DB_HOST/PORT/USER/PASSWORD/NAME like every other
    component — a hardcoded ``localhost`` default resolves to the container
    itself when this runs as the compose ``migrate`` service.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--until",
        default=None,
        metavar="FILENAME",
        help="apply migrations up to and including this file, then stop",
    )
    args = parser.parse_args()

    dsn = os.environ.get("PIXAV_DSN", "").strip() or get_settings().dsn
    try:
        asyncio.run(run_migrations(dsn, until=args.until))
    except ValueError as exc:
        parser.exit(2, f"migration refused: {exc}\n")


if __name__ == "__main__":
    main()
