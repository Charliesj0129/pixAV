"""Tests for DockerRedroidManager."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from pixav.pixel_injector.redroid import DockerRedroidManager
from pixav.shared.exceptions import RedroidError


@pytest.fixture
def mock_container() -> MagicMock:
    c = MagicMock()
    c.id = "abc123def456"
    c.status = "running"
    c.attrs = {
        "State": {"Health": {"Status": "none"}},
        "NetworkSettings": {"Ports": {"5555/tcp": [{"HostIp": "127.0.0.1", "HostPort": "32768"}]}},
    }
    c.remove = MagicMock()
    c.reload = MagicMock()
    return c


@pytest.fixture
def mock_docker(mock_container: MagicMock) -> MagicMock:
    client = MagicMock()
    client.containers.run.return_value = mock_container
    client.containers.get.return_value = mock_container
    return client


@pytest.fixture
def manager(mock_docker: MagicMock) -> DockerRedroidManager:
    m = DockerRedroidManager(image="redroid/redroid:latest")
    m._docker = mock_docker
    return m


class TestDockerRedroidManager:
    async def test_create_success(self, manager: DockerRedroidManager, mock_docker: MagicMock) -> None:
        session = await manager.create("task-001-abcdef")

        assert session.container_id == "abc123def456"
        assert session.adb_host == "127.0.0.1"
        assert session.adb_port == 32768
        mock_docker.containers.run.assert_called_once()

    async def test_create_api_error(self, manager: DockerRedroidManager, mock_docker: MagicMock) -> None:
        from docker.errors import APIError

        mock_docker.containers.run.side_effect = APIError("create failed")

        with pytest.raises(RedroidError, match="failed to create"):
            await manager.create("task-fail")

    async def test_destroy_success(
        self, manager: DockerRedroidManager, mock_docker: MagicMock, mock_container: MagicMock
    ) -> None:
        await manager.destroy("abc123def456")
        mock_container.remove.assert_called_once_with(force=True)

    async def test_destroy_not_found(self, manager: DockerRedroidManager, mock_docker: MagicMock) -> None:
        from docker.errors import NotFound

        mock_docker.containers.get.side_effect = NotFound("gone")

        # Should not raise — just logs warning
        await manager.destroy("missing-container")

    async def test_wait_ready_running(self, manager: DockerRedroidManager, mock_container: MagicMock) -> None:
        result = await manager.wait_ready("abc123def456", timeout=5)
        assert result is True

    async def test_wait_ready_exited(self, manager: DockerRedroidManager, mock_container: MagicMock) -> None:
        mock_container.status = "exited"

        result = await manager.wait_ready("abc123def456", timeout=5)
        assert result is False
