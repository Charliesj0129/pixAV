from __future__ import annotations

from datetime import datetime, timezone

import pytest

from pixav.shared.retry import DEFAULT_RETRY_BACKOFF_SECONDS, parse_retry_backoff, retry_deadline


def test_six_stage_retry_schedule() -> None:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    delays = [int((retry_deadline(i, now=base) - base).total_seconds()) for i in range(1, 7)]
    assert delays == list(DEFAULT_RETRY_BACKOFF_SECONDS)


def test_parse_retry_schedule_falls_back() -> None:
    assert parse_retry_backoff("bad,0") == DEFAULT_RETRY_BACKOFF_SECONDS


def test_retry_number_must_be_positive() -> None:
    with pytest.raises(ValueError):
        retry_deadline(0)
