"""Configuration drift and fail-closed liveness without live media access."""

import argparse
import asyncio
import sys
import time
import uuid
from unittest.mock import AsyncMock

import pytest

from pixav.media_loader.video_parts import MediaOperationError, PartMedia, monitored_run
from pixav.pixel_injector.canary import CanaryBlockedError
from scripts.first_4k_contracts import RunHeartbeat, resolve_configuration


def test_saved_values_replace_omitted_defaults_and_allow_invocation_limit():
    state = {}
    original = resolve_configuration(
        argparse.Namespace(board="fixture", max_movie_gib=16), state, {"image": "sha256:a"}, "default"
    )
    state["configuration"] = original
    args = argparse.Namespace(max_parts=1)
    assert resolve_configuration(args, state, {"image": "sha256:a"}, "changed-default") == original
    assert args.board == "fixture" and args.max_movie_gib == 16 and args.max_parts == 1
    assert "max_parts" not in original
    assert "password" not in original


@pytest.mark.parametrize(
    "args,runtime", [(argparse.Namespace(board="different"), {}), (argparse.Namespace(), {"image": "new"})]
)
def test_explicit_drift_is_rejected(args, runtime):
    config = resolve_configuration(argparse.Namespace(), {}, {}, "fixture")
    with pytest.raises(CanaryBlockedError):
        resolve_configuration(args, {"configuration": config}, runtime, "fixture")


def test_legacy_requires_complete_evidence_even_with_explicit_arguments():
    legacy = {"candidates": [{"video_id": str(uuid.uuid4())}]}
    with pytest.raises(CanaryBlockedError, match="legacy"):
        resolve_configuration(argparse.Namespace(max_movie_gib=80), legacy, {}, "fixture")
    evidence = resolve_configuration(argparse.Namespace(), {}, {}, "fixture")
    legacy["configuration_evidence"] = evidence
    assert resolve_configuration(argparse.Namespace(), legacy, {}, "fixture") == evidence


async def test_heartbeat_progress_continues_while_worker_thread_is_busy():
    pool = argparse.Namespace(execute=AsyncMock(return_value="UPDATE 1"))
    heartbeat = RunHeartbeat(pool, uuid.uuid4(), {"stage": "decoding"}, interval=0.01)
    async with heartbeat.watch():
        await asyncio.to_thread(time.sleep, 0.08)
    assert pool.execute.await_count >= 3
    assert all("document=" not in call.args[0] for call in pool.execute.await_args_list)


@pytest.mark.parametrize("failure", [False, OSError("session disconnected")])
async def test_pool_reconnection_cannot_mask_lost_advisory_lock(failure):
    pool = argparse.Namespace(execute=AsyncMock(return_value="UPDATE 1"))
    guard = argparse.Namespace(fetchval=AsyncMock(side_effect=[True, failure]))
    heartbeat = RunHeartbeat(pool, uuid.uuid4(), {"stage": "decoding"}, lock_connection=guard)
    await heartbeat.pulse()
    with pytest.raises((CanaryBlockedError, OSError)):
        await heartbeat.pulse(transient_ok=True)
    assert pool.execute.await_count == 1
    with pytest.raises(CanaryBlockedError):
        heartbeat.check()


def _flaky_pool(fail_from: int = 2, fail_until: int | None = None, tag: str = "UPDATE 1"):
    """A pool whose execute() fails for a chosen span of calls."""
    calls = {"n": 0}

    async def execute(*args):
        calls["n"] += 1
        if calls["n"] >= fail_from and (fail_until is None or calls["n"] <= fail_until):
            raise OSError("offline")
        return tag

    return argparse.Namespace(execute=execute, calls=calls)


async def test_a_transient_database_fault_does_not_end_the_run():
    """One crash recovery must not discard hours of work.

    check() already tolerated a staleness window, but latching failure on the
    first error made it unreachable: a five second Postgres crash recovery
    ended an eight hour segmentation run five parts from finishing.
    """
    pool = _flaky_pool(fail_from=2, fail_until=3)
    heartbeat = RunHeartbeat(pool, uuid.uuid4(), {"stage": "decoding"}, interval=0.01, tolerance=5)
    effects = []
    async with heartbeat.watch():
        await asyncio.sleep(0.1)
        # Recovered pulses refresh the window, so external effects stay permitted
        # inside the scope. watch() latches failure on exit either way.
        heartbeat.check()
        effects.append("publish")
    assert effects == ["publish"]
    assert pool.calls["n"] > 3


async def test_an_outage_beyond_the_window_still_cancels_flow_and_stays_failed():
    pool = _flaky_pool(fail_from=2)
    heartbeat = RunHeartbeat(pool, uuid.uuid4(), {"stage": "decoding"}, interval=0.01, tolerance=0.05)
    effects = []
    with pytest.raises(CanaryBlockedError, match="heartbeat failed"):
        async with heartbeat.watch():
            await asyncio.sleep(0.5)
            effects.append("publish")
    assert effects == []
    with pytest.raises(CanaryBlockedError):
        heartbeat.check()


async def test_a_missing_run_row_is_terminal_without_waiting_out_the_window():
    """A row that is gone will not come back, so the window must not apply."""
    calls = {"n": 0}

    async def execute(*args):
        calls["n"] += 1
        return "UPDATE 1" if calls["n"] == 1 else "UPDATE 0"

    pool = argparse.Namespace(execute=execute)
    heartbeat = RunHeartbeat(pool, uuid.uuid4(), {"stage": "decoding"}, interval=0.01, tolerance=600)
    effects = []
    with pytest.raises(CanaryBlockedError, match="heartbeat failed"):
        async with heartbeat.watch():
            await asyncio.sleep(0.2)
            effects.append("publish")
    assert effects == []


async def test_a_database_that_is_down_at_startup_still_refuses_to_begin():
    pool = _flaky_pool(fail_from=1)
    heartbeat = RunHeartbeat(pool, uuid.uuid4(), {"stage": "decoding"}, interval=0.01, tolerance=600)
    with pytest.raises(OSError):
        async with heartbeat.watch():
            await asyncio.sleep(0.05)


def test_monitored_process_stops_on_disk_latch():
    calls = 0

    def check():
        nonlocal calls
        calls += 1
        if calls > 2:
            raise MediaOperationError("disk latch", operation="disk", category="disk_latch")

    started = time.monotonic()
    with pytest.raises(MediaOperationError, match="disk latch"):
        monitored_run([sys.executable, "-c", "import time; time.sleep(30)"], 40, check)
    assert time.monotonic() - started < 5


def test_failed_heartbeat_rejects_media_before_launch(monkeypatch):
    heartbeat = RunHeartbeat(None, uuid.uuid4(), {"stage": "decoding"})
    heartbeat.failed.set()
    media = PartMedia()
    media.check = heartbeat.check
    with pytest.raises(CanaryBlockedError):
        media.command(["missing-tool"], 1)


def test_status_cli_bypasses_mutating_file_lock(monkeypatch, capsys):
    from scripts import first_4k_movie as module

    def forbidden():
        raise AssertionError("status must not take the mutating run lock")

    monkeypatch.setattr(module, "single_flight", forbidden)
    monkeypatch.setattr(module, "execute", AsyncMock(return_value={"status": "NO_RUN"}))
    monkeypatch.setattr(sys, "argv", ["first_4k_movie.py", "status"])
    assert module.main() == 0
    assert '"NO_RUN"' in capsys.readouterr().out


def test_failed_scratch_allocation_is_retained(tmp_path):
    from pixav.media_loader.video_parts import retained_temporary

    with pytest.raises(MediaOperationError):
        with retained_temporary(prefix="split-", directory=tmp_path) as path:
            (path / "partial.mp4").write_bytes(b"retained evidence")
            raise MediaOperationError("disk latch")
    assert (path / "partial.mp4").read_bytes() == b"retained evidence"
    with retained_temporary(prefix="success-", directory=tmp_path) as success:
        (success / "scratch").write_bytes(b"done")
    assert not success.exists()
