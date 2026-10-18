"""Mutual-TLS client for a fixed Windows runtime and original UI ticket."""

from __future__ import annotations

import asyncio
import re
import ssl
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx

from pixav.pixel_injector.errors import CanaryBlockedError
from pixav.pixel_injector.windows_host_models import (
    HostCapacityReceipt,
    HostHaltReceipt,
    HostReadinessReceipt,
    HostReadinessRequest,
    HostRuntimeBinding,
    HostUIRequest,
)

if TYPE_CHECKING:
    from pixav.config import Settings
    from pixav.pixel_injector.session import WindowsAndroidSession
    from pixav.shared.disk import PhysicalCapacity
    from pixav.shared.photos_ui_session import PhotosUITicket


class WindowsHostClient:
    def __init__(self, url: str, *, ca: Path, certificate: Path, key: Path, timeout_seconds: int = 60) -> None:
        endpoint = urlsplit(url)
        if (
            endpoint.scheme != "https"
            or endpoint.hostname not in {"127.0.0.1", "localhost", "host.docker.internal"}
            or endpoint.username
            or endpoint.password
            or endpoint.query
            or endpoint.fragment
            or endpoint.path not in {"", "/"}
        ):
            raise ValueError("Windows control requires the private mutual-TLS endpoint")
        context = ssl.create_default_context(cafile=str(ca))
        context.load_cert_chain(str(certificate), str(key))
        self._client = httpx.Client(base_url=url.rstrip("/"), verify=context, timeout=timeout_seconds, trust_env=False)

    def close(self) -> None:
        self._client.close()

    def call(self, operation: str, request: HostUIRequest | HostReadinessRequest) -> dict:
        try:
            result = self._client.post(
                "/v1/" + operation, content=request.model_dump_json(), headers={"Content-Type": "application/json"}
            )
        except (httpx.HTTPError, ValueError):
            raise CanaryBlockedError("Windows native result unconfirmed; retain resource fence") from None
        try:
            payload = result.json() if len(result.content) <= 16384 else None
        except ValueError:
            payload = None
        if result.status_code != 200:
            code = payload.get("detail") if isinstance(payload, dict) else None
            if not isinstance(code, str) or not re.fullmatch(r"NATIVE_[A-Z_]{1,80}", code):
                code = "NATIVE_CONTROL_REJECTED"
            raise CanaryBlockedError(f"Windows native control rejected: {code}; retain resource fence")
        if not isinstance(payload, dict):
            raise CanaryBlockedError("Windows native response invalid")
        return payload

    def readiness(self, request: HostReadinessRequest) -> HostReadinessReceipt:
        try:
            receipt = HostReadinessReceipt.model_validate(self.call("readiness", request))
        except ValueError:
            raise CanaryBlockedError("Windows readiness response invalid") from None
        if any(receipt.model_dump().get(key) != value for key, value in request.model_dump().items()):
            raise CanaryBlockedError("Windows readiness response binding changed")
        return receipt

    def capacity(self, request: HostReadinessRequest) -> HostCapacityReceipt:
        try:
            receipt = HostCapacityReceipt.model_validate(self.call("capacity", request))
        except ValueError:
            raise CanaryBlockedError("Windows physical capacity response invalid") from None
        if any(receipt.model_dump().get(key) != value for key, value in request.model_dump().items()):
            raise CanaryBlockedError("Windows physical capacity binding changed")
        return receipt


class WindowsCapacityProbe:
    """Read the enrolled physical volume using a fresh nonce and bounded mTLS."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._binding = HostRuntimeBinding(
            runtime_id=UUID(settings.storage_windows_runtime_id),
            runtime_generation=settings.storage_windows_runtime_generation,
            identity_sha256=settings.windows_control_identity_sha256,
        )
        if settings.windows_capacity_volume_device is None:
            raise ValueError("Windows capacity requires the enrolled volume identity")

    def _read(self) -> PhysicalCapacity:
        from pixav.shared.disk import PhysicalCapacity

        settings = self._settings
        client = WindowsHostClient(
            settings.windows_control_url,
            ca=Path(settings.windows_control_ca),
            certificate=Path(settings.windows_control_certificate),
            key=Path(settings.windows_control_key),
            timeout_seconds=5,
        )
        try:
            receipt = client.capacity(HostReadinessRequest(**self._binding.model_dump(), nonce=uuid4()))
            if receipt.volume_device != settings.windows_capacity_volume_device:
                raise CanaryBlockedError("Windows physical capacity volume changed")
            return PhysicalCapacity(receipt.volume_root, receipt.total_bytes, receipt.free_bytes)
        finally:
            client.close()

    async def __call__(self) -> PhysicalCapacity:
        return await asyncio.to_thread(self._read)


class BoundWindowsControl:
    def __init__(
        self, client: WindowsHostClient, session: WindowsAndroidSession, ticket: PhotosUITicket, *, role="upload"
    ) -> None:
        from pixav.shared.activity_context import require_activity_context

        context = require_activity_context()
        self.client, self.session = client, session
        self.request = HostUIRequest(
            runtime_id=session.runtime_id,
            runtime_generation=session.generation,
            identity_sha256=session.identity_sha256,
            session_id=ticket.id,
            session_token=ticket.token,
            operation_id=context.request.operation_id,
            generation=context.request.generation,
            role=role,
        )
        self.last_halt: dict = {}

    def claim(self) -> None:
        self._active(self.client.call("claim", self.request))

    def _active(self, response: dict) -> None:
        if response != {"state": "ACTIVE", **self.request.public_binding()}:
            raise CanaryBlockedError("Windows host activity binding changed")

    def verify(self, session: WindowsAndroidSession) -> None:
        if session != self.session:
            raise CanaryBlockedError("Windows native session changed")
        self._active(self.client.call("observe", self.request))

    def halt(self, *, cold: bool) -> dict:
        receipt = HostHaltReceipt.model_validate(
            self.client.call("halt", self.request.model_copy(update={"cold": cold}))
        )
        expected = self.request.public_binding()
        actual = receipt.model_dump(mode="json")
        if (
            any(actual.get(key) != value for key, value in expected.items())
            or receipt.device_key != self.session.device_key
            or receipt.tools_id != self.session.tools_id
        ):
            raise CanaryBlockedError("Windows halt receipt binding changed")
        self.last_halt = receipt.model_dump(mode="json", by_alias=True)
        return self.last_halt
