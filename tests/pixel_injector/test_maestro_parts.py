"""Replay safety at the calibrated Maestro/ADB boundaries; no mock live claims."""

import hashlib
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
            [],
            [{"text": "124.88 MB of 15 GB"}, {"text": "Backup complete"}],
            [{"resource-id": "com.google.android.apps.photos:id/photos_backup_overview_quality", "text": "Original"}],
        ]
    )
    await u._photos_ready()
    assert u.recovery["original_setting"]
    assert u.recovery["quota_before"]["display"] == "124.88 MB of 15 GB"
    u.attributes = AsyncMock(return_value=[])
    with pytest.raises(CanaryBlockedError, match="Original"):
        await u._photos_ready()


async def test_resume_reconciles_guest_and_media_store_without_repush(upload):
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
        await u._media_store("remote")


async def test_share_created_then_crash_reads_existing_link_only(upload):
    u, _, _ = upload
    u.recovery.update(share_intent=True, clipboard_before="https://photos.app.goo.gl/previous")
    u.clipboard = AsyncMock(return_value="https://photos.app.goo.gl/existing")
    verifier = MaestroPartVerifier(u)
    assert await verifier._share() == "https://photos.app.goo.gl/existing"
    assert await verifier._share() == "https://photos.app.goo.gl/existing"
    u.flow.assert_not_awaited()
    u.commands.assert_not_awaited()
    assert u.clipboard.await_count == 1


async def test_uncertain_share_never_creates_another_album(upload):
    u, _, _ = upload
    u.recovery.update(share_intent=True, clipboard_before="https://photos.app.goo.gl/previous")
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
