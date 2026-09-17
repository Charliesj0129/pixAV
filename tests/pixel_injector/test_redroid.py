"""Tests for DockerRedroidManager."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pixav.pixel_injector.profiles import AndroidProfile, ReadinessCheck
from pixav.pixel_injector.redroid import DockerRedroidManager
from pixav.shared.exceptions import RedroidError


def _exec_result(exit_code: int, output: bytes) -> MagicMock:
    result = MagicMock()
    result.exit_code = exit_code
    result.output = output
    return result


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
    c.stop = MagicMock()
    c.wait = MagicMock(side_effect=TimeoutError("still running"))
    c.reload = MagicMock()
    # Android has booted, so the default readiness probe succeeds.
    c.exec_run = MagicMock(return_value=_exec_result(0, b"1\n"))
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
        # Android init must remain PID 1; Docker's init wrapper prevents boot.
        assert "init" not in mock_docker.containers.run.call_args.kwargs

    async def test_create_api_error(self, manager: DockerRedroidManager, mock_docker: MagicMock) -> None:
        from docker.errors import APIError

        mock_docker.containers.run.side_effect = APIError("create failed")

        with pytest.raises(RedroidError, match="failed to create"):
            await manager.create("task-fail")

    async def test_create_waits_for_dynamic_adb_port(
        self, manager: DockerRedroidManager, mock_container: MagicMock
    ) -> None:
        mock_container.attrs["NetworkSettings"]["Ports"]["5555/tcp"] = None
        reloads = 0

        def publish_port() -> None:
            nonlocal reloads
            reloads += 1
            if reloads == 2:
                mock_container.attrs["NetworkSettings"]["Ports"]["5555/tcp"] = [
                    {"HostIp": "127.0.0.1", "HostPort": "45678"}
                ]

        mock_container.reload.side_effect = publish_port
        with patch("pixav.pixel_injector.redroid.asyncio.sleep", new=AsyncMock()):
            session = await manager.create("task-port-race")

        assert reloads == 2
        assert session.adb_port == 45678

    async def test_create_removes_container_when_adb_port_never_appears(
        self, manager: DockerRedroidManager, mock_container: MagicMock
    ) -> None:
        with patch("pixav.pixel_injector.redroid._wait_for_adb_port", new=AsyncMock(return_value=None)):
            with pytest.raises(RedroidError, match="did not publish an ADB port"):
                await manager.create("task-no-port")

        mock_container.remove.assert_called_once_with(force=True)

    async def test_destroy_success(
        self, manager: DockerRedroidManager, mock_docker: MagicMock, mock_container: MagicMock
    ) -> None:
        await manager.destroy("abc123def456")
        mock_container.exec_run.assert_called_once_with(["setprop", "sys.powerctl", "shutdown"])
        mock_container.wait.assert_called_once_with(timeout=30)
        mock_container.stop.assert_called_once_with(timeout=10)
        mock_container.remove.assert_called_once_with(force=True)

    async def test_destroy_exited_container_does_not_stop_again(
        self, manager: DockerRedroidManager, mock_container: MagicMock
    ) -> None:
        mock_container.status = "exited"

        await manager.destroy("abc123def456")

        mock_container.stop.assert_not_called()
        mock_container.remove.assert_called_once_with(force=True)

    async def test_destroy_skips_docker_stop_after_android_exits(
        self, manager: DockerRedroidManager, mock_container: MagicMock
    ) -> None:
        mock_container.wait.side_effect = None
        mock_container.wait.return_value = {"StatusCode": 0}
        mock_container.reload.side_effect = lambda: setattr(mock_container, "status", "exited")

        await manager.destroy("abc123def456")

        mock_container.stop.assert_not_called()
        mock_container.remove.assert_called_once_with(force=True)

    async def test_destroy_not_found(self, manager: DockerRedroidManager, mock_docker: MagicMock) -> None:
        from docker.errors import NotFound

        mock_docker.containers.get.side_effect = NotFound("gone")

        # Should not raise — just logs warning
        await manager.destroy("missing-container")

    async def test_wait_ready_requires_android_boot_not_just_a_running_container(
        self, manager: DockerRedroidManager, mock_container: MagicMock
    ) -> None:
        """A running container proves nothing: stock Redroid declares no HEALTHCHECK."""
        result = await manager.wait_ready("abc123def456", timeout=5)

        assert result is True
        mock_container.exec_run.assert_called_with("getprop sys.boot_completed")

    async def test_wait_ready_false_while_android_is_still_booting(
        self, manager: DockerRedroidManager, mock_container: MagicMock
    ) -> None:
        """sys.boot_completed is empty until Android finishes booting."""
        mock_container.exec_run.return_value = _exec_result(0, b"\n")

        result = await manager.wait_ready("abc123def456", timeout=5)

        assert result is False

    async def test_wait_ready_exited(self, manager: DockerRedroidManager, mock_container: MagicMock) -> None:
        mock_container.status = "exited"

        result = await manager.wait_ready("abc123def456", timeout=5)
        assert result is False


class TestProfileApplication:
    """The spoof is only real if the profile reaches the container."""

    @pytest.fixture
    def profile(self) -> AndroidProfile:
        return AndroidProfile(
            name="test_pixel_xl",
            image="pixav/redroid-gphotos-pixelxl@sha256:deadbeef",
            args=("ro.product.model=Pixel XL", "ro.product.device=marlin"),
            readiness=(
                ReadinessCheck(command="getprop sys.boot_completed", contains="1"),
                ReadinessCheck(command="pm list features", contains="PIXEL_2016_EXPERIENCE"),
            ),
        )

    async def test_profile_args_are_passed_as_container_command(
        self, mock_docker: MagicMock, profile: AndroidProfile
    ) -> None:
        """Regression: the old _DEFAULT_PROPS never reached containers.run at all."""
        manager = DockerRedroidManager("ignored:latest", profile=profile)
        manager._docker = mock_docker

        await manager.create("task-001-abcdef")

        args, kwargs = mock_docker.containers.run.call_args
        assert args[0] == "pixav/redroid-gphotos-pixelxl@sha256:deadbeef"
        assert kwargs["command"] == ["ro.product.model=Pixel XL", "ro.product.device=marlin"]

    async def test_profile_image_wins_over_the_positional_image(
        self, mock_docker: MagicMock, profile: AndroidProfile
    ) -> None:
        manager = DockerRedroidManager("stale/image:latest", profile=profile)
        assert manager.image == "pixav/redroid-gphotos-pixelxl@sha256:deadbeef"

    async def test_readiness_requires_every_profile_check(
        self, mock_docker: MagicMock, mock_container: MagicMock, profile: AndroidProfile
    ) -> None:
        """Booted, but the Pixel feature is absent: not ready."""
        manager = DockerRedroidManager("ignored:latest", profile=profile)
        manager._docker = mock_docker
        mock_container.exec_run.side_effect = lambda command: (
            _exec_result(0, b"1\n")
            if command == "getprop sys.boot_completed"
            else _exec_result(0, b"feature:android.hardware.wifi\n")
        )

        assert await manager.wait_ready("abc123def456", timeout=5) is False

    async def test_readiness_passes_when_the_spoof_is_visible(
        self, mock_docker: MagicMock, mock_container: MagicMock, profile: AndroidProfile
    ) -> None:
        manager = DockerRedroidManager("ignored:latest", profile=profile)
        manager._docker = mock_docker
        mock_container.exec_run.side_effect = lambda command: (
            _exec_result(0, b"1\n")
            if command == "getprop sys.boot_completed"
            else _exec_result(0, b"feature:com.google.android.feature.PIXEL_2016_EXPERIENCE\n")
        )

        assert await manager.wait_ready("abc123def456", timeout=5) is True
