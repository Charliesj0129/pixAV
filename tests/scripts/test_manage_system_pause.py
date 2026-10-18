from __future__ import annotations

import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pixav.shared.pause import is_paused_value
from scripts import manage_system_pause as module
from scripts.manage_system_pause import _decode_record


def test_owned_pause_record_is_recognized() -> None:
    raw = '{"owner":"pixav-phase0","paused":true,"token":"abc"}'

    assert is_paused_value(raw) is True
    assert _decode_record(raw) == {"owner": "pixav-phase0", "paused": True, "token": "abc"}


def test_legacy_values_remain_compatible() -> None:
    assert is_paused_value("true") is True
    assert is_paused_value("false") is False
    assert is_paused_value(None) is False


def test_owned_record_accepts_redis_bytes() -> None:
    assert _decode_record(b'{"owner":"pixav-phase0","paused":true}') == {
        "owner": "pixav-phase0",
        "paused": True,
    }


@pytest.fixture
def pause_client(monkeypatch):
    settings = SimpleNamespace(system_pause_key="synthetic:pause", expected_redis_run_id="a" * 40)
    client = AsyncMock()
    client.info.return_value = {"run_id": settings.expected_redis_run_id}
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr(module, "create_redis", AsyncMock(return_value=client))
    return settings, client


async def test_status_does_not_disclose_pause_token(pause_client, capsys):
    settings, client = pause_client
    token = "synthetic-owner-token-00000000000000000"
    client.get.return_value = json.dumps(
        {"owner": "pixav-phase0", "paused": True, "token": token, "redis_run_id": settings.expected_redis_run_id}
    )
    assert await module._run("status", reason="", token="") == 0
    output = capsys.readouterr().out
    assert token not in output
    assert json.loads(output)["paused"] is True
    client.set.assert_not_awaited()
    client.eval.assert_not_awaited()
    client.delete.assert_not_awaited()


async def test_resume_preserves_concurrent_replacement(pause_client, capsys):
    settings, client = pause_client
    token = "synthetic-owner-token-00000000000000000"
    selected = json.dumps(
        {"owner": "pixav-phase0", "paused": True, "token": token, "redis_run_id": settings.expected_redis_run_id}
    )
    replacement = '{"owner":"another-operator","paused":true}'
    state = {settings.system_pause_key: selected}

    async def read_then_replace(key):
        observed = state[key]
        state[key] = replacement
        return observed

    async def unconditional_delete(key):
        return int(state.pop(key, None) is not None)

    async def compare_delete(_script, _count, key, observed, *_identity):
        if state.get(key) != observed:
            return 0
        return int(state.pop(key, None) is not None)

    client.get.side_effect = read_then_replace
    client.delete.side_effect = unconditional_delete
    client.eval.side_effect = compare_delete
    assert await module._run("resume", reason="", token=token) == 2
    assert state[settings.system_pause_key] == replacement
    assert token not in capsys.readouterr().out


async def test_mutations_require_instance_binding_before_connect(pause_client, capsys):
    settings, _client = pause_client
    settings.expected_redis_run_id = ""
    assert await module._run("pause", reason="", token="synthetic-owner-token-00000000000000000") == 2
    module.create_redis.assert_not_awaited()
    assert json.loads(capsys.readouterr().out)["code"] == "REDIS_INSTANCE_BINDING_REQUIRED"


async def test_unexpected_connection_error_is_redacted(pause_client, capsys):
    private_error = "synthetic-private-connection-string"
    module.create_redis.side_effect = RuntimeError(private_error)
    assert await module._run("status", reason="", token="") == 2
    assert private_error not in capsys.readouterr().out


def test_pause_token_is_private_from_creation_and_reused(tmp_path):
    path = tmp_path / "owner-token"
    observed_modes = []
    real_open = module.os.open

    def checked_open(name, flags, mode=0o777):
        descriptor = real_open(name, flags, mode)
        observed_modes.append(os.fstat(descriptor).st_mode & 0o777)
        return descriptor

    from unittest.mock import patch

    with patch.object(module.os, "open", checked_open):
        token = module._read_token(path, create=True)
        assert module._read_token(path, create=True) == token
    assert observed_modes and set(observed_modes) == {0o600}


@pytest.mark.parametrize("defect", ["file-mode", "parent-mode", "symlink", "fifo", "oversized", "invalid-token"])
def test_pause_token_refuses_unsafe_file(tmp_path, defect):
    path = tmp_path / "owner-token"
    path.write_text("synthetic-owner-token-00000000000000000")
    path.chmod(0o600)
    if defect == "file-mode":
        path.chmod(0o644)
    elif defect == "parent-mode":
        tmp_path.chmod(0o755)
    elif defect == "symlink":
        link = tmp_path / "link"
        link.symlink_to(path)
        path = link
    elif defect == "fifo":
        path.unlink()
        os.mkfifo(path, 0o600)
    elif defect == "oversized":
        path.write_text("a" * 4097)
    else:
        path.write_text("synthetic-short")
    with pytest.raises((module.PauseRefusedError, OSError)):
        module._read_token(path)


def test_inline_cli_token_is_rejected_without_echo(monkeypatch, capsys):
    import sys

    token = "synthetic-inline-private-token-00000000"
    monkeypatch.setattr(sys, "argv", ["pause", "resume", "--token", token])
    with pytest.raises(SystemExit) as exit_info:
        module.main()
    assert exit_info.value.code == 2
    captured = capsys.readouterr()
    assert token not in captured.out + captured.err
    assert "INLINE_PAUSE_TOKEN_REFUSED" in captured.out
