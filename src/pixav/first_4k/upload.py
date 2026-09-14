"""Per-part upload to Google Photos, with the daily quota and pause rules."""

from __future__ import annotations

import asyncio
import uuid

from pixav.maxwell_core.scheduler import LruAccountScheduler
from pixav.media_loader.video_parts import require_space
from pixav.pixel_injector.canary import CanaryBlockedError, read_private
from pixav.pixel_injector.maestro_parts import MaestroPartUploader, MaestroPartVerifier, UserActionRequiredError
from pixav.pixel_injector.session import RedroidSession
from pixav.shared.models import Account, VideoPart

from .recovery import reconcile
from .settings import ROOT, WORK
from .state import FlowState


class UploadMixin(FlowState):
    """Serial per-part effects; every one of them is journalled before it happens."""

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
