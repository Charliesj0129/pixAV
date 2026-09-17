from __future__ import annotations

from pixav.shared.pause import is_paused_value
from scripts.manage_system_pause import _decode_record


def test_owned_pause_record_is_recognized() -> None:
    raw = '{"owner":"pixav-phase0","paused":true,"token":"abc"}'

    assert is_paused_value(raw) is True
    assert _decode_record(raw) == {"owner": "pixav-phase0", "paused": True, "token": "abc"}


def test_legacy_values_remain_compatible() -> None:
    assert is_paused_value("true") is True
    assert is_paused_value("false") is False
    assert is_paused_value(None) is False
