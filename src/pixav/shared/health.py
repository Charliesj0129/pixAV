"""Lightweight FastAPI health application shared by worker services.

Each worker module calls ``create_health_app()`` to get a minimal FastAPI
application that exposes:

- ``GET /health``  → ``{"status": "ok", "module": "<name>"}``
- ``GET /metrics`` → Prometheus text-format metrics
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

from pixav.shared.metrics import get_metrics_output


def create_health_app(module_name: str, extra_info: dict[str, Any] | None = None) -> FastAPI:
    """Create a minimal FastAPI application for health and metrics.

    Args:
        module_name: Short identifier for the worker (e.g. ``"maxwell_core"``).
        extra_info:  Optional additional fields to include in the /health response.

    Returns:
        Configured FastAPI application.
    """
    app = FastAPI(title=f"pixAV {module_name} health", docs_url=None, redoc_url=None)
    app.state.module_name = module_name
    app.state.extra_info = extra_info or {}

    @app.get("/health")
    async def health() -> dict[str, Any]:
        response: dict[str, Any] = {"status": "ok", "module": app.state.module_name}
        response.update(app.state.extra_info)
        return response

    @app.get("/metrics", response_class=PlainTextResponse)
    async def metrics() -> PlainTextResponse:
        return PlainTextResponse(
            content=get_metrics_output().decode("utf-8"),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    return app
