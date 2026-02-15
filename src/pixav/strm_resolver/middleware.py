"""Middleware components for strm_resolver."""

from __future__ import annotations

import logging
import time

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)


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
