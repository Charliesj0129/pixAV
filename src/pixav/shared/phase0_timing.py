"""Opt-in Phase 0 evidence, never execution truth. No payloads or error messages."""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)
STAGES = ("download", "remux", "local_finalize")
FAILURE_CLASSES = frozenset(
    {
        "SourceUnavailableError",
        "DownloadError",
        "RemuxError",
        "DatabaseError",
        "RedisError",
        "QueueError",
        "UploadError",
        "VerificationError",
        "TimeoutError",
        "OSError",
    }
)


def failure_class(error: str) -> str | None:
    if not error:
        return None
    prefix = error.partition(":")[0]
    return prefix if prefix in FAILURE_CLASSES else "unclassified"


def _emit(directory: str, payload: dict) -> None:
    try:
        root = Path(directory)
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = root / f"{uuid.uuid4()}.json"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle)
    except OSError:
        # Lost telemetry must not repeat an external side effect. Reports expose missing spans.
        logger.warning("phase0 timing evidence could not be written")


@contextmanager
def phase0_span(task_id: uuid.UUID, video_id: uuid.UUID, stage: str) -> Iterator[None]:
    directory = os.environ.get("PIXAV_PHASE0_EVENTS_DIR", "")
    if not directory:
        yield
        return
    if stage not in STAGES:
        raise ValueError("unsupported Phase 0 timing stage")
    span_id = str(uuid.uuid4())
    common = {
        "schema_version": 1,
        "span_id": span_id,
        "task_id": str(task_id),
        "video_id": str(video_id),
        "stage": stage,
    }
    _emit(directory, dict(common, event="start", at=datetime.now(timezone.utc).isoformat()))
    started = time.monotonic()
    outcome, error = "complete", None
    try:
        yield
    except BaseException as exc:
        outcome = (
            "interrupted"
            if isinstance(exc, (KeyboardInterrupt, SystemExit)) or type(exc).__name__ == "CancelledError"
            else "failed"
        )
        error = failure_class(type(exc).__name__)
        raise
    finally:
        _emit(
            directory,
            dict(
                common,
                event="end",
                at=datetime.now(timezone.utc).isoformat(),
                elapsed_seconds=time.monotonic() - started,
                outcome=outcome,
                failure_class=error,
            ),
        )
