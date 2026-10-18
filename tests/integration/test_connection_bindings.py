"""Runtime clients refuse a different instance before they can do work."""

import os
from urllib.parse import urlsplit

import pytest

from pixav.config import Settings
from pixav.shared.db import create_pool
from pixav.shared.migrations import run_migrations
from pixav.shared.redis_client import create_redis

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("defect", ["none", "cluster", "database"])
@pytest.mark.parametrize("runtime", ["worker", "resolver"])
async def test_database_pool_binds_every_connection(integration_db, defect, runtime):
    parsed = urlsplit(os.environ["PIXAV_E2E_ADMIN_DSN"])
    identifier = str(await integration_db.fetchval("SELECT system_identifier FROM pg_control_system()"))
    database = await integration_db.fetchval("SELECT current_database()")
    await integration_db.execute("CREATE EXTENSION IF NOT EXISTS vector")
    settings = Settings(
        db_host=parsed.hostname,
        db_port=parsed.port,
        db_user=parsed.username,
        db_password=parsed.password,
        db_name=database,
        expected_db_identity="0" if defect == "cluster" else identifier,
        expected_db_name="synthetic_other_database" if defect == "database" else database,
    )

    async def open_pool():
        if runtime == "resolver":
            from pixav.strm_resolver.app import _open_database

            return await _open_database(settings.dsn, None, settings=settings)
        return await create_pool(settings)

    if defect != "none":
        code = "PostgreSQL is required" if runtime == "resolver" else "DATABASE_INSTANCE_BINDING_CHANGED"
        with pytest.raises(RuntimeError, match=code):
            await open_pool()
        return
    pool = await open_pool()
    try:
        assert await pool.fetchval("SELECT current_database()") == database
        # Recreated pool connections must still pass the same init guard.
        await pool.expire_connections()
        assert await pool.fetchval("SELECT current_database()") == database
    finally:
        await pool.close()


@pytest.mark.parametrize("matches", [True, False])
@pytest.mark.parametrize("runtime", ["worker", "resolver"])
async def test_redis_startup_binds_the_live_run_id(integration_redis, matches, runtime):
    original, _key = integration_redis
    identity = (await original.info("server"))["run_id"]
    settings = Settings(
        redis_url=os.environ["PIXAV_E2E_REDIS_URL"], expected_redis_run_id=identity if matches else "0" * 40
    )

    async def open_client():
        if runtime == "resolver":
            from pixav.strm_resolver.app import _open_redis

            return await _open_redis(settings.redis_url, settings)
        return await create_redis(settings)

    if not matches:
        code = "Redis instance binding required" if runtime == "resolver" else "REDIS_INSTANCE_BINDING_CHANGED"
        with pytest.raises(RuntimeError, match=code):
            await open_client()
        return
    client = await open_client()
    try:
        assert await client.ping()
    finally:
        await client.aclose()


async def test_wrong_database_migration_refuses_before_creating_a_ledger(integration_db):
    parsed = urlsplit(os.environ["PIXAV_E2E_ADMIN_DSN"])
    database = await integration_db.fetchval("SELECT current_database()")
    dsn = parsed._replace(path="/" + database).geturl()
    with pytest.raises(ValueError, match="database name mismatch"):
        await run_migrations(dsn, expected_database="synthetic_other_database")
    assert await integration_db.fetchval("SELECT to_regclass('public._migrations')") is None
