#!/usr/bin/env python3
"""Inspect and safely own/release the global worker pause in Redis."""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from pixav.config import get_settings
from pixav.shared.pause import is_paused_value
from pixav.shared.redis_client import create_redis


def _decode_record(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    try:
        payload = json.loads(str(raw))
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


async def _run(command: str, *, reason: str, token: str) -> int:
    settings = get_settings()
    redis = await create_redis(settings)
    try:
        raw = await redis.get(settings.system_pause_key)
        info = await redis.info("server")
        run_id = str(info.get("run_id", ""))

        if command == "status":
            record = _decode_record(raw)
            print(
                json.dumps(
                    {
                        "key": settings.system_pause_key,
                        "paused": is_paused_value(raw),
                        "owned": bool(record and record.get("owner") == "pixav-phase0"),
                        "reason": record.get("reason") if record else None,
                        "token": record.get("token") if record else None,
                        "redis_run_id": run_id,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0

        if command == "pause":
            if raw is not None:
                raise RuntimeError("system pause already exists; refusing to overwrite another operator's value")
            owned_token = token or uuid.uuid4().hex
            record = {
                "owner": "pixav-phase0",
                "paused": True,
                "paused_at": datetime.now(timezone.utc).isoformat(),
                "reason": reason or "Phase 0 controlled maintenance",
                "redis_run_id": run_id,
                "token": owned_token,
            }
            created = await redis.set(settings.system_pause_key, json.dumps(record, sort_keys=True), nx=True)
            if not created:
                raise RuntimeError("system pause changed concurrently; nothing was overwritten")
            print(json.dumps(record, indent=2, sort_keys=True))
            return 0

        record = _decode_record(raw)
        if not token:
            raise RuntimeError("resume requires --token from the matching pause operation")
        if not record or record.get("owner") != "pixav-phase0" or record.get("token") != token:
            raise RuntimeError("refusing to remove an unowned or token-mismatched system pause")
        deleted = int(await redis.delete(settings.system_pause_key))
        if deleted != 1:
            raise RuntimeError("system pause changed before resume; nothing was removed")
        print(json.dumps({"paused": False, "released_token": token}, indent=2, sort_keys=True))
        return 0
    except RuntimeError as exc:
        print(json.dumps({"status": "refused", "reason": str(exc)}, indent=2, sort_keys=True))
        return 2
    finally:
        await redis.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("status", "pause", "resume"))
    parser.add_argument("--reason", default="")
    parser.add_argument("--token", default="")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_run(args.command, reason=args.reason, token=args.token)))


if __name__ == "__main__":
    main()
