"""Inspect cleanup evidence, authorize, or execute an exact private selection.

files/authorize never unlink media. execute defaults to dry-run; --apply also
requires the deployment deletion switch, owned pause and stopped-downloader
proof. recover reconciles only selected committed intents and never unlinks.
Use the host media-path namespace verified by capture-downloader.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import docker
from pixav.config import get_settings
from pixav.maxwell_core.cleanup_authorization import CleanupAuthorizer
from pixav.maxwell_core.cleanup_operation import CleanupOperator
from pixav.maxwell_core.cleanup_quiescence import StoppedDownloaderGuard, capture_downloader_mount
from pixav.shared.cleanup_operation import (
    CleanupOperationUnconfirmedError,
    CleanupSelection,
    read_cleanup_runtime,
    read_cleanup_selection,
    write_downloader_mount,
)
from pixav.shared.cleanup_recovery import (
    CleanupApprovalError,
    CleanupRuntimeBinding,
    read_cleanup_request,
    validate_cleanup_recovery,
)
from pixav.shared.db import create_pool
from pixav.shared.media_file import file_version, open_media_file
from pixav.shared.redis_client import create_redis
from scripts.manage_system_pause import _decode_record, _matches_owner


def read_maintenance_token(path: Path) -> str:
    try:
        if not path.is_absolute() or os.path.abspath(path) != str(path):
            raise CleanupApprovalError("CLEANUP_PRIVATE_PAUSE_TOKEN_REQUIRED")
        with open_media_file(path) as source:
            info = os.fstat(source.descriptor)
            if (
                stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size > 4096
                or stat.S_IMODE(os.fstat(source.parent_descriptor).st_mode) != 0o700
            ):
                raise CleanupApprovalError("CLEANUP_PRIVATE_PAUSE_TOKEN_REQUIRED")
            with source.reader() as reader:
                token = reader.read(4097).decode().rstrip("\r\n")
            source.assert_current()
            if file_version(info) != file_version(os.fstat(source.descriptor)):
                raise CleanupApprovalError("CLEANUP_PRIVATE_PAUSE_TOKEN_REQUIRED")
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token):
            raise CleanupApprovalError("CLEANUP_PRIVATE_PAUSE_TOKEN_REQUIRED")
        return token
    except CleanupApprovalError:
        raise
    except (OSError, ValueError):
        raise CleanupApprovalError("CLEANUP_PRIVATE_PAUSE_TOKEN_REQUIRED") from None


class LiveCleanupGuard:
    def __init__(self, client: Any, redis: Any, *, pause_key: str, token: str, expected: CleanupRuntimeBinding):
        self.client, self.redis = client, redis
        self.pause_key, self.token, self.expected = pause_key, token, expected

    async def check(self) -> CleanupRuntimeBinding:
        binary = shutil.which("docker")
        if binary is None:
            raise CleanupApprovalError("CLEANUP_DOCKER_IDENTITY_UNAVAILABLE")
        context = await asyncio.to_thread(
            subprocess.run, [binary, "context", "show"], capture_output=True, check=True, timeout=15
        )
        daemon = await asyncio.to_thread(
            subprocess.run, [binary, "info", "--format", "{{.ID}}"], capture_output=True, check=True, timeout=15
        )
        sdk = await asyncio.to_thread(self.client.info)
        redis_info = await self.redis.info("server")
        observed = CleanupRuntimeBinding(
            docker_context=context.stdout.decode().strip(),
            docker_daemon_id=daemon.stdout.decode().strip(),
            redis_run_id=str(redis_info.get("run_id", "")),
        )
        if observed != self.expected or sdk["ID"] != observed.docker_daemon_id:
            raise CleanupApprovalError("CLEANUP_RUNTIME_BINDING_MISMATCH")
        raw = await self.redis.get(self.pause_key)
        if not _matches_owner(_decode_record(raw), self.token, observed.redis_run_id):
            raise CleanupApprovalError("CLEANUP_OWNED_SYSTEM_PAUSE_REQUIRED")
        return observed


async def run(args: argparse.Namespace) -> dict:
    if args.command == "capture-downloader":
        return await observe_downloader(args)
    request = (
        read_cleanup_selection(args.request)
        if args.command in {"execute", "recover"}
        else read_cleanup_request(args.request)
    )
    if args.command == "files":
        if isinstance(request, CleanupSelection):
            raise CleanupApprovalError("CLEANUP_APPROVAL_REQUEST_REQUIRED")
        stamp = await asyncio.to_thread(
            validate_cleanup_recovery,
            request,
            checked_at=datetime.now(timezone.utc),
            allow_operator_exception=args.allow_operator_exception,
        )
        return {
            "result": "FILES_VERIFIED_ONLY",
            "expected_count": 1,
            "authorized_count": 0,
            "recovery_mode": stamp.mode,
            "runtime_checked": False,
            "database_clock_checked": False,
        }
    settings = get_settings()
    target = request.requests[0].target if isinstance(request, CleanupSelection) else request.target
    if settings.db_name != target.database_name:
        raise CleanupApprovalError("CLEANUP_DATABASE_NAME_MISMATCH")
    settings = settings.model_copy(
        update={
            "expected_db_identity": target.database_identifier,
            "expected_db_name": target.database_name,
            "expected_redis_run_id": request.runtime.redis_run_id,
        }
    )
    token = read_maintenance_token(args.maintenance_token_file)
    client = None
    redis = None
    pool = None
    try:
        client = docker.from_env(timeout=15)
        redis = await create_redis(settings)
        guard = LiveCleanupGuard(
            client, redis, pause_key=settings.system_pause_key, token=token, expected=request.runtime
        )
        await guard.check()
        pool = await create_pool(settings)
        if isinstance(request, CleanupSelection):
            operation = CleanupOperator(
                pool,
                StoppedDownloaderGuard(client, guard, request.downloader_mount, Path(settings.download_dir)),
                staging_root=Path(settings.download_dir),
                library_root=Path(settings.library_root),
                apply_allowed=settings.local_cleanup_apply,
                allow_operator_exception=args.allow_operator_exception,
            )
            if args.command == "recover":
                return await operation.recover(request, apply=args.apply)
            return await operation.execute(request, apply=args.apply)
        service = CleanupAuthorizer(
            pool,
            guard,
            staging_root=Path(settings.download_dir),
            library_root=Path(settings.library_root),
            allow_operator_exception=args.allow_operator_exception,
        )
        return await service.authorize(request, apply=args.apply)
    finally:
        try:
            if pool is not None:
                await pool.close()
        finally:
            try:
                if redis is not None:
                    await redis.aclose()
            finally:
                if client is not None:
                    client.close()


async def observe_downloader(args: argparse.Namespace) -> dict:
    runtime = read_cleanup_runtime(args.runtime_file)
    settings = get_settings().model_copy(update={"expected_redis_run_id": runtime.redis_run_id})
    token = read_maintenance_token(args.maintenance_token_file)
    client = None
    redis = None
    try:
        client = docker.from_env(timeout=15)
        redis = await create_redis(settings)
        guard = LiveCleanupGuard(client, redis, pause_key=settings.system_pause_key, token=token, expected=runtime)
        await guard.check()
        proof = await asyncio.to_thread(
            capture_downloader_mount,
            client,
            args.container_id,
            args.mount_destination,
            Path(settings.download_dir),
            runtime,
        )
        if await guard.check() != runtime:
            raise CleanupApprovalError("CLEANUP_RUNTIME_CHANGED")
        reference = write_downloader_mount(args.output, proof)
        return {
            "result": "DOWNLOADER_MOUNT_OBSERVED",
            "receipt_sha256": reference.sha256,
            "receipt_size_bytes": reference.size_bytes,
            "quiescence_verified": False,
        }
    finally:
        try:
            if redis is not None:
                await redis.aclose()
        finally:
            if client is not None:
                client.close()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    actions = result.add_subparsers(dest="command", required=True)
    for command in ("files", "authorize", "execute", "recover"):
        child = actions.add_parser(command)
        child.add_argument("--request", type=Path, required=True)
        child.add_argument("--allow-operator-exception", action="store_true")
        if command != "files":
            child.add_argument("--maintenance-token-file", type=Path, required=True)
            child.add_argument("--apply", action="store_true")
    capture = actions.add_parser("capture-downloader")
    capture.add_argument("--runtime-file", type=Path, required=True)
    capture.add_argument("--maintenance-token-file", type=Path, required=True)
    capture.add_argument("--container-id", required=True)
    capture.add_argument("--mount-destination", required=True)
    capture.add_argument("--output", type=Path, required=True)
    return result


def main() -> None:
    args = parser().parse_args()
    try:
        result = asyncio.run(run(args))
    except Exception as error:
        code = str(error) if isinstance(error, CleanupApprovalError) else "CLEANUP_AUTHORIZATION_UNCONFIRMED"
        if isinstance(error, CleanupOperationUnconfirmedError):
            print(json.dumps({"result": "PARTIAL_OR_UNCONFIRMED", "code": code, "reconciliation_required": True}))
        else:
            print(json.dumps({"result": "REFUSED", "code": code}))
        raise SystemExit(2) from None
    print(json.dumps(result))
    if result.get("result") in {"REFUSED", "PARTIAL_OR_REFUSED"}:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
