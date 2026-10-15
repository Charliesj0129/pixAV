"""Loopback, mutual-TLS native control; it cannot advance domain executions.

Only a fixed manifest can select a device or executable. Network requests carry
activity references and opaque resource tickets, never paths or shell commands.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import ssl
import subprocess
import tempfile
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from pydantic import ValidationError

from pixav.pixel_injector.windows_host_models import (
    HostHaltReceipt,
    HostUIRequest,
    WindowsHostManifest,
    canonical_digest,
)


class NativeControlError(RuntimeError):
    """A bounded failure; never propagate native stdout/stderr to clients."""


class NativeControl:
    def __init__(self, script: Path, manifest: WindowsHostManifest, *, script_sha256: str, timeout: int = 45) -> None:
        if not hmac.compare_digest(hashlib.sha256(script.read_bytes()).hexdigest(), script_sha256):
            raise NativeControlError("NATIVE_SCRIPT_IDENTITY_MISMATCH")
        self.script, self.manifest, self.timeout = script, manifest, timeout
        self.script_sha256 = script_sha256

    def run(self, mode: str, subject: HostUIRequest) -> dict:
        if not hmac.compare_digest(hashlib.sha256(self.script.read_bytes()).hexdigest(), self.script_sha256):
            raise NativeControlError("NATIVE_SCRIPT_IDENTITY_MISMATCH")
        payload = {**self.manifest.native.payload(), "session_id": str(subject.session_id)}
        try:
            result = subprocess.run(  # noqa: S603 - fixed executable and digest-pinned local script; JSON only on stdin
                [
                    str(Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"),
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-File",
                    str(self.script),
                    "-Mode",
                    mode,
                ],
                input=json.dumps(payload).encode(),
                capture_output=True,
                timeout=self.timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise NativeControlError("NATIVE_OBSERVATION_UNCONFIRMED") from None
        if result.returncode != 0 or len(result.stdout) > 16384:
            raise NativeControlError("NATIVE_OBSERVATION_REJECTED")
        try:
            response = json.loads(result.stdout.decode("utf-8-sig"))
        except (ValueError, UnicodeError):
            raise NativeControlError("NATIVE_RESPONSE_INVALID") from None
        if not isinstance(response, dict):
            raise NativeControlError("NATIVE_RESPONSE_INVALID")
        self._validate_response(mode, response)
        return response

    def _validate_response(self, mode: str, response: dict) -> None:
        expected = self.manifest.native.payload()
        if any(response.get(key) != expected[key] for key in ("schema", "sdk_root", "serial", "avd_name", "boot_id")):
            raise NativeControlError("NATIVE_RESPONSE_BINDING_CHANGED")
        processes = response.get("processes")
        roles = {"emulator", "qemu"} if mode in {"halt", "stopped"} else {"adb", "emulator", "qemu"}
        if not isinstance(processes, dict) or set(processes) != roles:
            raise NativeControlError("NATIVE_RESPONSE_PROCESSES_INVALID")
        for role in roles:
            actual, original = processes[role], expected["processes"][role]
            if not isinstance(actual, dict) or actual.get("pid") != original["pid"]:
                raise NativeControlError("NATIVE_RESPONSE_PROCESSES_INVALID")
            if mode in {"halt", "stopped"} and actual.get("original_process_absent") is True:
                continue
            if any(actual.get(key) != original[key] for key in ("created_filetime", "sha256")):
                raise NativeControlError("NATIVE_RESPONSE_PROCESS_BINDING_CHANGED")


class WindowsHost:
    def __init__(self, manifest: WindowsHostManifest, control: NativeControl, journal: Path) -> None:
        self.manifest, self.control, self.journal = manifest, control, journal
        self.lock = asyncio.Lock()
        self.state = json.loads(journal.read_text()) if journal.exists() else None

    def _persist(self, state: dict) -> None:
        state = {"closed_sessions": (self.state or {}).get("closed_sessions", []), **state}
        self.journal.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".native-state-", dir=self.journal.parent)
        try:
            os.fchmod(descriptor, 0o600) if hasattr(os, "fchmod") else None
            with os.fdopen(descriptor, "w") as handle:
                json.dump(state, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.journal)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        self.state = state

    def _binding(self, request: HostUIRequest) -> None:
        if (
            request.runtime_id != self.manifest.runtime_id
            or request.runtime_generation != self.manifest.runtime_generation
            or request.identity_sha256 != self.manifest.identity_sha256
        ):
            raise NativeControlError("NATIVE_RUNTIME_BINDING_CHANGED")

    @staticmethod
    def _token(request: HostUIRequest) -> str:
        return hashlib.sha256(request.session_token.bytes).hexdigest()

    def _owner(self, request: HostUIRequest) -> None:
        self._binding(request)
        if (
            not self.state
            or self.state.get("binding") != request.public_binding()
            or not hmac.compare_digest(self.state.get("token_sha256", ""), self._token(request))
        ):
            raise NativeControlError("NATIVE_RESOURCE_TICKET_REJECTED")

    async def _run(self, mode: str, request: HostUIRequest) -> dict:
        # Cancellation cannot release the host lock while its native child still
        # runs. Finish the bounded observation/effect and its journal first.
        operation = asyncio.create_task(asyncio.to_thread(self.control.run, mode, request))
        while not operation.done():
            try:
                await asyncio.shield(operation)
            except asyncio.CancelledError:
                continue
        return operation.result()

    async def claim(self, request: HostUIRequest) -> dict:
        async with self.lock:
            self._binding(request)
            repeated = False
            if str(request.session_id) in (self.state or {}).get("closed_sessions", []):
                raise NativeControlError("NATIVE_RESOURCE_TICKET_RETIRED")
            if self.state and self.state.get("state") != "HALTED":
                self._owner(request)
                if self.state.get("state") != "ACTIVE":
                    raise NativeControlError("NATIVE_HALT_RECONCILIATION_REQUIRED")
                repeated = True
            else:
                self._persist(
                    {"state": "ACTIVE", "binding": request.public_binding(), "token_sha256": self._token(request)}
                )
            # Persist ownership first: a crash or timeout cannot authorize another owner.
            mode = "observe" if repeated and request.role != "cold_read" else "quiescence"
            expected = "NATIVE_IDENTITY_OBSERVED" if mode == "observe" else "NATIVE_UI_QUIESCENT"
            try:
                result = await self._run(mode, request)
            except NativeControlError:
                if request.role != "cold_read":
                    raise
                result = await self._run("stopped", request)
                expected = "ORIGINAL_NATIVE_WRITERS_STOPPED"
            if result.get("result") != expected:
                raise NativeControlError("NATIVE_RUNTIME_UNAVAILABLE")
            return {"state": "ACTIVE", **request.public_binding()}

    async def observe(self, request: HostUIRequest) -> dict:
        async with self.lock:
            self._owner(request)
            if self.state is None or self.state.get("state") != "ACTIVE":
                raise NativeControlError("NATIVE_RESOURCE_HALTED")
            result = await self._run("observe", request)
            if result.get("result") != "NATIVE_IDENTITY_OBSERVED":
                raise NativeControlError("NATIVE_RUNTIME_UNAVAILABLE")
            return {"state": "ACTIVE", **request.public_binding()}

    async def halt(self, request: HostUIRequest) -> dict:
        async with self.lock:
            self._owner(request)
            previous = dict(self.state or {})
            self._persist({**previous, "state": "HALT_REQUIRED"})
            mode: Literal["cold", "quiesce"] = "cold" if request.cold else "quiesce"
            try:
                result = await self._run("halt" if request.cold else "quiesce", request)
                expected = "ORIGINAL_NATIVE_WRITERS_STOPPED" if request.cold else "NATIVE_UI_QUIESCENT"
                if result.get("result") != expected:
                    raise NativeControlError("NATIVE_HALT_UNCONFIRMED")
            except NativeControlError:
                if request.cold:
                    raise
                result = await self._run("halt", request)
                if result.get("result") != "ORIGINAL_NATIVE_WRITERS_STOPPED":
                    raise NativeControlError("NATIVE_HALT_UNCONFIRMED") from None
                mode = "cold"
            receipt = self._receipt(request, mode, result)
            closed = list(previous.get("closed_sessions", []))
            if str(request.session_id) not in closed:
                closed.append(str(request.session_id))
            self._persist({**previous, "state": "HALTED", "receipt": receipt, "closed_sessions": closed})
            return receipt

    def _receipt(self, request: HostUIRequest, mode: Literal["quiesce", "cold"], observation: dict) -> dict:
        return HostHaltReceipt(
            **request.public_binding(),
            device_key=self.manifest.device_key,
            tools_id=self.manifest.tools_id,
            native_writers_stopped=True,
            halt_mode=mode,
            receipt_sha256=canonical_digest(observation),
        ).model_dump(mode="json", by_alias=True)

    async def verify_halt(self, request: HostUIRequest) -> dict:
        async with self.lock:
            self._owner(request)
            if not self.state or self.state.get("state") != "HALTED":
                raise NativeControlError("NATIVE_HALT_NOT_RECORDED")
            mode = self.state["receipt"]["halt_mode"]
            result = await self._run("stopped" if mode == "cold" else "quiescence", request)
            expected = "ORIGINAL_NATIVE_WRITERS_STOPPED" if mode == "cold" else "NATIVE_UI_QUIESCENT"
            if result.get("result") != expected:
                raise NativeControlError("NATIVE_HALT_UNCONFIRMED")
            return self._receipt(request, mode, result)


def create_app(host: WindowsHost) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ready", "runtime_generation": host.manifest.runtime_generation}

    @app.post("/v1/{operation}")
    async def control(operation: Literal["claim", "observe", "halt", "verify-halt"], request: Request) -> dict:
        if request.headers.get("transfer-encoding") or not request.headers.get("content-length", "").isdigit():
            raise HTTPException(400, "INVALID_REQUEST_FRAMING")
        if int(request.headers["content-length"]) > 16384:
            raise HTTPException(413, "REQUEST_TOO_LARGE")
        try:
            subject = HostUIRequest.model_validate_json(await request.body())
        except (ValueError, ValidationError):
            raise HTTPException(400, "INVALID_REQUEST") from None
        try:
            return await getattr(host, operation.replace("-", "_"))(subject)
        except NativeControlError:
            raise HTTPException(409, "NATIVE_CONTROL_REJECTED") from None

    return app


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="Mutual-TLS control for one registered Windows Android runtime")
    for flag in ("manifest", "script", "script-sha256", "journal", "server-cert", "server-key", "client-ca"):
        parser.add_argument("--" + flag, required=True)
    parser.add_argument("--port", type=int, default=5043)
    args = parser.parse_args()
    manifest = WindowsHostManifest.model_validate_json(Path(args.manifest).read_text(encoding="utf-8-sig"))
    host = WindowsHost(
        manifest, NativeControl(Path(args.script), manifest, script_sha256=args.script_sha256), Path(args.journal)
    )
    uvicorn.run(
        create_app(host),
        host="127.0.0.1",
        port=args.port,
        access_log=False,
        log_level="warning",
        ssl_certfile=args.server_cert,
        ssl_keyfile=args.server_key,
        ssl_ca_certs=args.client_ca,
        ssl_cert_reqs=ssl.CERT_REQUIRED,
        timeout_keep_alive=5,
        limit_concurrency=4,
    )


if __name__ == "__main__":
    main()
