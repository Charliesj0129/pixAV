"""Bounded FFmpeg stream-copy preparation; no upload or execution framework."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from fractions import Fraction
from pathlib import Path
from typing import Any

from pixav.shared.models import VideoPart

TARGET_BYTES = 9_500_000_000
LIMIT_BYTES = 10_000_000_000
TRANSFER_SECONDS = 6 * 3600


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class IncompatibleMediaError(ValueError):
    """A source cannot meet the lossless segmented-storage contract."""


class MediaOperationError(ValueError):
    """The media tooling failed, which says nothing about the source.

    Deliberately not an :class:`IncompatibleMediaError`. A deadline, a killed
    container or a missing executable is an infrastructure fault, so the correct
    response is to retain the downloaded film and block until a human looks. The
    candidate loop catches only source-level errors, so this propagates instead
    of rejecting a film that may be perfectly good and starting another
    multi-hour download in its place.
    """

    def __init__(self, message: str, *, operation: str = "media", category: str = "tool_failure") -> None:
        super().__init__(message)
        self.operation = operation
        self.category = category


# timeout(1) reports 124 when it fires and 125-127 when it cannot run the command
# at all, which is also how docker reports a daemon or exec failure. 137 and 143 are
# the shell's encoding of SIGKILL and SIGTERM, so an out-of-memory kill lands here
# too. None of these are FFmpeg judging the media.
OPERATION_EXIT_CODES = frozenset({124, 125, 126, 127, 137, 143})


def contained_file(root: Path, path: Path) -> Path:
    absolute = path.absolute()
    if any(p.is_symlink() for p in (absolute, *absolute.parents)):
        raise ValueError("symlink media path rejected")
    absolute.resolve(strict=True).relative_to(root.resolve(strict=True))
    if not absolute.is_file():
        raise ValueError("regular media file required")
    return absolute


def disk_budget(requirements: list[tuple[Path, int]]) -> list[dict]:
    """Aggregate peak allocations sharing a filesystem, preserving both latches."""
    groups: dict[int, dict] = {}
    for path, required in requirements:
        if required < 0:
            raise ValueError("negative disk estimate")
        probe = path.absolute()
        while not probe.exists():
            probe = probe.parent
        if any(p.is_symlink() for p in (probe, *probe.parents)):
            raise ValueError("symlink mount rejected")
        usage = shutil.disk_usage(probe)
        device = probe.stat().st_dev
        group = groups.setdefault(
            device, {"device": device, "paths": [], "required": 0, "free": usage.free, "total": usage.total}
        )
        group["paths"].append(str(path))
        group["required"] += required
    for group in groups.values():
        group["reserve"] = max(100 * 1024**3, math.ceil(group["total"] * 0.10))
        group["ready"] = group["free"] - group["required"] >= group["reserve"]
    return list(groups.values())


def require_space(requirements: list[tuple[Path, int]]) -> None:
    if not all(group["ready"] for group in disk_budget(requirements)):
        raise MediaOperationError("allocation would cross disk latch", operation="disk", category="disk_latch")


@contextmanager
def retained_temporary(*, prefix: str, directory: Path) -> Iterator[Path]:
    """Successful scratch work is released; failures preserve allocated evidence."""
    path = Path(tempfile.mkdtemp(prefix=prefix, dir=directory))
    try:
        yield path
    except BaseException:
        raise
    else:
        shutil.rmtree(path)


def monitored_run(
    command: list[str],
    timeout: float,
    check: Callable[[], None],
    *,
    data: bytes | None = None,
    output: Any = subprocess.PIPE,
) -> subprocess.CompletedProcess:
    """Bound a tools process and stop its owned Docker container on guard failure."""
    check()
    argv = list(command)
    name = None
    if argv[:2] == ["docker", "run"]:
        name = "pixav-media-op-" + uuid.uuid4().hex
        if "--name" in argv:
            argv[argv.index("--name") + 1] = name
        else:
            argv[2:2] = ["--name", name]
    started = time.monotonic()
    with subprocess.Popen(  # noqa: S603 - controlled argv, no shell
        argv,
        stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,
        stdout=output,
        stderr=subprocess.PIPE,
    ) as process:
        try:
            pending = data
            while True:
                check()
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(argv, timeout)
                try:
                    stdout, stderr = process.communicate(pending, timeout=min(1, remaining))
                    check()
                    return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
                except subprocess.TimeoutExpired:
                    pending = None
        except BaseException:
            # Killing the Docker client alone leaves the writer running. Stop only
            # the name assigned to this operation; mounts and files are retained.
            try:
                if name:
                    subprocess.run(  # noqa: S603 - only this operation's random container name
                        ["docker", "stop", "--time", "2", name],  # noqa: S607
                        capture_output=True,
                        timeout=15,
                        check=False,
                    )
            finally:
                process.kill()
                process.communicate()
            raise


class PartMedia:
    def __init__(self, ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe", *, prefix: list[str] | None = None) -> None:
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.prefix = prefix or []
        self.check: Callable[[], None] | None = None

    @staticmethod
    def deadline(duration: float) -> int:
        return max(1800, math.ceil(duration * 4))

    def command(self, args: list[str], timeout: int, *, output: Any = subprocess.PIPE) -> bytes:
        if self.check:
            self.check()
        operation = "probe" if args[0] == self.ffprobe else "ffmpeg"
        try:
            command = self.prefix + (["timeout", "--kill-after=5s", str(timeout)] if self.prefix else []) + args
            if self.check:
                result = monitored_run(command, timeout + (15 if self.prefix else 0), self.check, output=output)
            else:
                result = subprocess.run(  # noqa: S603 - argv only, controlled FFmpeg tools
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.PIPE,
                    timeout=timeout + (15 if self.prefix else 0),
                    check=False,
                )
        except subprocess.TimeoutExpired as exc:
            raise MediaOperationError(
                "media operation deadline exceeded", operation=operation, category="timeout"
            ) from exc
        except OSError as exc:
            raise MediaOperationError(
                "media tool could not start", operation=operation, category="launch_failure"
            ) from exc
        if result.returncode < 0 or result.returncode in OPERATION_EXIT_CODES:
            raise MediaOperationError(
                "media operation failed before the tool could judge the media",
                operation=operation,
                category=(
                    "terminated" if result.returncode < 0 or result.returncode in {137, 143} else "runtime_failure"
                ),
            )
        if result.returncode:
            # Do not expose filenames or tool stderr to public logs.
            raise IncompatibleMediaError("FFmpeg rejected media; no automatic transcoding")
        if self.check:
            self.check()
        return result.stdout or b""

    def probe(self, path: Path) -> dict:
        raw = json.loads(
            self.command(
                [self.ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                120,
            )
        )
        duration = float(raw["format"]["duration"])
        if not math.isfinite(duration) or duration <= 0:
            raise IncompatibleMediaError("invalid media duration")
        return raw

    def validate_source(self, path: Path) -> dict:
        raw = self.probe(path)
        streams = raw["streams"]
        videos = [s for s in streams if s["codec_type"] == "video"]
        audio = [s for s in streams if s["codec_type"] == "audio"]
        if len(videos) != 1 or videos[0].get("width", 0) < 3840 or videos[0].get("height", 0) < 2160:
            raise IncompatibleMediaError("source must contain one video at least 3840x2160")
        if not audio:
            raise IncompatibleMediaError("complete film requires audio")
        if any(s["codec_type"] not in {"video", "audio"} for s in streams):
            raise IncompatibleMediaError("unsupported additional streams; refusing to drop them")
        if videos[0]["codec_name"] not in {"h264", "hevc"}:
            raise IncompatibleMediaError("unsupported video codec for original Photos MP4")
        if any(s["codec_name"] not in {"aac", "ac3", "eac3", "alac"} for s in audio):
            raise IncompatibleMediaError("audio cannot safely be copied to the selected MP4 container")
        return raw

    def validate_timeline(self, path: Path, raw: dict) -> dict:
        """Reject a repeated container timestamp without decoding.

        ``fingerprint()`` refuses a source whose decoded frames do not advance,
        but reaches that verdict only after a full decode: measured here, a 2.5
        hour feature decoded for 6 hours 40 minutes before the parse found the
        defect, and the candidate loop then discarded the whole download. The
        container carries the same timestamps and costs one sequential read.

        Only a genuinely repeated timestamp is a defect. A variable frame rate is
        not: the fingerprint now measures at the container's own time base, so
        irregular intervals are recorded faithfully rather than rounded onto a
        nominal grid. Refusing them here would reject films the decode accepts.

        Packets arrive in decode order, so their timestamps legitimately run
        backwards around B-frames; only presentation order must increase, which
        is why this sorts first and looks for an equal neighbour.
        """
        stamps = self.presentation_stamps(path, float(raw["format"]["duration"]))
        if any(stamps[index] == stamps[index + 1] for index in range(len(stamps) - 1)):
            raise IncompatibleMediaError("non-monotonic source video timeline")
        return {"packets": len(stamps), "first_pts": stamps[0], "last_pts": stamps[-1]}

    def presentation_stamps(self, path: Path, duration: float) -> list[int]:
        """Every video packet timestamp, in presentation order."""
        raw = self.command(
            [
                self.ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "packet=pts",
                "-of",
                "csv=p=0",
                str(path),
            ],
            self.deadline(duration),
        )
        stamps: list[int] = []
        for line in raw.decode().splitlines():
            field = line.strip().rstrip(",")
            if not field:
                continue
            if field == "N/A":
                raise IncompatibleMediaError("source video packet carries no presentation timestamp")
            stamps.append(int(field))
        if len(stamps) < 2:
            raise IncompatibleMediaError("source video has too few packets to segment")
        stamps.sort()
        return stamps

    @staticmethod
    def signature(raw: dict) -> list[dict]:
        fields = (
            "codec_type",
            "codec_name",
            "profile",
            "width",
            "height",
            "pix_fmt",
            "r_frame_rate",
            "sample_rate",
            "channels",
            "channel_layout",
            "color_range",
            "color_space",
            "color_transfer",
            "color_primaries",
            "chroma_location",
            "side_data_list",
        )
        return [{k: s[k] for k in fields if k in s} for s in raw["streams"]]

    def fingerprint(self, path: Path, directory: Path, duration: float) -> dict:
        """Full decode: ordered video frames and PCM audio samples plus timing.

        framehash output is streamed to disk. Per-stream rolling digests bound RAM
        and detect lost/repeated frames. Audio uses streamhash because codec packet
        boundaries need not equal decoder output frame boundaries after remuxing.
        """
        result: dict[str, Any] = {}
        with tempfile.TemporaryFile(dir=directory) as output:
            self.command(
                [
                    self.ffmpeg,
                    "-v",
                    "error",
                    "-xerror",
                    "-i",
                    str(path),
                    "-map",
                    "0:v:0",
                    "-fps_mode",
                    "passthrough",
                    # Without this the framehash muxer writes frame times onto a grid
                    # derived from the nominal frame rate. A variable-rate source then
                    # collides: measured on one 2.5 hour feature, 539,626 natively
                    # distinct timestamps collapsed onto 528,778 ticks and the
                    # monotonic check rejected a film whose timeline was in fact
                    # intact. -1 keeps the input stream's own time base, which records
                    # the real intervals. Per-frame content hashes are unaffected.
                    "-enc_time_base",
                    "-1",
                    "-f",
                    "framehash",
                    "-hash",
                    "sha256",
                    "-",
                ],
                self.deadline(duration),
                output=output,
            )
            output.seek(0)
            digest = hashlib.sha256()
            time_base = Fraction(0)
            first = last = None
            previous = ""
            count = collisions = 0
            for binary in output:
                line = binary.decode().strip()
                if line.startswith("#tb 0:"):
                    time_base = Fraction(line.split(":", 1)[1].strip())
                if not line or line.startswith("#"):
                    continue
                fields = [field.strip() for field in line.split(",")]
                pts = int(fields[2]) * time_base
                first = pts if first is None else first
                # Microsecond-normalized timeline detects discontinuities as well as content changes.
                digest.update(f"{round((pts-first)*1_000_000)},{fields[4]},{fields[5]}\n".encode())
                if last is not None and pts <= last:
                    if pts < last:
                        raise IncompatibleMediaError("non-monotonic decoded video timeline")
                    # An equal stamp is not automatically a defect. FFmpeg's
                    # best_effort_timestamp heuristic overrides a container stamp it
                    # judges implausible for the nominal rate: measured on one 2.5 hour
                    # feature, two frames 6,333 ticks apart at t=9002.74s were emitted
                    # under a single tick, both full 3840x2160 frames carrying different
                    # content hashes. Nothing was lost and nothing was repeated, and
                    # validate_timeline() has already proved every container stamp
                    # distinct. A genuinely repeated frame also carries the previous
                    # content hash, and is still refused below.
                    if fields[5] == previous:
                        raise IncompatibleMediaError("repeated decoded video frame")
                    collisions += 1
                last = pts
                previous = fields[5]
                count += 1
            if count < 2 or not time_base or first is None:
                raise IncompatibleMediaError("empty decoded video")
            # A handful of overridden stamps is a labelling artifact; a systematic
            # collapse is not. The nominal-grid defect that -enc_time_base replaced put
            # 10,848 of 539,626 frames on shared ticks, so a rate approaching that means
            # the timeline is being measured against the wrong base again.
            if collisions * 1000 > count:
                raise IncompatibleMediaError("decoded video timeline collapses onto repeated timestamps")
            result.update(
                video_sha256=digest.hexdigest(),
                video_frames=count,
                video_start=float(first),
                video_tick_collisions=collisions,
            )
        audio = self.command(
            [
                self.ffmpeg,
                "-v",
                "error",
                "-xerror",
                "-i",
                str(path),
                "-map",
                "0:a",
                "-c:a",
                "pcm_s32le",
                "-f",
                "streamhash",
                "-hash",
                "sha256",
                "-",
            ],
            self.deadline(duration),
        )
        result["audio_sha256"] = hashlib.sha256(audio).hexdigest()
        encoded = self.command(
            [
                self.ffmpeg,
                "-v",
                "error",
                "-xerror",
                "-i",
                str(path),
                "-map",
                "0",
                "-c",
                "copy",
                "-f",
                "streamhash",
                "-hash",
                "sha256",
                "-",
            ],
            self.deadline(duration),
        )
        result["encoded_sha256"] = hashlib.sha256(encoded).hexdigest()
        raw = self.probe(path)
        result["av_offsets"] = [
            round(float(s.get("start_time", 0)) - result["video_start"], 6)
            for s in raw["streams"]
            if s["codec_type"] == "audio"
        ]
        result["signature"] = self.signature(raw)
        result["duration"] = float(raw["format"]["duration"])
        return result

    @staticmethod
    def compare(expected: dict, actual: dict) -> None:
        for key in ("video_sha256", "video_frames", "audio_sha256", "encoded_sha256", "signature"):
            if expected[key] != actual[key]:
                raise IncompatibleMediaError(f"lossless merged {key} mismatch")
        if abs(expected["duration"] - actual["duration"]) > 0.05:
            raise IncompatibleMediaError("merged duration mismatch")
        if len(expected["av_offsets"]) != len(actual["av_offsets"]) or any(
            abs(a - b) > 0.002 for a, b in zip(expected["av_offsets"], actual["av_offsets"], strict=True)
        ):
            raise IncompatibleMediaError("merged A/V sync mismatch")

    def merge(self, paths: list[Path], destination: Path, duration: float, *, spans: list[float] | None = None) -> None:
        if destination.exists() or destination.is_symlink():
            raise ValueError("merge output already exists")
        if spans is not None and (len(spans) != len(paths) or any(not math.isfinite(s) or s <= 0 for s in spans)):
            raise ValueError("invalid concat timeline")
        with tempfile.NamedTemporaryFile(mode="w", suffix=".ffconcat", dir=destination.parent) as listing:
            listing.write("ffconcat version 1.0\n")
            for index, path in enumerate(paths):
                # New part names are controlled; escape apostrophes in the root as FFmpeg requires.
                listing.write("file '" + str(path.absolute()).replace("'", "'\\''") + "'\n")
                if spans is not None:
                    listing.write(f"duration {spans[index]:.9f}\n")
            listing.flush()
            self.command(
                [
                    self.ffmpeg,
                    "-v",
                    "error",
                    "-xerror",
                    "-n",
                    "-copyts",
                    "-f",
                    "concat",
                    "-auto_convert",
                    "0",
                    "-safe",
                    "0",
                    "-i",
                    listing.name,
                    "-map",
                    "0",
                    "-c",
                    "copy",
                    "-avoid_negative_ts",
                    "disabled",
                    "-map_metadata",
                    "0",
                    "-movflags",
                    "+faststart",
                    str(destination),
                ],
                self.deadline(duration),
            )

    def prepare(
        self,
        source: Path,
        directory: Path,
        video_id: uuid.UUID,
        *,
        target: int = TARGET_BYTES,
        source_checkpoint: dict | None = None,
        save_source_checkpoint: Callable[[dict], None] | None = None,
    ) -> tuple[list[VideoPart], dict]:
        if not 0 < target < LIMIT_BYTES:
            raise ValueError("invalid part target")
        raw = self.validate_source(source)
        duration = float(raw["format"]["duration"])
        # Source, previous parts and quarantined attempts already reduce free
        # space. Reserve only the NEW split, premerge and streamed frame hashes.
        require_space([(directory, math.ceil(source.stat().st_size * 2.1) + math.ceil(duration * 120 * 256))])
        # Cheapest verdict first: one container read rejects a broken timeline
        # before the two full-file hashes and the three decoding passes below.
        validated = self.source_reference(source, directory, raw, source_checkpoint)
        source_hash, timeline, reference = validated["sha256"], validated["timeline"], validated["reference"]
        if save_source_checkpoint is not None and source_checkpoint is None:
            # The caller persists this in PostgreSQL before any split allocation.
            # A failed persistence operation must stop preparation here.
            save_source_checkpoint(validated)
        # Estimate only seeds the first trial. Actual muxed bytes decide acceptance.
        interval = duration / max(2, math.ceil(source.stat().st_size / target))
        for _attempt in range(16):
            with retained_temporary(prefix="split-", directory=directory) as temporary:
                root = Path(temporary)
                listing = root / "segments.csv"
                self.command(
                    [
                        self.ffmpeg,
                        "-v",
                        "error",
                        "-xerror",
                        "-n",
                        "-i",
                        str(source),
                        "-map",
                        "0",
                        "-c",
                        "copy",
                        "-avoid_negative_ts",
                        "disabled",
                        "-map_metadata",
                        "0",
                        "-f",
                        "segment",
                        "-segment_time",
                        str(interval),
                        "-segment_time_delta",
                        str(
                            float(
                                1
                                / Fraction(
                                    next(s for s in raw["streams"] if s["codec_type"] == "video")["r_frame_rate"]
                                )
                                / 2
                            )
                        ),
                        "-segment_format_options",
                        "avoid_negative_ts=disabled",
                        "-reset_timestamps",
                        "1",
                        "-segment_list",
                        str(listing),
                        "-segment_list_type",
                        "csv",
                        str(root / "part-%06d.mp4"),
                    ],
                    self.deadline(duration),
                )
                with listing.open() as handle:
                    entries = list(csv.reader(handle))
                paths = [root / Path(row[0]).name for row in entries]
                if len(paths) < 2 or any(p.stat().st_size >= LIMIT_BYTES or p.stat().st_size > target for p in paths):
                    interval /= 2
                    continue
                merged = root / "precheck.mp4"
                spans = [float(row[2]) - float(row[1]) for row in entries]
                self.merge(paths, merged, duration, spans=spans)
                self.compare(reference, self.fingerprint(merged, root, duration))
                parts = []
                for index, path in enumerate(paths):
                    # Decode every part independently: an open GOP is not silently accepted.
                    part_info = self.probe(path)
                    self.fingerprint(path, root, float(part_info["format"]["duration"]))
                    digest = sha256(path)
                    filename = f"pixav-{video_id}-part-{index:06d}-{digest[:16]}.mp4"
                    destination = directory / filename
                    if destination.exists():
                        if (
                            contained_file(directory, destination).stat().st_size != path.stat().st_size
                            or sha256(destination) != digest
                        ):
                            raise ValueError("prepared part collision")
                    else:
                        os.link(path, destination)
                    # Segment-list time is a muxer timeline; store contiguous source-relative boundaries.
                    start = 0.0 if index == 0 else float(entries[index][1]) - float(entries[0][1])
                    end = duration if index == len(paths) - 1 else float(entries[index + 1][1]) - float(entries[0][1])
                    parts.append(
                        VideoPart(
                            video_id=video_id,
                            part_index=index,
                            manifest_version=1,
                            start_seconds=start,
                            end_seconds=end,
                            size_bytes=path.stat().st_size,
                            sha256=digest,
                            filename=filename,
                            media_info=part_info,
                        )
                    )
                if sha256(source) != source_hash:
                    raise ValueError("source changed during preparation")
                return parts, {
                    "sha256": source_hash,
                    "size": source.stat().st_size,
                    "media": raw,
                    "reference": reference,
                    "local_merge": "PASS",
                    "timeline": timeline,
                    "target_bytes": target,
                }
        raise IncompatibleMediaError("no compliant independently decodable keyframe segmentation after 16 attempts")

    def source_reference(self, source: Path, directory: Path, raw: dict, checkpoint: dict | None) -> dict:
        """Reuse only a DB-backed reference tied to the complete source hash.

        Split/merge/independent-part checks still run on every preparation.
        Runtime image identity is separately pinned by the single-film CLI.
        """
        digest = sha256(source)
        identity: dict[str, Any] = {
            "version": 1,
            "sha256": digest,
            "size": source.stat().st_size,
            "signature": self.signature(raw),
            "duration": float(raw["format"]["duration"]),
        }
        if checkpoint is not None:
            if (
                any(checkpoint.get(key) != value for key, value in identity.items())
                or not isinstance(checkpoint.get("timeline"), dict)
                or not isinstance(checkpoint.get("reference"), dict)
                or not {
                    "video_sha256",
                    "video_frames",
                    "audio_sha256",
                    "encoded_sha256",
                    "signature",
                    "duration",
                    "av_offsets",
                }
                <= checkpoint["reference"].keys()
            ):
                raise MediaOperationError("source validation checkpoint mismatch", category="checkpoint_mismatch")
            return checkpoint
        timeline = self.validate_timeline(source, raw)
        reference = self.fingerprint(source, directory, identity["duration"])
        if sha256(source) != digest:
            raise MediaOperationError("source changed during validation", category="checkpoint_mismatch")
        return {**identity, "timeline": timeline, "reference": reference}
