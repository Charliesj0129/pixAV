"""BDD-146 push admission and the stage-copy release after the published copy matched."""

import hashlib
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pixav.pixel_injector.canary import OWNER_LABEL, CanaryBlockedError
from pixav.pixel_injector.guest_space import (
    GUEST_PUSH_MARGIN_BYTES,
    guest_push_required_bytes,
    parse_available_bytes,
)
from pixav.pixel_injector.maestro_parts import MaestroPartUploader, UploadReviewRequiredError
from pixav.pixel_injector.session import RedroidSession
from pixav.shared.models import VideoPart

HEADER = "Filesystem 1K-blocks Used Available Use% Mounted on\n"


def df(available_kib: int) -> str:
    return HEADER + f"/dev/block/dm-36 10295368 9762216 {available_kib} 97% /data\n"


@pytest.fixture
def upload(tmp_path):
    owner, video = str(uuid.uuid4()), uuid.uuid4()
    digest = hashlib.sha256(b"original").hexdigest()
    part = VideoPart(
        video_id=video,
        part_index=0,
        manifest_version=1,
        start_seconds=0,
        end_seconds=10,
        size_bytes=8,
        sha256=digest,
        filename=f"pixav-{video}-part-0-{digest[:16]}.mp4",
        media_info={},
    )
    guest = SimpleNamespace(id="guest", labels={OWNER_LABEL: owner, "pixav.photos_canary.role": "guest"})
    runner = SimpleNamespace(id="tools", labels={OWNER_LABEL: owner, "pixav.photos_canary.role": "tools"})
    u = MaestroPartUploader(guest, runner, owner, part, {}, AsyncMock(), Path("config/maestro/photos-canary"))
    u.adb = AsyncMock()
    path = tmp_path / part.filename
    path.write_bytes(b"original")
    return u, RedroidSession(owner, "guest", "127.0.0.1", 5555), path


def test_required_bytes_is_two_copies_plus_margin():
    assert guest_push_required_bytes(1_000) == 2_000 + GUEST_PUSH_MARGIN_BYTES
    with pytest.raises(ValueError):
        guest_push_required_bytes(0)


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (df(381952), 381952 * 1024),
        ("/dev/block/dm-36 10295368 9762216 381952 97% /data", 381952 * 1024),
        (HEADER, None),
        ("", None),
        ("df: /data/local/tmp: Permission denied", None),
        (df(1) + "/dev/block/dm-1 10 5 5 50% /other\n", None),
    ],
)
def test_available_bytes_reads_exactly_one_filesystem_row(output, expected):
    assert parse_available_bytes(output) == expected


@pytest.mark.parametrize("output", [df(0), "df: unknown", ""])
async def test_push_is_refused_before_any_byte_when_space_is_short_or_unknown(upload, output):
    """BDD-146: no push intent, no stage file, a closed code for the operator."""
    u, session, path = upload
    u.adb.return_value = output
    with pytest.raises(UploadReviewRequiredError) as caught:
        await u.push_file(session, str(path))
    assert caught.value.code == "GUEST_SPACE_INSUFFICIENT"
    assert "push_intent" not in u.recovery
    assert [call.args for call in u.adb.await_args_list] == [("shell", "df", "-k", "/data/local/tmp")]


async def test_space_below_two_copies_plus_margin_is_refused(upload):
    u, session, path = upload
    needed = guest_push_required_bytes(u.part.size_bytes)
    u.adb.return_value = df(needed // 1024 - 1)
    with pytest.raises(UploadReviewRequiredError, match="free space"):
        await u.push_file(session, str(path))


async def test_resumed_push_does_not_ask_for_space_again(upload):
    """An interrupted push already wrote bytes; admission belongs to the first attempt only."""
    u, session, path = upload
    u.recovery["push_intent"] = True
    u.adb.return_value = "wrong hash"
    with pytest.raises(UploadReviewRequiredError, match="stage hash"):
        await u.push_file(session, str(path))
    assert all("df" not in call.args for call in u.adb.await_args_list)


def answers(u, *, stage_left: bool = False):
    async def adb(*args, **kwargs):
        if "df" in args:
            return df(64 * 1024 * 1024)
        if any("[ -e " in arg for arg in args):
            return "PRESENT\n" if stage_left else "ABSENT\n"
        if "sha256sum" in args:
            return u.part.sha256 + " file"
        if args[0] == "shell" and args[1].startswith("content query"):
            return f"Row: 0 _id=42, _display_name={u.part.filename}, _size=8"
        return ""

    return adb


async def test_stage_copy_is_removed_only_after_the_published_copy_matched(upload):
    u, session, path = upload
    effects = []
    inner = answers(u)

    async def adb(*args, **kwargs):
        effects.append(args)
        if args[:3] == ("shell", "rm", "-f"):
            assert u.recovery["operations"]["publish"]["identity_verified"]
            assert u.recovery["stage_release_intent"]
        return await inner(*args, **kwargs)

    u.adb.side_effect = adb
    await u.push_file(session, str(path))
    removals = [args for args in effects if args[:3] == ("shell", "rm", "-f")]
    assert removals == [("shell", "rm", "-f", f"/data/local/tmp/{u.part.filename}")]
    published_check = effects.index(("shell", "sha256sum", f"/sdcard/DCIM/Camera/{u.part.filename}"))
    assert effects.index(removals[0]) > published_check
    assert u.recovery["operations"]["stage_release"]["identity_verified"]


async def test_a_stage_copy_that_survives_removal_stops_the_upload(upload):
    u, session, path = upload
    u.adb.side_effect = answers(u, stage_left=True)
    with pytest.raises(CanaryBlockedError, match="not removed"):
        await u.push_file(session, str(path))
    assert "completed_at" not in u.recovery["operations"]["stage_release"]


async def test_resume_after_stage_release_rechecks_only_the_published_copy(upload):
    u, session, path = upload
    u.recovery.update(push_intent=True, publish_intent=True, stage_release_intent=True, media_id="42")
    u.adb.side_effect = answers(u)
    await u.push_file(session, str(path))
    hashed = [call.args[-1] for call in u.adb.await_args_list if "sha256sum" in call.args]
    assert hashed == [f"/sdcard/DCIM/Camera/{u.part.filename}"]
    assert all(call.args[0] != "push" and "cp" not in call.args for call in u.adb.await_args_list)


async def test_a_mismatched_published_copy_keeps_the_stage_copy(upload):
    u, session, path = upload
    inner = answers(u)

    async def adb(*args, **kwargs):
        if "sha256sum" in args and args[-1].startswith("/sdcard/"):
            return "0" * 64 + " file"
        return await inner(*args, **kwargs)

    u.adb.side_effect = adb
    with pytest.raises(UploadReviewRequiredError, match="published"):
        await u.push_file(session, str(path))
    assert "stage_release_intent" not in u.recovery
    assert all(call.args[:2] != ("shell", "rm") for call in u.adb.await_args_list)
