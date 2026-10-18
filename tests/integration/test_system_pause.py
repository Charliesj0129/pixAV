"""Instance-bound global pause ownership against a dedicated real Redis."""

import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

from pixav.config import Settings
from scripts import manage_system_pause as module

pytestmark = pytest.mark.integration


def bind_settings(monkeypatch, key):
    settings = Settings(
        redis_url=os.environ["PIXAV_E2E_REDIS_URL"],
        expected_redis_run_id=os.environ["PIXAV_TEST_REDIS_IDENTITY"],
        system_pause_key=key,
    )
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    return settings


async def test_private_pause_roundtrip_and_foreign_resume_refusal(integration_redis, monkeypatch, tmp_path, capsys):
    redis, key = integration_redis
    settings = bind_settings(monkeypatch, key)
    token = module._read_token(tmp_path / "pause-token", create=True)
    assert await module._run("pause", reason="synthetic maintenance", token=token) == 0
    original = await redis.get(key)
    record = json.loads(original)
    assert token.encode() not in original
    assert record["owner_sha256"] == hashlib.sha256(token.encode()).hexdigest()
    assert record["redis_run_id"] == settings.expected_redis_run_id
    assert token not in capsys.readouterr().out
    assert await module._run("status", reason="", token=token) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["owned"] and status["paused"] and status["token_present"]
    assert await module._run("resume", reason="", token="synthetic-foreign-owner-00000000000000") == 2
    assert await redis.get(key) == original
    assert await module._run("resume", reason="", token=token) == 0
    assert await redis.get(key) is None
    assert token not in capsys.readouterr().out


async def test_legacy_record_can_be_released_without_disclosing_token(integration_redis, monkeypatch, capsys):
    redis, key = integration_redis
    settings = bind_settings(monkeypatch, key)
    token = "synthetic-legacy-owner-000000000000000"
    await redis.set(
        key,
        json.dumps(
            {"owner": "pixav-phase0", "paused": True, "token": token, "redis_run_id": settings.expected_redis_run_id}
        ),
    )
    assert await module._run("status", reason="", token="") == 0
    assert await module._run("resume", reason="", token=token) == 0
    assert await redis.get(key) is None
    assert token not in capsys.readouterr().out


async def test_cli_uses_private_token_file_through_pause_and_resume(integration_redis, monkeypatch, tmp_path):
    redis, key = integration_redis
    settings = bind_settings(monkeypatch, key)
    root = Path(module.__file__).resolve().parents[1]
    path = tmp_path / "cli-owner-token"
    env = {
        **os.environ,
        "PIXAV_REDIS_URL": settings.redis_url,
        "PIXAV_EXPECTED_REDIS_RUN_ID": settings.expected_redis_run_id,
        "PIXAV_SYSTEM_PAUSE_KEY": key,
        "PIXAV_SETTINGS_SECRETS_DIR": "",
        "PYTHONPATH": os.pathsep.join([str(root / "src"), str(root)]),
    }
    outputs = []
    for command in ["pause", "status", "resume"]:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "scripts.manage_system_pause",
            command,
            "--token-file",
            str(path),
            cwd=tmp_path,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=20)
        assert process.returncode == 0, "ISOLATED_PAUSE_CLI_UNCONFIRMED"
        outputs.extend([stdout, stderr])
        result = json.loads(stdout)
        if command == "status":
            assert result["owned"] and result["paused"]
    token = path.read_bytes().strip()
    assert all(token not in output for output in outputs), "CLI_DISCLOSED_PRIVATE_TOKEN"
    assert path.stat().st_mode & 0o777 == 0o600
    assert await redis.get(key) is None


async def test_lua_refuses_wrong_instance_before_set_or_delete(integration_redis, monkeypatch):
    redis, key = integration_redis
    settings = bind_settings(monkeypatch, key)
    wrong_instance = "0" * 40
    assert wrong_instance != settings.expected_redis_run_id
    raw = '{"paused":true,"owner":"synthetic"}'
    assert await redis.eval(module._PAUSE, 1, key, raw, wrong_instance) == -1
    assert await redis.get(key) is None
    await redis.set(key, raw)
    assert await redis.eval(module._RESUME, 1, key, raw, wrong_instance) == -1
    assert await redis.get(key) == raw.encode()


@pytest.mark.parametrize("command", ["pause", "resume"])
async def test_real_redis_preserves_replacement_between_read_and_write(integration_redis, monkeypatch, command):
    redis, key = integration_redis
    settings = bind_settings(monkeypatch, key)
    token = "synthetic-selected-owner-0000000000000"
    if command == "resume":
        await redis.set(
            key,
            json.dumps(
                {
                    "owner": "pixav-phase0",
                    "paused": True,
                    "token": token,
                    "redis_run_id": settings.expected_redis_run_id,
                }
            ),
        )
    replacement = '{"paused":true,"owner":"another-operator"}'
    factory = module.create_redis

    async def create_client(settings):
        client = await factory(settings)
        read = client.get

        async def replace_after_read(selected_key):
            selected = await read(selected_key)
            await redis.set(selected_key, replacement)
            return selected

        monkeypatch.setattr(client, "get", replace_after_read)
        return client

    monkeypatch.setattr(module, "create_redis", create_client)
    assert await module._run(command, reason="", token=token) == 2
    assert await redis.get(key) == replacement.encode()
