"""Startup diagnostics must not disclose dependency credentials."""

import logging
from unittest.mock import AsyncMock

from pixav.strm_resolver.app import create_app, lifespan


async def test_startup_failure_redacts_connection_secrets(monkeypatch, caplog):
    logger = logging.getLogger("pixav.strm_resolver.app")
    monkeypatch.setattr(logger, "handlers", [caplog.handler])
    monkeypatch.setattr(logger, "propagate", False)
    monkeypatch.setattr(logger, "disabled", False)
    caplog.set_level(logging.WARNING, logger=logger.name)
    redis_url = "redis://:synthetic-redis-secret@localhost:6379/0"
    db_dsn = "postgresql://test:synthetic-db-secret@localhost/test"
    redis_client = AsyncMock()
    redis_client.ping.side_effect = RuntimeError(redis_url)
    monkeypatch.setattr("pixav.strm_resolver.app.aioredis.from_url", lambda *args, **kwargs: redis_client)
    monkeypatch.setattr(
        "pixav.strm_resolver.app.asyncpg.create_pool",
        AsyncMock(side_effect=RuntimeError(db_dsn)),
    )
    app = create_app(redis_url=redis_url, db_dsn=db_dsn)
    app.state.resolver = AsyncMock()

    async with lifespan(app):
        assert app.state.redis is None
        assert app.state.db_pool is None

    assert "redis unavailable at startup (RuntimeError)" in caplog.text
    assert "postgres unavailable at startup (RuntimeError)" in caplog.text
    assert "synthetic-redis-secret" not in caplog.text
    assert "synthetic-db-secret" not in caplog.text
    redis_client.aclose.assert_awaited_once()
