"""Operator boundary: bounded output, private token and read-only identities."""

from __future__ import annotations

import hashlib
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from pixav.shared.cleanup_recovery import CleanupApprovalError, CleanupRuntimeBinding
from scripts import manage_cleanup as module

RUNTIME = CleanupRuntimeBinding(docker_context="synthetic", docker_daemon_id="synthetic-daemon", redis_run_id="a" * 40)
TOKEN = "synthetic-owner-token-00000000000000000"


def test_authorize_defaults_to_dry_run_and_has_no_delete_command():
    args = module.parser().parse_args(
        ["authorize", "--request", "/private/request.json", "--maintenance-token-file", "/private/token"]
    )
    assert not args.apply and not args.allow_operator_exception
    with pytest.raises(SystemExit):
        module.parser().parse_args(["delete", "--request", "/private/request.json"])


def test_token_is_private_from_read_and_does_not_change_atime(tmp_path):
    tmp_path.chmod(0o700)
    path = tmp_path / "token"
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(TOKEN + "\n")
    os.utime(path, ns=(1_000_000_000, path.stat().st_mtime_ns))
    before = path.stat()
    assert module.read_maintenance_token(path) == TOKEN
    assert path.stat() == before
    path.chmod(0o644)
    with pytest.raises(CleanupApprovalError, match="PRIVATE_PAUSE"):
        module.read_maintenance_token(path)
    path.chmod(0o600)
    alias = tmp_path / "alias"
    alias.symlink_to(path)
    with pytest.raises(CleanupApprovalError, match="PRIVATE_PAUSE"):
        module.read_maintenance_token(alias)


async def test_files_never_connects_to_settings_docker_db_or_redis(monkeypatch):
    approval = object()
    monkeypatch.setattr(module, "read_cleanup_request", lambda _: approval)
    check = Mock(return_value=SimpleNamespace(mode="operator-exception"))
    monkeypatch.setattr(module, "validate_cleanup_recovery", check)
    for name in ("get_settings", "create_pool", "create_redis"):
        monkeypatch.setattr(module, name, Mock(side_effect=AssertionError("unexpected connection")))
    monkeypatch.setattr(module.docker, "from_env", Mock(side_effect=AssertionError("unexpected Docker")))
    args = module.parser().parse_args(["files", "--request", "/private/request.json", "--allow-operator-exception"])
    result = await module.run(args)
    assert result == {
        "result": "FILES_VERIFIED_ONLY",
        "expected_count": 1,
        "authorized_count": 0,
        "recovery_mode": "operator-exception",
        "runtime_checked": False,
        "database_clock_checked": False,
    }
    assert check.call_args.kwargs["allow_operator_exception"]


@pytest.fixture
def live_guard(monkeypatch):
    record = {
        "owner": "pixav-phase0",
        "paused": True,
        "redis_run_id": RUNTIME.redis_run_id,
        "owner_sha256": hashlib.sha256(TOKEN.encode()).hexdigest(),
    }
    redis = AsyncMock()
    redis.info.return_value = {"run_id": RUNTIME.redis_run_id}
    redis.get.return_value = json.dumps(record)
    client = Mock()
    client.info.return_value = {"ID": RUNTIME.docker_daemon_id}
    monkeypatch.setattr(module.shutil, "which", lambda _: "/synthetic/docker")
    command = Mock(
        side_effect=[
            SimpleNamespace(stdout=(RUNTIME.docker_context + "\n").encode()),
            SimpleNamespace(stdout=(RUNTIME.docker_daemon_id + "\n").encode()),
        ]
    )
    monkeypatch.setattr(module.subprocess, "run", command)
    guard = module.LiveCleanupGuard(client, redis, pause_key="synthetic:pause", token=TOKEN, expected=RUNTIME)
    return guard, redis, client, command, record


async def test_live_guard_reads_matching_cli_sdk_redis_and_owned_pause_only(live_guard):
    guard, redis, _, command, _ = live_guard
    assert await guard.check() == RUNTIME
    assert [call.args[0][1:] for call in command.call_args_list] == [
        ["context", "show"],
        ["info", "--format", "{{.ID}}"],
    ]
    redis.set.assert_not_awaited()
    redis.delete.assert_not_awaited()
    redis.eval.assert_not_awaited()


@pytest.mark.parametrize("fault", ["redis", "sdk", "owner", "token", "unpaused", "pause-instance"])
async def test_live_guard_rejects_changed_identity_or_unowned_pause(live_guard, fault):
    guard, redis, client, _, record = live_guard
    if fault == "redis":
        redis.info.return_value = {"run_id": "f" * 40}
    elif fault == "sdk":
        client.info.return_value = {"ID": "different-daemon"}
    else:
        key, value = {
            "owner": ("owner", "someone-else"),
            "token": ("owner_sha256", "f" * 64),
            "unpaused": ("paused", False),
            "pause-instance": ("redis_run_id", "f" * 40),
        }[fault]
        redis.get.return_value = json.dumps({**record, key: value})
    with pytest.raises(CleanupApprovalError):
        await guard.check()


async def test_authorize_closes_all_clients_even_when_pool_close_fails(monkeypatch):
    request = SimpleNamespace(
        target=SimpleNamespace(database_name="synthetic_db", database_identifier="123"), runtime=RUNTIME
    )
    settings = SimpleNamespace(
        db_name="synthetic_db", system_pause_key="synthetic:pause", download_dir="/synthetic", library_root="/library"
    )
    settings.model_copy = Mock(return_value=settings)
    pool = SimpleNamespace(close=AsyncMock(side_effect=RuntimeError("synthetic close failure")))
    redis = SimpleNamespace(aclose=AsyncMock())
    client = Mock()
    service = SimpleNamespace(authorize=AsyncMock(return_value={"result": "ELIGIBLE_FOR_AUTHORIZATION"}))
    monkeypatch.setattr(module, "read_cleanup_request", lambda _: request)
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr(module, "read_maintenance_token", lambda _: TOKEN)
    monkeypatch.setattr(module.docker, "from_env", lambda **_: client)
    monkeypatch.setattr(module, "create_redis", AsyncMock(return_value=redis))
    monkeypatch.setattr(module, "create_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(module.LiveCleanupGuard, "check", AsyncMock(return_value=RUNTIME))
    monkeypatch.setattr(module, "CleanupAuthorizer", Mock(return_value=service))
    args = module.parser().parse_args(
        ["authorize", "--request", "/private/request.json", "--maintenance-token-file", "/private/token"]
    )
    with pytest.raises(RuntimeError, match="close failure"):
        await module.run(args)
    assert service.authorize.call_args.kwargs == {"apply": False}
    redis.aclose.assert_awaited_once()
    client.close.assert_called_once()


def test_main_redacts_external_errors(monkeypatch, capsys):
    monkeypatch.setattr(module, "run", AsyncMock(side_effect=RuntimeError("synthetic-private-token")))
    monkeypatch.setattr(module, "parser", lambda: SimpleNamespace(parse_args=lambda: SimpleNamespace()))
    with pytest.raises(SystemExit) as error:
        module.main()
    assert error.value.code == 2
    assert json.loads(capsys.readouterr().out) == {"result": "REFUSED", "code": "CLEANUP_AUTHORIZATION_UNCONFIRMED"}
