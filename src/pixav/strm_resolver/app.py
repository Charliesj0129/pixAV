"""FastAPI application factory for strm_resolver."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI
from redis import asyncio as aioredis

from pixav.config import get_settings
from pixav.strm_resolver.middleware import RateLimitMiddleware, setup_cors
from pixav.strm_resolver.resolver import GooglePhotosResolver
from pixav.strm_resolver.routes import router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Initialize and cleanup application resources."""
    redis_url: str | None = app.state.redis_url
    db_dsn: str | None = app.state.db_dsn
    redis_client: aioredis.Redis | None = None
    db_pool: asyncpg.Pool | None = None

    if redis_url:
        try:
            redis_client = aioredis.from_url(redis_url, encoding="utf-8", decode_responses=True)
            await redis_client.ping()
            app.state.redis = redis_client
        except Exception as exc:  # pragma: no cover - depends on external redis
            logger.warning("redis unavailable at startup (%s): %s", redis_url, exc)
            app.state.redis = None
    else:
        app.state.redis = None

    if db_dsn:
        try:
            db_pool = await asyncpg.create_pool(dsn=db_dsn, min_size=1, max_size=5)
            app.state.db_pool = db_pool
        except Exception as exc:  # pragma: no cover - depends on external postgres
            logger.warning("postgres unavailable at startup (%s): %s", db_dsn, exc)
            app.state.db_pool = None
    else:
        app.state.db_pool = None

    try:
        yield
    finally:
        if redis_client is not None:
            await redis_client.aclose()
        if db_pool is not None:
            await db_pool.close()


def create_app(
    redis_url: str | None = "redis://localhost:6379/0",
    db_dsn: str | None = "auto",
) -> FastAPI:
    """Create and configure the FastAPI application."""
    if db_dsn == "auto":
        db_dsn = get_settings().dsn

    app = FastAPI(title="pixAV Strm-Resolver", lifespan=lifespan)
    app.state.redis_url = redis_url
    app.state.db_dsn = db_dsn
    app.state.redis = None
    app.state.db_pool = None
    app.state.resolver = GooglePhotosResolver()
    setup_cors(app)
    app.add_middleware(RateLimitMiddleware)
    app.include_router(router)
    return app
