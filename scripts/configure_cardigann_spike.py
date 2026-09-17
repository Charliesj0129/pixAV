#!/usr/bin/env python3
"""Install the fixture-backed definition and configure only the dedicated Jackett."""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import subprocess
import tarfile
from pathlib import Path

import httpx

from pixav.shared.cookies import parse_cookie_header
from scripts.backup_files import create_backup_file

CONTAINER = "pixav-cardigann-jackett-1"


def docker(*args: str, payload: bytes | None = None) -> bytes:
    result = subprocess.run(  # noqa: S603 -- all callers use the fixed isolated container
        ["docker", *args],  # noqa: S607 -- trusted Docker CLI
        input=payload,
        capture_output=True,
        check=False,
        timeout=60,
    )
    if result.returncode:
        raise RuntimeError("isolated Docker operation failed; details withheld")
    return result.stdout


async def configure(output: Path) -> dict:
    info = json.loads(docker("inspect", CONTAINER))[0]
    if info["Config"]["Labels"].get("com.docker.compose.project") != "pixav-cardigann":
        raise RuntimeError("refusing to configure a different Compose project")
    config = json.loads(docker("exec", CONTAINER, "cat", "/config/Jackett/ServerConfig.json"))
    raw_cookie = docker("exec", CONTAINER, "cat", "/run/pixav-secrets/sehuatang_cookie").decode()
    cookies = parse_cookie_header("\n".join(line for line in raw_cookie.splitlines() if not line.startswith("#")))
    if not cookies:
        raise RuntimeError("secret-mounted cookie is empty")
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    with create_backup_file(output / "api-key") as handle:
        handle.write(config["APIKey"])
    with create_backup_file(output / "server-config-before.json") as handle:
        json.dump(config, handle)
    config.update(FlareSolverrUrl="http://flaresolverr:8191", CacheEnabled=False, UpdateDisabled=True)
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        for name, raw in (
            ("Jackett/ServerConfig.json", json.dumps(config).encode()),
            ("cardigann/definitions/sehuatang-pixav.yml", Path("config/cardigann/sehuatang-pixav.yml").read_bytes()),
        ):
            entry = tarfile.TarInfo(name)
            entry.size, entry.mode, entry.uid, entry.gid = len(raw), 0o600, 1000, 1000
            tar.addfile(entry, io.BytesIO(raw))
    docker("stop", CONTAINER)
    try:
        docker("cp", "-", CONTAINER + ":/config", payload=archive.getvalue())
    finally:
        docker("start", CONTAINER)
    async with httpx.AsyncClient(timeout=180, follow_redirects=True) as client:
        for attempt in range(30):
            try:
                response = await client.get("http://127.0.0.1:19117/UI/Login", timeout=5)
                response.raise_for_status()
                break
            except httpx.HTTPError:
                if attempt == 29:
                    raise RuntimeError("isolated Jackett readiness failed") from None
                await asyncio.sleep(1)
        url = "http://127.0.0.1:19117/api/v2.0/indexers/sehuatang-pixav/config"
        response = await client.get(url)
        response.raise_for_status()
        fields = response.json()
        if not isinstance(fields, list) or not any(field.get("id") == "cookie" for field in fields):
            raise RuntimeError("Jackett config contract changed")
        for field in fields:
            if field["id"] == "cookie":
                field["value"] = "; ".join(f"{key}={value}" for key, value in cookies.items())
        response = await client.post(url, json=fields)
        return {
            "configured": response.status_code == 204,
            "http_status": response.status_code,
            "project": "pixav-cardigann",
            "cache_enabled": False,
            "promoted": False,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="new private directory under .verify/ or backups/")
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:
        result = asyncio.run(configure(args.output))
    except Exception:
        result = {"configured": False, "status": "BLOCKED", "reason": "isolated_configuration_failed"}
    args.output.mkdir(mode=0o700, parents=True, exist_ok=True)
    with create_backup_file(args.output / "evidence.json") as handle:
        json.dump(result, handle, indent=2)
    print(json.dumps(result, indent=2))
    if not result["configured"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
