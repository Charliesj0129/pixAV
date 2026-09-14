"""Media safety and opt-in real FFmpeg lossless round trips."""

# ruff: noqa: S603, S607 -- fixed opt-in Docker commands, generated fixture paths
import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from pixav.media_loader.video_parts import (
    IncompatibleMediaError,
    MediaOperationError,
    PartMedia,
    contained_file,
    disk_budget,
)
from pixav.shared.models import VideoPart
from pixav.sht_probe.scoring import QualityScorer


def test_source_reference_reuses_only_the_same_complete_file(tmp_path, monkeypatch):
    source = tmp_path / "synthetic.mp4"
    source.write_bytes(b"synthetic source")
    raw = {"format": {"duration": "10"}, "streams": [{"codec_type": "video", "codec_name": "h264"}]}
    reference = {
        "video_sha256": "a" * 64,
        "video_frames": 300,
        "audio_sha256": "b" * 64,
        "encoded_sha256": "c" * 64,
        "signature": [],
        "duration": 10,
        "av_offsets": [0],
    }
    media = PartMedia()
    monkeypatch.setattr(media, "validate_timeline", lambda *_: {"packets": 300})
    monkeypatch.setattr(media, "fingerprint", lambda *_: reference)
    checkpoint = media.source_reference(source, tmp_path, raw, None)

    def forbidden(*_):
        raise AssertionError("a verified source must not be decoded again")

    monkeypatch.setattr(media, "fingerprint", forbidden)
    assert media.source_reference(source, tmp_path, raw, checkpoint) == checkpoint
    source.write_bytes(b"different source")
    with pytest.raises(MediaOperationError, match="checkpoint mismatch"):
        media.source_reference(source, tmp_path, raw, checkpoint)


def test_source_reference_never_checkpoints_a_file_changed_during_decode(tmp_path, monkeypatch):
    source = tmp_path / "synthetic.mp4"
    source.write_bytes(b"original")
    media = PartMedia()
    raw = {"format": {"duration": "10"}, "streams": []}
    monkeypatch.setattr(media, "validate_timeline", lambda *_: {})

    def changing(*_):
        source.write_bytes(b"changed!")
        return {}

    monkeypatch.setattr(media, "fingerprint", changing)
    with pytest.raises(MediaOperationError, match="source changed"):
        media.source_reference(source, tmp_path, raw, None)


def test_failed_reference_persistence_stops_before_segmentation(tmp_path, monkeypatch):
    source = tmp_path / "synthetic.mp4"
    source.write_bytes(b"source")
    media = PartMedia()
    monkeypatch.setattr(media, "validate_source", lambda *_: {"format": {"duration": "10"}})
    monkeypatch.setattr(media, "source_reference", lambda *_: {"sha256": "a", "timeline": {}, "reference": {}})
    monkeypatch.setattr("pixav.media_loader.video_parts.require_space", lambda *_: None)

    def unavailable(_):
        raise OSError("database unavailable")

    def forbidden(*_, **__):
        raise AssertionError("must not split after a failed checkpoint write")

    monkeypatch.setattr(media, "command", forbidden)
    with pytest.raises(OSError, match="database unavailable"):
        media.prepare(source, tmp_path, uuid.uuid4(), save_source_checkpoint=unavailable)


def test_scoring_only_relaxes_size_for_segmented_flow():
    title = "movie 2160p h265.mkv"
    assert QualityScorer().score(title, size_bytes=16 * 1024**3) == -10000
    assert QualityScorer(segmented_storage=True).score(title, size_bytes=16 * 1024**3) > 0
    assert QualityScorer(segmented_storage=True).score(title + " 3d") == -10000


def test_disk_aggregates_mounts_and_preserves_both_latches(tmp_path):
    with patch("shutil.disk_usage", return_value=type("Usage", (), {"free": 500 * 1024**3, "total": 1000 * 1024**3})()):
        result = disk_budget([(tmp_path / "source", 250 * 1024**3), (tmp_path / "cloud", 160 * 1024**3)])
    assert len(result) == 1
    assert not result[0]["ready"]
    assert result[0]["required"] == 410 * 1024**3
    with pytest.raises(ValueError, match="negative"):
        disk_budget([(tmp_path, -1)])


def test_path_and_part_limits(tmp_path):
    source = tmp_path / "file"
    source.write_bytes(b"a")
    link = tmp_path / "link"
    link.symlink_to(source)
    assert contained_file(tmp_path, source) == source
    with pytest.raises(ValueError, match="symlink"):
        contained_file(tmp_path, link)
    with pytest.raises((ValueError, FileNotFoundError)):
        contained_file(tmp_path / "other", source)
    with pytest.raises(ValidationError):
        VideoPart(
            video_id=uuid.uuid4(),
            part_index=0,
            manifest_version=1,
            start_seconds=0,
            end_seconds=1,
            size_bytes=10_000_000_000,
            sha256="a" * 64,
            filename="../escape.mp4",
        )


@pytest.mark.parametrize(
    ("failure", "expected"),
    # A deadline is an operation fault and a non-zero decode is a source verdict.
    # Both stay redacted, but only the second may reject the film.
    [("timeout", MediaOperationError), ("decode", IncompatibleMediaError)],
)
def test_media_tool_failure_is_bounded_and_redacted(failure, expected):
    media = PartMedia()
    effect = subprocess.TimeoutExpired("ffmpeg", 1) if failure == "timeout" else None
    with patch(
        "subprocess.run", side_effect=effect, return_value=subprocess.CompletedProcess([], 1, b"", b"private filename")
    ):
        with pytest.raises(expected) as error:
            media.command(["ffmpeg"], 1)
    assert "private" not in str(error.value)
    assert media.deadline(100) == 1800
    assert media.deadline(3600) == 14400


@pytest.mark.parametrize(
    "key", ["video_sha256", "video_frames", "audio_sha256", "encoded_sha256", "signature", "duration", "av_offsets"]
)
def test_reject_changed_content_or_timeline(key):
    reference = {
        "video_sha256": "a",
        "video_frames": 100,
        "audio_sha256": "b",
        "encoded_sha256": "c",
        "signature": [],
        "duration": 10,
        "av_offsets": [0],
    }
    actual = dict(reference)
    actual[key] = [1] if key in {"av_offsets", "signature"} else 12
    with pytest.raises(IncompatibleMediaError):
        PartMedia.compare(reference, actual)


@pytest.mark.integration
def test_real_four_k_stream_copy_round_trip(tmp_path):
    if os.getenv("PIXAV_RUN_MEDIA_TESTS") != "1":
        pytest.skip("opt-in real FFmpeg tools container")
    media = PartMedia(
        prefix=[
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "-v",
            f"{tmp_path}:{tmp_path}",
            "pixav-photos-canary:maestro-2.10.0",
        ]
    )
    source = tmp_path / "fixture.mp4"
    media.command(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=3840x2160:rate=8:duration=4",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=4",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "28",
            "-g",
            "16",
            "-bf",
            "0",
            "-c:a",
            "aac",
            "-shortest",
            str(source),
        ],
        1800,
    )
    checkpoints = []
    parts, reference = media.prepare(source, tmp_path, uuid.uuid4(), save_source_checkpoint=checkpoints.append)
    assert len(checkpoints) == 1
    assert checkpoints[0]["reference"] == reference["reference"]
    assert len(parts) >= 2
    assert all(p.size_bytes < 10_000_000_000 for p in parts)
    assert reference["local_merge"] == "PASS"
    assert reference["reference"]["video_frames"] == 32
    # Container bytes can differ; decoded samples, encoded content, HDR and timing may not.
    assert source.exists()
    assert all((tmp_path / p.filename).is_file() for p in parts)
    cold = tmp_path / "cold"
    (cold / "cloud").mkdir(parents=True)
    for part in parts:
        shutil.copyfile(tmp_path / part.filename, cold / "cloud" / part.filename)
        (cold / "cloud" / (part.filename + ".json")).write_text(
            json.dumps(
                {
                    "method": "photos-original-browser",
                    "sha256": part.sha256,
                    "size": part.size_bytes,
                    "fixture_only": True,
                }
            )
        )
    # Synthetic receipts test recovery mechanics, never Photos provenance. The
    # process physically cannot access the source or upload directories.
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-i",
            "--network",
            "none",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "-v",
            f"{Path('src').resolve()}:/app/src:ro",
            "-v",
            f"{cold}:/work",
            "pixav-first-4k-tools:1",
            "python",
            "-m",
            "pixav.pixel_injector.parts_download",
        ],
        input=json.dumps(
            {
                "parts": [
                    p.model_copy(update={"share_url": "https://photos.app.goo.gl/fixture"}).model_dump(mode="json")
                    for p in parts
                ],
                "reference": reference["reference"],
            }
        ).encode(),
        capture_output=True,
        timeout=300,
        check=True,
    )  # noqa: S603,S607 - fixed isolated tools process
    assert json.loads(result.stdout)["content"] == "PASS"


class TestOperationFailureClassification:
    """An infrastructure fault must not be blamed on the media.

    The candidate loop rejects a film on IncompatibleMediaError and moves to the
    next source, which for an already downloaded feature means discarding hours
    of transfer. A deadline or a killed container says nothing about the file.
    """

    def _media(self):
        return PartMedia(prefix=["docker", "run"])

    def test_a_deadline_is_an_operation_fault(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="ffmpeg", timeout=1)):
            with pytest.raises(MediaOperationError, match="deadline exceeded"):
                self._media().command(["ffmpeg"], 1)

    @pytest.mark.parametrize("code", [124, 125, 126, 127, 137, 143])
    def test_a_killed_or_unlaunchable_tool_is_an_operation_fault(self, code):
        completed = subprocess.CompletedProcess(args=["ffmpeg"], returncode=code, stdout=b"", stderr=b"")
        with patch("subprocess.run", return_value=completed):
            with pytest.raises(MediaOperationError):
                self._media().command(["ffmpeg"], 1)

    def test_an_operation_fault_is_not_a_source_rejection(self):
        assert not issubclass(MediaOperationError, IncompatibleMediaError)

    def test_ffmpeg_judging_the_media_still_rejects_the_source(self):
        completed = subprocess.CompletedProcess(args=["ffmpeg"], returncode=1, stdout=b"", stderr=b"")
        with patch("subprocess.run", return_value=completed):
            with pytest.raises(IncompatibleMediaError, match="no automatic transcoding"):
                self._media().command(["ffmpeg"], 1)


@pytest.mark.parametrize("code", [-9, -15, -11])
def test_direct_signal_is_an_operation_failure(code):
    with patch("subprocess.run", return_value=subprocess.CompletedProcess([], code, b"", b"private")):
        with pytest.raises(MediaOperationError) as error:
            PartMedia().command(["ffmpeg"], 1)
    assert error.value.category == "terminated"
    assert error.value.operation == "ffmpeg"
    assert "private" not in str(error.value)


@pytest.mark.parametrize("failure", [FileNotFoundError("private path"), PermissionError("private path")])
def test_tool_launch_failure_is_redacted(failure):
    with patch("subprocess.run", side_effect=failure):
        with pytest.raises(MediaOperationError) as error:
            PartMedia().command(["ffprobe"], 1)
    assert error.value.category == "launch_failure"
    assert error.value.operation == "probe"
    assert "private" not in str(error.value)


def test_quarantine_bytes_remain_charged_to_remaining_peak(tmp_path, monkeypatch):
    """Existing retained bytes reduce free space; they are never a budget credit."""
    from pixav.media_loader.video_parts import require_space

    reserve = 100 * 1024**3
    monkeypatch.setattr(
        "shutil.disk_usage", lambda _p: type("Usage", (), {"free": reserve + 3000, "total": reserve * 2})()
    )
    require_space([(tmp_path / "cloud", 3000)])
    (tmp_path / "quarantine").mkdir()
    (tmp_path / "quarantine/partial.zip").write_bytes(b"x" * 1000)
    monkeypatch.setattr(
        "shutil.disk_usage", lambda _p: type("Usage", (), {"free": reserve + 2000, "total": reserve * 2})()
    )
    with pytest.raises(MediaOperationError, match="disk latch"):
        require_space([(tmp_path / "cloud", 3000)])
    assert (tmp_path / "quarantine/partial.zip").stat().st_size == 1000


class TestTimelinePrecheck:
    """A timeline fingerprint() cannot measure must be cheap to find.

    fingerprint() reaches the same verdict only after a full decode, which cost
    6 hours 40 minutes on a 2.5 hour feature before the film was discarded. The
    pre-check must reject a malformed container and nothing else: a variable
    frame rate is legitimate, and the fingerprint now records it faithfully.
    """

    def _raw(self, duration=9003.0):
        return {"streams": [{"codec_type": "video"}], "format": {"duration": str(duration)}}

    def _scan(self, stdout: bytes):
        completed = subprocess.CompletedProcess(args=["ffprobe"], returncode=0, stdout=stdout, stderr=b"")
        with patch("subprocess.run", return_value=completed):
            return PartMedia().validate_timeline(Path("film.mp4"), self._raw())

    def test_a_strictly_increasing_timeline_is_accepted(self):
        assert self._scan(b"0\n1\n2\n3\n") == {"packets": 4, "first_pts": 0, "last_pts": 3}

    def test_decode_order_reordering_is_not_a_defect(self):
        # Packets arrive in decode order, so a B-frame stream legitimately emits
        # timestamps out of order. Presentation order is what must increase.
        assert self._scan(b"0\n3\n1\n2\n6\n4\n5\n")["packets"] == 7

    def test_a_variable_frame_rate_is_not_a_defect(self):
        # The measured feature ran 151000 to 177000 ticks between frames against a
        # nominal 166833. Every stamp is distinct, so nothing here is wrong.
        stamps = b"0\n166833\n340500\n496833\n647833\n"
        assert self._scan(stamps)["packets"] == 5

    def test_a_repeated_timestamp_is_a_malformed_container(self):
        with pytest.raises(IncompatibleMediaError, match="non-monotonic source video timeline"):
            self._scan(b"0\n1\n1\n2\n")

    def test_a_repeated_timestamp_is_found_however_far_apart(self):
        with pytest.raises(IncompatibleMediaError, match="non-monotonic source video timeline"):
            self._scan(b"0\n1\n2\n3\n4\n5\n6\n0\n")

    def test_a_missing_timestamp_is_a_source_defect(self):
        with pytest.raises(IncompatibleMediaError, match="no presentation timestamp"):
            self._scan(b"0\n1\nN/A\n3\n")

    def test_a_stream_too_short_to_split_is_rejected(self):
        with pytest.raises(IncompatibleMediaError, match="too few packets"):
            self._scan(b"0\n")

    def test_blank_lines_and_trailing_separators_are_tolerated(self):
        # ffprobe's csv writer emits a trailing separator for some builds, and the
        # stream ends with a newline. Neither is a defect in the media.
        assert self._scan(b"0,\n1,\n\n2,\n")["packets"] == 3

    def test_a_tool_deadline_does_not_reject_the_film(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="ffprobe", timeout=1)):
            with pytest.raises(MediaOperationError) as error:
                PartMedia().validate_timeline(Path("film.mp4"), self._raw())
        assert error.value.operation == "probe"
        assert not isinstance(error.value, IncompatibleMediaError)


class StopProbeError(Exception):
    """Stops fingerprint() once the argv under test has been captured."""


def test_the_frame_hash_pass_measures_at_the_container_time_base(tmp_path):
    """A nominal-rate grid cannot represent a variable-rate source.

    Rounding frame times onto 60000/1001 collapsed 10,848 pairs of a measured
    feature onto shared ticks and rejected a film whose timeline was intact.
    """
    captured: list[list[str]] = []

    def record(self, args, timeout, **kwargs):
        captured.append(args)
        raise StopProbeError

    with patch.object(PartMedia, "command", record):
        with pytest.raises(StopProbeError):
            PartMedia().fingerprint(tmp_path / "film.mp4", tmp_path, 10.0)

    argv = captured[0]
    assert "framehash" in argv
    assert argv[argv.index("-enc_time_base") + 1] == "-1"
    # The flag must reach the encoder, so it has to precede the output specifier.
    assert argv.index("-enc_time_base") < argv.index("-f")


class TestDecodedTimelineGuard:
    """Equal decoded stamps separate a mislabelled frame from a repeated one.

    FFmpeg's best_effort_timestamp heuristic overrides a container stamp it judges
    implausible for the nominal rate. Measured on one 2.5 hour feature, two frames
    6,333 ticks apart at t=9002.74s were emitted under a single tick, both full
    3840x2160 frames carrying different content hashes: nothing was lost and
    nothing was repeated, yet the guard discarded a 26 GB download after a 6 hour
    39 minute decode. validate_timeline() has already proved every container stamp
    distinct by the time this runs.
    """

    HEADER = b"#format: frame checksums\n#version: 2\n#hash: SHA256\n#tb 0: 1/10000000\n"

    def _payload(self, rows):
        # framehash writes stream, dts, pts, duration, size, hash.
        body = "".join(f"0, {pts}, {pts},   166833, 12441600, {content}\n" for pts, content in rows)
        return self.HEADER + body.encode()

    def _rows(self, count=1200):
        return [(index * 166833, f"{index:064x}") for index in range(count)]

    def _parse(self, rows, tmp_path):
        """Runs fingerprint() far enough to parse the frame hashes and no further."""
        calls: list[list[str]] = []
        payload = self._payload(rows)

        def record(self, args, timeout, **kwargs):
            calls.append(args)
            if len(calls) == 1:
                kwargs["output"].write(payload)
                return b""
            raise StopProbeError

        with patch.object(PartMedia, "command", record):
            PartMedia().fingerprint(tmp_path / "film.mp4", tmp_path, 10.0)

    def test_an_overridden_stamp_with_different_content_is_accepted(self, tmp_path):
        rows = self._rows()
        rows[700] = (rows[699][0], rows[700][1])
        # Reaching the audio pass is what proves the frame hashes were accepted.
        with pytest.raises(StopProbeError):
            self._parse(rows, tmp_path)

    def test_a_genuinely_repeated_frame_is_still_refused(self, tmp_path):
        rows = self._rows()
        rows[700] = rows[699]
        with pytest.raises(IncompatibleMediaError, match="repeated decoded video frame"):
            self._parse(rows, tmp_path)

    def test_a_timeline_running_backwards_is_still_refused(self, tmp_path):
        rows = self._rows()
        rows[700] = (rows[699][0] - 1, rows[700][1])
        with pytest.raises(IncompatibleMediaError, match="non-monotonic decoded video timeline"):
            self._parse(rows, tmp_path)

    def test_a_systematic_collapse_onto_shared_ticks_is_refused(self, tmp_path):
        # The nominal-grid defect put 2.0% of a feature's frames onto shared ticks.
        # Tolerating a labelling artifact must not tolerate that defect returning.
        rows = self._rows()
        for index in range(700, 710):
            rows[index] = (rows[699][0], rows[index][1])
        with pytest.raises(IncompatibleMediaError, match="collapses onto repeated timestamps"):
            self._parse(rows, tmp_path)

    def test_the_measured_feature_tail_is_accepted(self, tmp_path):
        # The final container stamps of the rejected 26 GB download as the decoder
        # emitted them: the frame the container puts at 90027365000 arrives under
        # its neighbour's tick, with its own content hash intact.
        measured = [90027198166, 90027371333, 90027531833, 90027531833, 90027874500]
        rows = self._rows() + [(pts, f"{index:064x}") for index, pts in enumerate(measured, start=9000)]
        with pytest.raises(StopProbeError):
            self._parse(rows, tmp_path)

    def test_the_tolerated_stamp_count_is_recorded_as_evidence(self, tmp_path):
        rows = self._rows()
        rows[700] = (rows[699][0], rows[700][1])
        payload = self._payload(rows)
        probed = json.dumps(
            {
                "streams": [
                    {"codec_type": "video", "codec_name": "hevc", "start_time": "0.000000"},
                    {"codec_type": "audio", "codec_name": "aac", "start_time": "0.000000"},
                ],
                "format": {"duration": "10.0"},
            }
        ).encode()
        calls: list[list[str]] = []

        def record(self, args, timeout, **kwargs):
            calls.append(args)
            if len(calls) == 1:
                kwargs["output"].write(payload)
                return b""
            return probed if len(calls) == 4 else b"stream-digest"

        with patch.object(PartMedia, "command", record):
            result = PartMedia().fingerprint(tmp_path / "film.mp4", tmp_path, 10.0)

        assert result["video_tick_collisions"] == 1
        assert result["video_frames"] == len(rows)
