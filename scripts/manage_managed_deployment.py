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

from scripts.prepare_managed_qbittorrent import QBIT_IMAGE, validate_qbit_files

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
VPN_IMAGE = "qmcgaw/gluetun:v3.41.3@sha256:fa19cc76b2af13d57a8d3dc3066f2ada061b1c761b8aecf989b3877c0486e027"
JELLYFIN_IMAGE = "jellyfin/jellyfin:12.1@sha256:78d3ea1207d1322471fcac39a614f004f2ccf7e878f95ab2977d752f07e4dd7e"
PRIVATE_PROXY_ENV = {
    **{key: "" for key in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]},
    "NO_PROXY": "*",
    "no_proxy": "*",
}
VPN_CONTROL_CONFIG = re.compile(
    r'\A\[\[roles\]\]\nname = "pixav-runtime"\n'
    r'routes = \["GET /v1/vpn/status", "PUT /v1/vpn/status"\]\n'
    r'auth = "apikey"\napikey = "[1-9A-HJ-NP-Za-km-z]{32,128}"\n\Z'
)


def run(command: list[str]) -> str:
    return subprocess.run(  # noqa: S603 -- fixed Docker CLI, no shell
        command, capture_output=True, text=True, check=True, timeout=30
    ).stdout


def validate_vpn(  # noqa: C901 -- fixed VPN boundary
    config: dict, wireguard: Path, secrets_root: Path, resolv_conf: Path
) -> None:
    if set(config["services"]) != {"gluetun"}:
        raise ValueError("VPN_PROFILE_ISOLATION_INVALID")
    service = config["services"]["gluetun"]
    if (
        service.get("image") != VPN_IMAGE
        or service.get("profiles") != ["vpn", "torrent"]
        or service.get("pull_policy") != "never"
        or service.get("restart") != "no"
        or service.get("user") != "0:0"
        or not service.get("read_only")
        or not service.get("init")
        or service.get("cap_drop") != ["ALL"]
        or service.get("cap_add") != ["NET_ADMIN", "DAC_OVERRIDE"]
        or service.get("security_opt") != ["no-new-privileges:true"]
        or service.get("privileged")
        or service.get("ports")
        or service.get("container_name")
        or service.get("network_mode")
        or service.get("entrypoint") is not None
        or service.get("command") is not None
        or set(service.get("networks", {})) != {"runtime", "download_control"}
        or config.get("networks", {}).get("runtime", {}).get("internal")
        or not config.get("networks", {}).get("download_control", {}).get("internal")
        or service.get("devices") != [{"source": "/dev/net/tun", "target": "/dev/net/tun", "permissions": "rw"}]
    ):
        raise ValueError("VPN_SERVICE_ISOLATION_INVALID")
    expected_environment = {
        "VPN_SERVICE_PROVIDER": "custom",
        "VPN_TYPE": "wireguard",
        "WIREGUARD_CONF_SECRETFILE": "/run/secrets/wg0.conf",
        "FIREWALL_ENABLED_DISABLING_IT_SHOOTS_YOU_IN_YOUR_FOOT": "on",
        "FIREWALL_OUTBOUND_SUBNETS": "",
        "FIREWALL_INPUT_PORTS": "8080",
        "FIREWALL_VPN_INPUT_PORTS": "",
        "HEALTH_SMALL_CHECK_TYPE": "dns",
        "HEALTH_TARGET_ADDRESSES": "1.1.1.1:443,1.0.0.1:443",
        "HEALTH_SERVER_ADDRESS": "127.0.0.1:9999",
        "HEALTH_RESTART_VPN": "on",
        "DNS_KEEP_NAMESERVER": "on",
        "HTTP_CONTROL_SERVER_ADDRESS": "127.0.0.1:8000",
        "HTTP_CONTROL_SERVER_AUTH_CONFIG_FILEPATH": "/run/secrets/vpn_control_auth",
        "HTTP_CONTROL_SERVER_LOG": "off",
        "HTTPPROXY": "off",
        "SHADOWSOCKS": "off",
        "PUID": "0",
        "PGID": "0",
    }
    if service.get("environment") != expected_environment:
        raise ValueError("VPN_FIREWALL_OR_SECRET_BINDING_INVALID")
    if (
        float(service.get("cpus", 0)) != 0.5
        or int(service.get("mem_limit", 0)) != 512 * 1024**2
        or int(service.get("memswap_limit", 0)) != 512 * 1024**2
        or service.get("pids_limit") != 128
        or service.get("healthcheck", {}).get("test") != ["CMD", "/gluetun-entrypoint", "healthcheck"]
        or service.get("tmpfs")
        != [
            "/tmp:rw,nosuid,nodev,noexec,size=64m,mode=1777",  # noqa: S108 -- bounded container tmpfs contract
            "/gluetun:rw,nosuid,nodev,noexec,size=64m,mode=0700",
        ]
    ):
        raise ValueError("VPN_RESOURCE_OR_HEALTH_INVALID")
    mounts = service.get("volumes", [])
    if (
        len(mounts) != 1
        or mounts[0].get("type") != "bind"
        or mounts[0].get("source") != str(resolv_conf)
        or mounts[0].get("target") != "/etc/resolv.conf"
        or mounts[0].get("read_only") is not True
        or mounts[0].get("bind", {}).get("create_host_path") is not False
        or not resolv_conf.is_absolute()
        or resolv_conf.resolve() != resolv_conf
        or not resolv_conf.is_file()
        or stat.S_IMODE(resolv_conf.stat().st_mode) != 0o600
        or stat.S_IMODE(resolv_conf.parent.stat().st_mode) != 0o700
    ):
        raise ValueError("VPN_RESOLVER_BINDING_INVALID")
    nameservers = [
        line.split() for line in resolv_conf.read_text().splitlines() if line.strip().startswith("nameserver")
    ]
    if nameservers != [["nameserver", "127.0.0.1"]]:
        raise ValueError("VPN_RESOLVER_BYPASS_REFUSED")
    mounted = service.get("secrets", [])
    if len(mounted) != 2 or {row["source"]: row["target"] for row in mounted} != {
        "vpn_wireguard": "/run/secrets/wg0.conf",
        "vpn_control_auth": "/run/secrets/vpn_control_auth",
    }:
        raise ValueError("VPN_PRIVATE_SECRET_REQUIRED")
    expected_files = {"vpn_wireguard": wireguard, "vpn_control_auth": secrets_root / "vpn_control_auth"}
    if set(config.get("secrets", {})) != set(expected_files):
        raise ValueError("VPN_PRIVATE_SECRET_REQUIRED")
    for name, path in expected_files.items():
        if (
            config["secrets"][name].get("file") != str(path)
            or not path.is_absolute()
            or path.resolve() != path
            or not path.is_file()
            or stat.S_IMODE(path.stat().st_mode) != 0o600
            or stat.S_IMODE(path.parent.stat().st_mode) != 0o700
            or path.stat().st_size > 65536
        ):
            raise ValueError("VPN_PRIVATE_SECRET_REQUIRED")
    if not VPN_CONTROL_CONFIG.fullmatch(expected_files["vpn_control_auth"].read_text()):
        raise ValueError("VPN_CONTROL_AUTH_REQUIRED")


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


def validate_activity_mounts(service: dict, args: argparse.Namespace) -> None:  # noqa: C901 -- exact bind contract
    env = service["environment"]
    project = args.project_root
    staging = args.staging_root
    if (
        project is None
        or staging is None
        or args.data_root != project / "data"
        or not project.is_absolute()
        or project.resolve() != project
        or not staging.is_absolute()
        or staging.resolve() != staging
        or not staging.is_dir()
        or env.get("PIXAV_HOST_PROJECT_ROOT") != str(project)
        or env.get("PIXAV_STORAGE_STAGING_DIR") != str(staging)
        or env.get("PIXAV_STORAGE_READBACK_DIR") != "/app/data/storage-readback"
        or env.get("PIXAV_PLAYBACK_CACHE_DIR") != "/app/data/playback-cache"
        or env.get("PIXAV_PHOTOS_COLD_AUTH_SECRET") != "/run/photos-auth/cookies.json"
        or service.get("command") != []
    ):
        raise ValueError("ACTIVITY_PATH_BINDING_INVALID")
    try:
        UUID(env["PIXAV_STORAGE_WORKER_OWNER"])
        UUID(env["PIXAV_STORAGE_WINDOWS_RUNTIME_ID"])
        if int(env["PIXAV_STORAGE_WINDOWS_RUNTIME_GENERATION"]) <= 0:
            raise ValueError
    except (KeyError, ValueError, TypeError):
        raise ValueError("ACTIVITY_RUNTIME_BINDING_INVALID") from None
    expected = {"/var/run/docker.sock": (Path("/var/run/docker.sock"), True)}
    if args.profile == "storage":
        expected.update(
            {
                "/app/data/remuxed": (args.data_root / "remuxed", True),
                "/app/data/storage-readback": (args.data_root / "storage-readback", False),
                str(staging): (staging, False),
            }
        )
        probe = "/app/data/storage-readback"
    else:
        expected.update(
            {
                "/app/data/storage-readback": (args.data_root / "storage-readback", True),
                "/app/data/playback-cache": (args.data_root / "playback-cache", False),
            }
        )
        probe = "/app/data/playback-cache"
    if env.get("PIXAV_DOWNLOAD_DIR") != probe:
        raise ValueError("ACTIVITY_DISK_PROBE_PATH_INVALID")
    mounts = service.get("volumes", [])
    if len(mounts) != len(expected) or {row.get("target") for row in mounts} != set(expected):
        raise ValueError("ACTIVITY_MOUNTS_INVALID")
    for row in mounts:
        source, readonly = expected[row["target"]]
        if (
            row.get("type") != "bind"
            or row.get("source") != str(source)
            or row.get("read_only", False) != readonly
            or row.get("bind", {}).get("create_host_path") is not False
        ):
            raise ValueError("ACTIVITY_MOUNTS_INVALID")
        if row["target"] == "/var/run/docker.sock":
            if service.get("group_add") != [str(source.stat().st_gid)]:
                raise ValueError("ACTIVITY_DOCKER_SOCKET_INVALID")
        elif (
            source.resolve() != source
            or not source.is_dir()
            or source.stat().st_uid != int(service["user"].split(":")[0])
        ):
            raise ValueError("ACTIVITY_MOUNTS_INVALID")
    uid, gid = service["user"].split(":")
    if service.get("tmpfs") != [
        "/tmp:rw,nosuid,nodev,size=64m,mode=1777",  # noqa: S108 -- bounded container tmpfs contract
        f"/run/photos-auth:rw,nosuid,nodev,noexec,size=1m,mode=0700,uid={uid},gid={gid}",
    ] or not service.get("healthcheck", {}).get("test"):
        raise ValueError("ACTIVITY_PRIVATE_WORKSPACE_INVALID")


def validate_playback_auth(path: Path, uid: int) -> None:
    if (
        not path.is_absolute()
        or path.resolve() != path
        or not path.is_dir()
        or stat.S_IMODE(path.stat().st_mode) != 0o700
        or path.stat().st_uid != uid
        or {entry.name for entry in path.iterdir()} != {"resolver_token"}
    ):
        raise ValueError("PRIVATE_PLAYBACK_AUTH_DIRECTORY_REQUIRED")
    token = path / "resolver_token"
    if (
        token.is_symlink()
        or not token.is_file()
        or token.stat().st_uid != uid
        or stat.S_IMODE(token.stat().st_mode) != 0o600
        or token.stat().st_size > 4096
        or not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", token.read_text().strip())
    ):
        raise ValueError("PRIVATE_PLAYBACK_TOKEN_REQUIRED")


def validate_private_binds(service: dict, expected: dict[str, tuple[Path, bool]]) -> None:
    mounts = service.get("volumes", [])
    if len(mounts) != len(expected) or {row.get("target") for row in mounts} != set(expected):
        raise ValueError("PRIVATE_PLAYBACK_MOUNTS_INVALID")
    uid = int(service["user"].split(":")[0])
    for row in mounts:
        source, readonly = expected[row["target"]]
        if (
            row.get("type") != "bind"
            or row.get("source") != str(source)
            or row.get("read_only", False) != readonly
            or row.get("bind", {}).get("create_host_path") is not False
            or not source.is_absolute()
            or source.resolve() != source
            or not source.exists()
            or (row["target"] not in {"/config/config/logging.json", "/etc/resolv.conf"} and not source.is_dir())
            or (row["target"] in {"/config/config/logging.json", "/etc/resolv.conf"} and not source.is_file())
            or (source.is_dir() and source.stat().st_uid != uid)
        ):
            raise ValueError("PRIVATE_PLAYBACK_MOUNTS_INVALID")


def validate_resolver(config: dict, service: dict, args: argparse.Namespace) -> None:
    env = service["environment"]
    expected = {
        **PRIVATE_PROXY_ENV,
        "PIXAV_DB_USER": "pixav_reader",
        "PIXAV_EXPECTED_REDIS_RUN_ID": args.redis_run_id,
        "PIXAV_MANAGED_MEDIA_WORKFLOW": "true",
        "PIXAV_PIXEL_INJECTOR_MODE": "windows_android",
        "PIXAV_RESOLVER_REQUIRE_READER_ROLE": "true",
        "PIXAV_RESOLVER_AUTH_REQUIRED": "true",
        "PIXAV_RESOLVER_TOKEN_FILE": "/run/playback-auth/resolver_token",
        "PIXAV_RESOLVER_RATE_LIMIT_RPM": "6000",
        "PIXAV_PLAYBACK_CACHE_DIR": "/app/data/playback-cache",
    }
    database_keys = {
        "PIXAV_DB_HOST",
        "PIXAV_DB_PORT",
        "PIXAV_DB_NAME",
        "PIXAV_EXPECTED_DB_IDENTITY",
        "PIXAV_EXPECTED_DB_NAME",
        "PIXAV_SETTINGS_SECRETS_DIR",
    }
    if (
        not args.redis_run_id
        or set(env) != set(expected) | database_keys
        or any(env.get(key) != value for key, value in expected.items())
        or service.get("command")
        != [
            "pixav.strm_resolver.app:create_app",
            "--factory",
            "--host",
            "0.0.0.0",  # noqa: S104 -- private container networks only
            "--port",
            "8000",
            "--no-access-log",
        ]  # noqa: S104 -- private container networks only
        or not config["networks"]["playback_backend"].get("internal")
        or not service.get("healthcheck", {}).get("test")
    ):
        raise ValueError("PRIVATE_RESOLVER_BINDING_INVALID")
    validate_playback_auth(args.playback_auth_dir, int(service["user"].split(":")[0]))
    validate_private_binds(
        service,
        {
            "/app/data/playback-cache": (args.data_root / "playback-cache", False),
            "/run/playback-auth": (args.playback_auth_dir, True),
        },
    )


def validate_library_isolation(service: dict, *, memory: int, cpus: float, pids: int) -> None:
    if (
        service.get("profiles") != ["library"]
        or service.get("pull_policy") != "never"
        or service.get("restart") != "no"
        or not service.get("read_only")
        or not service.get("init")
        or service.get("cap_drop") != ["ALL"]
        or service.get("cap_add")
        or service.get("privileged")
        or service.get("devices")
        or service.get("container_name")
        or service.get("network_mode")
        or service.get("security_opt") != ["no-new-privileges:true"]
        or service.get("secrets")
        or not re.fullmatch(r"[1-9][0-9]*:[1-9][0-9]*", service.get("user", ""))
        or int(service.get("mem_limit", 0)) != memory
        or int(service.get("memswap_limit", 0)) != memory
        or float(service.get("cpus", 0)) != cpus
        or service.get("pids_limit") != pids
        or not service.get("healthcheck", {}).get("test")
    ):
        raise ValueError("PRIVATE_LIBRARY_ISOLATION_INVALID")


def validate_library(config: dict, args: argparse.Namespace) -> None:  # noqa: C901 -- fixed private service topology
    services = config["services"]
    if set(services) != {"playback-gateway", "jellyfin", "jellyfin-client-gateway"} or config.get("secrets"):
        raise ValueError("PRIVATE_LIBRARY_PROFILE_INVALID")
    networks = {
        "playback-gateway": {"playback_backend", "library_media"},
        "jellyfin": {"library_media", "library_origin"},
        "jellyfin-client-gateway": {"library_origin", "client_capture", "library_ingress"},
    }
    for name in ["playback_backend", "library_media", "library_origin", "client_capture"]:
        if not config.get("networks", {}).get(name, {}).get("internal"):
            raise ValueError("PRIVATE_LIBRARY_NETWORK_INVALID")
    if config.get("networks", {}).get("library_ingress", {}).get("internal"):
        raise ValueError("PRIVATE_LIBRARY_INGRESS_INVALID")
    images = {services[name].get("image", "") for name in ["playback-gateway", "jellyfin-client-gateway"]}
    if len(images) != 1 or not IMAGE.fullmatch(next(iter(images))):
        raise ValueError("PRIVATE_GATEWAY_IMMUTABLE_IMAGE_REQUIRED")
    for name, service in services.items():
        uid, gid = service.get("user", ":").split(":")
        is_origin = name == "jellyfin"
        validate_library_isolation(
            service,
            memory=(1024 if is_origin else 128) * 1024**2,
            cpus=1 if is_origin else 0.25,
            pids=256 if is_origin else 64,
        )
        env = {**PRIVATE_PROXY_ENV}
        if name == "playback-gateway":
            env["PIXAV_RESOLVER_TOKEN_FILE"] = "/run/playback-auth/resolver_token"  # noqa: S105 -- file reference only
        if service.get("environment") != env or set(service.get("networks", {})) != networks[name]:
            raise ValueError("PRIVATE_LIBRARY_NETWORK_OR_PROXY_INVALID")
        if name != "jellyfin-client-gateway" and service.get("ports"):
            raise ValueError("PRIVATE_LIBRARY_ORIGIN_EXPOSED")
        if is_origin:
            if (
                service.get("image") != JELLYFIN_IMAGE
                or service.get("entrypoint") is not None
                or service.get("command") is not None
            ):
                raise ValueError("PRIVATE_JELLYFIN_IMAGE_OR_ENTRY_INVALID")
            if service["networks"]["library_origin"].get("aliases") != ["jellyfin-origin"]:
                raise ValueError("PRIVATE_JELLYFIN_ORIGIN_ALIAS_REQUIRED")
            validate_private_binds(
                service,
                {
                    "/config": (args.data_root / "jellyfin/config", False),
                    "/cache": (args.data_root / "jellyfin/cache", False),
                    "/media/library": (args.data_root / "library", True),
                    "/config/config/logging.json": (args.jellyfin_logging_policy, True),
                },
            )
            for folder in [
                args.data_root / "jellyfin/config",
                args.data_root / "jellyfin/config/config",
                args.data_root / "jellyfin/cache",
            ]:
                if (
                    folder.resolve() != folder
                    or stat.S_IMODE(folder.stat().st_mode) != 0o700
                    or folder.stat().st_uid != int(uid)
                ):
                    raise ValueError("PRIVATE_JELLYFIN_DATA_REQUIRED")
            target = args.data_root / "jellyfin/config/config/logging.json"
            if target.is_symlink() or not target.is_file() or target.stat().st_uid != int(uid):
                raise ValueError("PRIVATE_JELLYFIN_LOG_TARGET_REQUIRED")
        else:
            entry = "/usr/local/bin/" + (
                "pixav-playback-gateway" if name == "playback-gateway" else "pixav-jellyfin-gateway"
            )
            if service.get("entrypoint") != [entry] or service.get("command") != []:
                raise ValueError("PRIVATE_GATEWAY_ENTRY_INVALID")
            if service.get("tmpfs") != [
                "/tmp:rw,nosuid,nodev,noexec,size=32m,mode=1777",  # noqa: S108 -- bounded container tmpfs contract
                f"/etc/nginx/conf.d:rw,nosuid,nodev,noexec,size=1m,mode=0700,uid={uid},gid={gid}",
            ]:
                raise ValueError("PRIVATE_GATEWAY_TMPFS_INVALID")
            if name == "playback-gateway":
                validate_playback_auth(args.playback_auth_dir, int(uid))
                validate_private_binds(service, {"/run/playback-auth": (args.playback_auth_dir, True)})
            elif service.get("volumes"):
                raise ValueError("PRIVATE_CLIENT_GATEWAY_MOUNTS_INVALID")
    policy = args.jellyfin_logging_policy
    expected_policy = {
        "Serilog": {
            "MinimumLevel": {
                "Default": "Warning",
                "Override": {
                    "Emby.Server.Implementations.Session.SessionManager": "Warning",
                    "Jellyfin.Server.Implementations.Users.UserManager": "Warning",
                },
            }
        }
    }
    if policy.resolve() != policy or not policy.is_file() or json.loads(policy.read_text()) != expected_policy:
        raise ValueError("PRIVATE_JELLYFIN_LOGGING_POLICY_REQUIRED")
    ports = services["jellyfin-client-gateway"].get("ports", [])
    if (
        not 1024 <= args.jellyfin_port <= 65535
        or len(ports) != 1
        or ports[0].get("target") != 8096
        or str(ports[0].get("published")) != str(args.jellyfin_port)
        or ports[0].get("host_ip") != "127.0.0.1"
        or ports[0].get("protocol") != "tcp"
    ):
        raise ValueError("PRIVATE_JELLYFIN_LOOPBACK_PORT_REQUIRED")


def validate_torrent(config: dict, args: argparse.Namespace) -> None:  # noqa: C901 -- fixed shared VPN namespace
    if set(config["services"]) != {"gluetun", "qbittorrent"} or args.data_root is None or args.secrets_root is None:
        raise ValueError("TORRENT_EXPLICIT_BINDING_REQUIRED")
    vpn = {
        **config,
        "services": {"gluetun": config["services"]["gluetun"]},
        "secrets": {key: config["secrets"][key] for key in ["vpn_wireguard", "vpn_control_auth"]},
    }
    if set(config.get("secrets", {})) != set(vpn["secrets"]):
        raise ValueError("TORRENT_SECRET_BOUNDARY_INVALID")
    validate_vpn(vpn, args.wireguard_config, args.secrets_root, args.vpn_resolv_conf)
    service = config["services"]["qbittorrent"]
    if (
        service.get("profiles") != ["torrent"]
        or service.get("image") != QBIT_IMAGE
        or service.get("pull_policy") != "never"
        or service.get("restart") != "no"
        or not service.get("read_only")
        or not service.get("init")
        or service.get("cap_drop") != ["ALL"]
        or service.get("cap_add")
        or service.get("privileged")
        or service.get("devices")
        or service.get("ports")
        or service.get("secrets")
        or service.get("group_add")
        or service.get("dns")
        or service.get("extra_hosts")
        or service.get("container_name")
        or service.get("networks")
        or service.get("network_mode") != "service:gluetun"
        or service.get("depends_on", {}).get("gluetun", {}).get("condition") != "service_healthy"
        or set(service.get("depends_on", {})) != {"gluetun"}
        or service.get("security_opt") != ["no-new-privileges:true"]
        or not re.fullmatch(r"[1-9][0-9]*:[1-9][0-9]*", service.get("user", ""))
        or float(service.get("cpus", 0)) != 1
        or int(service.get("mem_limit", 0)) != 512 * 1024**2
        or int(service.get("memswap_limit", 0)) != 512 * 1024**2
        or service.get("pids_limit") != 128
        or service.get("entrypoint") != ["/bin/sh", "-c"]
        or service.get("command") != ["umask 077; exec /app/qbittorrent-nox --confirm-legal-notice --webui-port=8080"]
        or service.get("healthcheck", {}).get("test") != ["CMD", "nc", "-z", "-w", "3", "127.0.0.1", "8080"]
        or service.get("tmpfs") != ["/tmp:rw,nosuid,nodev,noexec,size=32m,mode=1777"]  # noqa: S108 -- bounded tmpfs
        or service.get("environment")
        != {
            **PRIVATE_PROXY_ENV,
            "HOME": "/config",
            "XDG_CONFIG_HOME": "/config",
            "XDG_DATA_HOME": "/config",
            "XDG_CACHE_HOME": "/tmp",  # noqa: S108 -- bounded container tmpfs
        }
    ):
        raise ValueError("TORRENT_VPN_NAMESPACE_OR_ISOLATION_INVALID")
    validate_private_binds(
        service,
        {
            "/config": (args.data_root / "qbittorrent", False),
            "/downloads": (args.data_root / "downloads", False),
            "/etc/resolv.conf": (args.vpn_resolv_conf, True),
        },
    )
    if args.vpn_resolv_conf.stat().st_uid != int(service["user"].split(":")[0]):
        raise ValueError("TORRENT_PRIVATE_RESOLVER_UNREADABLE")
    validate_qbit_files(args.data_root, args.secrets_root, int(service["user"].split(":")[0]))


def validate_media(config: dict, service: dict, args: argparse.Namespace) -> None:
    env = service["environment"]
    fixed = {
        **PRIVATE_PROXY_ENV,
        "PIXAV_DOWNLOAD_DIR": "/app/data/downloads",
        "PIXAV_REMUX_DIR": "/app/data/remuxed",
        "PIXAV_QBIT_URL": "http://gluetun:8080",
        "PIXAV_QBIT_USER": "pixav",
        "PIXAV_QBIT_DOWNLOAD_DIR": "/downloads",
        "PIXAV_STASH_ENABLED": "false",
        "PIXAV_WINDOWS_CONTROL_URL": "https://host.docker.internal:5043",
        "PIXAV_WINDOWS_CONTROL_CA": "/run/secrets/windows_ca",
        "PIXAV_WINDOWS_CONTROL_CERTIFICATE": "/run/secrets/windows_certificate",
        "PIXAV_WINDOWS_CONTROL_KEY": "/run/secrets/windows_key",
    }
    if (
        any(env.get(key) != value for key, value in fixed.items())
        or service.get("command") != []
        or service.get("group_add")
        or service.get("depends_on")
        or not config.get("networks", {}).get("download_control", {}).get("internal")
        or not service.get("healthcheck", {}).get("test")
        or service.get("tmpfs") != ["/tmp:rw,nosuid,nodev,size=64m,mode=1777"]  # noqa: S108 -- bounded tmpfs
        or any("PHOTOS" in key or "READBACK" in key or "STAGING" in key for key in env)
    ):
        raise ValueError("MEDIA_DEPENDENCY_BOUNDARY_INVALID")
    try:
        UUID(env["PIXAV_STORAGE_WORKER_OWNER"])
        UUID(env["PIXAV_STORAGE_WINDOWS_RUNTIME_ID"])
        if int(env["PIXAV_STORAGE_WINDOWS_RUNTIME_GENERATION"]) <= 0:
            raise ValueError
    except (KeyError, ValueError, TypeError):
        raise ValueError("MEDIA_RUNTIME_BINDING_INVALID") from None
    validate_private_binds(
        service,
        {
            "/app/data/downloads": (args.data_root / "downloads", False),
            "/app/data/remuxed": (args.data_root / "remuxed", False),
        },
    )
    validate_qbit_files(args.data_root, args.secrets_root, int(service["user"].split(":")[0]))


def validate_application(config: dict, args: argparse.Namespace) -> None:  # noqa: C901 -- fixed managed roles
    profile = args.profile
    expected = {
        "schema": {"migrate", "provision"},
        "authority": {"maxwell_core"},
        "storage": {"storage_worker"},
        "playback": {"playback_worker"},
        "resolver": {"strm_resolver"},
        "media": {"media_loader"},
    }[profile]
    if set(config["services"]) != expected or not args.db_identity or not args.database:
        raise ValueError("APPLICATION_BINDING_REQUIRED")
    images = {service.get("image", "") for service in config["services"].values()}
    if len(images) != 1 or not IMAGE.fullmatch(next(iter(images))):
        raise ValueError("APPLICATION_IMMUTABLE_IMAGE_REQUIRED")
    entrypoints = {
        "migrate": "scripts.migrate",
        "provision": "scripts.provision_workflow_roles",
        "maxwell_core": "pixav.maxwell_core.worker",
        "storage_worker": "pixav.pixel_injector.storage_worker",
        "playback_worker": "pixav.pixel_injector.playback_worker",
        "strm_resolver": "uvicorn",
        "media_loader": "pixav.media_loader.worker",
    }
    secret_targets = {
        "strm_resolver": {
            "reader_password": "/run/secrets/PIXAV_DB_PASSWORD",
            "redis_client_url": "/run/secrets/PIXAV_REDIS_URL",
        },
        "migrate": {"postgres_password": "/run/secrets/PIXAV_DB_PASSWORD"},
        "provision": {
            "postgres_password": "/run/secrets/PIXAV_DB_PASSWORD",
            **{name: "/run/secrets/" + name for name in ["authority_password", "worker_password", "reader_password"]},
        },
        "maxwell_core": {
            "authority_password": "/run/secrets/PIXAV_DB_PASSWORD",
            "redis_client_url": "/run/secrets/PIXAV_REDIS_URL",
            **{name: "/run/secrets/" + name for name in ["windows_ca", "windows_certificate", "windows_key"]},
            "download_pause_token": "/run/secrets/download_pause_token",
        },
    }
    for name in ["storage_worker", "playback_worker"]:
        secret_targets[name] = {
            key: target for key, target in secret_targets["maxwell_core"].items() if key != "authority_password"
        } | {"worker_password": "/run/secrets/PIXAV_DB_PASSWORD", "photos_auth": "/run/photos-auth/cookies.json"}
    secret_targets["media_loader"] = {
        key: target for key, target in secret_targets["maxwell_core"].items() if key != "authority_password"
    } | {"worker_password": "/run/secrets/PIXAV_DB_PASSWORD", "qbit_password": "/run/secrets/PIXAV_QBIT_PASSWORD"}
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
        networks = (
            {"core"}
            if profile == "schema"
            else ({"core", "playback_backend"} if profile == "resolver" else {"core", "runtime"})
        )
        if profile == "media":
            networks = {"core", "runtime", "download_control"}
        if set(service.get("networks", {})) != networks or not config["networks"]["core"].get("internal"):
            raise ValueError("APPLICATION_NETWORK_INVALID")
        if profile == "schema":
            command = ["--db-identity", args.db_identity]
            if name == "migrate":
                command += ["--database", args.database]
            if service.get("command") != command or service.get("volumes"):
                raise ValueError("SCHEMA_DEFAULT_COMMAND_INVALID")
        elif profile == "resolver":
            validate_resolver(config, service, args)
        else:
            if (
                not args.redis_run_id
                or env.get("PIXAV_EXPECTED_REDIS_RUN_ID") != args.redis_run_id
                or env.get("PIXAV_DB_USER") != ("pixav_authority" if profile == "authority" else "pixav_worker")
                or env.get("PIXAV_MANAGED_MEDIA_WORKFLOW") != "true"
                or env.get("PIXAV_PIXEL_INJECTOR_MODE") != "windows_android"
                or env.get("PIXAV_LOCAL_CLEANUP_APPLY") != "false"
                or env.get("PIXAV_UPLOAD_MAX_CONCURRENCY") != "1"
                or (profile == "authority" and env.get("PIXAV_RESOLVER_BASE_URL") != "http://playback-gateway:8080")
                or env.get("PIXAV_WINDOWS_CONTROL_DOCKER_DAEMON_ID") != args.daemon_id
                or (profile != "media" and not IMAGE.fullmatch(env.get("PIXAV_STORAGE_READBACK_IMAGE", "")))
                or not re.fullmatch(r"[a-f0-9]{64}", env.get("PIXAV_WINDOWS_CONTROL_IDENTITY_SHA256", ""))
                or not re.fullmatch(r"[1-9][0-9]*", env.get("PIXAV_WINDOWS_CAPACITY_VOLUME_DEVICE", ""))
                or env.get("PIXAV_DOWNLOAD_PAUSE_TOKEN_FILE") != "/run/secrets/download_pause_token"
            ):
                raise ValueError("AUTHORITY_EXECUTION_BINDING_INVALID")
            if profile == "media":
                validate_media(config, service, args)
                continue
            if profile in {"storage", "playback"}:
                validate_activity_mounts(service, args)
                continue
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
    if set(config.get("secrets", {})) != {key for name in expected for key in secret_targets[name]}:
        raise ValueError("APPLICATION_ROLE_SECRETS_INVALID")
    for name, secret in config.get("secrets", {}).items():
        path = Path(secret.get("file", ""))
        if name == "photos_auth":
            if path != args.photos_auth_secret or stat.S_IMODE(path.parent.stat().st_mode) != 0o700:
                raise ValueError("ACTIVITY_PRIVATE_AUTH_REQUIRED")
        elif path.parent != args.secrets_root:
            raise ValueError("APPLICATION_PRIVATE_SECRETS_REQUIRED")
        if (
            not path.is_absolute()
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
    elif profile in {"vpn", "torrent"}:
        if args.wireguard_config is None or args.secrets_root is None or args.vpn_resolv_conf is None:
            raise ValueError("VPN_EXPLICIT_SECRET_REFERENCES_REQUIRED")
        if profile == "torrent":
            validate_torrent(config, args)
        else:
            validate_vpn(config, args.wireguard_config, args.secrets_root, args.vpn_resolv_conf)
    elif profile == "library":
        if args.data_root is None or args.playback_auth_dir is None:
            raise ValueError("PRIVATE_LIBRARY_EXPLICIT_ROOTS_REQUIRED")
        validate_library(config, args)
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
    parser.add_argument(
        "--profile",
        choices=[
            "windows-tools",
            "core",
            "schema",
            "authority",
            "storage",
            "playback",
            "vpn",
            "torrent",
            "media",
            "resolver",
            "library",
        ],
        default="windows-tools",
    )
    parser.add_argument("--data-root", type=Path)
    parser.add_argument("--secrets-root", type=Path)
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--staging-root", type=Path)
    parser.add_argument("--photos-auth-secret", type=Path)
    parser.add_argument("--wireguard-config", type=Path)
    parser.add_argument("--vpn-resolv-conf", type=Path)
    parser.add_argument("--playback-auth-dir", type=Path)
    parser.add_argument("--jellyfin-port", type=int, default=18096)
    parser.add_argument(
        "--jellyfin-logging-policy",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "config/jellyfin/logging.json",
    )
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
