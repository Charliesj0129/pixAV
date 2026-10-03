"""Tests for strm_resolver middleware behavior."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from pixav.strm_resolver.app import create_app


@pytest.mark.asyncio
async def test_rate_limit_blocks_when_rpm_exceeded() -> None:
    app = create_app(redis_url=None, db_dsn=None)
    redis = AsyncMock()
    redis.incr.return_value = 999
    app.state.redis = redis

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 429
    body = response.json()
    assert body["detail"] == "Too many requests"


@pytest.mark.asyncio
async def test_rate_limit_allows_when_redis_unavailable() -> None:
    app = create_app(redis_url=None, db_dsn=None)
    app.state.redis = None

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "module": "strm_resolver"}


@pytest.mark.parametrize("path", ["/stream/video", "/resolve/video", "/local/video"])
async def test_private_media_rejects_missing_token_and_revoked_token(tmp_path, path):
    from fastapi import FastAPI

    from pixav.strm_resolver.middleware import PlaybackAuthMiddleware

    secret = tmp_path / "token"
    secret.write_text("a" * 64)
    secret.chmod(0o600)
    app = FastAPI()
    app.add_middleware(PlaybackAuthMiddleware, token_file=str(secret))

    @app.get(path)
    async def protected():
        return {"ok": True}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get(path)).status_code == 401
        assert (await client.get(path + "?token=" + "a" * 64)).status_code == 401
        assert (await client.get(path, headers={"Authorization": "Bearer " + "a" * 64})).status_code == 200
        secret.write_text("b" * 64)
        assert (await client.get(path, headers={"Authorization": "Bearer " + "a" * 64})).status_code == 401
        assert (await client.get(path, headers={"Authorization": "Bearer " + "b" * 64})).status_code == 200
        secret.unlink()
        assert (await client.get(path, headers={"Authorization": "Bearer " + "b" * 64})).status_code == 503


@pytest.mark.parametrize("defect", ["missing", "permissions", "symlink", "invalid"])
async def test_private_playback_fails_closed_on_unusable_secret(tmp_path, defect):
    from fastapi import FastAPI

    from pixav.strm_resolver.middleware import PlaybackAuthMiddleware

    secret = tmp_path / "token"
    secret.write_text("a" * 64)
    secret.chmod(0o600)
    if defect == "missing":
        secret.unlink()
    elif defect == "permissions":
        secret.chmod(0o644)
    elif defect == "invalid":
        secret.write_text("invalid")
    else:
        link = tmp_path / "link"
        link.symlink_to(secret)
        secret = link
    app = FastAPI()
    app.add_middleware(PlaybackAuthMiddleware, token_file=str(secret))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/stream/video")).status_code == 503
