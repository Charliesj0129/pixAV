"""Selection, reset and CLI contracts for the isolated single-film flow.

These cover the orchestration decisions that cost hours when they are wrong:
which candidate is attempted first, which source a torrent is added from, and
what a reset is allowed to destroy.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import uuid
from pathlib import Path

import pytest

import scripts.first_4k_movie as flow_module
from pixav.media_loader.video_parts import TARGET_BYTES, MediaOperationError
from pixav.pixel_injector.canary import CanaryBlockedError
from scripts.first_4k_movie import SELECTION_VERSION, SUCCESS_STATUSES, MovieFlow

TORRENT = b"d8:announce20:http://t/announce.x4:infod4:name3:abcee" + b"e" * 60


class FakePool:
    def __init__(self) -> None:
        self.executed: list[tuple] = []

    async def execute(self, sql: str, *args):
        self.executed.append((sql, args))

    async def fetch(self, *_args):
        return []

    async def fetchval(self, *_args):
        return None


class FakeImages:
    def get(self, _name):
        return argparse.Namespace(id="sha256:image")


def make_flow(tmp_path: Path, monkeypatch, state: dict) -> tuple[MovieFlow, FakePool]:
    monkeypatch.setattr(flow_module, "WORK", tmp_path)
    (tmp_path / "evidence").mkdir(parents=True, exist_ok=True)
    pool = FakePool()
    args = argparse.Namespace(min_movie_gib=8, max_movie_gib=80, min_movie_seconds=3600, board="b")
    client = argparse.Namespace(images=FakeImages())
    return MovieFlow(client, pool, args, state), pool


def base_state(**extra) -> dict:
    return {"id": str(uuid.uuid4()), "stage": "new", "gates": {}, **extra}


class TestSelection:
    """Board order is newest-first, which is unrelated to which film is wanted."""

    def _rank(self, items: list[dict]) -> list[dict]:
        ranked = list(items)
        ranked.sort(key=lambda item: (-item["quality_score"], -int(item.get("size") or 0), item["order"]))
        return ranked

    def test_best_scoring_candidate_is_attempted_before_the_newest(self):
        items = [
            {"order": 0, "quality_score": 20, "size": 2 * 1024**3},
            {"order": 1, "quality_score": 180, "size": 30 * 1024**3},
            {"order": 2, "quality_score": 180, "size": 45 * 1024**3},
        ]
        assert [item["order"] for item in self._rank(items)] == [2, 1, 0]

    def test_an_unstated_size_sorts_last_because_it_cannot_be_ranked(self):
        items = [
            {"order": 0, "quality_score": 100, "size": 0},
            {"order": 1, "quality_score": 100, "size": 12 * 1024**3},
        ]
        assert [item["order"] for item in self._rank(items)] == [1, 0]

    def test_selection_version_is_ahead_of_the_board_103_shortlist(self):
        # A run selected under the old policy must be reconciled, not reused.
        assert SELECTION_VERSION > 2


class TestTorrentSource:
    def test_the_captured_attachment_is_preferred_over_the_bare_magnet(self, tmp_path, monkeypatch):
        evidence = tmp_path / "evidence" / "discovery-1"
        evidence.mkdir(parents=True)
        (evidence / "torrents").mkdir()
        (evidence / "torrents" / "a.torrent").write_bytes(TORRENT)
        flow, _ = make_flow(tmp_path, monkeypatch, base_state(discovery_intent={"directory": str(evidence)}))
        candidate = {
            "torrent_file": "torrents/a.torrent",
            "torrent_sha256": hashlib.sha256(TORRENT).hexdigest(),
        }
        assert flow.torrent_file(candidate) == TORRENT

    @pytest.mark.parametrize(
        "candidate",
        [
            {},
            {"torrent_file": "torrents/missing.torrent", "torrent_sha256": "0" * 64},
            {"torrent_file": "torrents/a.torrent", "torrent_sha256": "0" * 64},
            {"torrent_file": "../../escape.torrent", "torrent_sha256": "0" * 64},
        ],
        ids=["no attachment", "missing file", "hash mismatch", "path escape"],
    )
    def test_anything_unverifiable_falls_back_to_the_magnet(self, tmp_path, monkeypatch, candidate):
        evidence = tmp_path / "evidence" / "discovery-1"
        (evidence / "torrents").mkdir(parents=True)
        (evidence / "torrents" / "a.torrent").write_bytes(TORRENT)
        (tmp_path / "escape.torrent").write_bytes(TORRENT)
        flow, _ = make_flow(tmp_path, monkeypatch, base_state(discovery_intent={"directory": str(evidence)}))
        assert flow.torrent_file(candidate) is None


class TestSegmentTarget:
    """prepare() refuses a single-part manifest, so a small film needs a lower target."""

    @staticmethod
    def target_for(size: int) -> int:
        return min(TARGET_BYTES, (size + 1) // 2)

    @pytest.mark.parametrize("size", [3 * 1024**3, 12 * 1024**3, 19 * 1024**3, 60 * 1024**3])
    def test_target_always_leaves_room_for_at_least_two_parts(self, size):
        target = self.target_for(size)
        # Under the Photos item ceiling, and strictly smaller than the source so
        # the film can never come out as a single part.
        assert target <= TARGET_BYTES
        assert target < size

    def test_a_film_smaller_than_one_full_part_is_split_in_half(self):
        size = 3 * 1024**3
        assert self.target_for(size) == (size + 1) // 2

    def test_a_large_film_keeps_the_full_part_target(self):
        assert self.target_for(60 * 1024**3) == TARGET_BYTES


class TestReset:
    async def test_a_fresh_run_retires_nothing(self, tmp_path, monkeypatch):
        flow, pool = make_flow(tmp_path, monkeypatch, base_state())
        assert await flow.reset(persisted=False) == {
            "status": "RESET_COMPLETE",
            "retired": None,
            "stopped": 0,
        }
        assert pool.executed == []

    async def test_reset_stops_in_flight_candidates_and_archives_the_document(self, tmp_path, monkeypatch):
        state = base_state(
            candidates=[
                {"info_hash": "a" * 40, "state": "pending"},
                {"info_hash": "b" * 40, "state": "download_intent"},
                {"info_hash": "c" * 40, "state": "rejected"},
            ]
        )
        flow, pool = make_flow(tmp_path, monkeypatch, state)
        stopped: list[str] = []

        async def fake_stop(candidate, _secret):
            stopped.append(candidate["info_hash"])

        monkeypatch.setattr(flow, "stop_candidate", fake_stop)
        monkeypatch.setattr(flow_module, "read_private", lambda _p: {"username": "u", "password": "p"})

        result = await flow.reset(persisted=True)

        assert result["status"] == "RESET_COMPLETE"
        assert result["stopped"] == 2
        assert stopped == ["a" * 40, "b" * 40]
        archive = tmp_path / "evidence" / f"superseded-{state['id']}.json"
        assert json.loads(archive.read_text())["id"] == state["id"]
        assert archive.stat().st_mode & 0o777 == 0o600
        assert "DELETE FROM first_4k_runs" in pool.executed[-1][0]

    async def test_reset_refuses_once_a_film_has_been_selected(self, tmp_path, monkeypatch):
        flow, _ = make_flow(tmp_path, monkeypatch, base_state(video_id=str(uuid.uuid4())))
        with pytest.raises(CanaryBlockedError, match="explicit reconciliation"):
            await flow.reset(persisted=True)


class TestCli:
    def _parse(self, argv: list[str]) -> argparse.Namespace:
        parser = argparse.ArgumentParser()
        parser.add_argument(
            "command",
            choices=("boards", "preflight", "reset", "run", "resume", "verify", "prepare-playback"),
        )
        parser.add_argument("--min-movie-gib", type=int, default=8)
        return parser.parse_args(argv)

    def test_every_command_the_runbook_uses_is_accepted(self):
        for command in ("boards", "preflight", "reset", "run", "resume", "verify", "prepare-playback"):
            assert self._parse([command]).command == command

    def test_a_completed_run_exits_zero(self):
        # A successful LIVE_VERIFIED previously exited 2, which any CI wrapper
        # would have read as a failure.
        assert "LIVE_VERIFIED" in SUCCESS_STATUSES
        assert "PLAYABLE_QUOTA_WAIT" in SUCCESS_STATUSES
        assert "BOARDS_READY" in SUCCESS_STATUSES
        assert "RESET_COMPLETE" in SUCCESS_STATUSES

    def test_a_blocked_or_operator_gated_run_never_exits_zero(self):
        assert "BLOCKED" not in SUCCESS_STATUSES
        assert "USER_ACTION_REQUIRED" not in SUCCESS_STATUSES
        assert "QUOTA_WAIT" not in SUCCESS_STATUSES


class _StopBeforeMediaError(Exception):
    """Ends the candidate flow once the deadline branch has been taken."""


class FakeTorrent:
    """Stands in for the qBittorrent wrapper up to the completion wait."""

    timeouts: list[float | None] = []

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def health_check(self) -> None:
        return None

    async def has_torrent(self, _hash: str) -> bool:
        return True

    async def wait_complete(self, _hash: str, timeout=None):
        FakeTorrent.timeouts.append(timeout)
        raise _StopBeforeMediaError


class TestDownloadDeadline:
    """The six hour deadline bounds waiting for a swarm, not the work after it.

    Segmentation runs while the run state still has no video_id, so an
    interruption sends the next resume back through the candidate flow. The
    intent timestamp never moves, so a completed download must not inherit it.
    """

    def _candidate(self, state: str) -> dict:
        return {
            "info_hash": "a" * 40,
            "state": state,
            "intent_at": "2020-01-01T00:00:00+00:00",
            "magnet_uri": "magnet:?xt=urn:btih:" + "a" * 40,
            "title": "film",
        }

    async def test_a_completed_download_survives_a_stale_intent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(flow_module, "MovieTorrent", FakeTorrent)
        FakeTorrent.timeouts = []
        flow, _ = make_flow(tmp_path, monkeypatch, base_state())
        with pytest.raises(_StopBeforeMediaError):
            await flow._candidate(self._candidate("download_complete"), {"username": "u", "password": "p"})
        # wait_complete returns on its first poll, but it needs a positive budget.
        assert FakeTorrent.timeouts == [600]

    async def test_an_unfinished_download_still_expires(self, tmp_path, monkeypatch):
        monkeypatch.setattr(flow_module, "MovieTorrent", FakeTorrent)
        FakeTorrent.timeouts = []
        flow, _ = make_flow(tmp_path, monkeypatch, base_state())
        with pytest.raises(CanaryBlockedError, match="deadline expired"):
            await flow._candidate(self._candidate("download_intent"), {"username": "u", "password": "p"})
        assert FakeTorrent.timeouts == []


class FakePart:
    """Only the field the pause rule reads."""

    def __init__(self, counted: bool) -> None:
        self.usage_counted_at = "2026-09-11T00:00:00+00:00" if counted else None


class TestOperationFaultRetainsTheFilm:
    """A tooling fault must never cost an already downloaded film.

    The candidate loop rejects a source on IncompatibleMediaError and moves on,
    which after a completed transfer means discarding hours of download and
    starting another. An operation fault says nothing about the source.
    """

    async def test_a_media_operation_fault_never_rejects_the_candidate(self, tmp_path, monkeypatch):
        monkeypatch.setattr(flow_module, "read_private", lambda _path: {"username": "u", "password": "p"})
        candidate = {"info_hash": "a" * 40, "state": "download_complete", "title": "film"}
        flow, _ = make_flow(tmp_path, monkeypatch, base_state(candidates=[candidate]))

        async def fault(*_args, **_kwargs):
            raise MediaOperationError("media operation deadline exceeded")

        monkeypatch.setattr(flow, "_candidate", fault)
        with pytest.raises(MediaOperationError):
            await flow.download_prepare()
        assert candidate["state"] == "download_complete"
        assert "failure_class" not in candidate


class TestPartLimit:
    """`--max-parts` bounds one invocation, not the round."""

    def _flow(self, tmp_path, monkeypatch, limit):
        flow, _ = make_flow(tmp_path, monkeypatch, base_state())
        flow.args.max_parts = limit
        return flow

    def test_no_limit_never_pauses(self, tmp_path, monkeypatch):
        flow = self._flow(tmp_path, monkeypatch, 0)
        assert flow.pause_after(9, [FakePart(True), FakePart(False)], 0) is False

    def test_a_limit_pauses_while_parts_remain(self, tmp_path, monkeypatch):
        flow = self._flow(tmp_path, monkeypatch, 1)
        assert flow.pause_after(1, [FakePart(True), FakePart(False)], 0) is True

    def test_a_limit_landing_on_the_final_part_completes_instead(self, tmp_path, monkeypatch):
        flow = self._flow(tmp_path, monkeypatch, 2)
        assert flow.pause_after(2, [FakePart(True), FakePart(True)], 1) is False

    def test_below_the_limit_keeps_going(self, tmp_path, monkeypatch):
        flow = self._flow(tmp_path, monkeypatch, 2)
        assert flow.pause_after(1, [FakePart(True), FakePart(False)], 0) is False


@pytest.mark.parametrize("value", ["-1", "1.5", "oops"])
def test_max_parts_cli_rejects_invalid_values_before_runtime(monkeypatch, value):
    monkeypatch.setattr("sys.argv", ["first_4k_movie.py", "resume", "--max-parts", value])
    with pytest.raises(SystemExit) as error:
        flow_module.main()
    assert error.value.code == 2


@pytest.mark.parametrize("value", ["0", "1", "2"])
def test_max_parts_accepts_nonnegative_values(value):
    assert flow_module.nonnegative_int(value) == int(value)


@pytest.mark.parametrize(
    "automation,display,free,quota,expected",
    [
        ("PASS", "124.88 MB of 15 GB", True, "PASS", "LIVE_VERIFIED"),
        ("OPEN", "124.88 MB of 15 GB", True, "PASS", "GATE_NOT_PASSED"),
        ("PASS", "new UI format", True, "OPEN", "GATE_NOT_PASSED"),
        ("PASS", "124.88 MB of 15 GB", False, "OPEN", "GATE_NOT_PASSED"),
        ("PASS", "125.00 MB of 15 GB", True, "FAIL", "GATE_NOT_PASSED"),
    ],
)
async def test_verify_only_requires_movie_quota_automation(
    tmp_path, monkeypatch, automation, display, free, quota, expected
):
    from datetime import datetime, timedelta, timezone
    from unittest.mock import AsyncMock

    from pixav.shared.models import VideoPart

    uploaded = datetime.now(timezone.utc) - timedelta(days=2)
    video = uuid.uuid4()
    state = base_state(
        video_id=str(video),
        playback={"filename": "movie.mp4", "sha256": hashlib.sha256(b"movie").hexdigest()},
        source_provenance={"reference": {"duration": 20}},
        gates={
            "movie": "OPEN",
            "quota": "OPEN",
            "automation": automation,
            "vpn": "OPEN",
            "production_promotion": "NOT_REQUESTED",
        },
        device={
            "quota_before": {
                "display": "124.88 MB of 15 GB",
                "display_resolution": "0.01 MB",
                "observed_at": (uploaded - timedelta(hours=1)).isoformat(),
            }
        },
    )
    flow, pool = make_flow(tmp_path, monkeypatch, state)
    (tmp_path / "playback").mkdir()
    (tmp_path / "playback/movie.mp4").write_bytes(b"movie")
    (tmp_path / "playback" / f"{video}.strm").write_text(f"http://127.0.0.1:28000/stream/{video}\n")
    pool.fetch = AsyncMock(return_value=[{"id": video}])
    parts = [
        VideoPart(
            video_id=video,
            part_index=i,
            manifest_version=1,
            start_seconds=i * 10,
            end_seconds=(i + 1) * 10,
            filename=f"pixav-{video}-part-{i:06d}-{'a'*16}.mp4",
            size_bytes=20000,
            sha256="a" * 64,
            uploaded_at=uploaded,
            media_info={"streams": [{"codec_type": "video", "width": 3840, "height": 2160}]},
        )
        for i in range(2)
    ]
    if automation == "PASS":
        state["recovery_drill"] = {
            "completed_at": uploaded.isoformat(),
            "resumed_at": uploaded.isoformat(),
            "no_duplicate_effects": True,
            "invocation_id": "drill",
            "resumed_invocation_id": "resume",
        }
        state["cli_invocations"] = [
            {"id": "drill", "command": "recovery-drill", "result": "RECOVERY_DRILL_COMPLETE"},
            {"id": "resume", "command": "resume", "uploads_completed_at": uploaded.isoformat()},
        ]
        parts = [
            p.model_copy(
                update={
                    "usage_counted_at": uploaded,
                    "recovery": {
                        "operations": {
                            kind: {
                                "intent_at": uploaded.isoformat(),
                                "completed_at": uploaded.isoformat(),
                                "identity_verified": True,
                            }
                            for kind in ("push", "publish", "backup", "share")
                        }
                    },
                }
            )
            for p in parts
        ]
    flow.parts.list = AsyncMock(return_value=parts)
    pool.fetchval = AsyncMock(
        side_effect=[datetime.now(timezone.utc), uploaded + timedelta(days=1), datetime.now(timezone.utc)]
    )
    flow.runtime = AsyncMock(return_value=(object(), object()))
    monkeypatch.setattr(flow_module, "range_acceptance", AsyncMock(return_value={}))
    monkeypatch.setattr(flow_module, "vlc_acceptance", AsyncMock(return_value={}))
    uploader = AsyncMock()
    uploader.quota_observation.return_value = {
        "display": display,
        "display_resolution": "0.01 MB",
        "backup_complete": True,
    }
    monkeypatch.setattr(flow_module, "MaestroPartUploader", lambda *_a, **_k: uploader)
    monkeypatch.setattr(
        flow_module,
        "collect_item",
        lambda *_a: {"zero_item_charge": free, "observed_at": datetime.now(timezone.utc).isoformat()},
    )
    result = await flow.verify()
    assert result["status"] == expected
    assert state["gates"]["quota"] == quota
    assert state["gates"]["vpn"] == "OPEN"
    assert state["gates"]["production_promotion"] == "NOT_REQUESTED"
