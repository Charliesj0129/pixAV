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

from pixav.config import Settings, get_settings
from pixav.shared.db import connection_initializer
from pixav.shared.redis_client import create_redis
from pixav.shared.workflow import require_workflow_role
from pixav.strm_resolver.cache_janitor import PlaybackCacheJanitor, run_janitor
from pixav.strm_resolver.middleware import PlaybackAuthMiddleware, RateLimitMiddleware, setup_cors
from pixav.strm_resolver.playback import PlaybackCache
from pixav.strm_resolver.resolver import GooglePhotosResolver
from pixav.strm_resolver.routes import router

logger = logging.getLogger(__name__)


async def _open_database(
    dsn: str, redis_client: aioredis.Redis | None, *, settings: Settings | None = None
) -> asyncpg.Pool:
    try:
        return await asyncio.wait_for(
            asyncpg.create_pool(
                dsn=dsn, min_size=1, max_size=5, init=connection_initializer(settings or get_settings())
            ),
            timeout=10,
        )
    except Exception as exc:
        logger.warning("postgres unavailable at startup (%s)", type(exc).__name__)
        if redis_client is not None:
            await redis_client.aclose()
        # Let the container restart policy retry initialization. Serving with no
        # pool would leave playback broken after PostgreSQL recovered.
        raise RuntimeError("PostgreSQL is required for configured playback") from None


async def _require_reader(db_pool: asyncpg.Pool, redis_client: aioredis.Redis | None) -> None:
    try:
        await require_workflow_role(db_pool, "pixav_playback_reader")
    except Exception:
        await db_pool.close()
        if redis_client is not None:
            await redis_client.aclose()
        raise


async def _open_redis(url: str, settings: Settings) -> aioredis.Redis | None:
    client = None
    try:
        client = await asyncio.wait_for(create_redis(settings.model_copy(update={"redis_url": url})), timeout=10)
        await asyncio.wait_for(client.ping(), timeout=3)
        return client
    except Exception as exc:
        logger.warning("redis unavailable at startup (%s)", type(exc).__name__)
        if client is not None:
            await client.aclose()
        if settings.expected_redis_run_id or settings.expected_redis_database is not None:
            raise RuntimeError("Redis instance binding required for configured playback") from None
        return None


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
    db_pool: asyncpg.Pool | None = None
    settings: Settings = app.state.settings

    # Initialize resources
    if redis_url:
        redis_client = await _open_redis(redis_url, settings)
        app.state.redis = redis_client

    if db_dsn:
        db_pool = await _open_database(db_dsn, redis_client, settings=settings)
        if app.state.require_reader_role:
            await _require_reader(db_pool, redis_client)
        app.state.db_pool = db_pool

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
    if settings.managed_media_workflow and not all(
        (
            db_dsn,
            redis_url,
            settings.resolver_require_reader_role,
            settings.resolver_auth_required,
            settings.resolver_token_file,
            settings.expected_db_identity,
            settings.expected_db_name,
            settings.expected_redis_run_id,
        )
    ):
        raise RuntimeError("Managed playback requires private authentication and bound reader instances")

    app = FastAPI(title="pixAV Strm-Resolver", lifespan=lifespan)
    app.state.settings = settings
    app.state.redis_url = redis_url
    app.state.db_dsn = db_dsn
    app.state.redis = None
    app.state.db_pool = None
    app.state.require_reader_role = settings.resolver_require_reader_role
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
