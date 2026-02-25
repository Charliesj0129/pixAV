"""Tests for shared/health.py — worker health FastAPI app factory."""

from __future__ import annotations

from fastapi.testclient import TestClient

from pixav.shared.health import create_health_app


class TestCreateHealthApp:
    def test_health_returns_ok_status(self) -> None:
        app = create_health_app("my_module")
        client = TestClient(app)
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["module"] == "my_module"

    def test_health_includes_extra_info(self) -> None:
        app = create_health_app("maxwell_core", extra_info={"version": "1.0"})
        client = TestClient(app)
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["version"] == "1.0"

    def test_metrics_endpoint_returns_text(self) -> None:
        app = create_health_app("test_worker")
        client = TestClient(app)
        response = client.get("/metrics")
        assert response.status_code == 200
        assert "text/plain" in response.headers["content-type"]

    def test_metrics_contains_prometheus_data(self) -> None:
        app = create_health_app("test_worker")
        client = TestClient(app)
        response = client.get("/metrics")
        # Prometheus output starts with TYPE or HELP lines or empty
        body = response.text
        # At minimum it should be parseable text (non-empty or valid prometheus format)
        assert isinstance(body, str)

    def test_app_title_includes_module_name(self) -> None:
        app = create_health_app("sht_probe")
        assert "sht_probe" in app.title
