"""Replay safety at the calibrated Maestro/ADB boundaries; no mock live claims."""

import hashlib
import logging
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from pixav.pixel_injector.canary import OWNER_LABEL, CanaryBlockedError
from pixav.pixel_injector.interfaces import FileUploader, UploadVerifier
from pixav.pixel_injector.maestro_parts import MaestroPartUploader, MaestroPartVerifier, UserActionRequiredError
from pixav.pixel_injector.session import RedroidSession
from pixav.shared.models import Account, VideoPart

ACCOUNT_DISC = {"resource-id": "com.google.android.apps.photos:id/selected_account_disc"}


@pytest.fixture
def upload(tmp_path):
    owner, video = str(uuid.uuid4()), uuid.uuid4()
    digest = hashlib.sha256(b"original").hexdigest()
    part = VideoPart(
        video_id=video,
        part_index=1,
        manifest_version=1,
        start_seconds=10,
        end_seconds=20,
        size_bytes=8,
        sha256=digest,
        filename=f"pixav-{video}-part-000001-{digest[:16]}.mp4",
        media_info={"streams": [{"codec_type": "video", "width": 3840, "height": 2160}]},
    )
    guest = SimpleNamespace(id="guest", labels={OWNER_LABEL: owner, "pixav.photos_canary.role": "guest"})
    runner = SimpleNamespace(id="tools", labels={OWNER_LABEL: owner, "pixav.photos_canary.role": "tools"})
    u = MaestroPartUploader(guest, runner, owner, part, {}, AsyncMock(), Path("config/maestro/photos-canary"))
    u.adb, u.flow, u.commands = AsyncMock(), AsyncMock(), AsyncMock()
    return u, RedroidSession(owner, "guest", "127.0.0.1", 5555), tmp_path


def test_existing_upload_contracts(upload):
    u, session, _ = upload
    assert isinstance(u, FileUploader)
    assert isinstance(MaestroPartVerifier(u), UploadVerifier)
    u.guard(session)
    u.guest.labels[OWNER_LABEL] = "someone-else"
    with pytest.raises(CanaryBlockedError, match="ownership"):
        u.guard(session)


async def test_challenge_and_password_intent_never_repeat_credentials(upload):
    u, _, _ = upload
    with patch("pixav.pixel_injector.maestro_parts.hierarchy", return_value=[{"text": "Verify it's you"}]):
        with pytest.raises(UserActionRequiredError):
            await u.attributes()
    u.save.assert_awaited()
    account = Account(email="fixture@example.invalid", password="fixture")
    await u._credential_step("password", account)
    assert u.recovery["password_submitted"]
    with pytest.raises(UserActionRequiredError):
        await u._credential_step("password", account)
    assert u.flow.await_count == 1


async def test_confirmed_login_skips_credentials_and_rechecks_original(upload):
    u, session, _ = upload
    u.adb.side_effect = ["", "Account {name=fixture@example.invalid, type=com.google}"]
    u._photos_ready = AsyncMock()
    u._sign_in = AsyncMock()
    await u.login(session, Account(email="fixture@example.invalid"))
    u._sign_in.assert_not_awaited()
    u._photos_ready.assert_awaited_once()


async def test_original_quality_and_account_observation_are_required(upload):
    u, _, _ = upload
    u.adb.return_value = "package:photos"
    u.attributes = AsyncMock(
        side_effect=[
            [ACCOUNT_DISC],
            [{"text": "124.88 MB of 15 GB"}, {"text": "Backup complete"}],
            [{"resource-id": "com.google.android.apps.photos:id/photos_backup_overview_quality", "text": "Original"}],
        ]
    )
    await u._photos_ready()
    assert u.recovery["original_setting"]
    assert u.recovery["quota_before"]["display"] == "124.88 MB of 15 GB"
    u.attributes = AsyncMock(side_effect=[[ACCOUNT_DISC], []])
    with pytest.raises(CanaryBlockedError, match="Original"):
        await u._photos_ready()


async def test_resume_reconciles_guest_and_media_store_without_repush_bdd_048_003(upload):
    """A retried execution reconciles the existing effect instead of repeating it."""
    u, session, root = upload
    path = root / u.part.filename
    path.write_bytes(b"original")
    u.recovery.update(push_intent=True, publish_intent=True, media_id="42")
    u.adb.side_effect = [
        u.part.sha256 + " stage",
        u.part.sha256 + " remote",
        f"Row: 0 _id=42, _display_name={u.part.filename}, _size=8",
    ]
    remote = await u.push_file(session, str(path))
    assert remote.endswith(u.part.filename)
    assert all(call.args[0] != "push" for call in u.adb.await_args_list)
    assert all("cp" not in call.args for call in u.adb.await_args_list)
    assert u.recovery["media_id"] == "42"


async def test_fresh_push_journals_each_effect_before_call(upload):
    u, session, root = upload
    path = root / u.part.filename
    path.write_bytes(b"original")
    effects = []

    async def adb(*args, **kwargs):
        if args[0] == "push":
            assert u.recovery["push_intent"]
        if "cp" in args:
            assert u.recovery["publish_intent"]
        effects.append(args)
        if "sha256sum" in args:
            return u.part.sha256 + " file"
        if args[0] == "shell" and args[1].startswith("content query"):
            return f"Row: 0 _id=42, _display_name={u.part.filename}, _size=8"
        return ""

    u.adb.side_effect = adb
    await u.push_file(session, str(path))
    assert sum(args[0] == "push" for args in effects) == 1
    u.recovery["original_setting"] = True
    await u.trigger_upload(session, "remote")
    assert u.recovery["backup_intent"]


async def test_corrupt_stage_and_ambiguous_media_store_stop(upload):
    u, session, root = upload
    path = root / u.part.filename
    path.write_bytes(b"original")
    u.recovery["push_intent"] = True
    u.adb.return_value = "wrong hash"
    with pytest.raises(CanaryBlockedError, match="stage hash"):
        await u.push_file(session, str(path))
    u.adb.return_value = "Row: 0 _id=1\nRow: 1 _id=2"
    with pytest.raises(CanaryBlockedError, match="ambiguous"):
        await u._media_store("remote", attempts=1)


async def test_share_created_then_crash_reads_existing_link_only(upload):
    u, _, _ = upload
    u.recovery.update(link_intent=True, clipboard_before="https://photos.app.goo.gl/previous")
    u.clipboard = AsyncMock(return_value="https://photos.app.goo.gl/existing")
    verifier = MaestroPartVerifier(u)
    assert await verifier._share() == "https://photos.app.goo.gl/existing"
    assert await verifier._share() == "https://photos.app.goo.gl/existing"
    u.flow.assert_not_awaited()
    u.commands.assert_not_awaited()
    assert u.clipboard.await_count == 1


async def test_uncertain_share_never_creates_another_album(upload):
    u, _, _ = upload
    u.recovery.update(link_intent=True, clipboard_before="https://photos.app.goo.gl/previous")
    u.clipboard = AsyncMock(return_value="https://photos.app.goo.gl/previous")
    with pytest.raises(CanaryBlockedError, match="unresolved"):
        await MaestroPartVerifier(u)._share()
    u.flow.assert_not_awaited()


async def test_exact_backup_is_required_before_sharing(upload):
    u, session, _ = upload
    u.open_details = AsyncMock()
    u.attributes = AsyncMock(
        return_value=[{"text": t} for t in [u.part.filename, "Backed up", "Original quality", "3840 x 2160"]]
    )
    verifier = MaestroPartVerifier(u)
    verifier._share = AsyncMock(return_value="https://photos.app.goo.gl/verified")
    assert await verifier.wait_for_share_url(session) == "https://photos.app.goo.gl/verified"
    assert u.backup_evidence["source_sha256"] == u.part.sha256
    with pytest.raises(CanaryBlockedError, match="deadline"):
        await verifier.wait_for_share_url(session, timeout=0)


async def test_long_guest_effect_stops_owned_writers_after_guard_failure(upload):
    import threading

    u, _, _ = upload
    stopped = threading.Event()
    events = []
    checks = 0

    def check():
        nonlocal checks
        checks += 1
        if checks > 1:
            raise CanaryBlockedError("heartbeat unavailable")

    def operation():
        assert stopped.wait(5)
        return "partial transfer stopped"

    def stop(role, **_kwargs):
        events.append(role)
        stopped.set()

    u.check = check
    u.runner.stop = lambda **kwargs: stop("tools", **kwargs)
    u.guest.stop = lambda **kwargs: stop("guest", **kwargs)
    with pytest.raises(CanaryBlockedError, match="heartbeat"):
        await u._io(operation)
    assert events == ["tools", "guest"]


async def test_readonly_media_store_never_scans_or_changes_receipt(upload):
    u, _, _ = upload
    u.recovery["media_id"] = "42"
    u.adb.return_value = "No result found"
    with pytest.raises(CanaryBlockedError, match="ambiguous"):
        await u._media_store("remote", readonly=True)
    assert u.adb.await_count == 1
    u.save.assert_not_awaited()
    assert "scan_intent" not in u.recovery
    u.adb.return_value = f"Row: 0 _id=42, _display_name={u.part.filename}, _size=8"
    await u._media_store("remote", readonly=True)
    u.save.assert_not_awaited()


async def test_managed_segments_are_pushed_from_their_own_asset_directory(upload):
    """One staging root serves every asset, so a push names the asset it belongs to."""
    u, session, root = upload
    u.source_root = "/parts/asset-1"
    path = root / u.part.filename
    path.write_bytes(b"original")

    async def adb(*args, **kwargs):
        if "sha256sum" in args:
            return u.part.sha256 + " file"
        if args[0] == "shell" and args[1].startswith("content query"):
            return f"Row: 0 _id=42, _display_name={u.part.filename}, _size=8"
        return ""

    u.adb.side_effect = adb
    await u.push_file(session, str(path))

    pushes = [call.args for call in u.adb.await_args_list if call.args[0] == "push"]
    assert pushes and pushes[0][1] == f"/parts/asset-1/{u.part.filename}"


async def test_the_isolated_flow_keeps_addressing_parts_directly(upload):
    """The single-movie run mounts one video's parts, so its source is unchanged."""
    u, _, _ = upload
    assert u.source_root == "/parts"


async def test_a_transitional_frame_is_not_an_unknown_sign_in_screen(upload):
    """The store deep link repaints asynchronously; the first read is often blank."""
    u, _, _ = upload
    screens = [[{"text": ""}], [{"text": ""}], [{"text": "Sign in to find the latest Android apps"}]]
    u.attributes = AsyncMock(side_effect=screens)

    with patch("pixav.pixel_injector.maestro_parts.asyncio.sleep", AsyncMock()):
        assert await u._sign_in_screen(None) == "Sign in"
    assert u.attributes.await_count == 3


async def test_a_screen_that_stays_unknown_still_stops_the_walk(upload):
    u, _, _ = upload
    u.attributes = AsyncMock(return_value=[{"text": "something else entirely"}])

    with patch("pixav.pixel_injector.maestro_parts.asyncio.sleep", AsyncMock()):
        with pytest.raises(CanaryBlockedError, match="unrecognized sign-in screen"):
            await u._sign_in_screen(None, attempts=3, delay=0)
    assert u.attributes.await_count == 3


async def test_the_page_a_credential_was_typed_into_is_not_read_as_a_new_screen(upload):
    """A submitted page stays on screen while the WebView navigates.

    Naming it again would re-enter the same credential, which the replay guard
    refuses -- the live run stopped exactly there.
    """
    u, _, _ = upload
    u.attributes = AsyncMock(
        side_effect=[
            [{"text": "Sign in"}, {"text": "Forgot email?"}],
            [{"text": "Sign in"}, {"text": "Forgot email?"}],
            [{"text": "Sign in"}, {"text": "Show password"}],
        ]
    )

    with patch("pixav.pixel_injector.maestro_parts.asyncio.sleep", AsyncMock()):
        assert await u._sign_in_screen("Forgot email?") == "Show password"
    assert u.attributes.await_count == 3


async def test_a_page_that_never_advances_names_the_page_it_was_stuck_on(upload):
    u, _, _ = upload
    u.attributes = AsyncMock(return_value=[{"text": "Forgot email?"}])

    with patch("pixav.pixel_injector.maestro_parts.asyncio.sleep", AsyncMock()):
        with pytest.raises(CanaryBlockedError, match="did not advance past 'Forgot email\\?'"):
            await u._sign_in_screen("Forgot email?", attempts=2, delay=0)


async def test_the_email_page_is_entered_rather_than_tapped_as_a_heading(upload):
    """The GMS email page carries "Sign in" as its own heading, so a generic
    tap on that word would loop on a static label and never submit the address."""
    u, _, _ = upload
    u.attributes = AsyncMock(
        side_effect=[
            [{"text": "launcher"}],
            [{"text": "Sign in"}, {"text": "Forgot email?"}],
            [{"text": "Sign in"}, {"text": "Show password"}],
            [{"text": "Search apps"}],
        ]
    )

    await u._sign_in(Account(email="fixture@example.invalid", password="fixture"))

    assert u.recovery["email_submitted"] and u.recovery["password_submitted"]
    assert [call.args[0] for call in u.flow.await_args_list] == ["open-store", "email", "password"]
    u.commands.assert_not_awaited()


async def test_the_store_sign_in_prompt_taps_the_store_not_the_absent_photos_app(upload):
    u, _, _ = upload
    u.attributes = AsyncMock(
        side_effect=[
            [{"text": "launcher"}],
            [{"text": "Sign in to find the latest Android apps"}],
            [{"text": "Search apps"}],
        ]
    )

    await u._sign_in(Account(email="fixture@example.invalid", password="fixture"))

    u.commands.assert_awaited_once_with([{"tapOn": "Sign in"}], app_id="com.android.vending")


async def test_the_services_consent_page_after_the_terms_is_accepted(upload):
    """Sign-in does not end at the terms; a consent page follows it."""
    u, _, _ = upload
    u.attributes = AsyncMock(
        side_effect=[
            [{"text": "Google Terms of Service"}],
            [{"text": "Google Terms of Service"}],
            [{"text": "Backup"}, {"text": "Back up device data"}],
            [{"text": "Google Photos"}],
        ]
    )

    await u._sign_in(Account(email="fixture@example.invalid", password="fixture"))

    assert [call.args[0] for call in u.flow.await_args_list] == ["agree", "google-services"]


async def test_a_part_finished_setup_is_continued_rather_than_restarted(upload):
    """Re-opening the store would abandon an account add that is under way."""
    u, _, _ = upload
    u.attributes = AsyncMock(side_effect=[[{"text": "Back up device data"}], [{"text": "Google Photos"}]])

    await u._sign_in(Account(email="fixture@example.invalid", password="fixture"))

    assert "open-store" not in [call.args[0] for call in u.flow.await_args_list]


async def test_an_unrecognised_screen_still_opens_the_store_first(upload):
    u, _, _ = upload
    u.attributes = AsyncMock(side_effect=[[{"text": "launcher"}], [{"text": "Search apps"}]])

    await u._sign_in(Account(email="fixture@example.invalid", password="fixture"))

    assert u.flow.await_args_list[0].args[0] == "open-store"


async def test_an_absent_package_is_an_answer_not_a_broken_guest(upload):
    """`pm path` exits non-zero when the package is missing; that is the "no"
    `_install_photos` asks for, not an ADB failure that should stop the run."""
    u, _, _ = upload
    del u.adb
    u.runner.client = SimpleNamespace(api=SimpleNamespace(timeout=60))
    u.runner.exec_run = lambda command: SimpleNamespace(exit_code=1, output=b"")

    assert await u.adb("shell", "pm", "path", "x", answers_with_exit_code=True) == ""
    with pytest.raises(CanaryBlockedError, match="ADB operation failed"):
        await u.adb("shell", "pm", "path", "x")


async def test_an_unscanned_row_is_scanned_rather_than_called_ambiguous(upload):
    """MediaStore shows the row before the provider has read the file.

    The live run published 128,844,265 bytes and MediaStore answered with the
    right name and `_size=NULL`; judging that row as a contradicted identity
    stopped the upload one step before it could start.
    """
    u, _, _ = upload
    unscanned = f"Row: 0 _id=42, _display_name={u.part.filename}, _size=NULL"
    scanned = f"Row: 0 _id=42, _display_name={u.part.filename}, _size={u.part.size_bytes}"
    calls: list = []

    async def adb(*args, **kwargs):
        calls.append(args)
        if args[1].startswith("content query"):
            return scanned if any("scan_file" in str(a) for a in calls) else unscanned
        return ""

    u.adb.side_effect = adb

    await u._media_store("/sdcard/DCIM/Camera/" + u.part.filename, delay=0)

    assert u.recovery["media_id"] == "42"
    assert sum("scan_file" in str(args) for args in calls) == 1


async def test_a_row_that_never_finishes_scanning_is_still_refused(upload):
    u, _, _ = upload
    u.adb.return_value = f"Row: 0 _id=42, _display_name={u.part.filename}, _size=NULL"

    with pytest.raises(CanaryBlockedError, match="ambiguous"):
        await u._media_store("/sdcard/DCIM/Camera/" + u.part.filename, attempts=2, delay=0)
    assert "media_id" not in u.recovery


async def test_a_share_sheet_that_never_opened_is_retried_without_a_second_link(upload):
    """Opening the item and the sheet leaves nothing behind, so it may repeat.

    The live run tapped Share, Photos asked for contacts, the dialog covered
    the sheet and the flow timed out -- with no link created. Gating that on
    the same fact as the link creation stopped the segment for good.
    """
    u, _, _ = upload
    u.recovery.update(share_intent=True, clipboard_before=None)
    u.clipboard = AsyncMock(return_value="https://photos.app.goo.gl/created")

    assert await MaestroPartVerifier(u)._share() == "https://photos.app.goo.gl/created"

    assert [call.args[0] for call in u.flow.await_args_list] == ["open-share", "create-link"]
    assert u.recovery["link_intent"] is True


async def test_the_link_is_journalled_before_the_flow_that_creates_it(upload):
    u, _, _ = upload
    seen: list = []

    async def flow(name, **kwargs):
        seen.append((name, u.recovery.get("link_intent")))

    u.flow.side_effect = flow
    u.clipboard = AsyncMock(side_effect=[None, "https://photos.app.goo.gl/created"])

    await MaestroPartVerifier(u)._share()

    assert seen == [("open-share", None), ("create-link", True)]


async def test_an_existing_clipboard_reading_is_not_overwritten_on_retry(upload):
    """clipboard_before is what proves a link is new; re-reading it after a
    link exists would compare the new link against itself."""
    u, _, _ = upload
    u.recovery.update(share_intent=True, clipboard_before="https://photos.app.goo.gl/previous")
    u.clipboard = AsyncMock(return_value="https://photos.app.goo.gl/previous")

    with pytest.raises(CanaryBlockedError, match="unresolved"):
        await MaestroPartVerifier(u)._share()
    assert u.recovery["clipboard_before"] == "https://photos.app.goo.gl/previous"


async def test_a_contacts_dialog_left_by_an_interrupted_share_is_cleared(upload):
    """The dialog outlives the attempt that raised it and covers what follows."""
    u, _, _ = upload
    u._install_photos = AsyncMock()
    u.quota_observation = AsyncMock(return_value={})
    u.recovery["quota_before"] = {"display": "1 MB of 15 GB"}
    u.attributes = AsyncMock(
        side_effect=[
            [{"text": "Allow Photos to access your contacts?"}],
            [ACCOUNT_DISC],
            [{"resource-id": "com.google.android.apps.photos:id/photos_backup_overview_quality", "text": "Original"}],
        ]
    )

    await u._photos_ready()

    assert "deny-contacts" in [call.args[0] for call in u.flow.await_args_list]
    assert u.recovery["original_setting"] is True


async def test_a_screen_that_is_not_photos_home_is_cleared_to_it_bdd_048_003(upload):
    """Delivering the intent is not the app being in front, nor at its home screen."""
    u, _, _ = upload
    u._install_photos = AsyncMock()
    u.recovery["quota_before"] = {"display": "1 MB of 15 GB"}
    u.attributes = AsyncMock(
        side_effect=[
            # The store, left in front after a launch was dropped.
            [{"text": "Google Photos"}, {"text": "Uninstall"}],
            [ACCOUNT_DISC],
            [{"resource-id": "com.google.android.apps.photos:id/photos_backup_overview_quality", "text": "Original"}],
        ]
    )

    await u._photos_ready()

    assert [call.args[0] for call in u.flow.await_args_list] == ["account", "backup-settings"]
    # CLEAR_TOP is the whole point: it finishes the item pager or share sheet a
    # previous attempt died on, without force-stopping the session.
    assert u.adb.await_args.args == (
        "shell",
        "am",
        "start",
        "-a",
        "android.intent.action.MAIN",
        "-c",
        "android.intent.category.LAUNCHER",
        "-p",
        "com.google.android.apps.photos",
        "--activity-clear-top",
    )


async def test_a_home_screen_that_never_opens_stops_the_walk(upload):
    """No account disc means no toolbar; tapping one anyway is what wedged gen 10."""
    u, _, _ = upload
    u._install_photos = AsyncMock()
    u.attributes = AsyncMock(return_value=[{"text": "Google Photos"}, {"text": "Uninstall"}])

    with pytest.raises(CanaryBlockedError, match="Photos home screen did not open"):
        await u._photos_home(attempts=3)

    # Bounded: it asks twice and then stops rather than tapping at nothing.
    assert u.adb.await_count == 2
    assert u.flow.await_count == 0


def test_opening_photos_never_force_stops_a_retained_session():
    """A force-stop discards the share sheet a retry exists to resume."""
    flow = (Path("config/maestro/photos-canary/launch-photos.yaml")).read_text()
    assert "stopApp: false" in flow
    assert "clearState" not in flow


async def test_the_sharing_location_never_reaches_a_log(upload):
    """A Photos share link is a bearer capability, and httpx logs whole URLs."""
    u, _, _ = upload
    logging.getLogger("httpx").filters.clear()

    MaestroPartVerifier(u)

    record = logging.LogRecord(
        "httpx",
        logging.INFO,
        __file__,
        1,
        'HTTP Request: GET %s "HTTP/1.1 302 Found"',
        ("https://photos.app.goo.gl/sEcReT",),
        None,
    )
    assert all(item.filter(record) for item in logging.getLogger("httpx").filters)
    assert "sEcReT" not in record.getMessage()
    assert "<redacted-http-url>" in record.getMessage()


@pytest.mark.parametrize("name", ["deny-contacts", "open-share"])
def test_the_contacts_dialog_is_refused_in_both_of_its_shapes(name):
    """Android swaps the negative button once the permission has been asked before.

    Naming only the first-ask id left the dialog covering the share sheet, and
    the flow meant to clear it failed on a button that was not there.
    """
    flow = (Path("config/maestro/photos-canary") / f"{name}.yaml").read_text()
    assert "com.android.permissioncontroller:id/permission_deny_button" in flow
    assert "com.android.permissioncontroller:id/permission_deny_and_dont_ask_again_button" in flow
    # Whichever button is absent must not fail the flow, and the dialog going
    # away is what is actually asserted.
    assert flow.count("optional: true") == 2
    assert 'assertNotVisible: "Allow Photos to access your contacts?"' in flow
