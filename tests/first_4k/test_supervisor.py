"""The supervisor's guards, tested without a live run.

Four copies of this logic lived under `.verify/` and none of them had a test:
they were operator scripts, edited in place, differing from each other in ways
nobody could see. The parts that decide whether to re-enter the CLI are the
parts worth pinning, so they are tested here as ordinary functions.
"""

from __future__ import annotations

import json
import os
import stat
import uuid
from pathlib import Path

import pytest

from pixav.first_4k.boundary import Expectation, Supervision, _check_drill_boundary
from pixav.first_4k.evidence import event, source_digests, verify_sources, write
from pixav.first_4k.supervisor import _seeks, load_expectation
from pixav.pixel_injector.canary import CanaryBlockedError

IDENTITY = {
    "docker_id": "9b65bb04-cbe4-40cc-b195-83be675bfbf4",
    "database": "pixav_first_4k",
    "db_identity": "7683839729278689318",
    "redis_identity": "fd341096fca35fa550a6d706bf4cb0d23386cd11",
}


def part(
    index: int,
    *,
    counted: bool,
    state: str = "prepared",
    recovery: dict | None = None,
    share: str = "s",
    start: float | None = None,
):
    return SimpleNamespaceLike(
        part_index=index,
        usage_counted_at="2026-09-14T00:00:00+00:00" if counted else None,
        state=state,
        recovery=recovery if recovery is not None else ({"media_id": "m"} if counted else {}),
        share_url=share,
        start_seconds=float(index) * 100 if start is None else start,
    )


class SimpleNamespaceLike:
    def __init__(self, **fields: object) -> None:
        self.__dict__.update(fields)


class TestEvidenceIsWrittenOnce:
    def test_a_second_write_of_the_same_name_fails(self, tmp_path: Path) -> None:
        """Evidence is a record, not a buffer; a rerun must not replace it."""
        write(tmp_path, "meta.json", {"a": 1})

        with pytest.raises(FileExistsError):
            write(tmp_path, "meta.json", {"a": 2})

    def test_written_evidence_is_private(self, tmp_path: Path) -> None:
        write(tmp_path, "meta.json", {"a": 1})

        assert stat.S_IMODE(os.stat(tmp_path / "meta.json").st_mode) == 0o600

    def test_bytes_are_written_verbatim(self, tmp_path: Path) -> None:
        """Backups arrive as pg_dump bytes and must not be JSON-encoded."""
        write(tmp_path, "before.dump", b"\x00binary")

        assert (tmp_path / "before.dump").read_bytes() == b"\x00binary"

    def test_events_append_rather_than_replace(self, tmp_path: Path) -> None:
        event(tmp_path, "CHECKED", stage="preparing_media")
        event(tmp_path, "WAITING_EXISTING_RUNNER", stage="preparing_media")

        lines = (tmp_path / "events.jsonl").read_text().splitlines()
        assert [json.loads(line)["event"] for line in lines] == ["CHECKED", "WAITING_EXISTING_RUNNER"]
        assert stat.S_IMODE(os.stat(tmp_path / "events.jsonl").st_mode) == 0o600


class TestSourceDigests:
    def test_only_source_and_configuration_files_are_pinned(self, tmp_path: Path) -> None:
        (tmp_path / "src").mkdir()
        (tmp_path / "scripts").mkdir()
        (tmp_path / "config").mkdir()
        (tmp_path / "src/module.py").write_text("x = 1")
        (tmp_path / "config/profiles.yml").write_text("a: 1")
        (tmp_path / "src/notes.md").write_text("ignored")
        (tmp_path / "scripts/data.bin").write_bytes(b"ignored")

        digests = source_digests(tmp_path)

        assert sorted(Path(p).name for p in digests) == ["module.py", "profiles.yml"]

    def test_an_unchanged_tree_passes(self, tmp_path: Path) -> None:
        for name in ("src", "scripts", "config"):
            (tmp_path / name).mkdir()
        (tmp_path / "src/module.py").write_text("x = 1")

        verify_sources(source_digests(tmp_path))

    def test_an_edited_file_stops_the_supervisor(self, tmp_path: Path) -> None:
        """The exact case this branch created: a shim landing mid-run."""
        for name in ("src", "scripts", "config"):
            (tmp_path / name).mkdir()
        (tmp_path / "scripts/migrate.py").write_text("x = 1")
        digests = source_digests(tmp_path)
        (tmp_path / "scripts/migrate.py").write_text("x = 2")

        with pytest.raises(RuntimeError, match="source/config changed while waiting"):
            verify_sources(digests)

    def test_a_deleted_file_stops_the_supervisor(self, tmp_path: Path) -> None:
        """A move leaves the old path gone, which is a change like any other."""
        for name in ("src", "scripts", "config"):
            (tmp_path / name).mkdir()
        (tmp_path / "scripts/instance_guard.py").write_text("x = 1")
        digests = source_digests(tmp_path)
        (tmp_path / "scripts/instance_guard.py").unlink()

        with pytest.raises(RuntimeError, match="source/config changed while waiting"):
            verify_sources(digests)


class TestExpectation:
    def test_it_is_read_from_a_private_file(self, tmp_path: Path) -> None:
        path = tmp_path / "expected-identity.json"
        path.write_text(json.dumps(IDENTITY))
        path.chmod(0o600)

        assert load_expectation(path).db_identity == IDENTITY["db_identity"]

    def test_a_world_readable_expectation_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "expected-identity.json"
        path.write_text(json.dumps(IDENTITY))
        path.chmod(0o644)

        with pytest.raises(CanaryBlockedError):
            load_expectation(path)

    def test_a_missing_fingerprint_is_refused(self, tmp_path: Path) -> None:
        """Defaulting a fingerprint would authorise whatever instance answered."""
        path = tmp_path / "expected-identity.json"
        path.write_text(json.dumps({k: v for k, v in IDENTITY.items() if k != "redis_identity"}))
        path.chmod(0o600)

        with pytest.raises(ValueError, match="redis_identity"):
            load_expectation(path)

    def test_it_cannot_be_edited_after_loading(self, tmp_path: Path) -> None:
        path = tmp_path / "expected-identity.json"
        path.write_text(json.dumps(IDENTITY))
        path.chmod(0o600)

        with pytest.raises(ValueError):
            load_expectation(path).db_identity = "other"


class TestSupervisionRecordsItsAuthorisation:
    def test_the_authorisation_text_is_carried_with_the_run(self, tmp_path: Path) -> None:
        supervision = Supervision(
            run_id=uuid.uuid4(),
            expectation=Expectation.model_validate(IDENTITY),
            out=tmp_path,
            digests={},
            authorization="local 0600 backup authorised by the operator",
        )

        assert supervision.authorization == "local 0600 backup authorised by the operator"


class TestDrillBoundary:
    def test_the_exact_boundary_is_accepted(self) -> None:
        _check_drill_boundary({"stage": "upload_paused"}, [part(0, counted=True), part(1, counted=False)])

    @pytest.mark.parametrize(
        ("reason", "state", "parts"),
        [
            ("still uploading", {"stage": "uploading"}, [part(0, counted=True), part(1, counted=False)]),
            ("single-part film", {"stage": "upload_paused"}, [part(0, counted=True)]),
            (
                "nothing confirmed yet",
                {"stage": "upload_paused"},
                [part(0, counted=False), part(1, counted=False)],
            ),
            (
                "two already confirmed",
                {"stage": "upload_paused"},
                [part(0, counted=True), part(1, counted=True)],
            ),
            (
                "drill already run",
                {"stage": "upload_paused", "recovery_drill": {"at": "x"}},
                [part(0, counted=True), part(1, counted=False)],
            ),
            (
                "pending part carries recovery state",
                {"stage": "upload_paused"},
                [part(0, counted=True), part(1, counted=False, recovery={"push_intent": "x"})],
            ),
            (
                "pending part is not prepared",
                {"stage": "upload_paused"},
                [part(0, counted=True), part(1, counted=False, state="quota_wait")],
            ),
        ],
    )
    def test_anything_else_is_refused(self, reason: str, state: dict, parts: list) -> None:
        """The drill only proves what it claims at one point in the run."""
        with pytest.raises(RuntimeError, match="not the exact first-confirmed-part boundary"):
            _check_drill_boundary(state, parts)

    def test_a_confirmed_part_without_share_identity_is_refused(self) -> None:
        parts = [part(0, counted=True, recovery={}), part(1, counted=False)]

        with pytest.raises(RuntimeError, match="lacks backup/share identity"):
            _check_drill_boundary({"stage": "upload_paused"}, parts)


class TestSeeks:
    def test_every_internal_cut_is_probed_from_both_sides(self) -> None:
        parts = [part(0, counted=True), part(1, counted=True), part(2, counted=True)]

        assert _seeks(300.0, parts) == [0.0, 99.0, 101.0, 150.0, 199.0, 201.0, 295.0]

    def test_a_cut_near_either_end_is_clamped_into_the_film(self) -> None:
        """A probe one second before the first cut, or after the last, must stay seekable."""
        parts = [part(0, counted=True, start=0.0), part(1, counted=True, start=0.5), part(2, counted=True, start=299.5)]

        seeks = _seeks(300.0, parts)

        assert min(seeks) == 0.0
        assert max(seeks) == 299.0
