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


def preflight(args: argparse.Namespace) -> dict:
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
        "windows-tools",
    ]
    config = json.loads(run([*compose, "config", "--format", "json"]))
    if config.get("name") != args.project:
        raise ValueError("MANAGED_PROJECT_BINDING_CHANGED")
    validate_tools(config)
    run([*docker, "image", "inspect", config["services"]["windows_tools"]["image"], "--format", "{{.Id}}"])
    active = set()
    for label in ["pixav.runtime.role=android-dev-tools", "pixav.photos_canary.role=tools"]:
        active.update(run([*docker, "ps", "--quiet", "--no-trunc", "--filter", "label=" + label]).splitlines())
    for identifier in active:
        labels = json.loads(run([*docker, "inspect", identifier, "--format", "{{json .Config.Labels}}"]))
        if labels.get("com.docker.compose.project") != args.project:
            raise ValueError("PREVIOUS_ANDROID_TOOLS_STILL_RUNNING")
    return {
        "stage": "windows-tools",
        "eligible": True,
        "profile_services": 1,
        "daemon_identity_bound": True,
        "immutable_image": True,
        "containers_started": 0,
        "database_touched": False,
        "golden_path": "OPEN",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--daemon-id", required=True)
    parser.add_argument("--env-file", type=Path, required=True)
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
