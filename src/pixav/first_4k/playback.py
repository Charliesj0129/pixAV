"""Cold reload from Photos, publication, and the live acceptance gates."""

from __future__ import annotations

import asyncio
import json
import os
import uuid

from pixav.media_loader.video_parts import PartMedia, contained_file, monitored_run, sha256
from pixav.pixel_injector.canary import CanaryBlockedError
from pixav.pixel_injector.canary_acceptance import collect_item, quota_verdict
from pixav.pixel_injector.maestro_parts import MaestroPartUploader
from pixav.strm_resolver.movie_acceptance import range_acceptance, vlc_acceptance

from .recovery import automation_verdict
from .settings import PROJECT, ROOT, TOOLS, WORK, now
from .state import FlowState


class PlaybackMixin(FlowState):
    """Rebuild the film from Photos alone, publish it, then judge the gates."""

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
