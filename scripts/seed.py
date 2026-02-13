#!/usr/bin/env python3
"""Insert development seed data into the database."""

from __future__ import annotations

import asyncio
import logging
import os

import asyncpg

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


async def seed(dsn: str) -> None:
    conn: asyncpg.Connection = await asyncpg.connect(dsn)
    try:
        await conn.execute("""
            INSERT INTO accounts (email, status)
            VALUES
                ('dev1@gmail.com', 'active'),
                ('dev2@gmail.com', 'active'),
                ('dev3@gmail.com', 'cooldown')
            ON CONFLICT (email) DO NOTHING
            """)
        logger.info("seed accounts inserted")

        await conn.execute("""
            INSERT INTO videos (title, status)
            SELECT 'Test Video Alpha', 'discovered'
            WHERE NOT EXISTS (
                SELECT 1 FROM videos WHERE title = 'Test Video Alpha'
            )
            UNION ALL
            SELECT 'Test Video Beta', 'discovered'
            WHERE NOT EXISTS (
                SELECT 1 FROM videos WHERE title = 'Test Video Beta'
            )
            """)
        logger.info("seed videos inserted")
    finally:
        await conn.close()


def main() -> None:
    dsn = os.environ.get("PIXAV_DSN", "postgresql://pixav:pixav@localhost:5432/pixav")
    asyncio.run(seed(dsn))


if __name__ == "__main__":
    main()
