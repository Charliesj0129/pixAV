"""The current immutable activity envelope, never a second execution owner."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from uuid import UUID

from pixav.shared.workflow import ActivityRequest


@dataclass(frozen=True, slots=True)
class ActivityContext:
    request: ActivityRequest = field(repr=False)
    worker_token: UUID = field(repr=False)


_CURRENT: ContextVar[ActivityContext | None] = ContextVar("pixav_activity_context", default=None)


@contextmanager
def activity_context(request: ActivityRequest, worker_token: UUID) -> Iterator[ActivityContext]:
    context = ActivityContext(request, worker_token)
    previous = _CURRENT.set(context)
    try:
        yield context
    finally:
        _CURRENT.reset(previous)


def require_activity_context() -> ActivityContext:
    context = _CURRENT.get()
    if context is None:
        raise RuntimeError("Photos UI requires its original activity context")
    return context
