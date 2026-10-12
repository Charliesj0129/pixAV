"""Dedicated runtime roles refuse mixed membership and elevated capabilities."""

import uuid
from pathlib import Path

import pytest

from pixav.shared.workflow import require_workflow_role

pytestmark = pytest.mark.integration


@pytest.fixture
async def roles_db(integration_db):
    for path in sorted(Path("migrations").glob("*.sql")):
        await integration_db.execute(path.read_text())
    return integration_db


@pytest.mark.parametrize("group", ["pixav_execution_authority", "pixav_activity_worker", "pixav_playback_reader"])
async def test_runtime_role_accepts_only_its_own_group(roles_db, group):
    name = "role_test_" + uuid.uuid4().hex
    others = {"pixav_execution_authority", "pixav_activity_worker", "pixav_playback_reader"} - {group}
    async with roles_db.acquire() as conn:
        await conn.execute(f'CREATE ROLE "{name}"')
        try:
            await conn.execute(f'GRANT "{group}" TO "{name}"')
            await conn.execute(f'SET ROLE "{name}"')
            await require_workflow_role(conn, group)
            await conn.execute("RESET ROLE")
            other = sorted(others)[0]
            await conn.execute(f'GRANT "{other}" TO "{name}"')
            await conn.execute(f'SET ROLE "{name}"')
            with pytest.raises(RuntimeError, match="dedicated"):
                await require_workflow_role(conn, group)
        finally:
            await conn.execute("RESET ROLE")
            await conn.execute(f'DROP ROLE "{name}"')


@pytest.mark.parametrize("capability", ["SUPERUSER", "CREATEDB", "CREATEROLE", "REPLICATION", "BYPASSRLS"])
async def test_reader_cannot_have_administrative_capabilities(roles_db, capability):
    name = "role_test_" + uuid.uuid4().hex
    async with roles_db.acquire() as conn:
        await conn.execute(f'CREATE ROLE "{name}" {capability}')
        try:
            await conn.execute(f'GRANT pixav_playback_reader TO "{name}"')
            await conn.execute(f'SET ROLE "{name}"')
            with pytest.raises(RuntimeError, match="dedicated"):
                await require_workflow_role(conn, "pixav_playback_reader")
        finally:
            await conn.execute("RESET ROLE")
            await conn.execute(f'DROP ROLE "{name}"')
