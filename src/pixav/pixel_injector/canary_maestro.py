"""Private, bounded Maestro invocation for calibrated Photos canary flows."""

from __future__ import annotations

import base64
import uuid
from pathlib import Path
from typing import Any

from pixav.pixel_injector.canary import OWNER_LABEL, CanaryBlockedError


def run_flow(runner: Any, owner: str, flow: Path, *, credentials: dict[str, str] | None = None) -> None:
    """Use subprocess environment for credentials; discard private login traces."""
    if runner.labels.get(OWNER_LABEL) != owner or runner.labels.get("pixav.photos_canary.role") != "tools":
        raise CanaryBlockedError("Maestro runner ownership mismatch")
    name = "canary-flow-" + uuid.uuid4().hex
    root = "/tmp/" + name  # noqa: S108 - exclusive 0700 directory in owned container tmpfs
    environment = {
        "HOME": root,
        "JAVA_TOOL_OPTIONS": "-Duser.home=" + root,
        "MAESTRO_CLI_NO_ANALYTICS": "true",
        "MAESTRO_DISABLE_UPDATE_CHECK": "true",
        "MAESTRO_CLI_ANALYSIS_NOTIFICATION_DISABLED": "true",
    }
    if credentials:
        environment.update({"MAESTRO_EMAIL": credentials["email"], "MAESTRO_PASSWORD": credentials["password"]})
    previous_timeout = runner.client.api.timeout
    runner.client.api.timeout = 150
    staged_ok = False
    try:
        staged = runner.exec_run(
            [
                "python",
                "-c",
                "import os,sys,base64; p=sys.argv[1]; os.mkdir(p,0o700); "
                "fd=os.open(p+'/flow.yaml',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600); "
                "os.write(fd,base64.b64decode(os.environ['CANARY_FLOW'])); os.close(fd)",
                root,
            ],
            environment={"CANARY_FLOW": base64.b64encode(flow.read_bytes()).decode("ascii")},
        )
        if staged.exit_code != 0:
            raise CanaryBlockedError("could not stage private Maestro flow")
        staged_ok = True
        result = runner.exec_run(
            [
                "timeout",
                "--kill-after=5s",
                "90",
                "maestro",
                "--device",
                "127.0.0.1:5555",
                "test",
                "--test-output-dir",
                root + "/output",
                root + "/flow.yaml",
            ],
            environment=environment,
            workdir=root,
        )
        # Maestro may echo interpolated inputText into output or logs. Never return it.
        if result.exit_code != 0:
            raise CanaryBlockedError("Maestro flow failed or timed out; inspect UI before retry")
    finally:
        # Exact per-call generated directory, never the session or Android data.
        try:
            if staged_ok:
                removed = runner.exec_run(["rm", "-rf", "--", root])
                if removed.exit_code != 0:
                    raise CanaryBlockedError("private Maestro trace cleanup failed; review owned tools runtime")
        finally:
            runner.client.api.timeout = previous_timeout


def hierarchy(runner: Any, owner: str) -> list[dict[str, str]]:
    """Read UI attributes in memory; never persist account screens or raw CLI output."""
    import json

    if runner.labels.get(OWNER_LABEL) != owner or runner.labels.get("pixav.photos_canary.role") != "tools":
        raise CanaryBlockedError("Maestro runner ownership mismatch")
    root = "/tmp/canary-inspect-" + uuid.uuid4().hex  # noqa: S108 - exclusive owned tmpfs directory
    previous_timeout = runner.client.api.timeout
    runner.client.api.timeout = 150
    created = False
    try:
        result = runner.exec_run(["mkdir", "-m", "700", root])
        if result.exit_code:
            raise CanaryBlockedError("could not create private hierarchy directory")
        created = True
        result = runner.exec_run(
            ["timeout", "--kill-after=5s", "90", "maestro", "--device", "127.0.0.1:5555", "hierarchy"],
            environment={
                "HOME": root,
                "JAVA_TOOL_OPTIONS": "-Duser.home=" + root,
                "MAESTRO_CLI_NO_ANALYTICS": "true",
                "MAESTRO_DISABLE_UPDATE_CHECK": "true",
            },
            workdir=root,
        )
        if result.exit_code:
            raise CanaryBlockedError("hierarchy unavailable; UI review required")
        raw = result.output.decode()
        tree = json.JSONDecoder().raw_decode(raw[raw.index("{") :])[0]
        attributes: list[dict[str, str]] = []

        def walk(node: dict[str, Any]) -> None:
            attributes.append(node.get("attributes", {}))
            for child in node.get("children", []):
                walk(child)

        walk(tree)
        return attributes
    finally:
        try:
            if created and runner.exec_run(["rm", "-rf", "--", root]).exit_code:
                raise CanaryBlockedError("private hierarchy cleanup failed")
        finally:
            runner.client.api.timeout = previous_timeout
