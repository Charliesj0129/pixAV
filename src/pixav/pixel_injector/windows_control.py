"""Mutual-TLS client for a fixed Windows runtime and original UI ticket."""

from __future__ import annotations

import ssl
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import httpx

from pixav.pixel_injector.errors import CanaryBlockedError
from pixav.pixel_injector.windows_host_models import (
    HostHaltReceipt,
    HostReadinessReceipt,
    HostReadinessRequest,
    HostUIRequest,
)

if TYPE_CHECKING:
    from pixav.pixel_injector.session import WindowsAndroidSession
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
            if result.status_code != 200:
                raise CanaryBlockedError("Windows native control rejected; retain resource fence")
            payload = result.json()
        except (httpx.HTTPError, ValueError):
            raise CanaryBlockedError("Windows native result unconfirmed; retain resource fence") from None
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
