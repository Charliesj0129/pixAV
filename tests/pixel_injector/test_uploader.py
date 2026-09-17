from unittest.mock import AsyncMock, patch

import pytest

from pixav.pixel_injector.session import RedroidSession
from pixav.pixel_injector.uploader import UIAutomatorUploader, media_provider_scan_command
from pixav.shared.exceptions import UploadError
from pixav.shared.models import Account


@pytest.fixture
def session():
    return RedroidSession(
        task_id="task-123",
        container_id="cont-456",
        adb_host="127.0.0.1",
        adb_port=5555,
    )


@pytest.fixture
def adb_mock():
    return AsyncMock()


@pytest.fixture
def account():
    return Account(email="test@example.com", password="SecretPassword123!")


@pytest.mark.asyncio
async def test_login_success(adb_mock, session, account):
    uploader = UIAutomatorUploader(adb=adb_mock)

    # We mock out asyncio.sleep to run fast
    with patch("asyncio.sleep", new_callable=AsyncMock):
        await uploader.login(session, account)

    # Verify the connection was established
    adb_mock.connect.assert_called_once_with("127.0.0.1", 5555)

    # Verify the Google Intent was launched
    adb_mock.shell.assert_any_call("am start -a android.settings.ADD_ACCOUNT_SETTINGS -e account_types com.google")

    # Verify inputs
    adb_mock.shell.assert_any_call("input text 'test@example.com'", sensitive=True)
    adb_mock.shell.assert_any_call("input text 'SecretPassword123!'", sensitive=True)

    # Verify ENTER keys were sent
    assert adb_mock.shell.call_args_list.count((("input keyevent 66",), {})) == 3

    # Verify TAB keys were sent for accepting TOS
    assert adb_mock.shell.call_args_list.count((("input keyevent 61",), {})) == 3

    # Verify returning to home screen
    adb_mock.shell.assert_any_call("input keyevent 3")


@pytest.mark.asyncio
async def test_login_no_password(adb_mock, session):
    # Missing password
    account = Account(email="test@example.com")
    uploader = UIAutomatorUploader(adb=adb_mock)

    with pytest.raises(UploadError, match="account password not provided"):
        await uploader.login(session, account)


@pytest.mark.asyncio
async def test_login_adb_failure(adb_mock, session, account):
    uploader = UIAutomatorUploader(adb=adb_mock)
    adb_mock.connect.side_effect = Exception("device offline")

    with pytest.raises(UploadError, match="failed to execute login automation in cont-456: device offline"):
        await uploader.login(session, account)


@pytest.mark.asyncio
async def test_trigger_upload_uses_android_13_media_provider_call(adb_mock, session):
    uploader = UIAutomatorUploader(adb=adb_mock)

    await uploader.trigger_upload(session, "/sdcard/DCIM/Camera/video.mp4")

    adb_mock.connect.assert_awaited_once_with("127.0.0.1", 5555)
    adb_mock.shell.assert_awaited_once_with(
        "content call --uri content://media --method scan_file " "--arg /storage/emulated/0/DCIM/Camera/video.mp4"
    )


def test_media_provider_scan_command_quotes_untrusted_filename() -> None:
    command = media_provider_scan_command("/sdcard/DCIM/Camera/video name; reboot.mp4")

    assert command.endswith("--arg '/storage/emulated/0/DCIM/Camera/video name; reboot.mp4'")
