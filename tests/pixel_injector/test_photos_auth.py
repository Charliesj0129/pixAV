"""Synthetic credentials; real files prove private input and fresh-session guards."""

import asyncio
import hashlib
import json
import threading
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from pixav.pixel_injector.parts_download import download_original
from pixav.pixel_injector.photos_auth import (
    AccountCookieSecret,
    AccountSessionMismatchError,
    AccountSessionRequiredError,
    read_account_secret,
    verify_browser_account,
)
from pixav.pixel_injector.photos_storage import PhotosColdReadback
from pixav.pixel_injector.segment_readback import read_back
from pixav.shared.cold_receipts import AUTHENTICATED_RECEIPT, validate_account_cold_receipt
from pixav.shared.storage_models import RemoteAssetSegment

ACCOUNT = uuid.UUID("77777777-7777-4777-8777-777777777777")
OWNER = "88888888-8888-4888-8888-888888888888"
EMAIL = "synthetic@example.invalid"
SECRET = {
    "version": "photos-account-secret-v1",
    "account_id": str(ACCOUNT),
    "owner": OWNER,
    "account_email_sha256": hashlib.sha256(EMAIL.encode()).hexdigest(),
    "created_at": "2026-01-01T00:00:00Z",
    "cookies": [{"name": "SID", "value": "synthetic-cookie-secret", "domain": ".google.com", "path": "/"}],
}


def segment():
    return RemoteAssetSegment(
        asset_id=uuid.UUID("55555555-5555-4555-8555-555555555555"),
        segment_index=0,
        start_seconds=0,
        end_seconds=10,
        size_bytes=1024,
        sha256="a" * 64,
        local_path="",
        account_id=ACCOUNT,
        share_url="https://photos.app.goo.gl/synthetic",
    )


def receipt():
    part = segment()
    return {
        "receipt_version": AUTHENTICATED_RECEIPT,
        "decode": "PASS",
        "cold_inputs": "provider-only",
        "cold_mounts": ["/work:rw"],
        "authentication": {
            "mode": "original-account",
            "account_id": str(ACCOUNT),
            "owner": OWNER,
            "account_email_sha256": SECRET["account_email_sha256"],
            "session_id": str(uuid.uuid4()),
            "item_url_sha256": "b" * 64,
            "provider_filename_sha256": hashlib.sha256(part.filename.encode()).hexdigest(),
            "original_sha256": part.sha256,
            "fresh_context": True,
            "cookie_only": True,
            "local_media_inputs": False,
        },
    }


def secret_file(tmp_path):
    path = tmp_path / "session.json"
    path.write_text(json.dumps(SECRET))
    path.chmod(0o600)
    return path


def test_cookie_input_is_private_and_account_bound(tmp_path):
    path = secret_file(tmp_path)
    secret = read_account_secret(path, ACCOUNT, OWNER)
    assert "synthetic-cookie-secret" not in repr(secret)
    with pytest.raises(AccountSessionMismatchError):
        read_account_secret(path, uuid.uuid4(), OWNER)
    path.chmod(0o644)
    with pytest.raises(AccountSessionRequiredError):
        read_account_secret(path, ACCOUNT, OWNER)
    path.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(AccountSessionRequiredError):
        read_account_secret(link, ACCOUNT, OWNER)


@pytest.mark.parametrize(
    "extra",
    [
        {"profile": "/staging"},
        {"cookies": [{**SECRET["cookies"][0], "domain": "evil.invalid"}]},
        {"cookies": [{**SECRET["cookies"][0], "url": "file:///staging"}]},
    ],
)
def test_profile_local_input_and_foreign_cookie_are_rejected(extra):
    with pytest.raises(ValidationError):
        AccountCookieSecret.model_validate({**SECRET, **extra})


def test_expired_or_challenged_session_never_submits_credentials():
    page = MagicMock()
    page.locator.return_value.first.wait_for.side_effect = TimeoutError("private challenge")
    with pytest.raises(AccountSessionRequiredError, match="operator login"):
        verify_browser_account(page, AccountCookieSecret.model_validate(SECRET))
    page.fill.assert_not_called()
    page.click.assert_not_called()


@pytest.mark.parametrize("labels", [["Google Account: other (other@example.invalid)"], []])
def test_wrong_or_missing_account_is_rejected(labels):
    page = MagicMock()
    page.locator.return_value.evaluate_all.return_value = labels
    with pytest.raises(AccountSessionMismatchError):
        verify_browser_account(page, AccountCookieSecret.model_validate(SECRET))


def test_account_check_accepts_verified_localized_ui_contract():
    page = MagicMock()
    page.locator.return_value.evaluate_all.return_value = [f"Google 帳戶： Synthetic ({EMAIL})"]
    verify_browser_account(page, AccountCookieSecret.model_validate(SECRET))


def test_anonymous_history_and_new_receipts_remain_distinct():
    validate_account_cold_receipt(segment(), {})
    validate_account_cold_receipt(segment(), receipt())
    with pytest.raises(ValueError):
        validate_account_cold_receipt(segment(), {"authentication": receipt()["authentication"]})


@pytest.mark.parametrize(
    "field,value",
    [
        ("account_id", str(uuid.uuid4())),
        ("original_sha256", "c" * 64),
        ("provider_filename_sha256", "c" * 64),
        ("fresh_context", False),
        ("local_media_inputs", True),
        ("session_id", "not-a-session"),
    ],
)
def test_receipt_rejects_wrong_account_item_and_isolation(field, value):
    evidence = receipt()
    evidence["authentication"][field] = value
    with pytest.raises(ValueError):
        validate_account_cold_receipt(segment(), evidence)


def test_additional_mounts_are_not_a_secret_transport():
    evidence = receipt()
    evidence["cold_mounts"].append("/secret:ro")
    with pytest.raises(ValueError):
        validate_account_cold_receipt(segment(), evidence)


def test_authenticated_input_cannot_reuse_media_or_a_local_path(tmp_path):
    part = segment()
    (tmp_path / part.filename).write_bytes(b"local bytes")
    with pytest.raises(ValueError, match="fresh inputs"):
        download_original(part, tmp_path, authentication=SECRET)
    with pytest.raises(ValueError, match="empty workspace"):
        read_back({"segment": json.loads(part.model_dump_json()), "authentication": SECRET}, tmp_path)
    (tmp_path / part.filename).unlink()
    part = part.model_copy(update={"local_path": "/staging/original"})
    with pytest.raises(ValueError, match="no local inputs"):
        read_back({"segment": json.loads(part.model_dump_json()), "authentication": SECRET}, tmp_path)


async def test_ui_lock_loss_cancels_and_retires_child_before_unlock(tmp_path, monkeypatch):
    released = threading.Event()
    stopped_child = threading.Event()
    started = threading.Event()

    @asynccontextmanager
    async def ownership(*args):
        try:
            yield lambda: None
        finally:
            assert stopped_child.is_set()
            released.set()

    monkeypatch.setattr("pixav.pixel_injector.photos_storage.photos_ui_ownership", ownership)
    subject = PhotosColdReadback(
        tmp_path,
        image="synthetic",
        authentication_secret=str(secret_file(tmp_path)),
        owner=OWNER,
        ui_pool=AsyncMock(fetchval=AsyncMock(side_effect=lambda sql, *args: EMAIL if "SELECT email" in sql else OWNER)),
    )

    def run(destination, payload, *, timeout_seconds, stopped):
        assert json.loads(payload)["authentication"]["cookies"] == SECRET["cookies"]
        started.set()
        assert stopped.wait(5)
        stopped_child.set()
        raise RuntimeError("cancelled child")

    monkeypatch.setattr(subject, "_run", run)
    task = asyncio.create_task(
        subject.run_authenticated({"segment": json.loads(segment().model_dump_json())}, tmp_path, 10)
    )
    await asyncio.to_thread(started.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert released.is_set()


@pytest.mark.parametrize("fault", ["missing", "owner", "email", "extra_mount"])
async def test_authenticated_host_refuses_downgraded_or_rebound_receipt(tmp_path, monkeypatch, fault):
    from pixav.shared.exceptions import VerificationError

    @asynccontextmanager
    async def ownership(*args):
        yield lambda: None

    monkeypatch.setattr("pixav.pixel_injector.photos_storage.photos_ui_ownership", ownership)
    result = receipt()
    if fault == "missing":
        result = {}
    elif fault == "owner":
        result["authentication"]["owner"] = str(uuid.uuid4())
    elif fault == "email":
        result["authentication"]["account_email_sha256"] = "c" * 64
    else:
        result["cold_mounts"].append("/staging:ro")
    subject = PhotosColdReadback(
        tmp_path,
        image="synthetic",
        authentication_secret=str(secret_file(tmp_path)),
        owner=OWNER,
        ui_pool=AsyncMock(fetchval=AsyncMock(side_effect=lambda sql, *args: EMAIL if "SELECT email" in sql else OWNER)),
    )
    monkeypatch.setattr(subject, "_run", lambda *args, **kwargs: result)
    with pytest.raises(VerificationError):
        await subject.run_authenticated({"segment": json.loads(segment().model_dump_json())}, tmp_path, 10)


async def test_authenticated_host_sends_secret_only_over_stdin(tmp_path, monkeypatch):
    @asynccontextmanager
    async def ownership(*args):
        yield lambda: None

    monkeypatch.setattr("pixav.pixel_injector.photos_storage.photos_ui_ownership", ownership)
    subject = PhotosColdReadback(
        tmp_path,
        image="synthetic",
        authentication_secret=str(secret_file(tmp_path)),
        owner=OWNER,
        ui_pool=AsyncMock(fetchval=AsyncMock(side_effect=lambda sql, *args: EMAIL if "SELECT email" in sql else OWNER)),
    )
    work = tmp_path / "work"
    work.mkdir()
    response = receipt()

    def run(destination, payload, **kwargs):
        assert destination == work and not list(work.iterdir())
        assert json.loads(payload)["authentication"]["cookies"] == SECRET["cookies"]
        return response

    monkeypatch.setattr(subject, "_run", run)
    result = await subject.run_authenticated({"segment": json.loads(segment().model_dump_json())}, work, 10)
    assert result == response
    assert "synthetic-cookie-secret" not in json.dumps(result)
    assert not list(work.iterdir())


def test_missing_full_decode_evidence_cannot_validate():
    evidence = receipt()
    evidence.pop("decode")
    with pytest.raises(ValueError):
        validate_account_cold_receipt(segment(), evidence)


def test_private_secret_directory_is_required(tmp_path):
    folder = tmp_path / "public"
    folder.mkdir(mode=0o755)
    folder.chmod(0o755)  # Keep the negative fixture public even under umask 077.
    path = secret_file(folder)
    with pytest.raises(AccountSessionRequiredError):
        read_account_secret(path, ACCOUNT, OWNER)


@pytest.mark.parametrize("exit_code,diagnostic", [(1, b""), (0, b"decode error")])
def test_full_decoder_refuses_exit_failure_or_error_output(tmp_path, monkeypatch, exit_code, diagnostic):
    from types import SimpleNamespace

    from pixav.pixel_injector.canary_download import OriginalMismatchError
    from pixav.pixel_injector.parts_download import decode_original

    def run(command, **kwargs):
        assert command[-3:] == ["-f", "null", "-"]
        assert kwargs["timeout"] == 21600
        kwargs["stderr"].write(diagnostic)
        return SimpleNamespace(returncode=exit_code)

    monkeypatch.setattr("pixav.pixel_injector.parts_download.subprocess.run", run)
    (tmp_path / "synthetic.mp4").write_bytes(b"synthetic media")
    with pytest.raises(OriginalMismatchError):
        decode_original(tmp_path / "synthetic.mp4")


@pytest.mark.parametrize("fault", [None, "wrong_item", "expired_session"])
def test_authenticated_original_uses_fresh_context_and_probes_after_browser_exit(tmp_path, monkeypatch, fault):
    """Regression: asyncio media probing must run after sync Playwright exits."""
    import sys
    from types import ModuleType

    from pixav.pixel_injector import parts_download
    from pixav.pixel_injector.canary_download import OriginalMismatchError

    data = b"synthetic provider original"
    part = segment().model_copy(update={"size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    browser = MagicMock(version="synthetic-browser")
    page = browser.new_context.return_value.new_page.return_value
    page.url = "https://photos.google.com/share/synthetic"
    page.locator.return_value.evaluate_all.return_value = [f"Google Account: Synthetic ({EMAIL})"]
    page.get_by_role.return_value.count.return_value = 1
    if fault == "expired_session":
        page.locator.return_value.first.wait_for.side_effect = TimeoutError()
    download = MagicMock(suggested_filename="wrong.mp4" if fault == "wrong_item" else part.provider_filename)
    download.save_as.side_effect = lambda path: path.write_bytes(data)
    page.expect_download.return_value.__enter__.return_value.value = download
    manager = MagicMock()
    manager.__enter__.return_value.chromium.launch.return_value = browser
    exited = []
    manager.__exit__.side_effect = lambda *args: exited.append(True)
    module = ModuleType("playwright.sync_api")
    module.sync_playwright = lambda: manager
    monkeypatch.setitem(sys.modules, "playwright.sync_api", module)
    monkeypatch.setattr(parts_download.signal, "alarm", lambda seconds: None)
    monkeypatch.setattr(parts_download, "require_space", lambda budgets: None)

    def observe(path):
        assert exited and browser.close.called
        assert path.read_bytes() == data
        return {"sha256": part.sha256, "size_bytes": len(data)}

    monkeypatch.setattr(parts_download, "observed_media", observe)
    decoded = []
    monkeypatch.setattr(parts_download, "decode_original", lambda path: decoded.append(path.read_bytes()))
    error = AccountSessionRequiredError if fault == "expired_session" else OriginalMismatchError
    if fault:
        with pytest.raises(error):
            parts_download.download_original(part, tmp_path, authentication=SECRET)
        assert not decoded
        assert not (tmp_path / part.filename).exists()
        return
    result = parts_download.download_original(part, tmp_path, authentication=SECRET)
    validate_account_cold_receipt(part, result)
    assert decoded == [data]
    browser.new_context.assert_called_once_with(accept_downloads=True, locale="en-US")
    browser.new_context.return_value.add_cookies.assert_called_once_with(SECRET["cookies"])
    page.get_by_text.assert_called_once_with(part.provider_filename, exact=True)
    assert "synthetic-cookie-secret" not in json.dumps(result)


@pytest.mark.integration
def test_real_original_decoder_preserves_native_frame_clock(tmp_path):
    """The old nominal output clock reports errors for valid close VFR frames."""
    import inspect
    import os
    import subprocess

    from pixav.pixel_injector.parts_download import decode_original
    from pixav.shared import media_file

    if os.getenv("PIXAV_RUN_MEDIA_TESTS") != "1":
        pytest.skip("opt-in real FFmpeg tools container")
    code = r"""
import subprocess, tempfile
from pathlib import Path
class OriginalMismatchError(Exception):
    pass
base = ['/usr/bin/ffmpeg', '-v', 'error', '-nostdin']
subprocess.run(base + [
    '-f', 'lavfi', '-i', 'testsrc2=size=160x90:rate=30:duration=1',
    '-vf', r'settb=1/90000,setpts=if(eq(N\,15)\,42300\,N*3000)',
    '-fps_mode', 'passthrough', '-enc_time_base:v', '1:90000',
    '-c:v', 'libx264', '-preset', 'ultrafast', '-video_track_timescale', '90000',
    '/work/synthetic.mp4'], check=True)
old = subprocess.run(base + ['-xerror', '-i', '/work/synthetic.mp4',
    '-map', '0:v:0', '-f', 'null', '-'], capture_output=True, check=True)
assert b'non monotonically increasing dts' in old.stderr
"""
    code = inspect.getsource(media_file) + "\n" + code
    code += inspect.getsource(decode_original)
    code += "\ndecode_original(Path('/work/synthetic.mp4'))\n"
    subprocess.run(  # noqa: S603 - explicit opt-in, isolated synthetic media only
        [  # noqa: S607 - operator-installed Docker CLI for opt-in integration
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--cpus",
            "1",
            "--memory",
            "1g",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "-v",
            f"{tmp_path}:/work",
            os.getenv("PIXAV_READBACK_CONTRACT_IMAGE", "pixav-photos-canary:maestro-2.10.0"),
            "python",
            "-c",
            code,
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
