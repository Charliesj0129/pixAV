"""The run object itself: identity, persistence and the retained guest runtime."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import uuid
from datetime import datetime
from typing import Any

import asyncpg

from pixav.media_loader.video_parts import PartMedia, require_space
from pixav.pixel_injector.canary import OWNER_LABEL, CanaryBlockedError, read_private, write_private
from pixav.pixel_injector.profiles import get_profile
from pixav.shared.video_parts import VideoPartRepository

from .contracts import RunHeartbeat
from .playback import PlaybackMixin
from .prepare import PrepareMixin
from .recovery import retained
from .settings import MEDIA, PROJECT, ROOT, WORK, now
from .upload import UploadMixin


class MovieFlow(PrepareMixin, UploadMixin, PlaybackMixin):
    """One film, one run document. PostgreSQL is execution truth."""

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
