"""Stage A keeps two copies of each module; these tests prove they have not diverged.

The single-film pipeline is moving out of ``scripts/`` into ``pixav.first_4k``.
The move is staged because a live supervisor re-reads ``scripts/first_4k_movie.py``
from disk on every invocation, so the originals stay frozen until that run reaches
a natural stopping point. Until then both copies exist, and either one can be
edited by mistake -- which is the exact failure this consolidation exists to stop.
Two agents editing untracked copies could not see each other at all; these tests
turn that invisible overwrite into a failing assertion.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# Every module that currently exists twice, as (package copy, scripts original).
DUPLICATED = {
    "contracts": (ROOT / "src/pixav/first_4k/contracts.py", ROOT / "scripts/first_4k_contracts.py"),
    "recovery": (ROOT / "src/pixav/first_4k/recovery.py", ROOT / "scripts/first_4k_recovery.py"),
}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("name", sorted(DUPLICATED))
def test_the_package_copy_still_matches_its_script_original(name: str) -> None:
    package, original = DUPLICATED[name]
    assert package.exists(), f"{package} is missing; stage A copies it verbatim"
    if not original.exists():
        pytest.skip(f"{original} is already a shim; stage B has removed the duplication")
    assert digest(package) == digest(original), (
        f"{package} and {original} have diverged. One of them was edited alone. "
        "Reconcile the two before either is used again -- the run reads the scripts copy."
    )


def test_the_package_exposes_the_contracts_the_cli_depends_on() -> None:
    """A byte comparison proves sameness; this proves the copy actually imports."""
    from pixav.first_4k import contracts

    assert hasattr(contracts, "resolve_configuration")
    assert hasattr(contracts, "read_status")
    heartbeat = contracts.RunHeartbeat(object(), "run-id", {"stage": "preparing_media"}, tolerance=5)
    # The sticky guard must start clean and must latch once failure is recorded.
    heartbeat.check()
    heartbeat.failed.set()
    with pytest.raises(contracts.CanaryBlockedError):
        heartbeat.check()


def test_the_package_exposes_the_recovery_drill() -> None:
    from pixav.first_4k import recovery

    for name in ("drill", "reconcile", "retained", "automation_verdict"):
        assert hasattr(recovery, name), f"recovery.{name} did not survive the copy"
