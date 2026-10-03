"""FastAPI application factory for strm_resolver."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
from fastapi import FastAPI
from redis import asyncio as aioredis

from pixav.config import get_settings
from pixav.strm_resolver.cache_janitor import PlaybackCacheJanitor, run_janitor
from pixav.strm_resolver.middleware import PlaybackAuthMiddleware, RateLimitMiddleware, setup_cors
from pixav.strm_resolver.playback import PlaybackCache
from pixav.strm_resolver.resolver import GooglePhotosResolver
from pixav.strm_resolver.routes import router

logger = logging.getLogger(__name__)


def _start_janitor(app: FastAPI, db_pool: asyncpg.Pool | None) -> asyncio.Task[None] | None:
    """Eviction runs beside the readers it must wait for, so their locks are visible to it."""
    if db_pool is None or app.state.playback_cache_sweep_seconds <= 0:
        return None
    janitor = PlaybackCacheJanitor(
        db_pool,
        app.state.playback_cache.root,
        ttl_seconds=app.state.playback_cache_ttl_seconds,
        max_bytes=app.state.playback_cache_max_bytes,
    )
    return asyncio.create_task(run_janitor(janitor, app.state.playback_cache_sweep_seconds))


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Initialize and cleanup application resources."""
    redis_url: str | None = app.state.redis_url
    db_dsn: str | None = app.state.db_dsn
    redis_client: aioredis.Redis | None = None
    db_pool: asyncpg.Pool = None

    # Initialize resources
    if redis_url:
        try:
            redis_client = aioredis.from_url(redis_url, encoding="utf-8", decode_responses=True)
            await redis_client.ping()
            app.state.redis = redis_client
        except Exception as exc:
            logger.warning("redis unavailable at startup (%s)", type(exc).__name__)
            app.state.redis = None

    if db_dsn:
        try:
            db_pool = await asyncpg.create_pool(dsn=db_dsn, min_size=1, max_size=5)
            app.state.db_pool = db_pool
        except Exception as exc:
            logger.warning("postgres unavailable at startup (%s)", type(exc).__name__)
            app.state.db_pool = None

    # Start resolver client
    if hasattr(app.state, "resolver"):
        await app.state.resolver.start()

    janitor_task = _start_janitor(app, db_pool)

    try:
        yield
    finally:
        if janitor_task is not None:
            janitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await janitor_task
        if hasattr(app.state, "resolver"):
            await app.state.resolver.close()
        if redis_client is not None:
            await redis_client.aclose()
        if db_pool is not None:
            await db_pool.close()


def create_app(
    redis_url: str | None = "auto",
    db_dsn: str | None = "auto",
) -> FastAPI:
    """Create and configure the FastAPI application."""
    settings = get_settings()
    if db_dsn == "auto":
        db_dsn = settings.dsn
    if redis_url == "auto":
        redis_url = settings.redis_url

    app = FastAPI(title="pixAV Strm-Resolver", lifespan=lifespan)
    app.state.redis_url = redis_url
    app.state.db_dsn = db_dsn
    app.state.redis = None
    app.state.db_pool = None
    app.state.local_share_scheme = settings.pixel_injector_local_share_scheme
    app.state.playback_cache = PlaybackCache(Path(settings.playback_cache_dir))
    app.state.playback_cold_wait_seconds = settings.playback_cold_wait_seconds
    app.state.playback_cache_ttl_seconds = settings.playback_cache_ttl_hours * 3600
    app.state.playback_cache_max_bytes = settings.playback_cache_max_bytes
    app.state.playback_cache_sweep_seconds = settings.playback_cache_sweep_seconds

    # Initialize resolver with settings
    app.state.resolver = GooglePhotosResolver(concurrency=settings.resolver_concurrency)

    app.add_middleware(RateLimitMiddleware, rpm=settings.resolver_rate_limit_rpm)
    if settings.resolver_auth_required:
        app.add_middleware(PlaybackAuthMiddleware, token_file=settings.resolver_token_file)
    setup_cors(app)
    app.include_router(router)
    return app
