"""Managed Photos uploads through the registered Windows device and UI fence."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pixav.pixel_injector.canary import CanaryBlockedError
from pixav.pixel_injector.maestro_parts import MaestroPartUploader, MaestroPartVerifier
from pixav.pixel_injector.segment_staging import release_segment, stage_segment
from pixav.pixel_injector.windows_managed_runtime import WindowsManagedRuntime
from pixav.shared.media_file import run_media_work
from pixav.shared.models import Account
from pixav.shared.storage_models import RemoteAssetSegment

WINDOWS_PIXEL_MODE = "windows_android"


class WindowsSegmentUploader:
    def __init__(self, runtime: WindowsManagedRuntime, *, flows: Path, observation_clock: Any) -> None:
        self.runtime, self.flows, self.observation_clock = runtime, flows, observation_clock

    async def upload(self, segment: RemoteAssetSegment, account: Account, journal) -> tuple[str, dict]:
        # Never present old unknown effects to a new environment.
        binding = segment.recovery.get("runtime_binding")
        if segment.recovery and (not isinstance(binding, dict) or binding.get("kind") != WINDOWS_PIXEL_MODE):
            raise CanaryBlockedError("original upload requires reconciliation before Windows migration")
        staged = await run_media_work(stage_segment, segment, root=self.runtime.staging_root)
        async with self.runtime.acquire(account.id) as (device, lease):
            uploader = self._uploader(segment, account, journal, device)
            uploader.check = lease.check
            verifier = MaestroPartVerifier(uploader)
            await uploader.login(device.session, account)
            remote = await uploader.push_file(device.session, str(staged / segment.filename))
            await uploader.trigger_upload(device.session, remote)
            share_url = await verifier.wait_for_share_url(device.session, timeout=21600)
            if not await verifier.validate_share_url(share_url):
                raise CanaryBlockedError("Photos sharing location unavailable")
            return share_url, uploader.backup_evidence

    def _uploader(self, segment, account, journal, device) -> MaestroPartUploader:
        async def save(value: dict) -> None:
            await journal("upload_intent", value)

        return MaestroPartUploader(
            None,
            device.runner,
            str(self.runtime.owner),
            segment,
            dict(segment.recovery),
            save,
            self.flows,
            source_root=f"/parts/{segment.asset_id}",
            observation_clock=self.observation_clock,
            observation_binding={
                "account_id": str(account.id),
                "owner": str(self.runtime.owner),
                "profile": self.runtime.profile,
            },
            windows_device=device,
        )

    async def share_existing(self, segment: RemoteAssetSegment, account: Account, journal) -> tuple[str, dict]:
        binding = segment.recovery.get("runtime_binding", {})
        if binding.get("kind") != WINDOWS_PIXEL_MODE:
            raise CanaryBlockedError("existing item remains bound to its original environment")
        async with self.runtime.acquire(account.id) as (device, lease):
            uploader = self._uploader(segment, account, journal, device)
            uploader.check = lease.check
            return await MaestroPartVerifier(uploader).share_selected_existing(device.session, account)

    async def release(self, segment: RemoteAssetSegment) -> None:
        await run_media_work(release_segment, segment, root=self.runtime.staging_root)
