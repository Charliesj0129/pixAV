"""Real PostgreSQL refuses duplicate authority and cancels a disconnected owner."""

import asyncio

import pytest

from pixav.shared.authority_ownership import execution_authority_ownership
from pixav.shared.photos_ui_ownership import photos_ui_lock_key, photos_ui_ownership

pytestmark = pytest.mark.integration


async def test_duplicate_authority_refused_until_owner_exits(integration_db):
    async with execution_authority_ownership(integration_db):
        with pytest.raises(RuntimeError, match="another execution authority"):
            async with execution_authority_ownership(integration_db):
                pytest.fail("a competing authority acquired ownership")
    async with execution_authority_ownership(integration_db):
        pass


async def test_database_disconnect_cancels_owner_and_releases_ownership(integration_db):
    ready = asyncio.Event()
    backend = []

    async def owner():
        async with execution_authority_ownership(integration_db) as conn:
            backend.append(await conn.fetchval("SELECT pg_backend_pid()"))
            ready.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(owner())
    try:
        await asyncio.wait_for(ready.wait(), timeout=10)
        await integration_db.fetchval("SELECT pg_terminate_backend($1)", backend[0])
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=10)
        async with execution_authority_ownership(integration_db):
            pass
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_photos_uploader_and_observer_share_one_account_owner_lock(integration_db):
    async with photos_ui_ownership(integration_db, "synthetic-account", "synthetic-owner") as check:
        check()
        with pytest.raises(RuntimeError, match="another reader or writer"):
            async with photos_ui_ownership(integration_db, "synthetic-account", "synthetic-owner"):
                pytest.fail("observer overlapped uploader")
    async with photos_ui_ownership(integration_db, "synthetic-account", "synthetic-owner") as check:
        check()
    assert photos_ui_lock_key("synthetic-account", "synthetic-owner") != photos_ui_lock_key(
        "another", "synthetic-owner"
    )


async def test_photos_ui_disconnect_cancels_owner(integration_db):
    ready = asyncio.Event()

    async def reader():
        async with photos_ui_ownership(integration_db, "synthetic", "synthetic"):
            ready.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(reader())
    try:
        await asyncio.wait_for(ready.wait(), timeout=10)
        key = photos_ui_lock_key("synthetic", "synthetic")
        pid = await integration_db.fetchval(
            """SELECT pid FROM pg_locks WHERE locktype='advisory' AND granted
            AND classid::bigint=$1 AND objid::bigint=$2 AND objsubid=1""",
            (key & ((1 << 64) - 1)) >> 32,
            key & 0xFFFFFFFF,
        )
        await integration_db.fetchval("SELECT pg_terminate_backend($1)", pid)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=10)
        async with photos_ui_ownership(integration_db, "synthetic", "synthetic") as check:
            check()
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_same_photos_account_cannot_overlap_different_runtime_owners(integration_db):
    async with photos_ui_ownership(integration_db, "synthetic-account", "first-runtime"):
        with pytest.raises(RuntimeError, match="another reader or writer"):
            async with photos_ui_ownership(integration_db, "synthetic-account", "second-runtime"):
                pytest.fail("one account acquired overlapping UI owners")
    async with photos_ui_ownership(integration_db, "synthetic-account", "second-runtime") as check:
        check()


async def test_same_runtime_owner_cannot_overlap_different_photos_accounts(integration_db):
    async with photos_ui_ownership(integration_db, "first-account", "synthetic-runtime"):
        with pytest.raises(RuntimeError, match="another reader or writer"):
            async with photos_ui_ownership(integration_db, "second-account", "synthetic-runtime"):
                pytest.fail("one runtime acquired overlapping account sessions")
    async with photos_ui_ownership(integration_db, "second-account", "synthetic-runtime") as check:
        check()
