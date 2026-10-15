"""Dry-run by default; enroll an existing, freshly verified Windows runtime.

The private manifest selects an exact generation, account, tools object and
staging bind. Applying writes only its registry and immutable audit in one
transaction. No guest, container, account login, queue or execution is started.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import stat
from pathlib import Path

import docker
from pixav.config import get_settings
from pixav.pixel_injector.windows_control import WindowsHostClient
from pixav.pixel_injector.windows_registration import RegistrationError, WindowsRegistration, WindowsRuntimeRegistry
from pixav.shared.db import create_pool


def read_manifest(path: Path) -> WindowsRegistration:
    if (
        path.resolve() != path
        or not path.is_file()
        or stat.S_IMODE(path.stat().st_mode) != 0o600
        or stat.S_IMODE(path.parent.stat().st_mode) != 0o700
    ):
        raise RegistrationError("WINDOWS_MANIFEST_REQUIRES_PRIVATE_FILE")
    return WindowsRegistration.model_validate_json(path.read_text(encoding="utf-8-sig"))


async def run(args: argparse.Namespace) -> dict:
    spec = read_manifest(args.manifest)
    settings = get_settings()
    pool = await create_pool(settings)
    client = None
    control = None
    try:
        client = docker.from_env(timeout=20)
        control = WindowsHostClient(
            settings.windows_control_url,
            ca=Path(settings.windows_control_ca),
            certificate=Path(settings.windows_control_certificate),
            key=Path(settings.windows_control_key),
        )
        registry = WindowsRuntimeRegistry(pool, client, control, db_identity=args.db_identity, database=args.database)
        return await registry.enroll(spec, apply=args.apply, operator=args.operator, reason=args.reason)
    finally:
        if control is not None:
            control.close()
        if client is not None:
            client.close()
        await pool.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--db-identity", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--operator", default="")
    parser.add_argument("--reason", default="")
    args = parser.parse_args()
    try:
        result = asyncio.run(run(args))
    except Exception as error:
        # Adapter/validation/DB errors can contain private manifest/connection data.
        code = str(error) if isinstance(error, RegistrationError) else "WINDOWS_REGISTRATION_UNCONFIRMED"
        print(json.dumps({"result": "REFUSED", "code": code}))
        raise SystemExit(2) from None
    print(json.dumps(result))


if __name__ == "__main__":
    main()
