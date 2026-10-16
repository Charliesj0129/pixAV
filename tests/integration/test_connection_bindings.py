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
async def test_database_pool_binds_every_connection(integration_db, defect):
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
    if defect != "none":
        with pytest.raises(RuntimeError, match="DATABASE_INSTANCE_BINDING_CHANGED"):
            await create_pool(settings)
        return
    pool = await create_pool(settings)
    try:
        assert await pool.fetchval("SELECT current_database()") == database
        # Recreated pool connections must still pass the same init guard.
        await pool.expire_connections()
        assert await pool.fetchval("SELECT current_database()") == database
    finally:
        await pool.close()


@pytest.mark.parametrize("matches", [True, False])
async def test_redis_startup_binds_the_live_run_id(integration_redis, matches):
    original, _key = integration_redis
    identity = (await original.info("server"))["run_id"]
    settings = Settings(
        redis_url=os.environ["PIXAV_E2E_REDIS_URL"], expected_redis_run_id=identity if matches else "0" * 40
    )
    if not matches:
        with pytest.raises(RuntimeError, match="REDIS_INSTANCE_BINDING_CHANGED"):
            await create_redis(settings)
        return
    client = await create_redis(settings)
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
