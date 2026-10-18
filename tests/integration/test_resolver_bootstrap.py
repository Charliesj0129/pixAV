"""A bound reader login boots private playback without authority privileges."""

import os
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest

from pixav.config import Settings
from pixav.strm_resolver.app import create_app, lifespan

pytestmark = pytest.mark.integration


async def test_bound_private_resolver_checks_instances_role_and_revocation(
    integration_redis, integration_db, tmp_path, monkeypatch
):
    redis, _key = integration_redis
    pool = integration_db
    for migration in sorted(Path("migrations").glob("*.sql")):
        await pool.execute(migration.read_text())
    endpoint = urlsplit(os.environ["PIXAV_E2E_ADMIN_DSN"])
    name = "resolver_test_" + uuid.uuid4().hex
    token = tmp_path / "token"
    token.write_text("a" * 64)
    token.chmod(0o600)
    settings = Settings(
        db_host=endpoint.hostname,
        db_port=endpoint.port,
        db_user=name,
        db_password="integration-only",
        db_name=await pool.fetchval("SELECT current_database()"),
        expected_db_identity=str(await pool.fetchval("SELECT system_identifier FROM pg_control_system()")),
        expected_db_name=await pool.fetchval("SELECT current_database()"),
        redis_url=os.environ["PIXAV_E2E_REDIS_URL"],
        expected_redis_run_id=(await redis.info("server"))["run_id"],
        managed_media_workflow=True,
        resolver_auth_required=True,
        resolver_require_reader_role=True,
        resolver_token_file=str(token),
        playback_cache_dir=str(tmp_path / "cache"),
        playback_cache_sweep_seconds=0,
    )
    await pool.execute(
        f'CREATE ROLE "{name}" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE '
        "NOREPLICATION NOBYPASSRLS PASSWORD 'integration-only'"
    )
    try:
        await pool.execute(f'GRANT pixav_playback_reader TO "{name}"')
        monkeypatch.setattr("pixav.strm_resolver.app.get_settings", lambda: settings)
        app = create_app()
        async with lifespan(app):
            assert await app.state.db_pool.fetchval("SELECT current_user") == name
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, client=(name, 123)), base_url="http://synthetic"
            ) as client:
                assert (await client.get("/health")).status_code == 200
                route = "/stream/" + str(uuid.uuid4())
                assert (await client.get(route)).status_code == 401
                assert (await client.get(route, headers={"Authorization": "Bearer " + "a" * 64})).status_code == 404
                token.write_text("b" * 64)
                assert (await client.get(route, headers={"Authorization": "Bearer " + "a" * 64})).status_code == 401
                assert (await client.get(route, headers={"Authorization": "Bearer " + "b" * 64})).status_code == 404
    finally:
        async for key in redis.scan_iter(match=f"pixav:ratelimit:{name}:*"):
            await redis.delete(key)
        await pool.execute(f'DROP ROLE "{name}"')
