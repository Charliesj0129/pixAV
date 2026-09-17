"""Dedicated instances, unique database per test, exact owned-key cleanup."""

from __future__ import annotations

import os
import uuid
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest
import redis.asyncio as aioredis

from scripts.integration_guard import require_test_database, require_test_redis


@pytest.fixture
async def integration_db():
    if os.getenv("PIXAV_RUN_INTEGRATION") != "1":
        pytest.skip("set PIXAV_RUN_INTEGRATION=1 and instance identities")
    dsn = os.getenv("PIXAV_E2E_ADMIN_DSN", "postgresql://pixav_test:integration-only@127.0.0.1:15432/pixav_integration")
    admin = await asyncpg.connect(dsn)
    name = f"pixav_test_{uuid.uuid4().hex}"
    created = False
    pool = None
    try:
        await require_test_database(admin)
        await admin.execute(f'CREATE DATABASE "{name}"')
        created = True
        parsed = urlsplit(dsn)
        pool = await asyncpg.create_pool(urlunsplit(parsed._replace(path=f"/{name}")), min_size=1, max_size=3)
        yield pool
    finally:
        if pool is not None:
            await pool.close()
        try:
            if created:
                await require_test_database(admin)
                await admin.execute(f'DROP DATABASE "{name}"')
        finally:
            await admin.close()


@pytest.fixture
async def integration_redis(integration_db):
    client = aioredis.from_url(os.getenv("PIXAV_E2E_REDIS_URL", "redis://127.0.0.1:16379/0"))
    key = f"pixav:test:{uuid.uuid4().hex}"
    try:
        await require_test_redis(client)
        yield client, key
    finally:
        try:
            await require_test_redis(client)
            await client.delete(key, f"{key}:processing", f"{key}:unrelated")
        finally:
            await client.aclose()
