"""Exact Windows device and Docker tools bindings for the Photos adapter.

Native observation and stopping belong to a controlled host adapter. A tools
container cannot infer native process identity from an ADB serial or stop a
Windows writer by closing its ADB client. This adapter never releases a DB
fence; independently verified halt evidence is still required by the authority.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from pixav.pixel_injector.canary import OWNER_LABEL, CanaryBlockedError
from pixav.pixel_injector.session import WindowsAndroidSession


class WindowsAndroidDevice:
    def __init__(
        self,
        session: WindowsAndroidSession,
        runner: Any,
        *,
        verify_native: Callable[[WindowsAndroidSession], None],
        stop_native: Callable[[WindowsAndroidSession], Awaitable[None]],
    ) -> None:
        if not callable(verify_native) or not callable(stop_native):
            raise ValueError("Windows device requires native identity and halt observers")
        self.session, self.runner = session, runner
        self._verify_native, self._stop_native = verify_native, stop_native

    def _runner_identity(self) -> None:
        self.runner.reload()
        labels = self.runner.labels
        if (
            self.runner.id != self.session.tools_id
            or labels.get(OWNER_LABEL) != str(self.session.owner)
            or labels.get("pixav.photos_canary.role") != "tools"
            or labels.get("pixav.runtime.id") != str(self.session.runtime_id)
            or labels.get("pixav.runtime.generation") != str(self.session.generation)
            or "pixav.task_id" in labels
            or self.runner.attrs.get("Config", {}).get("Image") != self.session.tools_image
        ):
            raise CanaryBlockedError("Windows tools runtime identity changed")

    def verify(self, session: WindowsAndroidSession) -> None:
        if session != self.session:
            raise CanaryBlockedError("Windows device session changed")
        self._runner_identity()
        if self.runner.status != "running":
            raise CanaryBlockedError("Windows tools runtime unavailable")
        self.session.target.require_runner(self.runner)
        self._verify_native(self.session)

    async def halt(self) -> None:
        """Attempt both stops even when one fails; retain the persistent fence."""
        runner_stopped = False
        try:
            await asyncio.to_thread(self._runner_identity)
            await asyncio.to_thread(self.runner.stop, timeout=10)
            await asyncio.to_thread(self.runner.reload)
            runner_stopped = self.runner.status == "exited"
        finally:
            # The original native identity, including its boot epoch, is used
            # even if the tools container has vanished or its stop failed.
            await self._stop_native(self.session)
        if not runner_stopped:
            raise CanaryBlockedError("Windows writer halt unconfirmed; retain UI fence")
