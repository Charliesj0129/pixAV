"""Wrong host identities must stop work before media allocation."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from pixav.first_4k import guards
from pixav.pixel_injector.canary import CanaryBlockedError


@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.setattr(guards, "WORK", tmp_path)
    db = SimpleNamespace(
        attrs={"Mounts": [{"Type": "volume", "Name": guards.PROJECT + "_movie_postgres"}]},
        exec_run=Mock(return_value=SimpleNamespace(exit_code=0, output=b"db-id")),
    )
    cache = SimpleNamespace(
        attrs={"Mounts": [{"Type": "volume", "Name": guards.PROJECT + "_movie_redis"}]},
        exec_run=Mock(return_value=SimpleNamespace(exit_code=0, output=b"run_id:redis-id")),
    )
    qbit = SimpleNamespace(
        attrs={
            "Mounts": [
                {"Destination": "/downloads", "Source": str(tmp_path / "downloads")},
                {"Destination": "/config", "Source": str(tmp_path / "qbit-config")},
            ]
        }
    )
    monkeypatch.setattr(
        guards, "container", lambda _, name: {"postgres": db, "redis": cache, "qbittorrent": qbit}[name]
    )
    conn = AsyncMock()
    conn.fetchval.return_value = "pixav_first_4k"
    connect = AsyncMock(return_value=conn)
    monkeypatch.setattr(guards.asyncpg, "connect", connect)
    redis = AsyncMock()
    monkeypatch.setattr(guards.aioredis, "from_url", lambda _: redis)
    monkeypatch.setattr(guards, "database_identity", AsyncMock(return_value="db-id"))
    monkeypatch.setattr(guards, "redis_identity", AsyncMock(return_value="redis-id"))
    monkeypatch.setattr(guards, "get_profile", lambda *_a, **_k: SimpleNamespace(image="fixture"))
    budget = Mock(return_value=[{"ready": True}])
    monkeypatch.setattr(guards, "disk_budget", budget)
    return SimpleNamespace(
        db=db,
        cache=cache,
        qbit=qbit,
        conn=conn,
        redis=redis,
        connect=connect,
        budget=budget,
        client=SimpleNamespace(images=Mock()),
    )


def change_host(host, case):
    if case == "db-volume":
        host.db.attrs["Mounts"][0]["Name"] = "production"
    elif case == "redis-volume":
        host.cache.attrs["Mounts"][0]["Name"] = "production"
    elif case == "qbit":
        host.qbit.attrs["Mounts"][0]["Source"] = "/production"
    elif case == "db-id":
        host.db.exec_run.return_value.output = b"other-db"
    elif case == "db-name":
        host.conn.fetchval.return_value = "production"
    elif case == "redis-id":
        host.cache.exec_run.return_value.output = b"run_id:other-redis"
    elif case == "disk":
        host.budget.return_value = [{"ready": False}]


@pytest.mark.parametrize("case", ["owned", "db-volume", "redis-volume", "qbit", "db-id", "db-name", "redis-id", "disk"])
async def test_preflight_rejects_wrong_ownership_identity_and_low_space(host, case):
    change_host(host, case)
    if case == "owned":
        result = await guards.preflight(host.client, SimpleNamespace())
        assert result["db_identity"] == "db-id"
        assert result["redis_identity"] == "redis-id"
    else:
        with pytest.raises(CanaryBlockedError):
            await guards.preflight(host.client, SimpleNamespace())
    if case in {"db-volume", "redis-volume", "qbit"}:
        host.connect.assert_not_awaited()
    else:
        host.conn.close.assert_awaited_once()
        host.redis.aclose.assert_awaited_once()
    if case not in {"owned", "disk"}:
        host.budget.assert_not_called()


@pytest.mark.parametrize("case", ["owned", "volume", "identity"])
async def test_read_only_database_check_closes_on_mismatch(host, case):
    if case == "volume":
        host.db.attrs["Mounts"] = []
    if case == "identity":
        host.db.exec_run.return_value.output = b"other-db"
    if case == "owned":
        assert await guards.checked_database(host.client) is host.conn
        host.conn.close.assert_not_awaited()
    else:
        with pytest.raises(CanaryBlockedError):
            await guards.checked_database(host.client)
        if case == "identity":
            host.conn.close.assert_awaited_once()
        else:
            host.connect.assert_not_awaited()
