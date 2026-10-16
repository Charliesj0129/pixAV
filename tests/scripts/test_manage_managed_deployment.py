"""Managed tools preflight rejects unsafe deployment changes before startup."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from scripts import manage_managed_deployment as module


@pytest.fixture
def config(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    return {
        "name": "pixav-managed-synthetic",
        "services": {
            "windows_tools": {
                "profiles": ["windows-tools"],
                "image": "sha256:" + "a" * 64,
                "pull_policy": "never",
                "command": ["sleep", "infinity"],
                "restart": "no",
                "init": True,
                "read_only": True,
                "cap_drop": ["ALL"],
                "security_opt": ["no-new-privileges:true"],
                "cpus": 1,
                "mem_limit": "2147483648",
                "memswap_limit": "2147483648",
                "pids_limit": 256,
                "environment": {
                    "ADB_SERVER_SOCKET": "tcp:host.docker.internal:5037",
                    "PIXAV_ANDROID_ADB_SERVER_HOST": "host.docker.internal",
                    "ANDROID_SERIAL": "emulator-5554",
                    "MAESTRO_CLI_NO_ANALYTICS": "1",
                },
                "volumes": [
                    {
                        "type": "bind",
                        "source": str(staging),
                        "target": "/parts",
                        "read_only": True,
                        "bind": {"create_host_path": False},
                    }
                ],
                "tmpfs": ["/tmp:rw,size=512m", "/root:rw,size=512m", "/work:rw,size=512m"],
                "healthcheck": {"test": ["CMD", "true"]},
                "labels": {
                    "pixav.photos_canary.owner": "11111111-1111-4111-8111-111111111111",
                    "pixav.runtime.id": "22222222-2222-4222-8222-222222222222",
                    "pixav.runtime.generation": "3",
                    "pixav.photos_canary.role": "tools",
                },
            }
        },
    }


def test_resolved_compose_memory_strings_are_supported(config):
    module.validate_tools(config)


@pytest.mark.parametrize(
    "defect",
    [
        "mutable-image",
        "writable",
        "socket",
        "credential",
        "host-network",
        "privileged",
        "memory",
        "restart",
        "executor",
    ],
)
def test_tools_preflight_rejects_unsafe_configuration(config, defect):
    data = deepcopy(config)
    tool = data["services"]["windows_tools"]
    if defect == "mutable-image":
        tool["image"] = "tools:latest"
    elif defect == "writable":
        tool["volumes"][0]["read_only"] = False
    elif defect == "socket":
        tool["volumes"].append({"type": "bind", "source": "/var/run/docker.sock", "target": "/var/run/docker.sock"})
    elif defect == "credential":
        tool["environment"]["DB_PASSWORD"] = "synthetic-private-value"
    elif defect == "host-network":
        tool["network_mode"] = "host"
    elif defect == "privileged":
        tool["privileged"] = True
    elif defect == "memory":
        tool["mem_limit"] = "4294967296"
    elif defect == "restart":
        tool["restart"] = "always"
    elif defect == "executor":
        data["services"]["old_executor"] = {}
    with pytest.raises(ValueError):
        module.validate_tools(data)


def test_both_development_and_managed_foreign_runners_are_checked(config, tmp_path, monkeypatch):
    import json

    env = tmp_path / "managed.env"
    env.write_text("")
    env.chmod(0o600)
    tmp_path.chmod(0o700)
    args = SimpleNamespace(
        project=config["name"],
        context="default",
        daemon_id="synthetic-daemon",
        env_file=env,
        compose_file=tmp_path / "compose.yml",
    )
    calls = []

    def command(argv):
        calls.append(argv)
        if "info" in argv:
            return json.dumps("synthetic-daemon")
        if "config" in argv:
            return json.dumps(config)
        if "image" in argv:
            return "sha256:" + "a" * 64
        if "ps" in argv:
            return "c" * 64 if argv[-1] == "label=pixav.runtime.role=android-dev-tools" else ""
        if "inspect" in argv:
            return json.dumps({"com.docker.compose.project": "pixav-old-development"})
        pytest.fail("unexpected mutating command")

    monkeypatch.setattr(module, "run", command)
    with pytest.raises(ValueError, match="PREVIOUS_ANDROID_TOOLS_STILL_RUNNING"):
        module.preflight(args)
    assert sum("ps" in argv for argv in calls) == 2


@pytest.fixture
def core_config(tmp_path):
    import json
    import os
    import re
    from pathlib import Path

    import yaml

    data = tmp_path / "data"
    secrets = tmp_path / "secrets"
    data.mkdir(mode=0o700)
    secrets.mkdir(mode=0o700)
    for name in ["postgres", "redis"]:
        (data / name).mkdir()
    for name in ["postgres_password", "redis_config", "redis_password"]:
        (secrets / name).write_text("synthetic-private-value")
        (secrets / name).chmod(0o600)
    manifest = yaml.safe_load((Path(__file__).resolve().parents[2] / "docker-compose.managed.yml").read_text())
    manifest["services"].pop("windows_tools")
    replacements = {
        "PIXAV_MANAGED_DATA_ROOT": str(data),
        "PIXAV_MANAGED_SECRETS_ROOT": str(secrets),
        "PIXAV_MANAGED_UID": str(os.getuid()),
        "PIXAV_MANAGED_GID": str(os.getgid()),
    }
    encoded = re.sub(
        r"\$\{([^}:]+):-([^}]*)\}",
        lambda match: replacements.get(match[1], match[2]),
        json.dumps(manifest),
    )
    manifest = json.loads(encoded)
    for service in manifest["services"].values():
        service["networks"] = {"core": None}
        service["mem_limit"] = {"3g": "3221225472", "256m": "268435456"}[service["mem_limit"]]
        service["memswap_limit"] = service["mem_limit"]
        service["secrets"] = [{"source": name, "target": "/run/secrets/" + name} for name in service["secrets"]]
    return manifest, data, secrets


def test_core_manifest_keeps_credentials_in_files_and_selects_only_infrastructure(core_config):
    module.validate_core(*core_config)
    config, _, _ = core_config
    assert set(config["services"]) == {"postgres", "redis"}
    assert config["services"]["postgres"]["environment"]["PGDATA"].endswith("/pgdata")


@pytest.mark.parametrize(
    "defect",
    [
        "host-port",
        "public-network",
        "mutable-image",
        "inline-password",
        "wrong-bind",
        "root-redis",
        "executor-command",
        "secret-mode",
    ],
)
def test_core_preflight_rejects_unsafe_deployments(core_config, defect):
    config, data, secrets = core_config
    pg = config["services"]["postgres"]
    if defect == "host-port":
        pg["ports"] = [{"target": 5432, "published": "5432", "host_ip": "0.0.0.0"}]  # noqa: S104 -- refusal fixture
    elif defect == "public-network":
        config["networks"]["core"]["internal"] = False
    elif defect == "mutable-image":
        pg["image"] = "postgres:latest"
    elif defect == "inline-password":
        pg["environment"]["POSTGRES_PASSWORD"] = "synthetic-private-value"
    elif defect == "wrong-bind":
        pg["volumes"][0]["source"] = str(data.parent / "original-data")
    elif defect == "root-redis":
        config["services"]["redis"]["user"] = "0:0"
    elif defect == "executor-command":
        pg["command"] = ["python", "-m", "pixav.maxwell_core.worker"]
    elif defect == "secret-mode":
        (secrets / "postgres_password").chmod(0o644)
    with pytest.raises(ValueError):
        module.validate_core(config, data, secrets)
