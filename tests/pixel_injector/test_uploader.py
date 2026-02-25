from unittest.mock import AsyncMock, patch

import pytest

from pixav.pixel_injector.session import RedroidSession
from pixav.pixel_injector.uploader import UIAutomatorUploader
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
    adb_mock.shell.assert_any_call("input text 'test@example.com'")
    adb_mock.shell.assert_any_call("input text 'SecretPassword123!'")
    
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
