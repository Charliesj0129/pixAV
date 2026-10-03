"""Middleware components for strm_resolver."""

from __future__ import annotations

import logging
import re
import secrets
import stat
import time
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)


class PlaybackAuthMiddleware(BaseHTTPMiddleware):
    """Private deployments reread a service token on every media request.

    A removed or malformed file fails closed; replacing its contents revokes
    the old token for new requests without interrupting active readers.
    """

    def __init__(self, app, *, token_file: str) -> None:
        super().__init__(app)
        self._file = Path(token_file) if token_file else None

    async def dispatch(self, request: Request, call_next) -> Response:
        if not request.url.path.startswith(("/stream/", "/resolve/", "/local/")):
            return await call_next(request)
        try:
            path = self._file
            if path is None or path.is_symlink() or not path.is_file():
                raise ValueError("unavailable token")
            if stat.S_IMODE(path.stat().st_mode) != 0o600:
                raise ValueError("unsafe token permissions")
            token = path.read_text().strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", token):
                raise ValueError("invalid token")
        except (OSError, ValueError):
            return JSONResponse(status_code=503, content={"detail": "private playback unavailable"})
        if not secrets.compare_digest(request.headers.get("authorization", "").encode(), ("Bearer " + token).encode()):
            return JSONResponse(status_code=401, content={"detail": "authentication required"})
        return await call_next(request)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Sliding-window rate limiter backed by Redis.

    Uses INCR + EXPIRE on a per-IP key to enforce a requests-per-minute limit.
    Falls through (allows) if Redis is unavailable — fail-open.
    """

    def __init__(self, app, *, rpm: int = 60) -> None:
        super().__init__(app)
        self._rpm = rpm

    async def dispatch(self, request: Request, call_next) -> Response:
        """Enforce per-IP rate limit using Redis sliding window."""
        redis = getattr(request.app.state, "redis", None)
        if redis is None:
            # No Redis — fail open
            return await call_next(request)

        client_ip = request.client.host if request.client else "unknown"
        # Minute-granularity key
        minute = int(time.time() // 60)
        key = f"pixav:ratelimit:{client_ip}:{minute}"

        try:
            current = await redis.incr(key)
            if current == 1:
                await redis.expire(key, 120)  # expire after 2 min for safety

            if current > self._rpm:
                logger.warning("rate limit exceeded for %s (%d/%d)", client_ip, current, self._rpm)
                return JSONResponse(
                    status_code=429,
                    content={
                        "detail": "Too many requests",
                        "retry_after_seconds": 60 - (int(time.time()) % 60),
                    },
                )
        except Exception as exc:
            # Fail open if Redis is down
            logger.debug("rate limiter Redis error (failing open): %s", exc)

        return await call_next(request)


def setup_cors(app: FastAPI) -> None:
    """Add CORS middleware to the FastAPI application.

    Args:
        app: The FastAPI application instance
    """
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
