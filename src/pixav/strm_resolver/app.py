"""FastAPI application factory for strm_resolver."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from redis import asyncio as aioredis

from pixav.strm_resolver.routes import router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Initialize and cleanup application resources."""
    redis_url: str | None = app.state.redis_url
    redis_client: aioredis.Redis | None = None

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

    try:
        yield
    finally:
        if redis_client is not None:
            await redis_client.aclose()


def create_app(redis_url: str | None = "redis://localhost:6379/0") -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(title="pixAV Strm-Resolver", lifespan=lifespan)
    app.state.redis_url = redis_url
    app.include_router(router)
    return app
