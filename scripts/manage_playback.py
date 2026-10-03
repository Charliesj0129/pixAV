#!/usr/bin/env python3
"""Inspect playback readiness and record end-to-end stream verification.

``status`` reads PlayableAsset facts and any pending cache demand. ``verify``
runs the stable-URL acceptance against a running resolver: HEAD, a complete GET
hashed against the READY record, first/middle/suffix ranges compared with that
same body, and 416. Only when every check passes does it record
``playback_verified_at``. HTTP checks alone never satisfy cleanup: it additionally
requires a matching ``jellyfin-client-v1`` attestation and private recording.
Verification names an operator and a reason,
and refuses a different PostgreSQL cluster.

A cold object answers 503 while it is rebuilt from Google Photos; verification
then fails without recording anything and can simply be run again.

Nothing here prints a credential, a share URL or a local path: playable assets
hold identities, sizes, digests and states only, and evidence is never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

import httpx

from pixav.config import get_settings
from pixav.shared.client_playback_evidence import ClientPlaybackEvidence
from pixav.shared.db import create_pool
from pixav.shared.instance import database_identity
from pixav.shared.playable_assets import PlayableAssetRepository
from pixav.strm_resolver.movie_acceptance import stable_acceptance

VERIFICATION_METHOD = "stable-url-acceptance-v1"

_STATUS_SQL = """
SELECT p.id, p.video_id, p.remote_asset_id, p.preparation_version, p.state, p.size_bytes, p.sha256,
       p.ready_at, p.materialized_at, p.playback_verified_at, p.invalidated_reason, p.updated_at,
       d.requested_at AS demand_at, d.request_count
  FROM playable_assets p
  LEFT JOIN playback_cache_requests d ON d.playable_asset_id = p.id
 WHERE $1::uuid IS NULL OR p.video_id = $1
 ORDER BY p.updated_at DESC
 LIMIT $2
"""


def _render(row: Any) -> dict:
    return {key: (None if value is None else str(value)) for key, value in dict(row).items()}


async def _run(args: argparse.Namespace) -> int:
    if args.command != "status" and not args.db_identity:
        print("refused: --db-identity is required for recording verification")
        return 2
    settings = get_settings()
    pool = await create_pool(settings)
    try:
        identity = await database_identity(pool)
        if args.db_identity and args.db_identity != identity:
            print(f"refusing: database identity is {identity}, expected {args.db_identity}")
            return 2
        if args.command == "status":
            rows = await pool.fetch(_STATUS_SQL, args.video_id, args.limit)
            print(json.dumps({"database_identity": identity, "playable_assets": [_render(r) for r in rows]}, indent=2))
            return 0
        print(f"database identity: {identity}")
        return await _verify(args, PlayableAssetRepository(pool))
    finally:
        await pool.close()


async def _verify(args: argparse.Namespace, repository: PlayableAssetRepository) -> int:
    asset = await repository.eligible_for_video(args.video_id)
    if asset is None or asset.size_bytes is None or asset.sha256 is None:
        print(f"refused: video {args.video_id} has no READY playable object on a DURABLE copy")
        return 2
    try:
        options = {}
        if getattr(args, "token_file", None):
            options["bearer_token"] = args.token_file.read_text().strip()
        result = await stable_acceptance(
            args.base_url.rstrip("/"), str(args.video_id), asset.size_bytes, asset.sha256, **options
        )
    except (ValueError, OSError, httpx.HTTPError) as exc:
        print(f"stream verification failed, nothing recorded: {type(exc).__name__}")
        return 1
    evidence = {**result, "method": VERIFICATION_METHOD, "operator": args.operator, "reason": args.reason}
    client_path = getattr(args, "client_evidence", None)
    recording = getattr(args, "recording", None)
    if bool(client_path) != bool(recording):
        print("refused: client evidence and private recording must be supplied together")
        return 2
    if client_path:
        try:
            evidence["client_playback"] = _client_report(client_path, recording, asset.model_dump(), args.operator)
        except (ValueError, OSError):
            print("refused: client evidence or recording does not match; nothing recorded")
            return 2
    if not await repository.record_playback_verification(asset.id, evidence):
        print("refused: the READY object changed while it was being verified; nothing recorded")
        return 2
    print(
        f"HTTP verified for video {args.video_id} (playable asset {asset.id}); client attestation: {bool(client_path)}"
    )
    return 0


def _client_report(client_path: Path, recording: Path, asset: dict, operator: str) -> dict:
    client = ClientPlaybackEvidence.model_validate_json(Path(client_path).read_text())
    digest = hashlib.sha256()
    with Path(recording).open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    if not client.matches(asset) or client.operator != operator:
        raise ValueError("client evidence identity mismatch")
    if digest.hexdigest() != client.recording_sha256:
        raise ValueError("recording digest mismatch")
    return client.model_dump(mode="json")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-identity", default=None, help="refuse to act on a different PostgreSQL cluster")
    commands = parser.add_subparsers(dest="command", required=True)

    status = commands.add_parser("status", help="show playable assets, newest first")
    status.add_argument("--video-id", type=uuid.UUID, default=None)
    status.add_argument("--limit", type=int, default=50)

    verify = commands.add_parser("verify", help="accept the stable stream URL and record playback evidence")
    verify.add_argument("video_id", type=uuid.UUID)
    verify.add_argument("--base-url", default=get_settings().resolver_base_url, help="the resolver players use")
    verify.add_argument("--operator", required=True, help="who ran the verification")
    verify.add_argument("--reason", required=True, help="why, recorded with the evidence")
    verify.add_argument("--client-evidence", type=Path, help="private jellyfin-client-v1 operator attestation JSON")
    verify.add_argument("--recording", type=Path, help="private audiovisual recording matching the attestation digest")
    verify.add_argument("--token-file", type=Path, help="private resolver service token (never placed in the URL)")

    raise SystemExit(asyncio.run(_run(parser.parse_args())))


if __name__ == "__main__":
    main()
