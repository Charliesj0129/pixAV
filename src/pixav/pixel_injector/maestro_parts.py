"""Maestro implementations of existing upload contracts for one retained guest.

The caller supplies a PostgreSQL journal callback. Unknown side effects stop for
reconciliation; no exception here destroys the guest or retries credentials.
"""

from __future__ import annotations

import asyncio
import re
import struct
import tempfile
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any

import httpx
import yaml

from pixav.media_loader.video_parts import sha256
from pixav.pixel_injector.canary import OWNER_LABEL, CanaryBlockedError
from pixav.pixel_injector.canary_acceptance import collect_item
from pixav.pixel_injector.canary_maestro import hierarchy, run_flow
from pixav.pixel_injector.session import RedroidSession
from pixav.pixel_injector.uploader import media_provider_scan_command
from pixav.shared.logging import install_http_url_log_redaction
from pixav.shared.models import Account
from pixav.shared.storage_models import TransferableFile

PHOTOS_PACKAGE = "com.google.android.apps.photos"

# How long to let MediaProvider finish reading a freshly published file before
# its row is judged. A 128 MB video is not read instantly, and the row appears
# before the read completes.
MEDIA_SCAN_ATTEMPTS = 24
MEDIA_SCAN_DELAY_SECONDS = 5.0


class UserActionRequiredError(CanaryBlockedError):
    """Device confirmation needed; caller persists this and retains the guest."""


class MaestroPartUploader:
    def __init__(
        self,
        guest: Any,
        runner: Any,
        owner: str,
        part: TransferableFile,
        recovery: dict,
        save: Callable[[dict], Awaitable[None]],
        flows: Path,
        source_root: str = "/parts",
    ) -> None:
        self.guest, self.runner, self.owner = guest, runner, owner
        self.part, self.recovery, self.save, self.flows = part, recovery, save, flows
        # Where the tools container sees this file. One isolated run mounts a
        # single video's parts directly; the managed worker mounts a staging
        # root shared by every asset, so each asset addresses its own subtree.
        self.source_root = source_root.rstrip("/")
        self.backup_evidence: dict = {}
        self.check: Callable[[], None] | None = None

    async def _io(self, operation: Callable[[], Any]) -> Any:
        """Monitor long guest effects; a lost guard stops this run's writers."""
        if not self.check:
            return await asyncio.to_thread(operation)
        self.check()
        task = asyncio.create_task(asyncio.to_thread(operation))
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=1)
                self.check()
                if done:
                    return task.result()
        except BaseException:
            # Stopping only the CLI/ADB connection does not stop an in-guest cp.
            # Preserve both owned containers and /data, while halting their writes.
            self.guard(RedroidSession(self.owner, self.guest.id, "127.0.0.1", 5555))
            for item in (self.runner, self.guest):
                await asyncio.to_thread(item.stop, timeout=10)
            await asyncio.gather(task, return_exceptions=True)
            raise

    async def receipt(self, kind: str, *, complete: bool = False) -> None:
        """Persist only operation names, timestamps and verified identity facts."""
        if self.check:
            self.check()
        record = self.recovery.setdefault("operations", {}).setdefault(kind, {})
        field = "completed_at" if complete else "intent_at"
        record.setdefault(field, datetime.now(timezone.utc).isoformat())
        if complete:
            record["identity_verified"] = True
        await self.save(self.recovery)

    def guard(self, session: RedroidSession) -> None:
        if session.container_id != self.guest.id:
            raise CanaryBlockedError("guest session changed")
        for container, role in ((self.guest, "guest"), (self.runner, "tools")):
            if (
                container.labels.get(OWNER_LABEL) != self.owner
                or container.labels.get("pixav.photos_canary.role") != role
                or "pixav.task_id" in container.labels
            ):
                raise CanaryBlockedError("guest ownership mismatch")

    async def adb(self, *args: str, timeout: int = 120, answers_with_exit_code: bool = False) -> str:
        """Run one ADB command against the retained guest.

        A non-zero exit means the guest did not do what it was told, which is
        never something to carry on from -- except for the few commands used as
        questions, where "no" is reported as a failed exit. ``pm path`` for an
        absent package is the case that matters: the caller is asking whether
        the package is there, and the answer belongs to the caller, not to this
        method's idea of a broken guest.
        """
        if self.check:
            self.check()

        def execute() -> str:
            previous = self.runner.client.api.timeout
            self.runner.client.api.timeout = timeout + 30
            try:
                result = self.runner.exec_run(
                    ["timeout", "--kill-after=5s", str(timeout), "adb", "-s", "127.0.0.1:5555", *args]
                )
                if result.exit_code and not answers_with_exit_code:
                    raise CanaryBlockedError("ADB operation failed; retained guest requires reconciliation")
                return result.output.decode(errors="replace")
            finally:
                self.runner.client.api.timeout = previous

        return await self._io(execute)

    async def attributes(self) -> list[dict]:
        attributes = await self._io(partial(hierarchy, self.runner, self.owner))
        text = "\n".join(a.get("text", "") for a in attributes)
        if any(
            marker in text
            for marker in ("Check your phone", "2-Step Verification", "Verify it's you", "Check your device")
        ):
            self.recovery["user_action"] = "confirm Google device challenge, then resume"
            await self.save(self.recovery)
            raise UserActionRequiredError("Google device confirmation required; resume after confirmation")
        return attributes

    async def flow(self, name: str, *, credentials: dict | None = None) -> None:
        if self.check:
            self.check()
        await self._io(partial(run_flow, self.runner, self.owner, self.flows / f"{name}.yaml", credentials=credentials))

    async def commands(self, commands: list[dict | str], *, app_id: str = PHOTOS_PACKAGE) -> None:
        if self.check:
            self.check()
        with tempfile.TemporaryDirectory(prefix="movie-flow-") as temporary:
            path = Path(temporary) / "flow.yaml"
            path.write_text(f"appId: {app_id}\n---\n" + yaml.safe_dump(commands))
            await self._io(partial(run_flow, self.runner, self.owner, path))

    async def login(self, session: RedroidSession, account: Account) -> None:
        self.guard(session)
        await self.adb("connect", "127.0.0.1:5555")
        accounts = await self.adb("shell", "dumpsys", "account")
        if f"name={account.email}, type=com.google" not in accounts:
            await self._sign_in(account)
            accounts = await self.adb("shell", "dumpsys", "account")
            if f"name={account.email}, type=com.google" not in accounts:
                raise CanaryBlockedError("expected Google account not present; retain guest")
        self.recovery["login_confirmed"] = True
        await self.save(self.recovery)
        await self._photos_ready()

    # Every screen this walk recognises, most specific first. The store's own
    # prompt is last because "Sign in" is also the heading of the GMS email
    # page: matching it first would tap a static heading forever instead of
    # entering the address.
    SIGN_IN_SCREENS = (
        "Google Photos",
        "Search apps",
        "Forgot email?",
        "Show password",
        "Google Terms of Service",
        # The consent page that follows the terms. Its heading scrolls away
        # when ACCEPT is brought into view, so the walk names it by the one
        # line that stays on screen through the scroll.
        "Back up device data",
        "Sign in",
    )

    def _sign_in_screen_name(self, attributes: list[dict]) -> str | None:
        text = "\n".join(a.get("text", "") for a in attributes)
        return next((marker for marker in self.SIGN_IN_SCREENS if marker in text), None)

    async def _sign_in_screen(self, previous: str | None, attempts: int = 12, delay: float = 5.0) -> str:
        """Wait for a screen this walk knows and has not just acted on.

        Two asynchronous repaints sit between the walk and the UI. The store
        deep link and each credential page paint late, so a single read lands on
        a blank frame; and a submitted credential leaves the page it was typed
        into on screen for several seconds while the WebView navigates. Naming
        the page the walk just acted on as if it were a fresh one makes the walk
        re-enter that credential, which the replay guard then refuses. So a
        screen only counts once it differs from the one just handled.
        """
        for remaining in range(attempts, 0, -1):
            screen = self._sign_in_screen_name(await self.attributes())
            if screen is not None and screen != previous:
                return screen
            if remaining > 1:
                await asyncio.sleep(delay)
        if previous is not None:
            raise CanaryBlockedError(f"sign-in did not advance past {previous!r}; retain credentials and guest state")
        raise CanaryBlockedError("unrecognized sign-in screen; retain credentials and guest state")

    async def _sign_in(self, account: Account) -> None:
        if not account.password:
            raise CanaryBlockedError("dedicated account secret required")
        # An interrupted attempt leaves the device part-way through the Google
        # setup pages, where the account is not added yet. Opening the store
        # again would abandon that progress and start the walk over, so the
        # store is only opened when nothing the walk recognises is on screen.
        if self._sign_in_screen_name(await self.attributes()) is None:
            await self.flow("open-store")
        previous: str | None = None
        for _ in range(8):
            screen = await self._sign_in_screen(previous)
            if screen in ("Google Photos", "Search apps"):
                return
            if screen == "Forgot email?":
                await self._credential_step("email", account)
            elif screen == "Show password":
                await self._credential_step("password", account)
            elif screen == "Google Terms of Service":
                await self.flow("agree")
            elif screen == "Back up device data":
                await self.flow("google-services")
            else:
                # Photos is not installed yet, so the tap belongs to the store.
                await self.commands([{"tapOn": "Sign in"}], app_id="com.android.vending")
            previous = screen
        raise CanaryBlockedError("bounded sign-in steps exhausted")

    async def _credential_step(self, name: str, account: Account) -> None:
        key = name + "_submitted"
        if self.recovery.get(key):
            raise UserActionRequiredError("credential step already attempted; inspect device before resume")
        self.recovery[key] = True
        await self.receipt("credential_" + name)
        await self.flow(name, credentials={"email": account.email, "password": account.password or ""})
        # Submission completion is not proof of account identity. login() checks
        # the retained account independently before media operations proceed.
        self.recovery["operations"]["credential_" + name]["completed_at"] = datetime.now(timezone.utc).isoformat()
        await self.save(self.recovery)

    async def _install_photos(self) -> None:
        installed = await self.adb("shell", "pm", "path", PHOTOS_PACKAGE, answers_with_exit_code=True)
        if "package:" not in installed:
            await self.flow("open-store")
            text = "\n".join(a.get("text", "") for a in await self.attributes())
            if "Install" in text and not self.recovery.get("install_intent"):
                self.recovery["install_intent"] = True
                await self.save(self.recovery)
                await self.flow("install")
            for _ in range(60):
                if "package:" in await self.adb("shell", "pm", "path", PHOTOS_PACKAGE, answers_with_exit_code=True):
                    break
                await asyncio.sleep(5)
            else:
                raise CanaryBlockedError("Photos installation not completed; retain guest")

    async def _open_photos_home(self) -> None:
        """Bring Photos to its own home screen without discarding the session.

        ``launchApp`` is wrong here in both of its modes. By default it
        force-stops the app first, which throws away whatever the previous
        attempt left on screen -- including a share sheet a retry exists to
        resume -- and, when that teardown is still in flight, the launch itself
        is dropped and the app behind Photos stays in front. Told not to stop the
        app, it resumes the task wherever it happened to be, so an attempt that
        died on the item pager never reaches the toolbar this walk needs.

        A launcher intent with CLEAR_TOP does the one right thing in both cases:
        it starts Photos when it is not running, and otherwise brings the
        existing task forward and finishes whatever is stacked above its home
        screen.
        """
        await self.adb(
            "shell",
            "am",
            "start",
            "-a",
            "android.intent.action.MAIN",
            "-c",
            "android.intent.category.LAUNCHER",
            "-p",
            PHOTOS_PACKAGE,
            "--activity-clear-top",
        )

    async def _photos_home(self, attempts: int = 8) -> None:
        """Wait for the Photos home screen, opening it as often as it takes.

        Opening Photos reports success once the intent has been delivered, which
        is not the same thing as the home screen having painted. The account
        disc is the first element the rest of the walk needs from it, so that is
        what readiness means here. Anything else on screen is either a dialog to
        answer or a reason to ask for the home screen again, and the dialogs are
        answered first because they are drawn over a home screen whose disc is
        still in the hierarchy behind them.
        """
        for remaining in range(attempts, 0, -1):
            attrs = await self.attributes()
            text = "\n".join(a.get("text", "") for a in attrs)
            if "Allow Photos to access photos and videos on this device?" in text:
                await self.flow("allow-media")
            elif "Allow Photos to send you notifications?" in text:
                await self.flow("allow-notifications")
            elif "Allow Photos to access your contacts?" in text:
                # Left over from an interrupted share: the dialog outlives the
                # attempt that raised it and covers every screen after it.
                await self.flow("deny-contacts")
            elif any(a.get("resource-id", "").endswith("/onboarding_disclaimer") for a in attrs):
                await self.flow("enable-backup")
            elif any(a.get("resource-id", "").endswith("/selected_account_disc") for a in attrs):
                return
            elif remaining > 1:
                await self._open_photos_home()
        raise CanaryBlockedError("Photos home screen did not open; retain guest")

    async def _photos_ready(self) -> None:
        await self._install_photos()
        await self._photos_home()
        await self.flow("account")
        if not self.recovery.get("quota_before"):
            self.recovery["quota_before"] = await self.quota_observation()
            await self.save(self.recovery)
        await self.flow("backup-settings")
        attrs = await self.attributes()
        if not any(
            a.get("resource-id", "").endswith("/photos_backup_overview_quality") and a.get("text") == "Original"
            for a in attrs
        ):
            raise CanaryBlockedError("Original quality not confirmed; no media published")
        self.recovery["original_setting"] = True
        await self.save(self.recovery)

    async def quota_observation(self) -> dict:
        attrs = await self.attributes()
        values = {a.get(key, "") for a in attrs for key in ("text", "accessibilityText")}
        matches = [v for v in values if re.fullmatch(r"[0-9,.]+ MB of [0-9,.]+ GB", v)]
        if len(matches) != 1:
            raise CanaryBlockedError("account storage observation unavailable; UI review required")
        return {
            "display": matches[0],
            "display_resolution": "0.01 MB",
            "backup_complete": "Backup complete" in values,
            "collector": "Maestro hierarchy; account storage subtitle",
        }

    async def push_file(self, session: RedroidSession, local_path: str) -> str:
        self.guard(session)
        path = Path(local_path)
        if (
            path.is_symlink()
            or path.name != self.part.filename
            or path.stat().st_size != self.part.size_bytes
            or await asyncio.to_thread(sha256, path) != self.part.sha256
        ):
            raise CanaryBlockedError("part changed before upload")
        stage = "/data/local/tmp/" + self.part.filename
        remote = "/sdcard/DCIM/Camera/" + self.part.filename
        if not self.recovery.get("push_intent"):
            self.recovery["push_intent"] = True
            await self.receipt("push")
            await self.adb("push", self.source_root + "/" + self.part.filename, stage, timeout=21600)
        # An interrupted push may have left partial bytes. Never infer successful publication.
        if (await self.adb("shell", "sha256sum", stage, timeout=21600)).split()[0] != self.part.sha256:
            raise CanaryBlockedError("guest stage hash mismatch; retained for reconciliation")
        await self.receipt("push", complete=True)
        if not self.recovery.get("publish_intent"):
            self.recovery["publish_intent"] = True
            await self.receipt("publish")
            await self.adb("shell", "mkdir", "-p", "/sdcard/DCIM/Camera")
            await self.adb("shell", "cp", stage, remote, timeout=21600)
        if (await self.adb("shell", "sha256sum", remote, timeout=21600)).split()[0] != self.part.sha256:
            raise CanaryBlockedError("published guest file hash mismatch")
        await self.receipt("publish", complete=True)
        await self._media_store(remote)
        return remote

    def _media_store_scanned(self, rows: str) -> bool:
        """True once the provider reports this part's own byte count."""
        return bool(re.search(rf"\b_size={self.part.size_bytes}(?:,|\s|$)", rows))

    async def _media_store(
        self,
        remote: str,
        *,
        readonly: bool = False,
        attempts: int = MEDIA_SCAN_ATTEMPTS,
        delay: float = MEDIA_SCAN_DELAY_SECONDS,
    ) -> None:
        async def query() -> str:
            return await self.adb(
                "shell",
                "content query --uri content://media/external/video/media "
                "--projection _id:_display_name:_size "
                f'''--where "_display_name='{self.part.filename}'"''',
            )

        rows = await query()
        if not readonly and not self._media_store_scanned(rows):
            # A published file can reach MediaStore before the provider has
            # read it, as a row with the right name and a NULL _size. That is
            # an unscanned row, not a contradicted identity, and it needs the
            # same scan the missing row gets. Scanning refreshes the provider's
            # view of a path it already holds -- it is not a provider-side
            # effect and cannot produce a second upload -- so it is safe on a
            # later attempt too, unlike the push this method never repeats.
            self.recovery["scan_intent"] = True
            await self.save(self.recovery)
            await self.adb("shell", media_provider_scan_command(remote))
            for remaining in range(attempts, 0, -1):
                rows = await query()
                if self._media_store_scanned(rows):
                    break
                if remaining > 1:
                    await asyncio.sleep(delay)
        ids = re.findall(r"\b_id=(\d+)", rows)
        if (
            len(ids) != 1
            or f"_display_name={self.part.filename}" not in rows
            or not self._media_store_scanned(rows)
            or self.recovery.get("media_id", ids[0]) != ids[0]
        ):
            raise CanaryBlockedError("MediaStore identity ambiguous; no repush")
        if not readonly:
            self.recovery["media_id"] = ids[0]
            await self.save(self.recovery)

    async def trigger_upload(self, session: RedroidSession, remote_path: str) -> None:
        """Record the intent to back this part up, and nothing more.

        Backup is not something this process starts: it happens because Original
        quality and backup were confirmed during onboarding. The wait for the
        actual backed-up item lives in :meth:`MaestroPartVerifier.wait_for_share_url`,
        which polls the item's own details rather than assuming a trigger worked.
        """
        self.guard(session)
        if not self.recovery.get("original_setting") or not self.recovery.get("media_id"):
            raise CanaryBlockedError("Original setting and MediaStore identity required")
        self.recovery["backup_intent"] = True
        await self.receipt("backup")
        # Original backup is enabled before publication; wait for the exact item.

    async def open_details(self) -> None:
        media_id = str(self.recovery["media_id"])
        if not media_id.isdigit():
            raise CanaryBlockedError("invalid MediaStore ID")
        await self.adb(
            "shell",
            "am",
            "start",
            "-a",
            "android.intent.action.VIEW",
            "-d",
            "content://media/external/video/media/" + media_id,
            "-t",
            "video/mp4",
            "-p",
            PHOTOS_PACKAGE,
        )
        await self.flow("item-details")
        attrs = await self.attributes()
        if any(a.get("text") == "About" for a in attrs):
            await self.flow("about")

    async def clipboard(self) -> str | None:
        def read() -> str | None:
            output = self.guest.exec_run(["cmd", "package", "list", "packages", "-U", PHOTOS_PACKAGE])
            uid = re.search(r"uid:(\d+)", output.output.decode())
            if uid is None:
                raise CanaryBlockedError("Photos UID unavailable")
            result = self.guest.exec_run(
                ["service", "call", "clipboard", "4", "s16", PHOTOS_PACKAGE, "i32", "0"], user=uid[1]
            )
            words = []
            for line in result.output.decode(errors="replace").splitlines():
                if re.search(r"0x[0-9a-f]+:", line):
                    words.extend(re.findall(r"\b[0-9a-f]{8}\b", line.split(":", 1)[1].split("'")[0]))
            text = b"".join(struct.pack("<I", int(w, 16)) for w in words).decode("utf-16le", errors="ignore")
            urls = set(re.findall(r"https://photos\.app\.goo\.gl/[A-Za-z0-9_-]+", text))
            return urls.pop() if len(urls) == 1 else None

        return await asyncio.to_thread(read)


class MaestroPartVerifier:
    def __init__(self, uploader: MaestroPartUploader) -> None:
        # validate_share_url() fetches the sharing location, and httpx logs the
        # whole request URL at INFO. A Photos share link is a bearer capability:
        # anyone holding it can read the media, so it must not reach a log.
        install_http_url_log_redaction()
        self.uploader = uploader

    async def wait_for_share_url(self, session: RedroidSession, timeout: int = 300) -> str:
        upload = self.uploader
        upload.guard(session)
        deadline = time.monotonic() + min(timeout, 21600)
        while time.monotonic() < deadline:
            await upload.open_details()
            attrs = await upload.attributes()
            streams = upload.part.media_info["streams"]
            video = next(s for s in streams if s["codec_type"] == "video")
            try:
                item = collect_item(attrs, upload.part.filename, {**video, "sha256": upload.part.sha256})
            except CanaryBlockedError:
                await asyncio.sleep(15)
                continue
            upload.backup_evidence = {**item, "backed_up": True, "original_quality": True}
            await upload.receipt("backup", complete=True)
            return await self._share()
        raise CanaryBlockedError("backup deadline exceeded; guest retained")

    async def _share(self) -> str:
        upload = self.uploader
        if upload.recovery.get("share_url"):
            return str(upload.recovery["share_url"])
        if not upload.recovery.get("link_intent"):
            # Only the last step creates a sharing location. Opening the item
            # and the share sheet leaves nothing behind that outlives the
            # screen, so a failure there -- a permission dialog covering the
            # sheet, say -- must be retryable; treating the whole block as one
            # effect stopped a segment whose link had never been created.
            if "clipboard_before" not in upload.recovery:
                upload.recovery["clipboard_before"] = await upload.clipboard()
            upload.recovery["share_intent"] = True
            await upload.save(upload.recovery)
            await upload.commands(
                [
                    {"assertVisible": upload.part.filename},
                    {"tapOn": {"id": "com.google.android.apps.photos:id/collapse_info_panel_fab"}},
                ]
            )
            await upload.flow("open-share")
            upload.recovery["link_intent"] = True
            await upload.receipt("share")
            await upload.flow("create-link")
        # After interruption only read the existing clipboard. Never create another link.
        url = await upload.clipboard()
        if not url or url == upload.recovery.get("clipboard_before"):
            raise CanaryBlockedError("share intent unresolved; inspect existing sharing location before resume")
        upload.recovery["share_url"] = url
        await upload.receipt("share", complete=True)
        return url

    async def validate_share_url(self, share_url: str) -> bool:
        if not share_url.startswith(("https://photos.app.goo.gl/", "https://photos.google.com/")):
            return False
        async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
            return (await client.get(share_url)).status_code == 200
