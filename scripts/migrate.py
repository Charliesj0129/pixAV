#!/usr/bin/env python3
"""Minimal SQL migration runner — applies numbered .sql files in order."""

from __future__ import annotations

import asyncio
import glob
import logging
import os

import asyncpg

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

MIGRATIONS_DIR = os.path.join(os.path.dirname(__file__), "..", "migrations")


async def run_migrations(dsn: str) -> None:
    conn: asyncpg.Connection = await asyncpg.connect(dsn)
    try:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS _migrations (
                filename TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """)

        applied: set[str] = {row["filename"] for row in await conn.fetch("SELECT filename FROM _migrations")}

        sql_files = sorted(glob.glob(os.path.join(MIGRATIONS_DIR, "*.sql")))
        for path in sql_files:
            name = os.path.basename(path)
            if name in applied:
                logger.info("skip  %s (already applied)", name)
                continue

            logger.info("apply %s", name)
            with open(path, encoding="utf-8") as f:
                sql = f.read()
            await conn.execute(sql)
            await conn.execute("INSERT INTO _migrations (filename) VALUES ($1)", name)

        logger.info("migrations complete")
    finally:
        await conn.close()


def main() -> None:
    dsn = os.environ.get("PIXAV_DSN", "postgresql://pixav:pixav@localhost:5432/pixav")
    asyncio.run(run_migrations(dsn))


if __name__ == "__main__":
    main()
