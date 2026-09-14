"""Minimal SQL migration runner — applies numbered .sql files in order.

The ordering rules live here rather than in ``scripts/migrate.py`` because two
entry points need them: the compose ``migrate`` service, and the isolated
single-film pipeline, which applies the schema up to a named migration before
it touches the run document.
"""

from __future__ import annotations

import logging
from pathlib import Path

import asyncpg

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "migrations"
"""Repository ``migrations/`` directory, next to ``src/``.

``migrations/`` ships with the repository, not inside the wheel
(``pyproject.toml`` packages only ``src/pixav``), so this resolves through the
package's location on disk. That holds for an editable install and for the
``migrate`` image, which copies the whole tree before syncing.
"""


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
    if not MIGRATIONS_DIR.is_dir():
        # Globbing a missing directory yields nothing, so without this the
        # runner reports "migrations complete (0 applied)" and every caller
        # proceeds against an unmigrated database.
        raise FileNotFoundError(f"migrations directory not found: {MIGRATIONS_DIR}")

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
