"""Durable retry policy shared by download and upload workers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

DEFAULT_RETRY_BACKOFF_SECONDS = (60, 300, 900, 3600, 21600, 86400)


def parse_retry_backoff(raw: str) -> tuple[int, ...]:
    """Parse a comma-separated retry schedule, falling back to six stages."""
    values: list[int] = []
    for token in raw.split(","):
        try:
            value = int(token.strip())
        except ValueError:
            continue
        if value > 0:
            values.append(value)
    return tuple(values) or DEFAULT_RETRY_BACKOFF_SECONDS


def retry_deadline(
    retry_number: int,
    *,
    backoff_seconds: tuple[int, ...] = DEFAULT_RETRY_BACKOFF_SECONDS,
    now: datetime | None = None,
) -> datetime:
    """Return the PostgreSQL due time for a one-based retry number."""
    if retry_number < 1:
        raise ValueError("retry_number must be >= 1")
    if not backoff_seconds:
        raise ValueError("backoff_seconds must not be empty")
    index = min(retry_number - 1, len(backoff_seconds) - 1)
    base = now or datetime.now(timezone.utc)
    return base + timedelta(seconds=backoff_seconds[index])
