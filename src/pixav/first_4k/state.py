"""The attributes and core operations every MovieFlow stage relies on.

The stages are mixins in separate modules, so the state they share needs one
declared home. Splitting a 785-line class without this leaves each half calling
attributes nothing declares, which type checking cannot see and a typo reaches
only at runtime — during a segmentation run that costs hours to repeat.

The bodies raise: `MovieFlow` provides the real ones, and a mixin used without
it should say so rather than return None.
"""

from __future__ import annotations

import argparse
import uuid
from typing import Any

import asyncpg

from pixav.media_loader.video_parts import PartMedia
from pixav.shared.video_parts import VideoPartRepository

from .contracts import RunHeartbeat


class FlowState:
    """Declared state of a run in progress; see ``MovieFlow`` for the implementation."""

    client: Any
    pool: asyncpg.Pool
    args: argparse.Namespace
    state: dict
    id: uuid.UUID
    heartbeat: RunHeartbeat | None
    parts: VideoPartRepository
    media: PartMedia

    def image_id(self, name: str) -> str:
        """Resolve an image tag to the id this run pinned at configuration time."""
        raise NotImplementedError

    def check_operation(self) -> None:
        """Refuse to continue when liveness or free disk says the run must stop."""
        raise NotImplementedError

    async def save(self) -> None:
        """Persist the run document; intent is written before the effect it describes."""
        raise NotImplementedError

    async def runtime(self) -> tuple[Any, Any]:
        """Return the retained guest and tools containers, creating them once."""
        raise NotImplementedError
