"""BDD-025–030/035: strict media facts, preservation and crash output reuse."""

import asyncio
import json
import shutil
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pixav.media_loader.metadata import probe_media
from pixav.media_loader.preparation import (
    MediaFacts,
    PreparationPolicy,
    PreparedArtifact,
    PreparedSegment,
    prepare_media,
    segment_plan,
)
from pixav.shared.exceptions import MediaDependencyError, RemuxError


def facts(**changes):
    return MediaFacts(
        **{
            **dict(
                container="mov,mp4",
                size_bytes=100,
                duration_seconds=10,
                sha256="a" * 64,
                streams=(
                    {"kind": "video", "codec": "h264", "width": 1920, "height": 1080},
                    {"kind": "audio", "codec": "aac"},
                ),
            ),
            **changes,
        }
    )


@pytest.mark.parametrize("delta,valid", [(0, True), (0.05, True), (0.0501, False)])
def test_duration_policy_bdd_029(delta, valid):
    source = facts()
    output = facts(duration_seconds=10 + delta)
    if valid:
        PreparationPolicy().validate_output(source, output)
    else:
        with pytest.raises(RemuxError, match="duration"):
            PreparationPolicy().validate_output(source, output)


@pytest.mark.parametrize("duration", [0, -1, float("inf"), float("nan")])
def test_nonfinite_or_empty_duration_is_invalid_bdd_026(duration):
    with pytest.raises(ValueError):
        facts(duration_seconds=duration)


async def test_probe_missing_corrupt_and_extra_streams_bdd_025_026(tmp_path):
    media = tmp_path / "synthetic.mp4"
    with pytest.raises(RemuxError):
        await probe_media(str(media))
    media.write_bytes(b"synthetic")
    proc = AsyncMock(returncode=0)
    proc.communicate.return_value = (b"not-json", b"")
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        with pytest.raises(RemuxError):
            await probe_media(str(media))
        raw = dict(
            format=dict(format_name="mov,mp4", size=9, duration=10),
            streams=[
                dict(codec_type="video", codec_name="h264", width=1280, height=720),
                dict(codec_type="audio", codec_name="aac"),
            ],
        )
        proc.communicate.return_value = (json.dumps(raw).encode(), b"")
        result = await probe_media(str(media))
        assert len(result["streams"]) == 2 and len(result["sha256"]) == 64
        raw["streams"].append(dict(codec_type="subtitle", codec_name="mov_text"))
        proc.communicate.return_value = (json.dumps(raw).encode(), b"")
        with pytest.raises(RemuxError):
            await probe_media(str(media))


async def test_probe_timeout_reaps_child_bdd_026(tmp_path):
    media = tmp_path / "synthetic.mp4"
    media.write_bytes(b"synthetic")
    proc = AsyncMock(returncode=None)
    proc.kill = MagicMock()
    proc.communicate.side_effect = asyncio.TimeoutError()
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        with pytest.raises(MediaDependencyError):
            await probe_media(str(media))
    proc.kill.assert_called_once()
    proc.wait.assert_awaited_once()


@pytest.mark.parametrize("committed", [True, False])
async def test_verified_existing_output_is_reused_bdd_035(tmp_path, committed):
    source, output = tmp_path / "synthetic.mkv", tmp_path / "prepared.mp4"
    source.write_bytes(b"source")
    output.with_suffix(".intent.json").write_text(
        json.dumps(
            {
                "source": facts(container="matroska").model_dump(mode="json"),
                "policy": PreparationPolicy().model_dump(mode="json"),
            }
        )
    )
    (output if committed else output.with_suffix(".pending.mp4")).write_bytes(b"completed output before checkpoint")
    remuxer = AsyncMock()
    with patch("pixav.media_loader.preparation.inspect_media", side_effect=[facts(container="matroska"), facts()]):
        result = await prepare_media(str(source), str(output), remuxer)
    remuxer.remux.assert_not_awaited()
    assert result.path == str(output)
    assert source.read_bytes() == b"source"


async def test_existing_output_requires_exact_input_identity_bdd_035(tmp_path):
    source, output = tmp_path / "synthetic.mkv", tmp_path / "prepared.mp4"
    source.write_bytes(b"source")
    output.write_bytes(b"a different film with matching duration and codec")
    remuxer = AsyncMock()
    with patch("pixav.media_loader.preparation.inspect_media", return_value=facts(container="matroska")):
        with pytest.raises(RemuxError, match="no preparation intent"):
            await prepare_media(str(source), str(output), remuxer)
        output.with_suffix(".intent.json").write_text(
            json.dumps(
                {
                    "source": facts(container="matroska", sha256="b" * 64).model_dump(mode="json"),
                    "policy": PreparationPolicy().model_dump(mode="json"),
                }
            )
        )
        with pytest.raises(RemuxError, match="different input"):
            await prepare_media(str(source), str(output), remuxer)
    remuxer.remux.assert_not_awaited()
    assert output.read_bytes().startswith(b"a different film")


async def test_real_lossless_media_bdd_025_027_028_030(tmp_path):
    """Run the same standalone contract used inside the media-loader image."""
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("requires real FFmpeg/ffprobe (media-loader image)")
    from tests.integration.workflow_media_contract import verify_media_contract

    await verify_media_contract(tmp_path)


def _artifact(segments=(), duration=100.0, size=2048):
    facts = MediaFacts(
        container="mov,mp4",
        size_bytes=size,
        duration_seconds=duration,
        sha256="c" * 64,
        streams=(
            {"kind": "video", "codec": "h264", "width": 1920, "height": 1080},
            {"kind": "audio", "codec": "aac"},
        ),
    )
    return PreparedArtifact(
        path="/staging/prepared.mp4",
        input_path="/staging/source.mkv",
        input_sha256="b" * 64,
        facts=facts,
        segments=segments,
    )


def _segment(index, start, end, sha="d" * 64):
    return PreparedSegment(
        index=index,
        path=f"/staging/part-{index}.mp4",
        size_bytes=1024,
        sha256=sha,
        start_seconds=start,
        end_seconds=end,
    )


def test_artifact_within_the_provider_limit_uploads_whole():
    plan = segment_plan(_artifact())
    assert len(plan) == 1
    assert plan[0].path == "/staging/prepared.mp4"
    assert plan[0].sha256 == "c" * 64
    assert plan[0].end_seconds == 100.0
    # The Photos item check reads ffprobe-shaped streams off this.
    assert plan[0].media_info["streams"][0]["codec_type"] == "video"


def test_contiguous_plan_is_accepted():
    plan = segment_plan(_artifact(segments=(_segment(0, 0.0, 60.0), _segment(1, 60.0, 100.0))))
    assert [s.index for s in plan] == [0, 1]


def test_plan_with_a_gap_is_rejected():
    with pytest.raises(RemuxError):
        segment_plan(_artifact(segments=(_segment(0, 0.0, 50.0), _segment(1, 60.0, 100.0))))


def test_plan_with_an_overlap_is_rejected():
    with pytest.raises(RemuxError):
        segment_plan(_artifact(segments=(_segment(0, 0.0, 70.0), _segment(1, 60.0, 100.0))))


def test_plan_not_starting_at_the_beginning_is_rejected():
    with pytest.raises(RemuxError):
        segment_plan(_artifact(segments=(_segment(0, 5.0, 100.0),)))


def test_plan_short_of_the_full_duration_is_rejected():
    """A manifest that stops early cannot be repaired once the bytes are remote."""
    with pytest.raises(RemuxError):
        segment_plan(_artifact(segments=(_segment(0, 0.0, 60.0), _segment(1, 60.0, 80.0))))


def test_plan_with_a_missing_index_is_rejected():
    with pytest.raises(RemuxError):
        segment_plan(_artifact(segments=(_segment(0, 0.0, 60.0), _segment(2, 60.0, 100.0))))
