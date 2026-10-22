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
    manifest["services"] = {
        name: service for name, service in manifest["services"].items() if service.get("profiles") == ["core"]
    }
    manifest["secrets"] = {
        name: manifest["secrets"][name] for name in ["postgres_password", "redis_config", "redis_password"]
    }
    # Security fixtures represent an already resolved Compose model. The
    # actual nested default/override contract is exercised with Compose below.
    manifest["services"]["postgres"]["volumes"][0]["source"] = str(data / "postgres")
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


@pytest.fixture
def restored_postgres(tmp_path):
    root = tmp_path / "restored-postgres"
    (root / "pgdata/global").mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    (root / "pgdata").chmod(0o700)
    (root / "pgdata/PG_VERSION").write_text("16\n")
    (root / "pgdata/global/pg_control").write_bytes(b"synthetic control marker; not a valid live cluster")
    return root


def test_core_accepts_only_the_explicit_prepared_postgres_bind_without_changing_redis(core_config, restored_postgres):
    config, data, secrets = core_config
    pg = config["services"]["postgres"]
    pg["volumes"][0]["source"] = str(restored_postgres)
    paths = list(restored_postgres.rglob("*")) + [restored_postgres, data / "redis"]

    def metadata():
        return {
            str(path): (tuple(path.stat()), path.stat().st_atime_ns, path.stat().st_mtime_ns, path.stat().st_ctime_ns)
            for path in paths
        }

    before = metadata()
    module.validate_core(config, data, secrets, postgres_host_root=restored_postgres)
    assert metadata() == before
    assert config["services"]["redis"]["volumes"][0]["source"] == str(data / "redis")
    with pytest.raises(ValueError, match="CORE_DATA_BIND_INVALID"):
        module.validate_core(config, data, secrets)


@pytest.mark.parametrize(
    "defect",
    [
        "empty",
        "major",
        "oversized-version",
        "symlink-root",
        "symlink-version",
        "symlink-control",
        "public-root",
        "redis-overlap",
        "secret-overlap",
        "missing-control",
        "wrong-user",
    ],
)
def test_separate_postgres_root_refuses_unprepared_or_unsafe_locations(
    core_config, restored_postgres, tmp_path, defect
):
    import os
    import shutil

    config, data, secrets = core_config
    root = restored_postgres
    if defect == "empty":
        root = tmp_path / "empty"
        root.mkdir(mode=0o700)
    elif defect == "major":
        (root / "pgdata/PG_VERSION").write_text("17\n")
    elif defect == "oversized-version":
        (root / "pgdata/PG_VERSION").write_bytes(b"16" + b" " * 64)
    elif defect == "symlink-root":
        root = tmp_path / "alias"
        root.symlink_to(restored_postgres, target_is_directory=True)
    elif defect in {"symlink-version", "symlink-control"}:
        path = root / ("pgdata/PG_VERSION" if defect == "symlink-version" else "pgdata/global/pg_control")
        retained = path.with_name(path.name + ".retained")
        path.rename(retained)
        path.symlink_to(retained)
    elif defect == "public-root":
        root.chmod(0o755)
    elif defect in {"redis-overlap", "secret-overlap"}:
        root = data / "redis" if defect == "redis-overlap" else secrets
        root.chmod(0o700)
        shutil.copytree(restored_postgres / "pgdata", root / "pgdata")
    elif defect == "missing-control":
        (root / "pgdata/global/pg_control").unlink()
    elif defect == "wrong-user":
        config["services"]["postgres"]["user"] = f"{os.getuid()+1}:{os.getgid()}"
    config["services"]["postgres"]["volumes"][0]["source"] = str(root)
    with pytest.raises(ValueError):
        module.validate_core(config, data, secrets, postgres_host_root=root)


def test_prepared_postgres_marker_replacement_is_preserved_and_refused(core_config, restored_postgres, monkeypatch):
    import os

    config, data, secrets = core_config
    config["services"]["postgres"]["volumes"][0]["source"] = str(restored_postgres)
    version = restored_postgres / "pgdata/PG_VERSION"
    retained = version.with_suffix(".retained")
    read = os.read

    def replace(descriptor, count):
        version.rename(retained)
        version.write_text("16\n")
        return read(descriptor, count)

    monkeypatch.setattr(module.os, "read", replace)
    with pytest.raises(ValueError, match="MARKER_CHANGED"):
        module.validate_core(config, data, secrets, postgres_host_root=restored_postgres)
    assert retained.is_file() and version.is_file()


def test_preflight_forwards_the_approved_postgres_root_without_starting_services(
    core_config, restored_postgres, tmp_path, monkeypatch
):
    import json

    config, data, secrets = core_config
    config["name"] = "pixav-managed-synthetic"
    config["services"]["postgres"]["volumes"][0]["source"] = str(restored_postgres)
    private = tmp_path / "managed.env"
    private.write_text("")
    private.chmod(0o600)
    tmp_path.chmod(0o700)
    args = SimpleNamespace(
        project=config["name"],
        context="default",
        daemon_id="synthetic-daemon",
        env_file=private,
        compose_file=tmp_path / "compose.yml",
        profile="core",
        data_root=data,
        secrets_root=secrets,
        postgres_host_root=restored_postgres,
    )
    calls = []

    def command(argv):
        calls.append(argv)
        if "info" in argv:
            return json.dumps(args.daemon_id)
        if "config" in argv:
            return json.dumps(config)
        if "image" in argv:
            return "sha256:" + "a" * 64
        if "ps" in argv:
            return ""
        pytest.fail("preflight attempted a mutation")

    monkeypatch.setattr(module, "run", command)
    result = module.preflight(args)
    assert result["postgres_root_explicit"] and result["containers_started"] == 0 and not result["database_touched"]
    assert all("up" not in argv and "start" not in argv and "exec" not in argv for argv in calls)


def test_postgres_root_option_cannot_be_ignored_by_a_noncore_profile(restored_postgres, monkeypatch):
    monkeypatch.setattr(module, "run", lambda argv: pytest.fail("invalid profile reached Docker"))
    args = SimpleNamespace(project="pixav-managed-synthetic", profile="authority", postgres_host_root=restored_postgres)
    with pytest.raises(ValueError, match="REQUIRES_CORE_PROFILE"):
        module.preflight(args)


@pytest.mark.parametrize("setting", ["unset", "empty", "separate"])
def test_real_compose_resolves_postgres_root_separately_from_other_data(tmp_path, restored_postgres, setting):
    import json
    import os
    import shutil
    import subprocess
    from pathlib import Path

    binary = shutil.which("docker")
    if binary is None:
        pytest.skip("Docker Compose CLI is needed for the actual interpolation contract")
    repository = Path(__file__).resolve().parents[2]
    data = tmp_path / "data"
    values = {
        "PIXAV_MANAGED_PROJECT": "pixav-managed-synthetic",
        "PIXAV_MANAGED_DATA_ROOT": str(data),
        "PIXAV_MANAGED_SECRETS_ROOT": str(tmp_path / "secrets"),
        "PIXAV_MANAGED_STAGING_HOST_ROOT": str(tmp_path / "staging"),
        "PIXAV_MANAGED_WINDOWS_TOOLS_IMAGE": "sha256:" + "a" * 64,
        "PIXAV_ANDROID_SERIAL": "emulator-5554",
        "PIXAV_STORAGE_WORKER_OWNER": "11111111-1111-4111-8111-111111111111",
        "PIXAV_STORAGE_WINDOWS_RUNTIME_ID": "22222222-2222-4222-8222-222222222222",
        "PIXAV_STORAGE_WINDOWS_RUNTIME_GENERATION": "7",
    }
    if setting != "unset":
        values["PIXAV_MANAGED_POSTGRES_HOST_ROOT"] = str(restored_postgres) if setting == "separate" else ""
    private = tmp_path / "synthetic.env"
    private.write_text("".join(key + "=" + value + "\n" for key, value in values.items()))
    env = {key: value for key, value in os.environ.items() if not key.startswith("PIXAV_")}
    result = subprocess.run(  # noqa: S603 - config-only CLI, fixed recipe and synthetic paths; no daemon/data mutation
        [
            binary,
            "compose",
            "--env-file",
            str(private),
            "-f",
            str(repository / "docker-compose.managed.yml"),
            "--profile",
            "core",
            "config",
            "--format",
            "json",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        check=True,
        timeout=30,
    )
    config = json.loads(result.stdout)
    assert set(config["services"]) == {"postgres", "redis"}
    expected = restored_postgres if setting == "separate" else data / "postgres"
    assert config["services"]["postgres"]["volumes"][0]["source"] == str(expected)
    assert config["services"]["redis"]["volumes"][0]["source"] == str(data / "redis")


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


@pytest.fixture
def authority_config(core_config, monkeypatch):
    import json
    import os
    import re
    from pathlib import Path

    import yaml

    _, data, secrets = core_config
    for name in ["downloads", "remuxed", "library", "artwork", "playback-cache"]:
        (data / name).mkdir()
    names = [
        "authority_password",
        "redis_client_url",
        "windows_ca",
        "windows_certificate",
        "windows_key",
        "download_pause_token",
    ]
    for name in names:
        (secrets / name).write_text("synthetic-private-value")
        (secrets / name).chmod(0o600)
    manifest = yaml.safe_load((Path(__file__).resolve().parents[2] / "docker-compose.managed.yml").read_text())
    manifest["services"] = {"maxwell_core": manifest["services"]["maxwell_core"]}
    manifest["secrets"] = {name: manifest["secrets"][name] for name in names}
    replacements = {
        "PIXAV_MANAGED_APP_IMAGE": "sha256:" + "a" * 64,
        "PIXAV_MANAGED_COLD_IMAGE": "sha256:" + "b" * 64,
        "PIXAV_MANAGED_DATA_ROOT": str(data),
        "PIXAV_MANAGED_SECRETS_ROOT": str(secrets),
        "PIXAV_MANAGED_UID": str(os.getuid()),
        "PIXAV_MANAGED_GID": str(os.getgid()),
        "PIXAV_MANAGED_DATABASE": "synthetic_database",
        "PIXAV_EXPECTED_DB_IDENTITY": "123",
        "PIXAV_EXPECTED_REDIS_RUN_ID": "a" * 40,
        "PIXAV_WINDOWS_CONTROL_DOCKER_DAEMON_ID": "synthetic-daemon",
        "PIXAV_MANAGED_DOCKER_GID": "1337",
        "PIXAV_WINDOWS_CONTROL_IDENTITY_SHA256": "c" * 64,
        "PIXAV_WINDOWS_CAPACITY_VOLUME_DEVICE": "123",
    }
    encoded = re.sub(
        r"\$\{([^}:]+)(?::([-?])([^}]*))?\}",
        lambda m: replacements.get(m[1], m[3] if m[2] == "-" else "synthetic-binding"),
        json.dumps(manifest),
    )
    manifest = json.loads(encoded)
    service = manifest["services"]["maxwell_core"]
    service["networks"] = {name: None for name in service["networks"]}
    service["mem_limit"] = service["memswap_limit"] = "805306368"
    service["secrets"] = [
        row if isinstance(row, dict) else {"source": row, "target": "/run/secrets/" + row} for row in service["secrets"]
    ]
    real_stat = Path.stat

    def file_stat(path, *args, **kwargs):
        if str(path) == "/var/run/docker.sock":
            return SimpleNamespace(st_gid=1337)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", file_stat)
    args = SimpleNamespace(
        profile="authority",
        db_identity="123",
        database="synthetic_database",
        redis_run_id="a" * 40,
        daemon_id="synthetic-daemon",
        data_root=data,
        secrets_root=secrets,
    )
    return manifest, args


def test_authority_profile_binds_separate_role_secrets_and_exact_instances(authority_config):
    module.validate_application(*authority_config)
    assert (
        authority_config[0]["services"]["maxwell_core"]["environment"]["PIXAV_RESOLVER_BASE_URL"]
        == "http://playback-gateway:8080"
    )


@pytest.mark.parametrize("defect", ["admin-secret", "legacy-mode", "wrong-run-id", "raw-password", "database-mount"])
def test_authority_profile_refuses_privilege_and_identity_drift(authority_config, defect):
    config, args = authority_config
    service = config["services"]["maxwell_core"]
    if defect == "admin-secret":
        service["secrets"][0]["source"] = "postgres_password"
    elif defect == "legacy-mode":
        service["environment"]["PIXAV_MANAGED_MEDIA_WORKFLOW"] = "false"
    elif defect == "wrong-run-id":
        service["environment"]["PIXAV_EXPECTED_REDIS_RUN_ID"] = "b" * 40
    elif defect == "raw-password":
        service["environment"]["PIXAV_DB_PASSWORD"] = "synthetic-private-value"
    else:
        service["volumes"][1]["target"] = "/app/data/postgres"
    with pytest.raises(ValueError):
        module.validate_application(config, args)


@pytest.fixture(params=["storage", "playback"])
def activity_config(authority_config, request):
    import json
    import re
    from pathlib import Path

    import yaml

    authority, args = authority_config
    args.profile = request.param
    args.project_root = args.data_root.parent
    args.staging_root = args.project_root / "staging"
    args.staging_root.mkdir()
    (args.data_root / "storage-readback").mkdir()
    cookies = args.project_root / "photos-auth"
    cookies.mkdir(mode=0o700)
    args.photos_auth_secret = cookies / "cookies.json"
    args.photos_auth_secret.write_text("synthetic-cookie-only-reference")
    args.photos_auth_secret.chmod(0o600)
    (args.secrets_root / "worker_password").write_text("synthetic-worker-password")
    (args.secrets_root / "worker_password").chmod(0o600)
    name = args.profile + "_worker"
    manifest = yaml.safe_load((Path(__file__).resolve().parents[2] / "docker-compose.managed.yml").read_text())
    manifest["services"] = {name: manifest["services"][name]}
    names = [row["source"] if isinstance(row, dict) else row for row in manifest["services"][name]["secrets"]]
    manifest["secrets"] = {key: manifest["secrets"][key] for key in names}
    uid, gid = authority["services"]["maxwell_core"]["user"].split(":")
    replacements = {
        "PIXAV_MANAGED_APP_IMAGE": "sha256:" + "a" * 64,
        "PIXAV_MANAGED_COLD_IMAGE": "sha256:" + "b" * 64,
        "PIXAV_MANAGED_DATA_ROOT": str(args.data_root),
        "PIXAV_MANAGED_PROJECT_ROOT": str(args.project_root),
        "PIXAV_MANAGED_STAGING_HOST_ROOT": str(args.staging_root),
        "PIXAV_MANAGED_SECRETS_ROOT": str(args.secrets_root),
        "PIXAV_MANAGED_PHOTOS_AUTH_SECRET": str(args.photos_auth_secret),
        "PIXAV_MANAGED_UID": uid,
        "PIXAV_MANAGED_GID": gid,
        "PIXAV_MANAGED_DATABASE": args.database,
        "PIXAV_EXPECTED_DB_IDENTITY": args.db_identity,
        "PIXAV_EXPECTED_REDIS_RUN_ID": args.redis_run_id,
        "PIXAV_WINDOWS_CONTROL_DOCKER_DAEMON_ID": args.daemon_id,
        "PIXAV_MANAGED_DOCKER_GID": "1337",
        "PIXAV_WINDOWS_CONTROL_IDENTITY_SHA256": "c" * 64,
        "PIXAV_WINDOWS_CAPACITY_VOLUME_DEVICE": "123",
        "PIXAV_STORAGE_WORKER_OWNER": "11111111-1111-4111-8111-111111111111",
        "PIXAV_STORAGE_WINDOWS_RUNTIME_ID": "22222222-2222-4222-8222-222222222222",
        "PIXAV_STORAGE_WINDOWS_RUNTIME_GENERATION": "7",
    }
    manifest = json.loads(
        re.sub(
            r"\$\{([^}:]+)(?::([-?])([^}]*))?\}",
            lambda m: replacements.get(m[1], m[3] if m[2] == "-" else "synthetic-binding"),
            json.dumps(manifest),
        )
    )
    service = manifest["services"][name]
    service["networks"] = {key: None for key in service["networks"]}
    service["mem_limit"] = service["memswap_limit"] = "805306368"
    service["secrets"] = [
        row if isinstance(row, dict) else {"source": row, "target": "/run/secrets/" + row} for row in service["secrets"]
    ]
    return manifest, args


def test_activity_profile_binds_only_worker_credentials_and_exact_data_roots(activity_config):
    module.validate_application(*activity_config)


@pytest.mark.parametrize(
    "defect",
    [
        "authority-role",
        "staging-alias",
        "cookie-mode",
        "cookie-parent",
        "cold-image",
        "readback-writable",
        "command",
        "socket-group",
    ],
)
def test_activity_profiles_refuse_privilege_and_isolation_drift(activity_config, defect):
    config, args = activity_config
    service = config["services"][args.profile + "_worker"]
    if defect == "authority-role":
        service["environment"]["PIXAV_DB_USER"] = "pixav_authority"
    elif defect == "staging-alias":
        service["environment"]["PIXAV_STORAGE_STAGING_DIR"] = "/parts"
    elif defect == "cookie-mode":
        args.photos_auth_secret.chmod(0o644)
    elif defect == "cookie-parent":
        args.photos_auth_secret.parent.chmod(0o755)
    elif defect == "cold-image":
        service["environment"]["PIXAV_STORAGE_READBACK_IMAGE"] = "cold:latest"
    elif defect == "readback-writable":
        service["volumes"][1]["read_only"] = False
    elif defect == "command":
        service["command"] = ["--skip-fence"]
    elif defect == "socket-group":
        service["group_add"] = ["0"]
    with pytest.raises(ValueError):
        module.validate_application(config, args)


@pytest.fixture
def vpn_config(tmp_path):
    import json
    import re
    from pathlib import Path

    import yaml

    tmp_path.chmod(0o700)
    wireguard = tmp_path / "wg0.conf"
    auth = tmp_path / "vpn_control_auth"
    resolv = tmp_path / "resolv.conf"
    resolv.write_text("nameserver 127.0.0.1\n")
    resolv.chmod(0o600)
    for path in [wireguard, auth]:
        path.write_text("synthetic-secret-reference-only")
        path.chmod(0o600)
    auth.write_text(
        '[[roles]]\nname = "pixav-runtime"\nroutes = ["GET /v1/vpn/status", "PUT /v1/vpn/status"]\n'
        'auth = "apikey"\napikey = "' + "a" * 32 + '"\n'
    )
    manifest = yaml.safe_load((Path(__file__).resolve().parents[2] / "docker-compose.managed.yml").read_text())
    manifest["services"] = {"gluetun": manifest["services"]["gluetun"]}
    manifest["secrets"] = {key: manifest["secrets"][key] for key in ["vpn_wireguard", "vpn_control_auth"]}
    replacements = {
        "PIXAV_MANAGED_WIREGUARD_CONFIG": str(wireguard),
        "PIXAV_MANAGED_SECRETS_ROOT": str(tmp_path),
        "PIXAV_MANAGED_VPN_RESOLV_CONF": str(resolv),
    }
    manifest = json.loads(
        re.sub(
            r"\$\{([^}:]+)(?::([-?])([^}]*))?\}",
            lambda m: replacements.get(m[1], m[3] if m[2] == "-" else "synthetic-binding"),
            json.dumps(manifest),
        )
    )
    service = manifest["services"]["gluetun"]
    service["networks"] = {name: None for name in service["networks"]}
    service["devices"] = [{"source": "/dev/net/tun", "target": "/dev/net/tun", "permissions": "rw"}]
    service["mem_limit"] = service["memswap_limit"] = "536870912"
    service["secrets"] = [
        row if isinstance(row, dict) else {"source": row, "target": "/run/secrets/" + row} for row in service["secrets"]
    ]
    return manifest, wireguard, tmp_path, resolv


def test_vpn_profile_selects_only_gluetun_and_private_file_references(vpn_config):
    module.validate_vpn(*vpn_config)


@pytest.mark.parametrize(
    "defect",
    [
        "firewall-off",
        "lan-bypass",
        "inline-key",
        "control-port",
        "host-network",
        "privileged",
        "docker-socket",
        "core-network",
        "secret-mode",
        "wrong-config",
        "mutable-image",
        "unauthenticated-control",
        "credential-route",
        "dns-bypass",
        "dns-rewrite",
        "writable-resolver",
    ],
)
def test_vpn_profile_refuses_kill_switch_and_secret_bypasses(  # noqa: C901 -- explicit security cases
    vpn_config, defect
):
    config, wireguard, secrets, resolv = vpn_config
    service = config["services"]["gluetun"]
    if defect == "firewall-off":
        service["environment"]["FIREWALL_ENABLED_DISABLING_IT_SHOOTS_YOU_IN_YOUR_FOOT"] = "off"
    elif defect == "lan-bypass":
        service["environment"]["FIREWALL_OUTBOUND_SUBNETS"] = "192.168.0.0/16"
    elif defect == "inline-key":
        service["environment"]["WIREGUARD_PRIVATE_KEY"] = "synthetic-value"
    elif defect == "control-port":
        service["ports"] = [{"target": 8000, "published": "8000"}]
    elif defect == "host-network":
        service["network_mode"] = "host"
    elif defect == "privileged":
        service["privileged"] = True
    elif defect == "docker-socket":
        service["volumes"] = [{"source": "/var/run/docker.sock", "target": "/var/run/docker.sock"}]
    elif defect == "core-network":
        service["networks"]["core"] = None
    elif defect == "secret-mode":
        wireguard.chmod(0o644)
    elif defect == "wrong-config":
        config["secrets"]["vpn_wireguard"]["file"] = str(secrets / "other.conf")
    elif defect == "mutable-image":
        service["image"] = "qmcgaw/gluetun:v3"
    elif defect == "unauthenticated-control":
        auth = secrets / "vpn_control_auth"
        auth.write_text(auth.read_text().replace('auth = "apikey"', 'auth = "none"'))
    elif defect == "credential-route":
        auth = secrets / "vpn_control_auth"
        auth.write_text(auth.read_text().replace('"GET /v1/vpn/status"', '"GET /v1/vpn/settings"'))
    elif defect == "dns-bypass":
        resolv.write_text("nameserver 8.8.8.8\n")
    elif defect == "dns-rewrite":
        service["environment"]["DNS_KEEP_NAMESERVER"] = "off"
    elif defect == "writable-resolver":
        service["volumes"][0]["read_only"] = False
    with pytest.raises(ValueError):
        module.validate_vpn(config, wireguard, secrets, resolv)
