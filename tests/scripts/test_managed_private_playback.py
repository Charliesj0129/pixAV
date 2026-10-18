"""Private playback deployment rejects origin bypasses and credential exposure."""

import json
import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from scripts import manage_managed_deployment as module


@pytest.fixture(params=["resolver", "library"])
def deployment(tmp_path, request):
    tmp_path.chmod(0o700)
    data, secrets, auth = [tmp_path / name for name in ["data", "secrets", "playback-auth"]]
    for folder in [data, secrets, auth]:
        folder.mkdir(mode=0o700)
    for relative in [
        "playback-cache",
        "library",
        "jellyfin",
        "jellyfin/config",
        "jellyfin/config/config",
        "jellyfin/cache",
    ]:
        (data / relative).mkdir(mode=0o700)
    placeholder = data / "jellyfin/config/config/logging.json"
    placeholder.write_text("{}")
    placeholder.chmod(0o600)
    for name in ["reader_password", "redis_client_url"]:
        (secrets / name).write_text("synthetic-reference-only")
        (secrets / name).chmod(0o600)
    (auth / "resolver_token").write_text("a" * 64)
    (auth / "resolver_token").chmod(0o600)
    root = Path(__file__).resolve().parents[2]
    policy = tmp_path / "jellyfin-logging.json"
    policy.write_bytes((root / "config/jellyfin/logging.json").read_bytes())
    config = yaml.safe_load((root / "docker-compose.managed.yml").read_text())
    config["services"] = {
        name: service for name, service in config["services"].items() if service.get("profiles") == [request.param]
    }
    config["secrets"] = (
        {key: config["secrets"][key] for key in ["reader_password", "redis_client_url"]}
        if request.param == "resolver"
        else {}
    )
    replacements = {
        "PIXAV_MANAGED_APP_IMAGE": "sha256:" + "a" * 64,
        "PIXAV_MANAGED_GATEWAY_IMAGE": "sha256:" + "b" * 64,
        "PIXAV_MANAGED_DATA_ROOT": str(data),
        "PIXAV_MANAGED_SECRETS_ROOT": str(secrets),
        "PIXAV_MANAGED_PLAYBACK_AUTH_DIR": str(auth),
        "PIXAV_MANAGED_JELLYFIN_LOGGING_POLICY": str(policy),
        "PIXAV_MANAGED_UID": str(os.getuid()),
        "PIXAV_MANAGED_GID": str(os.getgid()),
        "PIXAV_MANAGED_DATABASE": "synthetic_database",
        "PIXAV_EXPECTED_DB_IDENTITY": "123",
        "PIXAV_EXPECTED_REDIS_RUN_ID": "a" * 40,
        "PIXAV_MANAGED_JELLYFIN_PORT": "18096",
    }
    config = json.loads(
        re.sub(
            r"\$\{([^}:]+)(?::([-?])([^}]*))?\}",
            lambda match: replacements.get(match[1], match[3] if match[2] == "-" else "synthetic-binding"),
            json.dumps(config),
        )
    )
    for service in config["services"].values():
        if isinstance(service["networks"], list):
            service["networks"] = {name: None for name in service["networks"]}
        service["mem_limit"] = service["memswap_limit"] = {
            "768m": "805306368",
            "128m": "134217728",
            "1g": "1073741824",
        }[service["mem_limit"]]
        service["secrets"] = [
            row if isinstance(row, dict) else {"source": row, "target": "/run/secrets/" + row}
            for row in service.get("secrets", [])
        ]
    args = SimpleNamespace(
        profile=request.param,
        data_root=data,
        secrets_root=secrets,
        playback_auth_dir=auth,
        jellyfin_logging_policy=policy,
        jellyfin_port=18096,
        db_identity="123",
        database="synthetic_database",
        redis_run_id="a" * 40,
    )
    return config, args


def validate(config, args):
    if args.profile == "library":
        module.validate_library(config, args)
    else:
        module.validate_application(config, args)


def test_private_profiles_preserve_reader_and_network_boundaries(deployment):
    validate(*deployment)


@pytest.mark.parametrize("deployment", ["resolver"], indirect=True)
def test_resolver_refuses_file_in_place_of_cache_directory(deployment):
    config, args = deployment
    cache = args.data_root / "playback-cache"
    cache.rmdir()
    cache.write_text("synthetic-wrong-mount-type")
    with pytest.raises(ValueError):
        validate(config, args)


@pytest.mark.parametrize("deployment", ["resolver"], indirect=True)
@pytest.mark.parametrize(
    "defect", ["public-auth", "authority-role", "redis-drift", "staging", "writable-auth", "access-log", "raw-proxy"]
)
def test_resolver_profile_refuses_privilege_or_privacy_drift(deployment, defect):
    config, args = deployment
    service = config["services"]["strm_resolver"]
    if defect == "public-auth":
        service["environment"]["PIXAV_RESOLVER_AUTH_REQUIRED"] = "false"
    elif defect == "authority-role":
        service["environment"]["PIXAV_DB_USER"] = "pixav_authority"
    elif defect == "redis-drift":
        service["environment"]["PIXAV_EXPECTED_REDIS_RUN_ID"] = "b" * 40
    elif defect == "staging":
        service["volumes"].append({"source": str(args.data_root), "target": "/parts", "type": "bind"})
    elif defect == "writable-auth":
        service["volumes"][1]["read_only"] = False
    elif defect == "access-log":
        service["command"].remove("--no-access-log")
    elif defect == "raw-proxy":
        service["environment"]["HTTPS_PROXY"] = "http://untrusted.invalid"
    with pytest.raises(ValueError):
        validate(config, args)


@pytest.mark.parametrize("deployment", ["library"], indirect=True)
@pytest.mark.parametrize(
    "defect",
    [
        "origin-port",
        "public-port",
        "origin-client-network",
        "injector-client-network",
        "writable-library",
        "info-logs",
        "socket",
        "mutable-gateway",
    ],
)
def test_library_profile_refuses_authentication_bypasses(deployment, defect):
    config, args = deployment
    origin = config["services"]["jellyfin"]
    client = config["services"]["jellyfin-client-gateway"]
    injector = config["services"]["playback-gateway"]
    if defect == "origin-port":
        origin["ports"] = [{"target": 8096, "published": "8096", "host_ip": "127.0.0.1"}]
    elif defect == "public-port":
        client["ports"][0]["host_ip"] = "0.0.0.0"  # noqa: S104 -- explicit refusal fixture
    elif defect == "origin-client-network":
        origin["networks"]["client_capture"] = None
    elif defect == "injector-client-network":
        injector["networks"]["client_capture"] = None
    elif defect == "writable-library":
        origin["volumes"][2]["read_only"] = False
    elif defect == "info-logs":
        policy = json.loads(args.jellyfin_logging_policy.read_text())
        policy["Serilog"]["MinimumLevel"]["Default"] = "Information"
        args.jellyfin_logging_policy.write_text(json.dumps(policy))
    elif defect == "socket":
        injector["volumes"].append({"source": "/var/run/docker.sock", "target": "/var/run/docker.sock", "type": "bind"})
    elif defect == "mutable-gateway":
        injector["image"] = "nginx:stable-alpine"
    with pytest.raises(ValueError):
        validate(config, args)


@pytest.mark.parametrize("defect", ["token-permissions", "token-symlink", "extra-secret"])
def test_only_the_private_service_token_directory_is_exposed(deployment, defect):
    config, args = deployment
    token = args.playback_auth_dir / "resolver_token"
    if defect == "token-permissions":
        token.chmod(0o644)
    elif defect == "token-symlink":
        other = args.playback_auth_dir.parent / "other-token"
        other.write_text("a" * 64)
        token.unlink()
        token.symlink_to(other)
    elif defect == "extra-secret":
        (args.playback_auth_dir / "unrelated-credential").write_text("synthetic-reference")
    with pytest.raises(ValueError):
        validate(config, args)
