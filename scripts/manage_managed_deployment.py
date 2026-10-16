"""Read-only preflight for the accepted profiles of the managed deployment.

The tools phase never selects, starts or migrates a database or executor.
Resolved Compose environments and Docker payloads stay in memory.
"""

from __future__ import annotations

import argparse
import json
import re
import stat
import subprocess
from pathlib import Path
from uuid import UUID

IMAGE = re.compile(r"^sha256:[a-f0-9]{64}$")
PROJECT = re.compile(r"^pixav-managed-[a-z0-9][a-z0-9-]*$")
CORE_IMAGES = {
    "postgres": "pgvector/pgvector:pg16@sha256:7d400e340efb42f4d8c9c12c6427adb253f726881a9985d2a471bf0eed824dff",
    "redis": "redis:8.2-alpine@sha256:b51665e66f00759be7c3152ad5ac3c66fb2f619c13ef62dea7cc1f9914524635",
}
CORE_COMMANDS = {
    "postgres": [
        "postgres",
        "-c",
        "max_wal_size=128MB",
        "-c",
        "min_wal_size=32MB",
        "-c",
        "log_min_error_statement=panic",
    ],
    "redis": [
        "redis-server",
        "/run/secrets/redis_config",
        "--dir",
        "/data",
        "--appendonly",
        "yes",
        "--appendfsync",
        "everysec",
        "--bind",
        "0.0.0.0",  # noqa: S104 -- internal Docker network only; no published port
        "--protected-mode",
        "yes",
    ],
}


def run(command: list[str]) -> str:
    return subprocess.run(  # noqa: S603 -- fixed Docker CLI, no shell
        command, capture_output=True, text=True, check=True, timeout=30
    ).stdout


def validate_tools(config: dict) -> None:
    if set(config["services"]) != {"windows_tools"}:
        raise ValueError("UNVERIFIED_MANAGED_PROFILE")
    service = config["services"]["windows_tools"]
    if (
        not IMAGE.fullmatch(service.get("image", ""))
        or service.get("pull_policy") != "never"
        or service.get("profiles") != ["windows-tools"]
        or service.get("container_name")
        or service.get("network_mode") == "host"
        or service.get("ports")
        or not service.get("read_only")
        or not service.get("init")
        or service.get("privileged")
        or service.get("devices")
        or service.get("cap_add")
        or service.get("command") != ["sleep", "infinity"]
        or service.get("restart") != "no"
        or service.get("cap_drop") != ["ALL"]
        or service.get("security_opt") != ["no-new-privileges:true"]
    ):
        raise ValueError("WINDOWS_TOOLS_ISOLATION_INVALID")
    if (
        float(service.get("cpus", 0)) != 1
        or int(service.get("mem_limit", 0)) != 2 * 1024**3
        or int(service.get("memswap_limit", 0)) != 2 * 1024**3
        or service.get("pids_limit") != 256
    ):
        raise ValueError("WINDOWS_TOOLS_RESOURCE_LIMITS_INVALID")
    environment = service.get("environment", {})
    expected = {
        "ADB_SERVER_SOCKET": "tcp:host.docker.internal:5037",
        "PIXAV_ANDROID_ADB_SERVER_HOST": "host.docker.internal",
        "ANDROID_SERIAL": environment.get("ANDROID_SERIAL"),
        "MAESTRO_CLI_NO_ANALYTICS": "1",
    }
    if environment != expected or not re.fullmatch(r"emulator-[0-9]{4,5}", environment.get("ANDROID_SERIAL", "")):
        raise ValueError("WINDOWS_TOOLS_TRANSPORT_INVALID")
    labels = service.get("labels", {})
    UUID(labels["pixav.photos_canary.owner"])
    UUID(labels["pixav.runtime.id"])
    if (
        labels.get("pixav.photos_canary.role") != "tools"
        or not labels.get("pixav.runtime.generation", "").isdigit()
        or int(labels["pixav.runtime.generation"]) <= 0
    ):
        raise ValueError("WINDOWS_TOOLS_RUNTIME_LABELS_INVALID")
    mounts = service.get("volumes", [])
    if (
        len(mounts) != 1
        or mounts[0].get("type") != "bind"
        or mounts[0].get("target") != "/parts"
        or mounts[0].get("read_only") is not True
        or mounts[0].get("bind", {}).get("create_host_path") is not False
    ):
        raise ValueError("WINDOWS_TOOLS_MOUNTS_INVALID")
    source = Path(mounts[0]["source"])
    if not source.is_absolute() or source.resolve() != source or not source.is_dir():
        raise ValueError("WINDOWS_TOOLS_STAGING_PATH_INVALID")
    if len(service.get("tmpfs", [])) != 3 or not service.get("healthcheck", {}).get("test"):
        raise ValueError("WINDOWS_TOOLS_HEALTH_OR_TMPFS_INVALID")


def validate_core(  # noqa: C901 -- explicit deployment refusals
    config: dict, data_root: Path, secrets_root: Path
) -> None:
    if set(config["services"]) != set(CORE_IMAGES) or not config.get("networks", {}).get("core", {}).get("internal"):
        raise ValueError("CORE_PROFILE_ISOLATION_INVALID")
    for root in [data_root, secrets_root]:
        if not root.is_absolute() or root.resolve() != root or stat.S_IMODE(root.stat().st_mode) != 0o700:
            raise ValueError("CORE_PRIVATE_ROOT_REQUIRED")
    for name, image in CORE_IMAGES.items():
        service = config["services"][name]
        if (
            service.get("image") != image
            or service.get("pull_policy") != "never"
            or service.get("profiles") != ["core"]
            or service.get("restart") != "no"
            or not service.get("read_only")
            or service.get("ports")
            or service.get("container_name")
            or service.get("entrypoint") is not None
            or service.get("command") != CORE_COMMANDS[name]
            or service.get("privileged")
            or service.get("devices")
            or service.get("network_mode")
            or set(service.get("networks", {})) != {"core"}
            or service.get("cap_drop") != ["ALL"]
            or service.get("cap_add")
            or not re.fullmatch(r"[1-9][0-9]*:[1-9][0-9]*", service.get("user", ""))
            or service.get("security_opt") != ["no-new-privileges:true"]
        ):
            raise ValueError("CORE_SERVICE_ISOLATION_INVALID")
        memory, cpus, pids = (3 * 1024**3, 2, 256) if name == "postgres" else (256 * 1024**2, 0.5, 64)
        if (
            int(service.get("mem_limit", 0)) != memory
            or int(service.get("memswap_limit", 0)) != memory
            or float(service.get("cpus", 0)) != cpus
            or service.get("pids_limit") != pids
            or not service.get("healthcheck", {}).get("test")
        ):
            raise ValueError("CORE_RESOURCE_OR_HEALTH_INVALID")
        mounts = service.get("volumes", [])
        target = "/var/lib/postgresql/data" if name == "postgres" else "/data"
        source = data_root / name
        if (
            len(mounts) != 1
            or mounts[0].get("type") != "bind"
            or mounts[0].get("source") != str(source)
            or mounts[0].get("target") != target
            or mounts[0].get("read_only", False)
            or mounts[0].get("bind", {}).get("create_host_path") is not False
            or source.resolve() != source
            or not source.is_dir()
            or source.stat().st_uid != int(service["user"].split(":")[0])
        ):
            raise ValueError("CORE_DATA_BIND_INVALID")
        expected_secrets = ["postgres_password"] if name == "postgres" else ["redis_config", "redis_password"]
        if {row["source"] for row in service.get("secrets", [])} != set(expected_secrets):
            raise ValueError("CORE_SECRET_BINDING_INVALID")
        for row in service["secrets"]:
            if row["target"] != "/run/secrets/" + row["source"]:
                raise ValueError("CORE_SECRET_BINDING_INVALID")
    expected_env = {"POSTGRES_USER", "POSTGRES_DB", "POSTGRES_PASSWORD_FILE", "POSTGRES_INITDB_ARGS", "PGDATA"}
    pg = config["services"]["postgres"]["environment"]
    if (
        set(pg) != expected_env
        or pg["POSTGRES_PASSWORD_FILE"] != "/run/secrets/postgres_password"  # noqa: S105 -- reference, not a password
        or pg["PGDATA"] != "/var/lib/postgresql/data/pgdata"
        or pg["POSTGRES_INITDB_ARGS"] != "--auth-host=scram-sha-256"
        or config["services"]["redis"].get("environment")
        or not re.fullmatch(r"[1-9][0-9]*:[1-9][0-9]*", config["services"]["redis"].get("user", ""))
    ):
        raise ValueError("CORE_CREDENTIAL_CONFIGURATION_INVALID")
    if set(config.get("secrets", {})) != {"postgres_password", "redis_config", "redis_password"}:
        raise ValueError("CORE_SECRET_BINDING_INVALID")
    for name, secret in config["secrets"].items():
        path = secrets_root / name
        if (
            secret.get("file") != str(path)
            or path.resolve() != path
            or not path.is_file()
            or stat.S_IMODE(path.stat().st_mode) != 0o600
        ):
            raise ValueError("CORE_PRIVATE_SECRET_REQUIRED")


def validate_application(config: dict, args: argparse.Namespace) -> None:  # noqa: C901 -- fixed managed roles
    profile = args.profile
    expected = {"migrate", "provision"} if profile == "schema" else {"maxwell_core"}
    if set(config["services"]) != expected or not args.db_identity or not args.database:
        raise ValueError("APPLICATION_BINDING_REQUIRED")
    images = {service.get("image", "") for service in config["services"].values()}
    if len(images) != 1 or not IMAGE.fullmatch(next(iter(images))):
        raise ValueError("APPLICATION_IMMUTABLE_IMAGE_REQUIRED")
    entrypoints = {
        "migrate": "scripts.migrate",
        "provision": "scripts.provision_workflow_roles",
        "maxwell_core": "pixav.maxwell_core.worker",
    }
    secret_targets = {
        "migrate": {"postgres_password": "/run/secrets/PIXAV_DB_PASSWORD"},
        "provision": {
            "postgres_password": "/run/secrets/PIXAV_DB_PASSWORD",
            **{name: "/run/secrets/" + name for name in ["authority_password", "worker_password", "reader_password"]},
        },
        "maxwell_core": {
            "authority_password": "/run/secrets/PIXAV_DB_PASSWORD",
            "redis_client_url": "/run/secrets/PIXAV_REDIS_URL",
            **{name: "/run/secrets/" + name for name in ["windows_ca", "windows_certificate", "windows_key"]},
        },
    }
    for name, service in config["services"].items():
        mounted = service.get("secrets", [])
        if (
            len(mounted) != len(secret_targets[name])
            or {row["source"]: row["target"] for row in mounted} != secret_targets[name]
        ):
            raise ValueError("APPLICATION_ROLE_SECRETS_INVALID")
        if (
            service.get("entrypoint") != ["python", "-m", entrypoints[name]]
            or service.get("profiles") != [profile]
            or service.get("pull_policy") != "never"
            or service.get("restart") != "no"
            or not service.get("read_only")
            or not service.get("init")
            or service.get("cap_drop") != ["ALL"]
            or service.get("cap_add")
            or service.get("ports")
            or service.get("privileged")
            or service.get("devices")
            or service.get("container_name")
            or service.get("network_mode")
            or service.get("security_opt") != ["no-new-privileges:true"]
            or not re.fullmatch(r"[1-9][0-9]*:[1-9][0-9]*", service.get("user", ""))
            or int(service.get("mem_limit", 0)) != 768 * 1024**2
            or int(service.get("memswap_limit", 0)) != 768 * 1024**2
            or float(service.get("cpus", 0)) != 1
            or service.get("pids_limit") != 256
        ):
            raise ValueError("APPLICATION_ISOLATION_INVALID")
        env = service.get("environment", {})
        if (
            env.get("PIXAV_DB_HOST") != "postgres"
            or env.get("PIXAV_DB_PORT") != "5432"
            or env.get("PIXAV_DB_NAME") != args.database
            or env.get("PIXAV_EXPECTED_DB_NAME") != args.database
            or env.get("PIXAV_EXPECTED_DB_IDENTITY") != args.db_identity
            or env.get("PIXAV_SETTINGS_SECRETS_DIR") != "/run/secrets"
            or any(key.endswith("_PASSWORD") or key == "PIXAV_REDIS_URL" for key in env)
        ):
            raise ValueError("APPLICATION_DATABASE_OR_SECRET_BINDING_INVALID")
        networks = {"core"} if profile == "schema" else {"core", "runtime"}
        if set(service.get("networks", {})) != networks or not config["networks"]["core"].get("internal"):
            raise ValueError("APPLICATION_NETWORK_INVALID")
        if profile == "schema":
            command = ["--db-identity", args.db_identity]
            if name == "migrate":
                command += ["--database", args.database]
            if service.get("command") != command or service.get("volumes"):
                raise ValueError("SCHEMA_DEFAULT_COMMAND_INVALID")
        else:
            if (
                not args.redis_run_id
                or env.get("PIXAV_EXPECTED_REDIS_RUN_ID") != args.redis_run_id
                or env.get("PIXAV_DB_USER") != "pixav_authority"
                or env.get("PIXAV_MANAGED_MEDIA_WORKFLOW") != "true"
                or env.get("PIXAV_PIXEL_INJECTOR_MODE") != "windows_android"
                or env.get("PIXAV_LOCAL_CLEANUP_APPLY") != "false"
                or env.get("PIXAV_UPLOAD_MAX_CONCURRENCY") != "1"
                or env.get("PIXAV_WINDOWS_CONTROL_DOCKER_DAEMON_ID") != args.daemon_id
                or not IMAGE.fullmatch(env.get("PIXAV_STORAGE_READBACK_IMAGE", ""))
            ):
                raise ValueError("AUTHORITY_EXECUTION_BINDING_INVALID")
            mounts = service.get("volumes", [])
            targets = {"/var/run/docker.sock"} | {
                "/app/data/" + name for name in ["downloads", "remuxed", "library", "artwork", "playback-cache"]
            }
            if len(mounts) != 6 or {mount.get("target") for mount in mounts} != targets:
                raise ValueError("AUTHORITY_MOUNTS_INVALID")
            for mount in mounts:
                target = mount.get("target")
                source = Path(mount.get("source", ""))
                if mount.get("bind", {}).get("create_host_path") is not False:
                    raise ValueError("AUTHORITY_MOUNTS_INVALID")
                if target == "/var/run/docker.sock":
                    if (
                        source != Path(target)
                        or not mount.get("read_only")
                        or service.get("group_add") != [str(source.stat().st_gid)]
                    ):
                        raise ValueError("AUTHORITY_DOCKER_SOCKET_INVALID")
                elif (
                    not target.startswith("/app/data/")
                    or args.data_root is None
                    or source != args.data_root / target.removeprefix("/app/data/")
                    or source.resolve() != source
                    or not source.is_dir()
                    or source.stat().st_uid != int(service["user"].split(":")[0])
                ):
                    raise ValueError("AUTHORITY_MOUNTS_INVALID")
    if args.secrets_root is None or stat.S_IMODE(args.secrets_root.stat().st_mode) != 0o700:
        raise ValueError("APPLICATION_PRIVATE_SECRETS_REQUIRED")
    for secret in config.get("secrets", {}).values():
        path = Path(secret.get("file", ""))
        if (
            path.parent != args.secrets_root
            or path.resolve() != path
            or not path.is_file()
            or stat.S_IMODE(path.stat().st_mode) != 0o600
        ):
            raise ValueError("APPLICATION_PRIVATE_SECRETS_REQUIRED")


def preflight(args: argparse.Namespace) -> dict:  # noqa: C901 -- profile and identity checks before deployment
    if not PROJECT.fullmatch(args.project):
        raise ValueError("MANAGED_PROJECT_REQUIRED")
    private = args.env_file
    if (
        private.resolve() != private
        or stat.S_IMODE(private.stat().st_mode) != 0o600
        or stat.S_IMODE(private.parent.stat().st_mode) != 0o700
    ):
        raise ValueError("PRIVATE_MANAGED_ENV_REQUIRED")
    docker = ["docker", "--context", args.context]
    observed = json.loads(run([*docker, "info", "--format", "{{json .ID}}"]))
    if observed != args.daemon_id:
        raise ValueError("MANAGED_DAEMON_IDENTITY_CHANGED")
    compose = [
        *docker,
        "compose",
        "--project-name",
        args.project,
        "--env-file",
        str(private),
        "-f",
        str(args.compose_file),
        "--profile",
        getattr(args, "profile", "windows-tools"),
    ]
    config = json.loads(run([*compose, "config", "--format", "json"]))
    if config.get("name") != args.project:
        raise ValueError("MANAGED_PROJECT_BINDING_CHANGED")
    profile = getattr(args, "profile", "windows-tools")
    if profile == "core":
        if args.data_root is None or args.secrets_root is None:
            raise ValueError("CORE_EXPLICIT_ROOTS_REQUIRED")
        validate_core(config, args.data_root, args.secrets_root)
    elif profile == "windows-tools":
        validate_tools(config)
    else:
        validate_application(config, args)
    for service in config["services"].values():
        run([*docker, "image", "inspect", service["image"], "--format", "{{.Id}}"])
    active = set()
    for label in ["pixav.runtime.role=android-dev-tools", "pixav.photos_canary.role=tools"]:
        active.update(run([*docker, "ps", "--quiet", "--no-trunc", "--filter", "label=" + label]).splitlines())
    for identifier in active:
        labels = json.loads(run([*docker, "inspect", identifier, "--format", "{{json .Config.Labels}}"]))
        if labels.get("com.docker.compose.project") != args.project:
            raise ValueError("PREVIOUS_ANDROID_TOOLS_STILL_RUNNING")
    return {
        "stage": profile,
        "eligible": True,
        "profile_services": len(config["services"]),
        "daemon_identity_bound": True,
        "immutable_image": True,
        "containers_started": 0,
        "database_touched": False,
        "runtime_instances_verified": False,
        "golden_path": "OPEN",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--daemon-id", required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--profile", choices=["windows-tools", "core", "schema", "authority"], default="windows-tools")
    parser.add_argument("--data-root", type=Path)
    parser.add_argument("--secrets-root", type=Path)
    parser.add_argument("--db-identity")
    parser.add_argument("--database")
    parser.add_argument("--redis-run-id")
    parser.add_argument("--compose-file", type=Path, default=Path("docker-compose.managed.yml"))
    args = parser.parse_args()
    try:
        result = preflight(args)
    except Exception as error:
        code = (
            str(error)
            if type(error) is ValueError and re.fullmatch(r"[A-Z_]+", str(error))
            else "MANAGED_PREFLIGHT_UNCONFIRMED"
        )
        print(json.dumps({"result": "REFUSED", "code": code}))
        raise SystemExit(2) from None
    print(json.dumps(result))


if __name__ == "__main__":
    main()
