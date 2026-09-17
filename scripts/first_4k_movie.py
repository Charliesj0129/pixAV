#!/usr/bin/env python3
"""Isolated single-film flow. PostgreSQL is execution truth; no production queues."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import shlex
import sys
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import asyncpg
import redis.asyncio as aioredis

import docker

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pixav.config import get_settings
from pixav.maxwell_core.scheduler import LruAccountScheduler
from pixav.media_loader.qbittorrent import (
    QBitClient,
    extract_info_hash,
    parse_extra_trackers,
    reject_watermark_hash,
)
from pixav.media_loader.remuxer import select_media_input
from pixav.media_loader.video_parts import (
    TARGET_BYTES,
    IncompatibleMediaError,
    MediaOperationError,
    PartMedia,
    contained_file,
    disk_budget,
    monitored_run,
    require_space,
    sha256,
)
from pixav.pixel_injector.canary import (
    OWNER_LABEL,
    CanaryBlockedError,
    private_directory,
    read_private,
    single_flight,
    write_private,
)
from pixav.pixel_injector.canary_acceptance import collect_item, quota_verdict
from pixav.pixel_injector.maestro_parts import MaestroPartUploader, MaestroPartVerifier, UserActionRequiredError
from pixav.pixel_injector.profiles import get_profile
from pixav.pixel_injector.session import RedroidSession
from pixav.shared.disk import DownloadSpaceGuard
from pixav.shared.exceptions import SourceUnavailableError
from pixav.shared.models import Account, Video, VideoPart
from pixav.shared.repository import VideoRepository
from pixav.shared.video_parts import VideoPartRepository
from pixav.sht_probe.scoring import QualityScorer
from pixav.strm_resolver.movie_acceptance import range_acceptance, vlc_acceptance
from scripts.cardigann_spike import collect, collect_boards
from scripts.first_4k_contracts import RunHeartbeat, read_status, resolve_configuration
from scripts.first_4k_recovery import automation_verdict, drill, reconcile, retained
from scripts.instance_guard import database_identity, redis_identity
from scripts.migrate import run_migrations

PROJECT = "pixav-first-4k"
ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / ".verify/first-4k"
DSN = "postgresql://pixav_test:first-4k-isolated@127.0.0.1:25432/pixav_first_4k"
REDIS = "redis://127.0.0.1:26379/0"
TOOLS = "pixav-first-4k-tools:1"
MEDIA = "pixav-photos-canary:maestro-2.10.0"
# Bumped whenever the selection policy changes, so an in-flight run is
# recognised as stale instead of silently reusing the old board's shortlist.
SELECTION_VERSION = 3
SUCCESS_STATUSES = {
    "PREFLIGHT_READY",
    "BOARDS_READY",
    "RESET_COMPLETE",
    "LIVE_VERIFIED",
    "PLAYABLE_QUOTA_WAIT",
    "STATUS",
    "NO_RUN",
    "RECOVERY_DRILL_COMPLETE",
}


class MovieTorrent(QBitClient):
    """Apply the existing disk latch to each isolated qBit progress observation."""

    check: Callable[[], None] | None = None
    progress: Callable[[], None] | None = None
    _owned_hash: str | None = None

    async def stop_owned(self) -> None:
        if self._owned_hash:
            response = await self._request("POST", "/api/v2/torrents/stop", data={"hashes": self._owned_hash})
            if response.status_code == 404:
                response = await self._request("POST", "/api/v2/torrents/pause", data={"hashes": self._owned_hash})
            response.raise_for_status()

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        try:
            if exc_type is not None:
                await asyncio.wait_for(self.stop_owned(), 15)
        finally:
            await super().__aexit__(exc_type, exc_val, exc_tb)

    async def _enforce_size_limit(self, info: Any, torrent_hash: str) -> None:
        if info.get("hash") != torrent_hash or str(info.get("save_path", "")).rstrip("/") != self._download_dir.rstrip(
            "/"
        ):
            raise CanaryBlockedError("torrent identity or owned download path changed")
        self._owned_hash = torrent_hash
        if self.check:
            self.check()
        await super()._enforce_size_limit(info, torrent_hash)
        # amount_left is conservative for preallocated files; completed bytes
        # are already charged to this filesystem and are not reserved again.
        remaining = int(info.get("amount_left") or 0)
        if not info.get("total_size"):
            remaining = self._max_download_bytes or 0
        require_space([(WORK / "downloads" / torrent_hash, remaining)])
        if self.progress:
            self.progress()
        redis = aioredis.from_url(REDIS)
        try:
            guard = DownloadSpaceGuard(
                redis,
                path=str(WORK / "downloads"),
                pause_key="first-4k:disk:pause",
                min_free_bytes=100 * 1024**3,
                min_free_percent=10,
            )
            if (await guard.check_and_latch()).paused:
                stopped = await self._request("POST", "/api/v2/torrents/stop", data={"hashes": torrent_hash})
                if stopped.status_code == 404:
                    stopped = await self._request("POST", "/api/v2/torrents/pause", data={"hashes": torrent_hash})
                stopped.raise_for_status()
                raise CanaryBlockedError("isolated disk pause latched; torrent stopped and retained")
        finally:
            await redis.aclose()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def container(client: Any, service: str) -> Any:
    item = client.containers.get(f"{PROJECT}-{service}-1")
    if (
        item.labels.get("com.docker.compose.project") != PROJECT
        or item.labels.get("com.docker.compose.service") != service
    ):
        raise CanaryBlockedError("isolated Compose identity mismatch")
    return item


async def preflight(client: Any, args: argparse.Namespace) -> dict:
    for name in ("downloads", "parts", "playback", "evidence", "guest"):
        private_directory(WORK / name)
    db = container(client, "postgres")
    redis_container = container(client, "redis")
    for service, name in ((db, "movie_postgres"), (redis_container, "movie_redis")):
        mounts = [m for m in service.attrs["Mounts"] if m.get("Type") == "volume"]
        if len(mounts) != 1 or mounts[0].get("Name") != f"{PROJECT}_{name}":
            raise CanaryBlockedError("database persistence is not owned by the isolated project")
    for service, destination in (("qbittorrent", "/downloads"), ("qbittorrent", "/config")):
        item = container(client, service)
        mounts = [m for m in item.attrs["Mounts"] if m["Destination"] == destination]
        expected = WORK / ("downloads" if destination == "/downloads" else "qbit-config")
        if len(mounts) != 1 or Path(mounts[0]["Source"]) != expected:
            raise CanaryBlockedError("qBit mount is not the isolated owned directory")
    conn = await asyncpg.connect(DSN)
    redis = aioredis.from_url(REDIS)
    try:
        identity = await database_identity(conn)
        observed = db.exec_run(
            [
                "psql",
                "-U",
                "pixav_test",
                "-d",
                "pixav_first_4k",
                "-Atc",
                "SELECT system_identifier FROM pg_control_system()",
            ]
        )
        if (
            observed.exit_code
            or identity != observed.output.decode().strip()
            or await conn.fetchval("SELECT current_database()") != "pixav_first_4k"
        ):
            raise CanaryBlockedError("PostgreSQL instance identity mismatch")
        redis_id = await redis_identity(redis)
        observed_redis = redis_container.exec_run(["redis-cli", "INFO", "server"])
        if observed_redis.exit_code or f"run_id:{redis_id}" not in observed_redis.output.decode():
            raise CanaryBlockedError("Redis instance identity mismatch")
    finally:
        await redis.aclose()
        await conn.close()
    profile = get_profile("gphotos_pixel_xl_v1", path=ROOT / "config/android_profiles.yml")
    for image in (profile.image, MEDIA, TOOLS):
        client.images.get(image)
    # Preflight checks the latch, not allocations already made by an earlier run.
    # Each allocating stage reserves its own remaining peak below.
    space = disk_budget(
        [
            (WORK / "downloads", 0),
            (WORK / "parts", 0),
            (WORK / "playback", 0),
            (WORK / "guest", 0),
        ]
    )
    if not all(item["ready"] for item in space):
        raise CanaryBlockedError("peak media allocation would cross 100 GiB / 10 percent disk latch")
    return {"db_identity": identity, "redis_identity": redis_id, "disk": space, "at": now(), "vpn": "OPEN"}


class MovieFlow:
    def __init__(self, client: Any, pool: asyncpg.Pool, args: argparse.Namespace, state: dict) -> None:
        self.client, self.pool, self.args, self.state = client, pool, args, state
        self.id = uuid.UUID(state["id"])
        self.heartbeat: RunHeartbeat | None = None
        self.parts = VideoPartRepository(pool)
        self.media = PartMedia(
            prefix=[
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--user",
                f"{os.getuid()}:{os.getgid()}",
                "-v",
                f"{WORK}:{WORK}",
                self.image_id(MEDIA),
            ]
        )

    def image_id(self, name: str) -> str:
        saved = self.state.get("configuration", {}).get("runtime", {}).get("images", {})
        return saved[name] if name in saved else str(self.client.images.get(name).id)

    def check_operation(self) -> None:
        if self.heartbeat:
            self.heartbeat.check()
        require_space([(WORK / name, 0) for name in ("downloads", "parts", "playback", "guest")])

    async def save(self) -> None:
        if self.heartbeat:
            self.heartbeat.check()
        try:
            await self.pool.execute(
                """INSERT INTO first_4k_runs(id,document,next_action_at) VALUES($1,$2::jsonb,$3)
                   ON CONFLICT(id) DO UPDATE SET document=excluded.document,next_action_at=excluded.next_action_at,updated_at=now()""",
                self.id,
                json.dumps(self.state),
                datetime.fromisoformat(self.state["next_action_at"]) if self.state.get("next_action_at") else None,
            )
            write_private(WORK / "evidence/checkpoint.json", self.state)
            if self.heartbeat:
                self.heartbeat.progress = True
                await self.heartbeat.pulse()
        except BaseException:
            if self.heartbeat:
                self.heartbeat.failed.set()
            raise

    async def recover_prepared(self) -> bool:  # noqa: C901 - fail-closed recovery checks
        selected = [c for c in self.state.get("candidates", []) if c.get("video_id")]
        if not selected:
            if self.state.get("video_id"):
                raise CanaryBlockedError("run video has no saved candidate identity")
            return False
        if len(selected) != 1:
            raise CanaryBlockedError("multiple candidate video identities; preserve evidence")
        candidate = selected[0]
        video_id = uuid.UUID(candidate["video_id"])
        if self.state.get("video_id", str(video_id)) != str(video_id):
            raise CanaryBlockedError("run and candidate video identities conflict")
        try:
            recovered = await self.parts.recover_manifest(video_id, candidate["info_hash"])
            if recovered is None:
                if self.state.get("video_id"):
                    raise ValueError("prepared run has no committed manifest")
                return False
            parts, provenance = recovered
            source = contained_file(WORK / "downloads" / candidate["info_hash"], Path(candidate["source"]))
            if (
                source.stat().st_size != provenance["size"]
                or await asyncio.to_thread(sha256, source) != provenance["sha256"]
            ):
                raise ValueError("source bytes changed")
            for part in parts:
                path = contained_file(WORK / "parts" / str(video_id), WORK / "parts" / str(video_id) / part.filename)
                if path.stat().st_size != part.size_bytes or await asyncio.to_thread(sha256, path) != part.sha256:
                    raise ValueError("committed part bytes changed")
        except (ValueError, OSError, KeyError) as exc:
            raise CanaryBlockedError("committed manifest recovery failed; preserve source and parts") from exc
        if not self.state.get("video_id"):
            self.state.update(video_id=str(video_id), stage="prepared", source_provenance=provenance)
            await self.save()
        elif self.state.get("source_provenance") != provenance:
            raise CanaryBlockedError("run provenance conflicts with committed manifest")
        return True

    async def discover(self) -> None:
        if "candidates" in self.state and self.state.get("selection_version") == SELECTION_VERSION:
            return
        if self.state.get("candidates"):
            raise CanaryBlockedError("candidate policy changed after selection; reconciliation required")
        directory = private_directory(WORK / "evidence" / ("discovery-" + uuid.uuid4().hex))
        self.state.update(
            stage="discovery", discovery_intent={"directory": str(directory), "at": now(), "board": self.args.board}
        )
        await self.save()
        evidence = await collect(
            argparse.Namespace(
                board=self.args.board,
                cookie_file=self.args.cookie_file,
                flaresolverr="http://127.0.0.1:28191",
                max_threads=self.args.max_threads,
                fetch_attachments=self.args.fetch_attachments,
                output=directory,
            )
        )
        if evidence["status"] != "INPUT_READY":
            self.state["discovery_evidence"] = evidence
            await self.save()
            raise CanaryBlockedError("website verification failed or no valid candidates")
        scorer = QualityScorer(segmented_storage=True)
        candidates, seen = [], set()
        for index, item in enumerate(json.loads((directory / "baseline.json").read_text())):
            info_hash = extract_info_hash(item["magnet_uri"])
            try:
                reject_watermark_hash(info_hash or "")
            except SourceUnavailableError:
                continue
            title = item["title"]
            if (
                not info_hash
                or info_hash in seen
                or scorer.score(title) < 0
                or any(t in title.casefold() for t in ("sample", "trailer", "廣告", "广告", "預告", "预告"))
            ):
                continue
            seen.add(info_hash)
            size_bytes = int(item.get("size") or 0)
            candidate = {
                **item,
                "order": index,
                "info_hash": info_hash,
                "quality_score": scorer.score(title, int(item.get("seeders") or 0), size_bytes),
                "state": "pending",
            }
            # A complete 4K film cannot be this small, and downloading it only to
            # fail validate_source() costs hours of transfer per candidate.
            if 0 < size_bytes < self.args.min_movie_gib * 1024**3:
                candidate.update(
                    state="rejected",
                    failure_class="IncompatibleMediaError",
                    reason="declared size below the 4K feature-film minimum",
                    rejected_at=now(),
                )
            candidates.append(candidate)
        # Best first. The board's own order is retained in "order" for provenance,
        # and an unstated size sorts last because it cannot be ranked on evidence.
        candidates.sort(key=lambda item: (-item["quality_score"], -int(item.get("size") or 0), item["order"]))
        self.state.update(candidates=candidates, discovery_evidence=evidence, discovery_completed_at=now())
        self.state["selection_version"] = SELECTION_VERSION
        await self.save()
        if not any(item["state"] == "pending" for item in candidates):
            raise CanaryBlockedError("board candidates exhausted: no eligible magnet after quality filtering")

    async def download_prepare(self) -> None:
        if await self.recover_prepared():
            return
        secret = read_private(WORK / "qbit.json")
        selected = [c for c in self.state.get("candidates", []) if c.get("video_id")]
        if selected:
            try:
                await self._candidate(selected[0], secret)
            except (SourceUnavailableError, IncompatibleMediaError) as exc:
                raise CanaryBlockedError(
                    "saved source cannot resume; preserve evidence without selecting another film"
                ) from exc
            return
        for candidate in self.state["candidates"]:
            if candidate["state"] == "rejected":
                await self.stop_candidate(candidate, secret)
                continue
            try:
                await self._candidate(candidate, secret)
                return
            except MediaOperationError as exc:
                self.state.update(
                    stage="media_blocked",
                    media_failure={"operation": exc.operation, "category": exc.category, "at": now()},
                    next_step="inspect media runtime and resume retained source",
                )
                await self.save()
                raise
            except (SourceUnavailableError, IncompatibleMediaError) as exc:
                candidate.update(
                    state="rejected",
                    failure_class=type(exc).__name__,
                    reason=str(exc) if isinstance(exc, IncompatibleMediaError) else "no swarm within 300 seconds",
                    rejected_at=now(),
                )
                await self.save()
                await self.stop_candidate(candidate, secret)
        raise CanaryBlockedError("all discovered candidates rejected; inspect recorded source failures")

    async def stop_candidate(self, candidate: dict, secret: dict) -> None:
        if candidate.get("stopped"):
            return
        candidate["stop_intent"] = now()
        await self.save()
        async with QBitClient("http://127.0.0.1:18085", secret["username"], secret["password"]) as qbit:
            if await qbit.has_torrent(candidate["info_hash"]):
                response = await qbit._request("POST", "/api/v2/torrents/stop", data={"hashes": candidate["info_hash"]})
                if response.status_code == 404:
                    response = await qbit._request(
                        "POST", "/api/v2/torrents/pause", data={"hashes": candidate["info_hash"]}
                    )
                response.raise_for_status()
        candidate["stopped"] = True
        await self.save()

    def torrent_file(self, candidate: dict) -> bytes | None:
        """The thread's attached .torrent, when discovery captured one.

        Prefer it over the bare magnet: it carries the uploader's trackers.
        Any mismatch or missing file falls back to the magnet rather than
        blocking, because the magnet path is still a valid way to find a swarm.
        """
        relative = candidate.get("torrent_file")
        if not relative:
            return None
        directory = Path(self.state["discovery_intent"]["directory"])
        try:
            path = contained_file(directory, directory / relative)
            payload = path.read_bytes()
        except (OSError, ValueError):
            return None
        if hashlib.sha256(payload).hexdigest() != candidate.get("torrent_sha256"):
            return None
        return payload

    async def start_download(self, qbit: MovieTorrent, candidate: dict, identifier: str) -> None:
        """Journal the intent, then hand qBittorrent the best source available."""
        require_space([(WORK / "downloads", self.args.max_movie_gib * 1024**3)])
        attachment = self.torrent_file(candidate)
        candidate.update(
            state="download_intent",
            intent_at=now(),
            source_kind="torrent_file" if attachment else "magnet",
        )
        self.state["stage"] = "downloading"
        await self.save()
        qbit._owned_hash = identifier
        if attachment is not None:
            await qbit.add_torrent_file(attachment, identifier)
        else:
            await qbit.add_magnet(candidate["magnet_uri"])

    def download_budget(self, candidate: dict) -> float:
        """Seconds still allowed for waiting on this candidate's swarm.

        The deadline bounds waiting for peers and nothing else. Segmentation runs
        with the run state's video_id still unset, so an interruption sends the next
        resume back through the candidate flow; an intent timestamp that never moves
        would then block an already finished download permanently. A completed
        download keeps a small positive budget because the completion poll still
        needs one to return on its first pass.
        """
        remaining = (
            21600 - (datetime.now(timezone.utc) - datetime.fromisoformat(candidate["intent_at"])).total_seconds()
        )
        if candidate["state"] == "download_complete":
            return max(remaining, 600.0)
        if remaining <= 0:
            raise CanaryBlockedError("download deadline expired; retained torrent requires review")
        return remaining

    async def _candidate(self, candidate: dict, secret: dict) -> None:  # noqa: C901 - retained source preparation
        identifier = candidate["info_hash"]
        download = WORK / "downloads" / identifier
        async with MovieTorrent(
            "http://127.0.0.1:18085",
            secret["username"],
            secret["password"],
            download_dir="/downloads/" + identifier,
            local_download_dir=str(download),
            download_timeout=21600,
            no_peer_grace_seconds=300,
            max_download_bytes=self.args.max_movie_gib * 1024**3,
            # Every magnet this board publishes is bare. Without trackers a cold
            # client has only DHT, which is what starved the first three candidates.
            extra_trackers=parse_extra_trackers(get_settings().qbit_extra_trackers),
        ) as qbit:
            qbit.check = self.check_operation
            if self.heartbeat:
                qbit.progress = lambda: setattr(self.heartbeat, "progress", True)
            await qbit.health_check()
            if candidate["state"] == "pending":
                if await qbit.has_torrent(identifier) or download.exists():
                    raise CanaryBlockedError("candidate has preexisting torrent or files; refusing shortcut")
                await self.start_download(qbit, candidate, identifier)
            elif not await qbit.has_torrent(identifier):
                raise CanaryBlockedError("download intent has no torrent; reconciliation required")
            remaining = self.download_budget(candidate)
            content = await asyncio.wait_for(qbit.wait_complete(identifier, timeout=int(remaining)), remaining + 30)
            response = await qbit._request("GET", "/api/v2/torrents/info", params={"hashes": identifier})
            response.raise_for_status()
            rows = response.json()
            if (
                len(rows) != 1
                or rows[0]["hash"] != identifier
                or rows[0]["progress"] != 1
                or rows[0].get("amount_left", 1) != 0
            ):
                raise CanaryBlockedError("torrent completion identity mismatch")
            if int(rows[0].get("total_size", 0)) > self.args.max_movie_gib * 1024**3:
                raise IncompatibleMediaError("torrent exceeds reserved disk budget")
            source = contained_file(download, Path(select_media_input(content)))
            if any(token in source.stem.casefold() for token in ("sample", "trailer")):
                raise IncompatibleMediaError("sample or trailer filename rejected")
            candidate.update(
                state="download_complete",
                completed_at=now(),
                source=str(source),
                torrent_evidence={
                    k: rows[0].get(k)
                    for k in (
                        "hash",
                        "progress",
                        "amount_left",
                        "total_size",
                        "downloaded",
                        "completion_on",
                        "added_on",
                    )
                },
            )
            await self.save()
            video_id = uuid.UUID(candidate.setdefault("video_id", str(uuid.uuid4())))
            await self.save()
            repo = VideoRepository(self.pool)
            if await repo.find_by_id(video_id) is None:
                await repo.insert(
                    Video(
                        id=video_id, title=candidate["title"], magnet_uri=candidate["magnet_uri"], info_hash=identifier
                    )
                )
            output = private_directory(WORK / "parts" / str(video_id))
            if any(output.iterdir()):
                raise CanaryBlockedError("uncommitted preparation artifacts retained; reconciliation required")
            self.state.update(stage="preparing_media", current_part_index=None)
            await self.save()
            media_info = await asyncio.to_thread(self.media.validate_source, source)
            if float(media_info["format"]["duration"]) < self.args.min_movie_seconds:
                raise IncompatibleMediaError("source duration below complete-film acceptance minimum")
            # prepare() refuses a single-part manifest, so a film smaller than one
            # full part needs a lower target. Halving the source guarantees two
            # parts while leaving the 10 GB Photos item ceiling untouched.
            target = min(TARGET_BYTES, (source.stat().st_size + 1) // 2)
            loop = asyncio.get_running_loop()

            async def persist_source(value: dict) -> None:
                if self.state.get("source_validation") not in (None, value):
                    raise CanaryBlockedError("source validation checkpoint changed")
                self.state["source_validation"] = value
                await self.save()

            def save_source(value: dict) -> None:
                pending = asyncio.run_coroutine_threadsafe(persist_source(value), loop)
                try:
                    pending.result(timeout=60)
                except BaseException:
                    pending.cancel()
                    raise

            parts, provenance = await asyncio.to_thread(
                self.media.prepare,
                source,
                output,
                video_id,
                target=target,
                source_checkpoint=self.state.get("source_validation"),
                save_source_checkpoint=save_source,
            )
            provenance["target_bytes_used"] = target
            provenance["discovery"] = {
                **candidate,
                "board": self.args.board,
                "captured_at": self.state["discovery_completed_at"],
            }
            await self.parts.install(video_id, parts, provenance)
            self.state.update(video_id=str(video_id), stage="prepared", source_provenance=provenance)
            await self.save()

    async def runtime(self) -> tuple[Any, Any]:  # noqa: C901 - explicit retained-runtime reconciliation
        runtime = self.state.setdefault("runtime", {})
        owner = str(self.id)
        existing = await asyncio.to_thread(
            self.client.containers.list, all=True, filters={"label": f"{OWNER_LABEL}={owner}"}
        )
        roles = {c.labels.get("pixav.photos_canary.role"): c for c in existing}
        if len(roles) != len(existing) or any("pixav.task_id" in c.labels for c in existing):
            raise CanaryBlockedError("ambiguous retained guest")
        profile = get_profile("gphotos_pixel_xl_v1", path=ROOT / "config/android_profiles.yml")
        if "guest" not in roles:
            if runtime.get("guest_intent"):
                raise CanaryBlockedError("guest creation intent unresolved; refusing replacement")
            runtime["guest_intent"] = now()
            await self.save()
            roles["guest"] = await asyncio.to_thread(
                self.client.containers.run,
                self.image_id(profile.image),
                command=list(profile.args),
                detach=True,
                privileged=True,
                name=f"{PROJECT}-guest-{owner}",
                labels={OWNER_LABEL: owner, "pixav.photos_canary.role": "guest"},
                volumes={str(WORK / "guest"): {"bind": "/data", "mode": "rw"}},
            )
        guest = roles["guest"]
        if "tools" not in roles:
            if runtime.get("tools_intent"):
                raise CanaryBlockedError("tools creation intent unresolved; refusing replacement")
            runtime["tools_intent"] = now()
            await self.save()
            roles["tools"] = await asyncio.to_thread(
                self.client.containers.run,
                self.image_id(MEDIA),
                detach=True,
                network_mode=f"container:{guest.id}",
                name=f"{PROJECT}-tools-{owner}",
                labels={OWNER_LABEL: owner, "pixav.photos_canary.role": "tools"},
                volumes={str(WORK / "parts" / self.state["video_id"]): {"bind": "/parts", "mode": "ro"}},
                tmpfs={"/tmp": "rw,exec,nosuid,nodev,mode=1777"},  # noqa: S108 - private container tmpfs
            )
        for role, item in roles.items():
            if runtime.get(role + "_id", item.id) != item.id:
                raise CanaryBlockedError("retained runtime identity changed")
            runtime[role + "_id"] = item.id
        await retained(self, WORK)
        for item in roles.values():
            if item.status != "running":
                await self.save()
                await asyncio.to_thread(item.start)
        await self.save()
        for _ in range(120):
            result = await asyncio.to_thread(guest.exec_run, ["getprop", "sys.boot_completed"])
            if result.exit_code == 0 and result.output.strip() == b"1":
                break
            await asyncio.sleep(2)
        else:
            raise CanaryBlockedError("retained guest boot deadline exceeded")
        for check in profile.readiness:
            result = await asyncio.to_thread(guest.exec_run, shlex.split(check.command))
            if result.exit_code or check.contains not in result.output.decode():
                raise CanaryBlockedError("retained guest does not match the configured Pixel XL profile")
        return guest, roles["tools"]

    async def upload(self) -> None:  # noqa: C901 - serial per-part effects and quota handling
        video_id = uuid.UUID(self.state["video_id"])
        secret = read_private(self.args.google_secret)
        account_id = uuid.UUID(self.state.setdefault("account_id", str(uuid.uuid4())))
        await self.save()
        await self.pool.execute(
            "INSERT INTO accounts(id,email) VALUES($1,$2) ON CONFLICT(id) DO NOTHING", account_id, secret["email"]
        )
        scheduler = LruAccountScheduler(self.pool, lease_seconds=22200)
        confirmed_here = 0
        parts = await self.parts.list(video_id)
        await reconcile(self, parts, WORK)
        for position, part in enumerate(parts):
            self.state["current_part_index"] = part.part_index
            await self.save()
            if part.usage_counted_at is not None:
                continue
            if part.account_id is None:
                try:
                    selected = await scheduler.next_account()
                except RuntimeError:
                    await self.quota_wait(part)
                    return
                if selected != str(account_id):
                    await scheduler.release_lease(selected)
                    raise CanaryBlockedError("unexpected account in isolated scheduler")
            row = await self.pool.fetchrow("SELECT *, now() AS db_now FROM accounts WHERE id=$1", account_id)
            usage = (
                0 if row["quota_reset_at"] and row["quota_reset_at"] <= row["db_now"] else row["daily_uploaded_bytes"]
            )
            if usage + part.size_bytes > row["daily_quota_bytes"]:
                await scheduler.release_lease(str(account_id))
                await self.quota_wait(part)
                return
            guest, runner = await self.runtime()
            recovery = {**self.state.get("device", {}), **part.recovery}
            copies = int(not recovery.get("push_intent")) + int(not recovery.get("publish_intent"))
            require_space([(WORK / "guest", part.size_bytes * copies)])

            async def save_recovery(value: dict, current: VideoPart = part) -> None:
                self.state["device"] = {
                    k: v
                    for k, v in value.items()
                    if k
                    in {
                        "email_submitted",
                        "password_submitted",
                        "install_intent",
                        "login_confirmed",
                        "original_setting",
                        "quota_before",
                        "user_action",
                    }
                }
                quota = self.state["device"].get("quota_before")
                if quota and not quota.get("observed_at"):
                    quota["observed_at"] = (await self.pool.fetchval("SELECT now()")).isoformat()
                await self.parts.journal(current, "upload_intent", value, account_id=account_id)
                await self.save()

            await save_recovery(recovery)
            uploader = MaestroPartUploader(
                guest, runner, str(self.id), part, recovery, save_recovery, ROOT / "config/maestro/photos-canary"
            )
            uploader.check = self.check_operation
            verifier = MaestroPartVerifier(uploader)
            session = RedroidSession(str(self.id), guest.id, "127.0.0.1", 5555)
            account = Account.model_validate({**dict(row), "password": secret["password"]})

            async def transfer(
                uploader=uploader, verifier=verifier, session=session, account=account, part=part
            ) -> None:
                await uploader.login(session, account)
                remote = await uploader.push_file(session, str(WORK / "parts" / str(video_id) / part.filename))
                await uploader.trigger_upload(session, remote)
                share = await verifier.wait_for_share_url(session, timeout=21600)
                if not await verifier.validate_share_url(share):
                    raise CanaryBlockedError("Photos sharing location unavailable")
                await self.parts.confirm_backup(part, share, uploader.backup_evidence)

            try:
                await asyncio.wait_for(transfer(), timeout=21600)
            except Exception as exc:
                stage = "user_action_required" if isinstance(exc, UserActionRequiredError) else "reconcile"
                await self.parts.journal(part, stage, recovery, account_id=account_id)
                self.state.update(
                    stage=stage, next_step="resume retained guest; do not resend credentials or republish"
                )
                await self.save()
                raise
            await scheduler.mark_used(str(account_id))
            confirmed_here += 1
            if self.pause_after(confirmed_here, parts, position):
                self.state.update(stage="upload_paused", next_step="resume")
                await self.save()
                return
        self.state.update(stage="uploaded", next_step="prepare-playback")
        self.state["quota_recheck_not_before"] = (
            await self.pool.fetchval(
                "SELECT max(uploaded_at) + interval '24 hours' FROM video_parts WHERE video_id=$1", video_id
            )
        ).isoformat()
        await self.save()

    def pause_after(self, confirmed: int, parts: list[VideoPart], position: int) -> bool:
        """Whether this invocation should stop after confirming ``position``.

        Pause only when something is left to pause before. A limit that lands
        exactly on the final part has finished the upload, and reporting that as
        a pause would leave a complete round looking unfinished.
        """
        if not self.args.max_parts or confirmed < self.args.max_parts:
            return False
        return any(p.usage_counted_at is None for p in parts[position + 1 :])

    async def quota_wait(self, part: VideoPart) -> None:
        due = await self.pool.fetchval("SELECT date_trunc('day',now()) + interval '1 day'")
        await self.pool.execute(
            "UPDATE video_parts SET state='quota_wait',retry_not_before=$3 WHERE video_id=$1 AND part_index=$2",
            part.video_id,
            part.part_index,
            due,
        )
        self.state.update(stage="quota_wait", next_step="resume", next_action_at=due.isoformat())
        await self.save()

    async def playback(self) -> None:
        video_id = uuid.UUID(self.state["video_id"])
        parts = await self.parts.list(video_id)
        if len(parts) < 2 or any(p.usage_counted_at is None or not p.share_url for p in parts):
            raise CanaryBlockedError("all cloud backups must be confirmed before playback preparation")
        manifest = {
            "parts": [p.model_dump(mode="json") for p in parts],
            "reference": self.state["source_provenance"]["reference"],
        }
        image = self.image_id(TOOLS)
        command = [
            "docker",
            "run",
            "--rm",
            "-i",
            "--name",
            f"{PROJECT}-cold-{video_id}",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--label",
            "pixav.first_4k.role=cold-playback",
            "-v",
            f"{ROOT / 'src'}:/app/src:ro",
            "-v",
            f"{WORK / 'playback'}:/work",
            image,
            "python",
            "-m",
            "pixav.pixel_injector.parts_download",
        ]
        if not self.state.get("cold_initial_cache_empty"):
            cloud = WORK / "playback/cloud"
            if cloud.is_symlink() or (cloud.exists() and any(cloud.iterdir())):
                raise CanaryBlockedError("first cold recovery requires an empty cloud cache")
            self.state["cold_initial_cache_empty"] = {"checked_at": now()}
        self.state.update(stage="preparing_playback", cold_mounts=["/app/src:ro", "/work:rw"])
        await self.save()

        result = await asyncio.to_thread(
            monitored_run,
            command,
            21600 * len(parts) + PartMedia.deadline(manifest["reference"]["duration"]) * 4,
            self.check_operation,
            data=json.dumps(manifest).encode(),
        )
        if result.returncode:
            raise CanaryBlockedError("cold Photos recovery failed; no playback publication")
        report = json.loads(result.stdout)
        for part, evidence in zip(parts, report["parts"], strict=True):
            await self.parts.confirm_original(part, evidence)
        await self.parts.publish(video_id, "/playback/" + report["filename"], report)
        path = WORK / "playback" / f"{video_id}.strm"
        temporary = path.with_suffix(".strm.tmp")
        temporary.write_text(f"http://127.0.0.1:28000/stream/{video_id}\n")
        temporary.replace(path)
        self.state.update(
            stage="playback_ready",
            playback=report,
            next_step="VLC seek and 24-hour quota observation",
            next_action_at=self.state.get("quota_recheck_not_before"),
        )
        await self.save()

    async def reset(self, persisted: bool) -> dict:
        """Retire the current run so a different board can be selected.

        Destroys no media and deletes no torrent: in-flight candidates are only
        stopped, and the retired document is archived next to the pg_dump that
        execute() already took. A published film is never reset away.
        """
        if not persisted:
            return {"status": "RESET_COMPLETE", "retired": None, "stopped": 0}
        if self.state.get("video_id"):
            raise CanaryBlockedError("run already selected a film; reset requires explicit reconciliation")
        secret = read_private(WORK / "qbit.json")
        stopped = 0
        for candidate in self.state.get("candidates", []):
            if candidate["state"] in {"pending", "download_intent"}:
                await self.stop_candidate(candidate, secret)
                stopped += 1
        write_private(WORK / "evidence" / ("superseded-" + self.state["id"] + ".json"), self.state)
        await self.pool.execute("DELETE FROM first_4k_runs WHERE id = $1", uuid.UUID(self.state["id"]))
        return {"status": "RESET_COMPLETE", "retired": self.state["id"], "stopped": stopped}

    async def verify(self) -> dict:  # noqa: C901 - independent live acceptance gates
        self.state["gates"].update(movie="OPEN", quota="OPEN", automation="OPEN")
        await self.save()
        if not self.state.get("playback"):
            raise CanaryBlockedError("no rebuilt cloud movie to verify")
        video_id = uuid.UUID(self.state["video_id"])
        parts = await self.parts.list(video_id)
        self.state["gates"]["automation"] = automation_verdict(self.state, parts)
        self.state["automation_mode"] = (
            "assisted" if any(p.recovery.get("user_action") for p in parts) else "no_recorded_intervention"
        )
        await self.save()
        local = contained_file(WORK / "playback", WORK / "playback" / self.state["playback"]["filename"])
        published = await self.pool.fetch(
            "SELECT id FROM videos WHERE status='available' AND playback_manifest_version IS NOT NULL"
        )
        strm = WORK / "playback" / f"{video_id}.strm"
        if (
            len(published) != 1
            or published[0]["id"] != video_id
            or list((WORK / "playback").glob("*.mp4")) != [local]
            or list((WORK / "playback").glob("*.strm")) != [strm]
            or contained_file(WORK / "playback", strm).read_text() != f"http://127.0.0.1:28000/stream/{video_id}\n"
        ):
            raise CanaryBlockedError("playback publication is not unique or its STRM changed")
        if await asyncio.to_thread(sha256, local) != self.state["playback"]["sha256"]:
            raise CanaryBlockedError("published playback hash changed")
        http = await range_acceptance("http://127.0.0.1:28000", str(video_id), local)
        duration = self.state["source_provenance"]["reference"]["duration"]
        seeks = sorted(
            {
                0.0,
                duration / 2,
                max(0, duration - 5),
                *(max(0, p.start_seconds - 1) for p in parts[1:]),
                *(min(duration - 1, p.start_seconds + 1) for p in parts[1:]),
            }
        )
        vlc = await vlc_acceptance(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                PROJECT + "_default",
                "--user",
                f"{os.getuid()}:{os.getgid()}",
                self.image_id(TOOLS),
            ],
            f"http://resolver:8000/stream/{video_id}",
            seeks,
            WORK / "evidence",
        )
        self.state["playback_acceptance"] = {**http, **vlc, "at": now()}
        self.state["gates"]["movie"] = "PASS"
        self.state["gates"]["quota"] = "OPEN"
        await self.save()
        clock = await self.pool.fetchval("SELECT now()")
        due = await self.pool.fetchval(
            "SELECT max(uploaded_at)+interval '24 hours' FROM video_parts WHERE video_id=$1", video_id
        )
        if due is None or clock < due:
            self.state.update(
                next_step="verify after 24-hour observation deadline", next_action_at=due.isoformat() if due else None
            )
            await self.save()
            return {
                "status": "PLAYABLE_QUOTA_WAIT",
                "gates": self.state["gates"],
                "resume_at": self.state["next_action_at"],
            }
        guest, runner = await self.runtime()
        items = []
        for part in parts:

            async def read_only_journal(value: dict) -> None:
                await self.save()

            uploader = MaestroPartUploader(
                guest,
                runner,
                str(self.id),
                part,
                dict(part.recovery),
                read_only_journal,
                ROOT / "config/maestro/photos-canary",
            )
            await uploader.adb("connect", "127.0.0.1:5555")
            await uploader._media_store("/sdcard/DCIM/Camera/" + part.filename, readonly=True)
            await uploader.open_details()
            media = next(s for s in part.media_info["streams"] if s["codec_type"] == "video")
            items.append(collect_item(await uploader.attributes(), part.filename, {**media, "sha256": part.sha256}))
        # open_details() left the last part on the item pager, and the account
        # disc lives on the home screen, so the pager has to be cleared first.
        await uploader._open_photos_home()
        await uploader.flow("account")
        after = await uploader.quota_observation()
        after["observed_at"] = (await self.pool.fetchval("SELECT now()")).isoformat()
        before = self.state.get("device", {}).get("quota_before")
        verdict = "OPEN"
        if before and after.get("backup_complete"):
            verdicts = [
                quota_verdict(
                    {
                        "quota_before": before,
                        "quota_after_24h": after,
                        "media": {"size": part.size_bytes},
                        "upload_completed_at": part.uploaded_at.isoformat() if part.uploaded_at else "",
                    },
                    item,
                )
                for part, item in zip(parts, items, strict=True)
            ]
            if "FAIL" in verdicts:
                verdict = "FAIL"
            elif verdicts and all(value == "PASS" for value in verdicts):
                verdict = "PASS"
        self.state["quota_observation"] = {"before": before, "after": after, "items": items}
        self.state["gates"]["quota"] = verdict
        await self.save()
        # A gate that did not pass must not exit zero. Recording the verdict in the
        # document and still returning the success status would let a FAIL or an
        # unresolved OPEN read as a completed round to anything scripting this.
        gates = self.state["gates"]
        if any(gates.get(name) != "PASS" for name in ("movie", "quota", "automation")):
            return {"status": "GATE_NOT_PASSED", "gates": gates}
        return {"status": "LIVE_VERIFIED", "gates": gates}


async def checked_database(client: Any) -> Any:
    """Read-only identity check usable with stopped non-DB dependencies."""
    db = await asyncio.to_thread(container, client, "postgres")
    mounts = [m for m in db.attrs["Mounts"] if m.get("Type") == "volume"]
    if len(mounts) != 1 or mounts[0].get("Name") != f"{PROJECT}_movie_postgres":
        raise CanaryBlockedError("isolated PostgreSQL volume mismatch")
    conn = await asyncpg.connect(DSN)
    try:
        observed = await asyncio.to_thread(
            db.exec_run,
            [
                "psql",
                "-U",
                "pixav_test",
                "-d",
                "pixav_first_4k",
                "-Atc",
                "SELECT system_identifier FROM pg_control_system()",
            ],
        )
        if (
            observed.exit_code
            or observed.output.decode().strip() != await database_identity(conn)
            or await conn.fetchval("SELECT current_database()") != "pixav_first_4k"
        ):
            raise CanaryBlockedError("PostgreSQL instance identity mismatch")
        return conn
    except BaseException:
        await conn.close()
        raise


def runtime_configuration(client: Any) -> dict:
    profile = get_profile("gphotos_pixel_xl_v1", path=ROOT / "config/android_profiles.yml")
    return {
        "profile": profile.model_dump(mode="json"),
        "images": {image: client.images.get(image).id for image in (profile.image, MEDIA, TOOLS)},
        "selection_version": SELECTION_VERSION,
        "segmentation": {"version": 1, "target_bytes": TARGET_BYTES, "strategy": "min-target-half-source"},
    }


async def dispatch(flow: MovieFlow) -> dict:  # noqa: C901 - explicit command and gate dispatch
    args, state, pool = flow.args, flow.state, flow.pool
    if args.command == "recovery-drill":
        return await drill(flow, WORK, ROOT)
    if args.command == "verify":
        return await flow.verify()
    if state.get("next_action_at") and state["stage"] == "quota_wait":
        clock = await pool.fetchval("SELECT now()")
        if clock < datetime.fromisoformat(state["next_action_at"]):
            return {"status": "QUOTA_WAIT", "resume_at": state["next_action_at"]}
        state.pop("next_action_at", None)
    if args.command != "prepare-playback":
        await flow.discover()
        await flow.download_prepare()
        await flow.upload()
        if state["stage"] == "uploaded":
            for invocation in state.get("cli_invocations", []):
                if invocation["id"] == state.get("invocation_id"):
                    invocation["uploads_completed_at"] = now()
            await flow.save()
        if state["stage"] == "quota_wait":
            return {"status": "QUOTA_WAIT", "resume_at": state["next_action_at"]}
        if state["stage"] == "upload_paused":
            return {"status": "PART_LIMIT_REACHED", "next_step": "resume"}
    await flow.playback()
    if flow.heartbeat:
        flow.heartbeat.check()
    process = await asyncio.create_subprocess_exec(
        "docker",
        "compose",
        "-f",
        str(ROOT / "docker-compose.first-4k.yml"),
        "up",
        "-d",
        "resolver",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    if await asyncio.wait_for(process.wait(), 120):
        raise CanaryBlockedError("isolated resolver failed to start")
    return await flow.verify()


async def execute(args: argparse.Namespace) -> dict:  # noqa: C901 - guarded CLI dispatch
    client = docker.from_env()
    default_board = get_settings().crawl_seed_urls.split(",")[0].split("|")[0]
    try:
        if args.command == "boards":
            directory = private_directory(WORK / "evidence" / ("boards-" + uuid.uuid4().hex))
            return await collect_boards(
                argparse.Namespace(
                    board=args.board or default_board,
                    cookie_file=args.cookie_file,
                    flaresolverr="http://127.0.0.1:28191",
                    output=directory,
                )
            )
        conn = await checked_database(client)
        try:
            if args.command == "status":
                return await read_status(conn, args.run_id)
            if not await conn.fetchval("SELECT pg_try_advisory_lock(410041004)"):
                raise CanaryBlockedError("another single-film runner is active")
            rows = (
                await conn.fetch("SELECT document FROM first_4k_runs")
                if await conn.fetchval("SELECT to_regclass('public.first_4k_runs')")
                else []
            )
            if len(rows) > 1:
                raise CanaryBlockedError("multiple film runs require explicit reconciliation")
            if not rows and args.command in {"resume", "verify", "prepare-playback", "recovery-drill"}:
                raise CanaryBlockedError("no saved run to resume")
            state = (
                json.loads(rows[0]["document"])
                if rows
                else {
                    "id": str(uuid.uuid4()),
                    "stage": "new",
                    "gates": {
                        "movie": "OPEN",
                        "quota": "OPEN",
                        "automation": "OPEN",
                        "vpn": "OPEN",
                        "production_promotion": "NOT_REQUESTED",
                    },
                }
            )
            if args.run_id is not None and str(args.run_id) != state["id"]:
                raise CanaryBlockedError("requested run identity mismatch")
            # Configuration drift is rejected before mkdir, backup, migration or any
            # external effect. Reset and preflight do not reinterpret saved settings.
            if args.command not in {"reset", "preflight"}:
                configuration = resolve_configuration(
                    args, state, await asyncio.to_thread(runtime_configuration, client), default_board
                )
                state["configuration"] = configuration
            evidence = await preflight(client, args)
            if args.command == "preflight":
                write_private(WORK / "evidence/preflight.json", evidence)
                return {"status": "PREFLIGHT_READY", "vpn": "OPEN"}
            backup = WORK / "evidence" / ("before-migration-" + uuid.uuid4().hex + ".sql")
            db = container(client, "postgres")
            dump = await asyncio.to_thread(db.exec_run, ["pg_dump", "-U", "pixav_test", "pixav_first_4k"])
            if dump.exit_code:
                raise CanaryBlockedError("isolated full backup failed")
            fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(dump.output)
            write_private(
                backup.with_suffix(".meta.json"),
                {
                    "db_identity": evidence["db_identity"],
                    "sha256": sha256(backup),
                    "at": now(),
                },
            )
            await run_migrations(DSN, until="011_first_4k_heartbeat.sql")
            pool = await asyncpg.create_pool(DSN, min_size=2, max_size=5)
            try:
                flow = MovieFlow(client, pool, args, state)
                if args.command == "reset":
                    return await flow.reset(bool(rows))
                invocation = {"id": str(uuid.uuid4()), "command": args.command, "started_at": now()}
                state["invocation_id"] = invocation["id"]
                state.setdefault("cli_invocations", []).append(invocation)
                await flow.save()
                flow.heartbeat = RunHeartbeat(pool, flow.id, state, lock_connection=conn)
                flow.media.check = flow.check_operation
                async with flow.heartbeat.watch():
                    result = await dispatch(flow)
                    invocation.update(result=result["status"], completed_at=now())
                    await flow.save()
                    return result
            finally:
                await pool.close()
        finally:
            # Closing the dedicated connection also releases the advisory lock.
            await conn.close()
    finally:
        client.close()


def nonnegative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a nonnegative integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be a nonnegative integer")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    # "resume" is an alias of "run": recovery is driven by the persisted state
    # document, not by a different code path, so both re-enter at the same stage.
    parser.add_argument(
        "command",
        choices=(
            "boards",
            "preflight",
            "reset",
            "run",
            "resume",
            "verify",
            "prepare-playback",
            "status",
            "recovery-drill",
        ),
    )
    parser.add_argument("--board")
    parser.add_argument("--run-id", type=uuid.UUID)
    parser.add_argument("--cookie-file", type=Path, default=ROOT / "secrets/sehuatang-cookies.txt")
    parser.add_argument("--google-secret", type=Path, default=ROOT / "secrets/google-photos-canary.json")
    parser.add_argument("--max-threads", type=int)
    parser.add_argument("--max-movie-gib", type=int)
    parser.add_argument("--min-movie-seconds", type=int)
    parser.add_argument("--min-movie-gib", type=int)
    # 0 means no limit. A positive value caps how many parts THIS invocation newly
    # confirms; parts already confirmed by an earlier run are skipped and do not count.
    parser.add_argument("--max-parts", type=nonnegative_int, default=0)
    # Off by default: the attachment endpoint answers this client with a
    # Cloudflare interstitial. Extra trackers reach the same swarms.
    parser.add_argument("--fetch-attachments", action=argparse.BooleanOptionalAction, default=None)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    os.umask(0o077)
    try:
        if args.command == "status":
            result = asyncio.run(execute(args))
        else:
            with single_flight():
                result = asyncio.run(execute(args))
        print(json.dumps(result))
        return 0 if result["status"] in SUCCESS_STATUSES else 2
    except Exception as exc:
        result = {
            "status": "USER_ACTION_REQUIRED" if isinstance(exc, UserActionRequiredError) else "BLOCKED",
            "error_type": type(exc).__name__,
        }
        if isinstance(exc, MediaOperationError):
            result.update(operation=exc.operation, category=exc.category)
        if isinstance(exc, CanaryBlockedError):
            result["reason"] = str(exc)
        print(json.dumps(result))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
