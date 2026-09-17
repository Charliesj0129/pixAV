import subprocess
import sys
from pathlib import Path

import pytest

from scripts.backup_postgres import build_command

ROOT = Path(__file__).resolve().parents[2]


def test_build_command_uses_custom_format_without_shell_redirection() -> None:
    command = build_command(container="pixav-postgres", user="pixav", database="pixav")

    assert command[:3] == ["docker", "exec", "pixav-postgres"]
    assert "pg_dump" in command
    assert "--format=custom" in command
    assert ">" not in command


@pytest.mark.parametrize("container", ["bad/name", "bad name", "bad;name"])
def test_build_command_rejects_unsafe_container_name(container: str) -> None:
    with pytest.raises(ValueError):
        build_command(container=container, user="pixav", database="pixav")


@pytest.mark.parametrize("script", ["backup_postgres.py", "cleanup_watermark_garbage.py"])
def test_operational_scripts_support_direct_help_invocation(script: str) -> None:
    completed = subprocess.run(  # noqa: S603 -- fixed interpreter and allowlisted filenames
        [sys.executable, str(ROOT / "scripts" / script), "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
