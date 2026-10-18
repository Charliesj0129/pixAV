#!/usr/bin/env python3
"""Inspect and safely own/release the global worker pause in Redis."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from redis.exceptions import RedisError

from pixav.config import get_settings
from pixav.shared.pause import is_paused_value
from pixav.shared.redis_client import create_redis

_INSTANCE_GUARD = """
if string.match(redis.call('INFO', 'server'), 'run_id:([0-9a-f]+)') ~= ARGV[2] then return -1 end
"""
_PAUSE = _INSTANCE_GUARD + """
if redis.call('SET', KEYS[1], ARGV[1], 'NX') then return 1 else return 0 end
"""
_RESUME = _INSTANCE_GUARD + """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
return redis.call('DEL', KEYS[1])
"""


class PauseRefusedError(RuntimeError):
    """A fixed refusal code safe to emit without external values."""


def _decode_record(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _matches_owner(record: dict[str, Any] | None, token: str, run_id: str) -> bool:
    if not record or record.get("owner") != "pixav-phase0" or record.get("redis_run_id") != run_id:
        return False
    if not token or record.get("paused") is not True:
        return False
    expected = record.get("owner_sha256")
    observed = hashlib.sha256(token.encode()).hexdigest()
    if expected is None:
        expected, observed = record.get("token"), token
    return isinstance(expected, str) and hmac.compare_digest(expected.encode(), observed.encode())


def _read_token(path: Path, *, create: bool = False) -> str:
    if (
        not path.is_absolute()
        or path.resolve() != path
        or not path.parent.is_dir()
        or stat.S_IMODE(path.parent.stat().st_mode) != 0o700
        or path.parent.stat().st_uid != os.geteuid()
    ):
        raise PauseRefusedError("PRIVATE_PAUSE_TOKEN_DIRECTORY_REQUIRED")
    if create and not path.exists():
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(secrets.token_urlsafe(48) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor) as handle:
        info = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != os.geteuid()
            or info.st_size > 4096
        ):
            raise PauseRefusedError("PRIVATE_PAUSE_TOKEN_FILE_REQUIRED")
        token = handle.read().rstrip("\r\n")
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token):
        raise PauseRefusedError("PRIVATE_PAUSE_TOKEN_INVALID")
    return token


def _refused(code: str) -> int:
    print(json.dumps({"status": "refused", "code": code}, sort_keys=True))
    return 2


def _require_applied(result: int) -> None:
    if result != 1:
        raise PauseRefusedError(
            {-1: "REDIS_INSTANCE_BINDING_CHANGED", 0: "SYSTEM_PAUSE_CHANGED"}.get(result, "PAUSE_OPERATION_UNCONFIRMED")
        )


def _validate_request(command: str, expected: str, token: str) -> None:
    if command not in {"status", "pause", "resume"}:
        raise PauseRefusedError("UNKNOWN_PAUSE_COMMAND")
    if command == "status":
        return
    if not re.fullmatch(r"[a-f0-9]{40}", expected):
        raise PauseRefusedError("REDIS_INSTANCE_BINDING_REQUIRED")
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token):
        raise PauseRefusedError("PRIVATE_PAUSE_TOKEN_REQUIRED")


async def _run(command: str, *, reason: str, token: str) -> int:
    redis = None
    try:
        settings = get_settings()
        expected = settings.expected_redis_run_id
        _validate_request(command, expected, token)
        redis = await create_redis(settings)
        info = await redis.info("server")
        run_id = str(info.get("run_id", ""))
        if expected and expected != run_id:
            raise PauseRefusedError("REDIS_INSTANCE_BINDING_CHANGED")
        raw = await redis.get(settings.system_pause_key)
        record = _decode_record(raw)

        if command == "status":
            print(
                json.dumps(
                    {
                        "key": settings.system_pause_key,
                        "paused": is_paused_value(raw),
                        "owned": _matches_owner(record, token, run_id),
                        "owner_recognized": bool(record and record.get("owner") == "pixav-phase0"),
                        "reason_present": bool(record and record.get("reason")),
                        "token_present": bool(record and (record.get("owner_sha256") or record.get("token"))),
                        "redis_run_id": run_id,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0

        if command == "pause":
            if raw is not None:
                raise PauseRefusedError("SYSTEM_PAUSE_ALREADY_EXISTS")
            record = {
                "owner": "pixav-phase0",
                "paused": True,
                "paused_at": datetime.now(timezone.utc).isoformat(),
                "reason": reason or "Phase 0 controlled maintenance",
                "redis_run_id": run_id,
                "owner_sha256": hashlib.sha256(token.encode()).hexdigest(),
            }
            created = int(
                await redis.eval(_PAUSE, 1, settings.system_pause_key, json.dumps(record, sort_keys=True), run_id)
            )
            _require_applied(created)
            print(json.dumps({"paused": True, "owned": True, "redis_run_id": run_id}, sort_keys=True))
            return 0

        if not _matches_owner(record, token, run_id):
            raise PauseRefusedError("SYSTEM_PAUSE_OWNER_MISMATCH")
        deleted = int(await redis.eval(_RESUME, 1, settings.system_pause_key, raw, run_id))
        _require_applied(deleted)
        print(json.dumps({"paused": False, "owned_pause_released": True, "redis_run_id": run_id}, sort_keys=True))
        return 0
    except PauseRefusedError as exc:
        return _refused(str(exc))
    except (OSError, ValueError, RedisError, RuntimeError):
        return _refused("PAUSE_OPERATION_UNCONFIRMED")
    finally:
        if redis is not None:
            await redis.aclose()


def main() -> None:
    if any(arg == "--token" or arg.startswith("--token=") for arg in sys.argv[1:]):
        raise SystemExit(_refused("INLINE_PAUSE_TOKEN_REFUSED_USE_PRIVATE_FILE"))
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("command", choices=("status", "pause", "resume"))
    parser.add_argument("--reason", default="")
    parser.add_argument("--token-file", type=Path)
    args = parser.parse_args()
    try:
        if args.command != "status" and args.token_file is None:
            raise PauseRefusedError("PRIVATE_PAUSE_TOKEN_FILE_REQUIRED")
        token = _read_token(args.token_file, create=args.command == "pause") if args.token_file else ""
    except PauseRefusedError as exc:
        raise SystemExit(_refused(str(exc))) from None
    except (OSError, ValueError):
        raise SystemExit(_refused("PRIVATE_PAUSE_TOKEN_FILE_UNCONFIRMED")) from None
    try:
        result = asyncio.run(asyncio.wait_for(_run(args.command, reason=args.reason, token=token), timeout=15))
    except (OSError, ValueError, RedisError, RuntimeError):
        result = _refused("PAUSE_OPERATION_UNCONFIRMED")
    raise SystemExit(result)


if __name__ == "__main__":
    main()
