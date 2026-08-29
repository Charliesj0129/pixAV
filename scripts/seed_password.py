"""Seed (or update) a single upload account from environment variables.

Credentials must never be hardcoded here. Set them before running:

    PIXAV_SEED_ACCOUNT_EMAIL=you@example.com \
    PIXAV_SEED_ACCOUNT_PASSWORD='...' \
    uv run python scripts/seed_password.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

import asyncpg

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"{name} is required (never hardcode credentials in this script)")
    return value


async def run() -> None:
    dsn = os.environ.get("PIXAV_DSN", "postgresql://pixav:pixav@localhost:5432/pixav")
    email = _require("PIXAV_SEED_ACCOUNT_EMAIL")
    password = _require("PIXAV_SEED_ACCOUNT_PASSWORD")

    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("ALTER TABLE accounts ADD COLUMN IF NOT EXISTS password TEXT")
        await conn.execute(
            """
            INSERT INTO accounts (email, password, status)
            VALUES ($1, $2, 'active')
            ON CONFLICT (email) DO UPDATE
                SET password = EXCLUDED.password,
                    status = 'active'
            """,
            email,
            password,
        )
        logger.info("account %s seeded as active", email)
    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
