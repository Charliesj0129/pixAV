"""Read-only release preflight for the standalone managed acceptance stack.

This records deployment inputs and instance identities. It never advances an
execution or marks the Golden Path PASS. Secrets stay in the private env file;
the resolved Compose environment is hashed in memory and never printed/saved.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from pixav.shared.backup_files import create_backup_file

COMPOSE = Path(__file__).resolve().parents[1] / "docker-compose.acceptance.yml"
APP_SERVICES = {
    "migrate",
    "provision",
    "maxwell_core",
    "media_loader",
    "storage_worker",
    "playback_worker",
    "strm_resolver",
}
REQUIRED_SERVICES = APP_SERVICES | {"postgres", "redis", "qbittorrent", "playback-gateway", "jellyfin"}
IMMUTABLE_IMAGE = re.compile(r"^(?:sha256:|[^\s]+@sha256:)[0-9a-f]{64}$")
PROJECT = re.compile(r"^pixav-acceptance-[a-z0-9][a-z0-9-]*$")


def _run(command: list[str]) -> str:
    return subprocess.run(  # noqa: S603 -- fixed executables, validated identifiers; never shell=True
        command, check=True, capture_output=True, text=True, timeout=30
    ).stdout


def validate_config(config: dict, root: Path) -> None:  # noqa: C901 -- explicit deployment refusals
    services = config["services"]
    if not REQUIRED_SERVICES <= services.keys():
        raise ValueError("required acceptance services are missing")
    app_services = APP_SERVICES | ({"sht_probe"} if "sht_probe" in services else set())
    app_images = {services[name]["image"] for name in app_services}
    if len(app_images) != 1:
        raise ValueError("application services must use the same immutable image")
    for name, service in services.items():
        if not IMMUTABLE_IMAGE.fullmatch(service.get("image", "")):
            raise ValueError("every acceptance image must be immutable")
        if service.get("container_name") or service.get("network_mode") == "host":
            raise ValueError("fixed container names and host networking are forbidden")
        for port in service.get("ports", []):
            if port.get("host_ip") != "127.0.0.1":
                raise ValueError("published acceptance ports must be loopback only")
        for mount in service.get("volumes", []):
            if mount["type"] != "bind":
                continue
            source = Path(mount["source"])
            if source == Path("/var/run/docker.sock"):
                if name not in {"storage_worker", "playback_worker"}:
                    raise ValueError("Docker socket belongs only to runtime workers")
            elif not source.is_relative_to(root) or source.resolve() != source:
                raise ValueError("acceptance binds must remain inside the private root without symlinks")
    for name in app_services - {"migrate", "provision"}:
        environment = services[name]["environment"]
        if environment.get("PIXAV_LOCAL_CLEANUP_APPLY") != "false":
            raise ValueError("live cleanup must remain disabled")
        if environment.get("PIXAV_MANAGED_MEDIA_WORKFLOW") != "true":
            raise ValueError("managed execution must be enabled")
        if environment.get("PIXAV_PIXEL_INJECTOR_MODE") != "redroid":
            raise ValueError("the selected Pixel upload mode must not change")
        if environment.get("PIXAV_REDROID_PROFILE") != "gphotos_pixel_xl_v1":
            raise ValueError("the selected Pixel profile must not change")
        if not IMMUTABLE_IMAGE.fullmatch(environment.get("PIXAV_STORAGE_READBACK_IMAGE", "")):
            raise ValueError("the cold image must be immutable")
        if not IMMUTABLE_IMAGE.fullmatch(environment.get("PIXAV_STORAGE_UPLOAD_TOOLS_IMAGE", "")):
            raise ValueError("the upload tools image must be immutable")
    if "sht_probe" in services:
        source = services["sht_probe"]
        mounts = source.get("volumes", [])
        if (
            len(mounts) != 1
            or mounts[0].get("source") != str(root / "secrets/source")
            or not mounts[0].get("read_only")
        ):
            raise ValueError("native source must use only its private read-only session and cohort")
        if source["environment"].get("PIXAV_SOURCE_MIN_QUALITY_SCORE") != "0" or source["environment"].get(
            "PIXAV_CRAWL_QUERIES"
        ):
            raise ValueError("native source must preserve normal policy and finite discovery")
    resolver = services["strm_resolver"]
    if resolver["environment"].get("PIXAV_RESOLVER_REQUIRE_READER_ROLE") != "true":
        raise ValueError("resolver reader startup check is required")
    if resolver["environment"].get("PIXAV_RESOLVER_AUTH_REQUIRED") != "true":
        raise ValueError("private playback authentication is required")
    allowed = {"/app/data/playback-cache", "/run/secrets/resolver_token"}
    if {mount["target"] for mount in resolver.get("volumes", [])} != allowed:
        raise ValueError("resolver must not see staging or guest data")
    origin = services["jellyfin"]
    if origin.get("ports") or "client_capture" in origin.get("networks", {}):
        raise ValueError("Jellyfin origin must be reachable only through the authentication gateway")
    gateway = services.get("jellyfin-client-gateway")
    if gateway is None or "client_capture" not in gateway.get("networks", {}):
        raise ValueError("the Jellyfin authentication gateway must own the client network")
    if "av_capture" in services:
        capture = services["av_capture"]
        expected_binds = {
            "/work": str(root / "evidence/client-capture"),
            "/run/secrets/capture-config.json": str(root / "secrets/capture-config.json"),
        }
        if {mount["target"]: mount.get("source") for mount in capture.get("volumes", [])} != expected_binds:
            raise ValueError("client capture must not see original media or guest data")
        if set(capture.get("networks", {})) != {"client_capture"}:
            raise ValueError("client capture may access only the dedicated Jellyfin network")
        if not config["networks"]["client_capture"].get("internal"):
            raise ValueError("client capture network must not allow external egress")


def _containers(payload: str) -> list[dict]:
    if not payload.strip():
        return []
    try:
        rows = json.loads(payload)
    except json.JSONDecodeError:
        return [json.loads(line) for line in payload.splitlines() if line.strip()]
    return rows if isinstance(rows, list) else [rows]


def configuration_hashes(root: Path) -> dict[str, str]:
    """Bind copied support configuration to the receipt without saving payloads."""
    hashes = {}
    for path in sorted((root / "config").rglob("*")):
        if path.is_symlink():
            raise ValueError("support configuration must not contain symlinks")
        if path.is_file():
            hashes[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def preflight(args: argparse.Namespace) -> dict:  # noqa: C901 -- explicit identity/mount refusals
    if not PROJECT.fullmatch(args.project):
        raise ValueError("an explicit dedicated acceptance project is required")
    env_file = args.env_file.absolute()
    root = args.root.absolute()
    if root.resolve() != root or env_file.resolve() != env_file or not env_file.is_relative_to(root):
        raise ValueError("env file and root must be contained without symlinks")
    if stat.S_IMODE(env_file.stat().st_mode) != 0o600 or stat.S_IMODE(env_file.parent.stat().st_mode) != 0o700:
        raise ValueError("private env file requires 0600 inside a 0700 directory")
    docker = ["docker", "--context", args.context]
    if _run([*docker, "info", "--format", "{{.ID}}"]).strip() != args.daemon_id:
        raise ValueError("Docker daemon identity mismatch")
    compose = [
        *docker,
        "compose",
        "-p",
        args.project,
        "--env-file",
        str(env_file),
        "-f",
        str(COMPOSE),
        "--profile",
        "acceptance-source",
        "--profile",
        "acceptance-av",
        "--profile",
        "acceptance-native-source",
    ]
    config = json.loads(_run([*compose, "config", "--format", "json"]))
    validate_config(config, root)
    images = {}
    for image in sorted({service["image"] for service in config["services"].values()}):
        images[image] = json.loads(_run([*docker, "image", "inspect", image]))[0]["Id"]
    cold = config["services"]["storage_worker"]["environment"]["PIXAV_STORAGE_READBACK_IMAGE"]
    images[cold] = json.loads(_run([*docker, "image", "inspect", cold]))[0]["Id"]
    tools_image = config["services"]["storage_worker"]["environment"]["PIXAV_STORAGE_UPLOAD_TOOLS_IMAGE"]
    images[tools_image] = json.loads(_run([*docker, "image", "inspect", tools_image]))[0]["Id"]
    containers = _containers(_run([*compose, "ps", "--all", "--format", "json"]))
    instance = {}
    observed_services = {}
    for row in containers:
        info = json.loads(_run([*docker, "inspect", row["ID"]]))[0]
        labels = info["Config"]["Labels"]
        if labels.get("com.docker.compose.project") != args.project:
            raise ValueError("container project ownership mismatch")
        files = labels.get("com.docker.compose.project.config_files", "").split(",")
        if files != [str(COMPOSE)]:
            raise ValueError("container was created by another Compose manifest")
        service = row["Service"]
        if service not in config["services"] or service in observed_services:
            raise ValueError("unknown or duplicate acceptance service")
        if info["Image"] != images[config["services"][service]["image"]]:
            raise ValueError("runtime image differs from the fixed manifest")
        observed_services[service] = {
            "container_id": info["Id"],
            "image_id": info["Image"],
            "state": info["State"]["Status"],
            "health": info["State"].get("Health", {}).get("Status"),
        }
        for mount in info.get("Mounts", []):
            if mount["Type"] == "volume" and not mount["Name"].startswith(args.project + "_"):
                raise ValueError("container volume ownership mismatch")
        if row["Service"] == "postgres" and row["State"] == "running":
            observed = _run(
                [
                    *docker,
                    "exec",
                    row["ID"],
                    "psql",
                    "-U",
                    "pixav_admin",
                    "-d",
                    "pixav_acceptance",
                    "-Atqc",
                    "SELECT system_identifier, current_database() FROM pg_control_system()",
                ]
            ).strip()
            identity, database = observed.split("|")
            expected = config["services"]["provision"]["command"][-1]
            if identity != expected or database != "pixav_acceptance":
                raise ValueError("PostgreSQL identity mismatch")
            instance.update(postgres_system_identifier=identity, database=database)
        if row["Service"] == "redis" and row["State"] == "running":
            server = _run([*docker, "exec", row["ID"], "redis-cli", "INFO", "server"])
            instance["redis_run_id"] = next(
                line.split(":", 1)[1] for line in server.splitlines() if line.startswith("run_id:")
            )
    return {
        "schema_version": 1,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "project": args.project,
        "docker_context": args.context,
        "docker_daemon_id": args.daemon_id,
        "compose_sha256": hashlib.sha256(COMPOSE.read_bytes()).hexdigest(),
        "resolved_config_sha256": hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest(),
        "support_configuration_sha256": configuration_hashes(root),
        "images": images,
        "observed_services": observed_services,
        "instance": instance,
        "deployment_contract": "PASS",
        "runtime": "OPEN",
        "golden_path": "OPEN",
        "production_promotion": "OPEN",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("preflight")
    check.add_argument("--context", required=True)
    check.add_argument("--daemon-id", required=True)
    check.add_argument("--project", required=True)
    check.add_argument("--root", required=True, type=Path)
    check.add_argument("--env-file", required=True, type=Path)
    check.add_argument("--output", required=True, type=Path)
    report = commands.add_parser("report")
    report.add_argument("receipt", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "report":
            receipt = json.loads(args.receipt.read_text())
            print(
                json.dumps(
                    {key: receipt[key] for key in ("project", "deployment_contract", "runtime", "golden_path")},
                    indent=2,
                )
            )
        else:
            receipt = preflight(args)
            with create_backup_file(args.output) as handle:
                json.dump(receipt, handle, indent=2)
            print("deployment preflight recorded; runtime and Golden Path remain OPEN")
    except (ValueError, KeyError, OSError, subprocess.SubprocessError):
        print("acceptance preflight refused; no execution or data was changed")
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
