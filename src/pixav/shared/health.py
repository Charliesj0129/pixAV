"""Lightweight FastAPI health application shared by worker services.

Each worker module calls ``create_health_app()`` to get a minimal FastAPI
application that exposes:

- ``GET /health``  → ``{"status": "ok", "module": "<name>"}``
- ``GET /metrics`` → Prometheus text-format metrics
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse

from pixav.shared.metrics import get_metrics_output, set_worker_health


@dataclass
class HealthState:
    """Thread-safe-enough state shared by an asyncio loop and watchdog thread."""

    module: str
    stale_after_seconds: float = 120.0
    ready: bool = False
    reason: str | None = "worker_starting"
    started_at: float = field(default_factory=time.time)
    last_heartbeat_wall: float = field(default_factory=time.time)
    last_heartbeat_monotonic: float = field(default_factory=time.monotonic)

    def touch(self) -> None:
        self.last_heartbeat_wall = time.time()
        self.last_heartbeat_monotonic = time.monotonic()
        set_worker_health(
            self.module,
            ready=self.ready,
            heartbeat_timestamp=self.last_heartbeat_wall,
            start_timestamp=self.started_at,
        )

    def mark_ready(self) -> None:
        self.ready = True
        self.reason = None
        self.touch()

    def mark_unready(self, reason: str) -> None:
        self.ready = False
        self.reason = reason
        self.touch()

    @property
    def stale(self) -> bool:
        return time.monotonic() - self.last_heartbeat_monotonic > self.stale_after_seconds

    def snapshot(self) -> tuple[bool, str | None, float]:
        age = max(0.0, time.monotonic() - self.last_heartbeat_monotonic)
        if not self.ready:
            return False, self.reason or "worker_not_ready", age
        if self.stale:
            return False, "heartbeat_stale", age
        return True, None, age


def create_health_app(
    module_name: str,
    extra_info: dict[str, Any] | None = None,
    *,
    state: HealthState | None = None,
) -> FastAPI:
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
    # Backward-compatible default for callers that only need a static health app.
    app.state.health_state = state or HealthState(module_name, ready=True, reason=None)
    app.state.health_state.touch()

    @app.get("/health")
    async def health() -> JSONResponse:
        healthy, reason, heartbeat_age = app.state.health_state.snapshot()
        response: dict[str, Any] = {
            "status": "ok" if healthy else "unhealthy",
            "module": app.state.module_name,
            "ready": app.state.health_state.ready,
            "heartbeat_age_seconds": round(heartbeat_age, 3),
        }
        if reason:
            response["reason"] = reason
        response.update(app.state.extra_info)
        return JSONResponse(response, status_code=200 if healthy else 503)

    @app.get("/metrics", response_class=PlainTextResponse)
    async def metrics() -> PlainTextResponse:
        return PlainTextResponse(
            content=get_metrics_output().decode("utf-8"),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    return app
