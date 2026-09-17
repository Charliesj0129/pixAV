"""Identity checks for opt-in tests; never infer safety from localhost or DB 15."""

from __future__ import annotations

import os
from typing import Any

from scripts.instance_guard import database_identity, redis_identity


async def require_test_database(connection: Any) -> str:
    expected = os.environ.get("PIXAV_TEST_DB_IDENTITY", "")
    observed = await database_identity(connection)
    database = await connection.fetchval("SELECT current_database()")
    if not expected or observed != expected or database != "pixav_integration":
        raise RuntimeError("test requires dedicated pixav_integration DB and PIXAV_TEST_DB_IDENTITY")
    return observed


async def require_test_redis(client: Any) -> str:
    expected = os.environ.get("PIXAV_TEST_REDIS_IDENTITY", "")
    observed = await redis_identity(client)
    if not expected or observed != expected:
        raise RuntimeError("test requires matching PIXAV_TEST_REDIS_IDENTITY")
    return observed
