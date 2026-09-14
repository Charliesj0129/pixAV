"""Board selection, download and segmentation: everything up to a committed manifest."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from pixav.config import get_settings
from pixav.media_loader.qbittorrent import QBitClient, extract_info_hash, parse_extra_trackers, reject_watermark_hash
from pixav.media_loader.remuxer import select_media_input
from pixav.media_loader.video_parts import (
    TARGET_BYTES,
    IncompatibleMediaError,
    MediaOperationError,
    contained_file,
    require_space,
    sha256,
)
from pixav.pixel_injector.canary import CanaryBlockedError, private_directory, read_private
from pixav.shared.exceptions import SourceUnavailableError
from pixav.shared.models import Video
from pixav.shared.repository import VideoRepository
from pixav.sht_probe.scoring import QualityScorer

from .discovery import collect
from .settings import SELECTION_VERSION, WORK, now
from .state import FlowState
from .torrent import MovieTorrent


class PrepareMixin(FlowState):
    """The stages that turn a board URL into verified parts on disk."""

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
