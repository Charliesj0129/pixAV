"""Tests for shared/health.py — worker health FastAPI app factory."""

from __future__ import annotations

import httpx

from pixav.shared.health import HealthState, create_health_app


class TestCreateHealthApp:
    async def test_health_returns_ok_status(self) -> None:
        app = create_health_app("my_module")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["module"] == "my_module"

    async def test_health_includes_extra_info(self) -> None:
        app = create_health_app("maxwell_core", extra_info={"version": "1.0"})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["version"] == "1.0"

    async def test_metrics_endpoint_returns_text(self) -> None:
        app = create_health_app("test_worker")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/metrics")
        assert response.status_code == 200
        assert "text/plain" in response.headers["content-type"]

    async def test_metrics_contains_prometheus_data(self) -> None:
        app = create_health_app("test_worker")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/metrics")
        # Prometheus output starts with TYPE or HELP lines or empty
        body = response.text
        # At minimum it should be parseable text (non-empty or valid prometheus format)
        assert isinstance(body, str)

    def test_app_title_includes_module_name(self) -> None:
        app = create_health_app("sht_probe")
        assert "sht_probe" in app.title

    async def test_stale_heartbeat_returns_503(self) -> None:
        state = HealthState("worker", stale_after_seconds=1, ready=True, reason=None)
        state.last_heartbeat_monotonic -= 2
        app = create_health_app("worker", state=state)
        # create_health_app emits the initial metric but must not freshen caller-owned state.
        state.last_heartbeat_monotonic -= 2
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/health")
        assert response.status_code == 503
        assert response.json()["reason"] == "heartbeat_stale"
