"""Middleware components for strm_resolver."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Rate limiting middleware placeholder.

    Currently passes through all requests without rate limiting.
    """

    async def dispatch(self, request: Request, call_next):
        """Process the request without rate limiting.

        Args:
            request: The incoming request
            call_next: The next middleware or route handler

        Returns:
            The response from the next handler
        """
        # Placeholder: pass through for now
        response = await call_next(request)
        return response


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
