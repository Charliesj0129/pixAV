"""Expansion invalidates unverified readiness without rewriting old effects."""

from pathlib import Path
from uuid import uuid4

import pytest

from tests.integration.test_external_android_runtime import claim, request, runtime

pytestmark = pytest.mark.integration


async def test_old_ready_migration_preserves_original_operation_and_ui_fence(integration_db):
    pool = integration_db
    for path in sorted(Path("migrations").glob("*.sql")):
        if path.name < "024_windows_runtime_enrollment.sql":
            await pool.execute(path.read_text())
    binding = await runtime(pool)
    original = await request(pool, binding)
    assert await claim(pool, original)
    legacy = uuid4()
    await pool.execute(
        """INSERT INTO upload_runtimes(runtime_id,generation,owner,kind,account_id,device_key,profile,
      identity,identity_sha256,tools_id,tools_image,staging_root,state)
      SELECT $2,1,$2,'redroid',account_id,'synthetic-retained','synthetic-redroid',
             identity,identity_sha256,tools_id,tools_image,staging_root,'READY'
        FROM upload_runtimes WHERE runtime_id=$1""",
        binding[0],
        legacy,
    )
    ui_before = await pool.fetchval("SELECT to_jsonb(s)::text FROM photos_ui_sessions s")
    execution_before = await pool.fetchval("SELECT to_jsonb(e)::text FROM executions e")
    identity_before = await pool.fetchval("SELECT identity::text FROM upload_runtimes WHERE runtime_id=$1", binding[0])
    await pool.execute(Path("migrations/024_windows_runtime_enrollment.sql").read_text())
    assert await pool.fetchval("SELECT state FROM upload_runtimes WHERE runtime_id=$1", binding[0]) == "REVIEW_REQUIRED"
    assert (
        await pool.fetchval("SELECT identity::text FROM upload_runtimes WHERE runtime_id=$1", binding[0])
        == identity_before
    )
    assert await pool.fetchval("SELECT state FROM upload_runtimes WHERE runtime_id=$1", legacy) == "READY"
    assert await pool.fetchval("SELECT to_jsonb(s)::text FROM photos_ui_sessions s") == ui_before
    assert await pool.fetchval("SELECT to_jsonb(e)::text FROM executions e") == execution_before
    assert await pool.fetchval("SELECT count(*) FROM windows_runtime_enrollments") == 0
