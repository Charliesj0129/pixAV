"""Focused tests for the optional external-player E2E server window."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from scripts import verify_e2e_full_pipeline as e2e


class _Pool:
    def __init__(self, identity: str) -> None:
        self.identity = identity

    async def fetchval(self, query: str) -> str:
        assert "pg_control_system" in query
        return self.identity


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("", "verify"), (" verify ", "verify"), ("FULL", "full")],
)
def test_parse_media_mode(raw: str, expected: str) -> None:
    assert e2e._parse_media_mode(raw) == expected


def test_parse_media_mode_rejects_unknown_mode() -> None:
    with pytest.raises(RuntimeError, match="verify.*full"):
        e2e._parse_media_mode("download")


def test_sintel_fixture_is_fixed_and_forces_isolated_full_qbit_path() -> None:
    magnet, mode = e2e._resolve_fixture_preset(
        "sintel",
        seed_magnet="",
        local_media_path="",
        isolated_db=True,
        requested_media_mode="verify",
    )

    assert mode == "full"
    assert e2e._extract_info_hash(magnet) == "08ada5a7a6183aae1e09d831df6748d566095a10"
    assert "&amp;" not in magnet
    assert "webtorrent.io" in magnet


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"seed_magnet": "magnet:?xt=urn:btih:abc"}, "mutually exclusive"),
        ({"local_media_path": "/tmp/shortcut.mp4"}, "forbids"),
        ({"isolated_db": False}, "requires PIXAV_E2E_ISOLATED_DB=1"),
    ],
)
def test_sintel_fixture_rejects_non_gate_paths(kwargs: dict[str, object], message: str) -> None:
    options: dict[str, object] = {
        "seed_magnet": "",
        "local_media_path": "",
        "isolated_db": True,
        "requested_media_mode": "verify",
    }
    options.update(kwargs)

    with pytest.raises(RuntimeError, match=message):
        e2e._resolve_fixture_preset("sintel", **options)  # type: ignore[arg-type]


async def test_recording_client_retains_attempted_fixture_hash_when_add_response_fails() -> None:
    delegate = AsyncMock()
    delegate.add_magnet.side_effect = RuntimeError("response lost")
    client = e2e._RecordingTorrentClient(delegate)

    with pytest.raises(RuntimeError, match="response lost"):
        await client.add_magnet(e2e._SINTEL_MAGNET)

    assert client.attempted_hash == e2e._SINTEL_INFO_HASH
    assert client.added_hash is None


async def test_authoritative_db_identity_guard_accepts_exact_match() -> None:
    assert await e2e._assert_db_identity(_Pool("7617854039601979430"), "7617854039601979430") == ("7617854039601979430")


async def test_authoritative_db_identity_guard_requires_expected_identity() -> None:
    with pytest.raises(RuntimeError, match="required"):
        await e2e._assert_db_identity(_Pool("7617854039601979430"), "")


async def test_authoritative_db_identity_guard_rejects_mismatch() -> None:
    with pytest.raises(RuntimeError, match="database identity mismatch"):
        await e2e._assert_db_identity(_Pool("live"), "expected")


class _RunningServer:
    def __init__(self) -> None:
        self.started = False
        self.should_exit = False

    async def serve(self) -> None:
        self.started = True
        while not self.should_exit:
            await asyncio.sleep(0)


class _StoppedServer:
    started = False
    should_exit = False

    async def serve(self) -> None:
        return


async def test_external_player_window_stops_after_acknowledgement(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _RunningServer()
    monkeypatch.setattr(e2e.uvicorn, "Config", MagicMock(return_value=object()))
    monkeypatch.setattr(e2e.uvicorn, "Server", MagicMock(return_value=server))
    completion_event = asyncio.Event()

    async def acknowledge() -> None:
        await asyncio.sleep(0)
        completion_event.set()

    acknowledge_task = asyncio.create_task(acknowledge())
    await e2e._serve_until_acknowledged(
        object(),
        port=18081,
        completion_event=completion_event,
        timeout_seconds=1,
    )
    await acknowledge_task

    assert server.should_exit is True


async def test_external_player_window_rejects_server_that_never_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _StoppedServer()
    monkeypatch.setattr(e2e.uvicorn, "Config", MagicMock(return_value=object()))
    monkeypatch.setattr(e2e.uvicorn, "Server", MagicMock(return_value=server))

    with pytest.raises(RuntimeError, match="stopped before becoming ready"):
        await e2e._serve_until_acknowledged(
            object(),
            port=18081,
            completion_event=asyncio.Event(),
            timeout_seconds=1,
        )

    assert server.should_exit is True
