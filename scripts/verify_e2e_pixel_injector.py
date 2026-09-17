"""Manual Phase 0 Google Photos experiment on an ephemeral Pixel XL Redroid.

This is deliberately an operator-assisted experiment, not unattended UI
automation. Google login, 2FA/CAPTCHA, Photos installation, backup settings and
share creation are completed through scrcpy. Credentials and the share token
never enter argv, the database, Redis, or the evidence report.

Run from the host after building the pixel-injector image:

  docker compose run --rm --no-deps pixel_injector \
    uv run --no-sync python scripts/verify_e2e_pixel_injector.py \
    --fixture data/downloads/phase0-synthetic-2gb.mp4

The script prints the dynamic ADB endpoint for Windows scrcpy, then waits at
explicit operator checkpoints. Evidence is written below ``data/phase0`` with
private permissions and is ignored by git.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import json
import logging
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from pixav.config import get_settings
from pixav.pixel_injector.adb import AdbConnection
from pixav.pixel_injector.redroid import DockerRedroidManager
from pixav.pixel_injector.uploader import media_provider_scan_command
from pixav.pixel_injector.verifier import extract_share_url
from pixav.strm_resolver.resolver import GooglePhotosResolver

logger = logging.getLogger("verify_e2e_pixel_injector")

_PHOTOS_PACKAGE = "com.google.android.apps.photos"
_PLAY_STORE_URI = f"market://details?id={_PHOTOS_PACKAGE}"
_REMOTE_FIXTURE = "/sdcard/DCIM/Camera/pixav-phase0-fixture.mp4"
_ALLOWED_GOOGLE_MEDIA_HOSTS = frozenset(
    {
        "lh3.googleusercontent.com",
        "video-downloads.googleusercontent.com",
    }
)
_GOOGLE_VIDEO_HOST_SUFFIX = ".googlevideo.com"
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _normalize_share_url(raw: str) -> str:
    value = raw.strip()
    extracted = extract_share_url(value)
    if extracted is None or extracted != value:
        raise RuntimeError("expected one complete https://photos.app.goo.gl/... URL")
    return extracted


def _quota_unchanged(before: str, after: str) -> bool:
    return bool(before.strip()) and before.strip() == after.strip()


def _extract_package_evidence(dumpsys: str) -> dict[str, str | None]:
    def _value(pattern: str) -> str | None:
        match = re.search(pattern, dumpsys, flags=re.MULTILINE)
        return match.group(1).strip() if match else None

    return {
        "version_name": _value(r"^\s*versionName=(\S+)"),
        "version_code": _value(r"^\s*versionCode=(\d+)"),
        "installer": _value(r"^\s*installerPackageName=(\S+)"),
    }


def _private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    _set_private_ownership(path, mode=0o700)
    return path


def _set_private_ownership(path: Path, *, mode: int) -> None:
    """Keep evidence private but readable by the host operator after bind writes."""
    path.chmod(mode)
    uid = os.environ.get("PIXAV_EVIDENCE_UID")
    gid = os.environ.get("PIXAV_EVIDENCE_GID")
    if uid is None and gid is None:
        return
    if uid is None or gid is None or not uid.isdecimal() or not gid.isdecimal():
        raise RuntimeError("PIXAV_EVIDENCE_UID and PIXAV_EVIDENCE_GID must both be non-negative integers")
    os.chown(path, int(uid), int(gid))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_private_json(path: Path, payload: dict[str, Any]) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise RuntimeError(f"refusing unsafe JSON evidence target: {path.name}")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        _set_private_ownership(temporary, mode=0o600)
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _validate_screenshot(path: Path) -> dict[str, int]:
    if not path.is_file() or path.stat().st_size < 24:
        raise RuntimeError("resume evidence requires a non-empty photos-after-share.png")
    with path.open("rb") as handle:
        header = handle.read(24)
    if not header.startswith(_PNG_SIGNATURE) or header[12:16] != b"IHDR":
        raise RuntimeError("resume screenshot is not a valid PNG header")
    return {
        "size_bytes": path.stat().st_size,
        "width": int.from_bytes(header[16:20], "big"),
        "height": int.from_bytes(header[20:24], "big"),
    }


def _resume_evidence_dir(evidence_root: Path, requested: str) -> Path:
    root = evidence_root.expanduser().resolve(strict=True)
    candidate = Path(requested).expanduser().resolve(strict=True)
    if not candidate.is_dir() or candidate.parent != root:
        raise RuntimeError("--resume-after-share must be an immediate child of --evidence-dir")
    return candidate


def _validated_media_url(raw_url: str | httpx.URL) -> httpx.URL:
    try:
        url = raw_url if isinstance(raw_url, httpx.URL) else httpx.URL(raw_url)
    except (TypeError, ValueError):
        raise RuntimeError("resolver returned an invalid media URL (token redacted)") from None
    host = url.host or ""
    if (
        url.scheme != "https"
        or (host not in _ALLOWED_GOOGLE_MEDIA_HOSTS and not host.endswith(_GOOGLE_VIDEO_HOST_SUFFIX))
        or url.username
        or url.password
        or url.port not in (None, 443)
    ):
        raise RuntimeError("resolver or redirect returned a non-allowlisted media URL (token redacted)")
    return url


async def _probe_cdn_range(
    client: httpx.AsyncClient,
    cdn_url: str,
    *,
    expected_prefix: bytes,
    source_size: int | None,
    max_redirects: int = 6,
    allow_non_206: bool = False,
    require_prefix_match: bool = True,
) -> tuple[str, dict[str, Any]]:
    """Follow only allowlisted redirects and read at most the requested bytes."""
    current = _validated_media_url(cdn_url)
    hops: list[dict[str, Any]] = []
    headers = {
        "Accept-Encoding": "identity",
        "Range": f"bytes=0-{len(expected_prefix) - 1}",
    }

    for hop_index in range(max_redirects + 1):
        host = current.host or "unknown"
        try:
            async with client.stream("GET", current, headers=headers) as response:
                hop = {
                    "hop": hop_index,
                    "host": host,
                    "status": response.status_code,
                    "accept_ranges": response.headers.get("accept-ranges"),
                    "content_length": response.headers.get("content-length"),
                    "content_type": response.headers.get("content-type"),
                    "content_range_present": "content-range" in response.headers,
                }
                hops.append(hop)

                if response.status_code in _REDIRECT_STATUSES:
                    if hop_index == max_redirects:
                        raise RuntimeError("CDN range redirect limit exceeded (URLs redacted)")
                    location = response.headers.get("location")
                    if not location:
                        raise RuntimeError("CDN redirect omitted Location (URLs redacted)")
                    current = _validated_media_url(response.url.join(location))
                    continue

                if response.status_code != 206:
                    # Do not iterate the body: some download endpoints answer a
                    # Range request with the complete multi-gigabyte object.
                    if allow_non_206:
                        return str(current), {
                            "range_status": response.status_code,
                            "range_supported": False,
                            "body_read": False,
                            "redirect_hops": hops,
                        }
                    raise RuntimeError(
                        f"CDN range request returned {response.status_code}, expected 206 "
                        f"at allowlisted host {host}; body was not read"
                    )
                content_range = response.headers.get("content-range")
                range_match = re.fullmatch(
                    rf"bytes 0-{len(expected_prefix) - 1}/(\d+)",
                    content_range or "",
                )
                if range_match is None:
                    raise RuntimeError("unexpected CDN Content-Range " f"{content_range!r}; URL redacted")
                total_size = int(range_match.group(1))
                if source_size is not None and total_size != source_size:
                    raise RuntimeError(f"CDN range total differs: expected {source_size}, got {total_size}")

                ranged = bytearray()
                async for chunk in response.aiter_raw():
                    if len(ranged) + len(chunk) > len(expected_prefix):
                        raise RuntimeError("CDN range response exceeded the requested byte count")
                    ranged.extend(chunk)
                prefix_matches = bytes(ranged) == expected_prefix
                if require_prefix_match and not prefix_matches:
                    raise RuntimeError("CDN range bytes differ from the uploaded fixture")
                return str(current), {
                    "range_status": 206,
                    "range_supported": True,
                    "range_bytes_received": len(ranged),
                    "source_prefix_match": prefix_matches,
                    "range_bytes_match": prefix_matches,
                    "content_range": content_range,
                    "total_size_bytes": total_size,
                    "redirect_hops": hops,
                }
        except httpx.HTTPError as exc:
            raise RuntimeError(f"CDN range request failed ({type(exc).__name__}; URL redacted)") from None

    raise RuntimeError("CDN range probe ended without a terminal response")  # pragma: no cover


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def _stream_download_to_part(
    client: httpx.AsyncClient,
    start_url: str,
    *,
    part: Path,
    expected_size: int,
    max_redirects: int = 6,
) -> tuple[int, str, list[dict[str, Any]]]:
    """Stream an allowlisted object to a new private file with a hard size cap."""
    current = _validated_media_url(start_url)
    digest = hashlib.sha256()
    downloaded_size = 0
    hops: list[dict[str, Any]] = []
    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as output:
            for hop_index in range(max_redirects + 1):
                host = current.host or "unknown"
                try:
                    async with client.stream(
                        "GET",
                        current,
                        headers={"Accept-Encoding": "identity"},
                    ) as response:
                        hops.append(
                            {
                                "hop": hop_index,
                                "host": host,
                                "status": response.status_code,
                                "content_length": response.headers.get("content-length"),
                                "content_type": response.headers.get("content-type"),
                            }
                        )
                        if response.status_code in _REDIRECT_STATUSES:
                            if hop_index == max_redirects:
                                raise RuntimeError("CDN download redirect limit exceeded (URLs redacted)")
                            location = response.headers.get("location")
                            if not location:
                                raise RuntimeError("CDN download redirect omitted Location (URLs redacted)")
                            current = _validated_media_url(response.url.join(location))
                            continue
                        if response.status_code != 200:
                            raise RuntimeError(f"CDN download returned {response.status_code}, expected 200 at {host}")
                        content_length = _optional_int(response.headers.get("content-length"))
                        if content_length is not None and content_length != expected_size:
                            raise RuntimeError("CDN download Content-Length differs from its range total")
                        async for chunk in response.aiter_raw():
                            if downloaded_size + len(chunk) > expected_size:
                                raise RuntimeError("CDN download exceeded its range-advertised total size")
                            output.write(chunk)
                            digest.update(chunk)
                            downloaded_size += len(chunk)
                        break
                except httpx.HTTPError as exc:
                    raise RuntimeError(f"CDN download failed ({type(exc).__name__}; URL redacted)") from None
            else:  # pragma: no cover - loop always returns or raises at its bound
                raise RuntimeError("CDN download ended without a terminal response")
    except Exception:
        part.unlink(missing_ok=True)
        raise
    return downloaded_size, digest.hexdigest(), hops


def _normalize_ffprobe(raw: dict[str, Any], *, fallback_size: int) -> dict[str, Any]:
    raw_streams = raw.get("streams")
    streams: list[Any] = raw_streams if isinstance(raw_streams, list) else []
    raw_format = raw.get("format")
    media_format: dict[str, Any] = raw_format if isinstance(raw_format, dict) else {}
    video: dict[str, Any] = next(
        (item for item in streams if isinstance(item, dict) and item.get("codec_type") == "video"),
        {},
    )
    audio: dict[str, Any] = next(
        (item for item in streams if isinstance(item, dict) and item.get("codec_type") == "audio"),
        {},
    )
    width = _optional_int(video.get("width"))
    height = _optional_int(video.get("height"))
    return {
        "size_bytes": _optional_int(media_format.get("size")) or fallback_size,
        "container": media_format.get("format_name"),
        "duration_seconds": _optional_float(media_format.get("duration")),
        "overall_bitrate_bps": _optional_int(media_format.get("bit_rate")),
        "video_codec": video.get("codec_name"),
        "video_bitrate_bps": _optional_int(video.get("bit_rate")),
        "width": width,
        "height": height,
        "resolution": f"{width}x{height}" if width and height else None,
        "audio_codec": audio.get("codec_name"),
        "audio_bitrate_bps": _optional_int(audio.get("bit_rate")),
    }


async def _probe_media_contract(path: Path) -> dict[str, Any]:
    try:
        process = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=format_name,size,duration,bit_rate:stream=codec_type,codec_name,width,height,bit_rate",
            "-of",
            "json",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout=120)
    except FileNotFoundError:
        raise RuntimeError("ffprobe is required for the download-back media contract") from None
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise RuntimeError("ffprobe timed out during the download-back media contract") from None
    if process.returncode != 0:
        raise RuntimeError("ffprobe rejected a download-back media file")
    try:
        raw = json.loads(stdout)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
        raise RuntimeError("ffprobe returned invalid JSON") from None
    if not isinstance(raw, dict):
        raise RuntimeError("ffprobe returned an invalid object")
    result = _normalize_ffprobe(raw, fallback_size=path.stat().st_size)
    required = ("video_codec", "overall_bitrate_bps", "width", "height", "resolution")
    if any(result.get(key) is None for key in required):
        raise RuntimeError("ffprobe omitted required codec, bitrate, or resolution evidence")
    return result


async def _capture_screenshot(adb: AdbConnection, destination: Path) -> None:
    remote = "/data/local/tmp/pixav-phase0-photos.png"
    await adb.shell(f"screencap -p {remote}")
    await adb.pull(remote, str(destination))
    await adb.shell(f"rm -f {remote}")
    _set_private_ownership(destination, mode=0o600)


async def _push_and_register_fixture(
    adb: AdbConnection,
    fixture: Path,
    *,
    adb_timeout: int,
    media_scan_timeout: int,
) -> None:
    """Transfer the exact fixture and wait for Android media registration."""
    await adb.push(str(fixture), _REMOTE_FIXTURE, timeout=adb_timeout)
    remote_size = (await adb.shell(f"stat -c %s {_REMOTE_FIXTURE}")).strip()
    if remote_size != str(fixture.stat().st_size):
        raise RuntimeError(f"ADB fixture size differs: expected {fixture.stat().st_size}, got {remote_size or 'empty'}")
    logger.info(
        "registering the %d-byte fixture with Android MediaProvider (timeout=%ds)",
        fixture.stat().st_size,
        media_scan_timeout,
    )
    scan_result = await adb.shell(
        media_provider_scan_command(_REMOTE_FIXTURE),
        timeout=media_scan_timeout,
    )
    if "Result: Bundle" not in scan_result:
        raise RuntimeError("Android MediaProvider scan_file returned no result bundle")
    rows = await adb.shell(
        "content query --uri content://media/external/video/media " "--projection _id:_display_name:_size"
    )
    filename = Path(_REMOTE_FIXTURE).name
    if filename not in rows or str(fixture.stat().st_size) not in rows:
        raise RuntimeError(f"Android MediaProvider did not register the exact fixture; rows={rows[:500]!r}")
    await adb.shell(
        "content call --uri content://media --method wait_for_idle",
        timeout=media_scan_timeout,
    )


async def _download_back(
    cdn_url: str,
    *,
    source_path: Path,
    destination: Path,
) -> dict[str, Any]:
    """Compare complete download bytes; report Range independently."""
    if not cdn_url.endswith("=dv"):
        raise RuntimeError("original verification requires the dv download, not a rendition")
    source_size = source_path.stat().st_size
    with source_path.open("rb") as source:
        expected_prefix = source.read(min(1024, source_size))

    timeout = httpx.Timeout(connect=30, read=600, write=30, pool=30)
    part = destination.with_suffix(destination.suffix + ".part")
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            _direct_final_url, direct_range = await _probe_cdn_range(
                client,
                cdn_url,
                expected_prefix=expected_prefix,
                source_size=source_size,
                allow_non_206=True,
            )
            # Range is an independent playback capability. HTTP 200 can still
            # deliver the complete original; never substitute a 720p rendition.
            if not direct_range["range_supported"] and direct_range["range_status"] != 200:
                raise RuntimeError("documented dv endpoint returned an unexpected status")
            selected_start_url = cdn_url
            selected_range = direct_range
            selected_size = source_size
            selected_modifier = "dv"
            selected_contract = "Google Photos video download; original only if full hash matches"

            # Resolve a fresh redirect without a Range header for the full
            # object. The signed URL obtained by the probe can itself be bound
            # to bytes=0-1023, so it is not reused here.
            downloaded_size, downloaded_sha256, download_hops = await _stream_download_to_part(
                client,
                selected_start_url,
                part=part,
                expected_size=selected_size,
            )
    except Exception:
        part.unlink(missing_ok=True)
        raise

    source_sha256 = await asyncio.to_thread(_sha256, source_path)
    if downloaded_size != selected_size:
        part.unlink(missing_ok=True)
        raise RuntimeError(f"download-back size differs: expected {selected_size}, got {downloaded_size}")

    try:
        source_media = await _probe_media_contract(source_path)
        downloaded_media = await _probe_media_contract(part)
    except Exception:
        part.unlink(missing_ok=True)
        raise
    comparison = {
        key: source_media.get(key) == downloaded_media.get(key)
        for key in (
            "size_bytes",
            "container",
            "video_codec",
            "overall_bitrate_bps",
            "video_bitrate_bps",
            "resolution",
            "audio_codec",
            "audio_bitrate_bps",
        )
    }
    sha256_matches = downloaded_sha256 == source_sha256
    if selected_modifier == "dv" and (not sha256_matches or not all(comparison.values())):
        part.unlink(missing_ok=True)
        raise RuntimeError("dv download differs from the uploaded fixture")

    os.replace(part, destination)
    _set_private_ownership(destination, mode=0o600)
    return {
        "verdict": ("PASS_DIRECT_DV_RANGE" if direct_range["range_supported"] else "PASS_DIRECT_DV_ORIGINAL_NO_RANGE"),
        "direct_dv_range": direct_range,
        "selected_download": {
            "modifier": selected_modifier,
            "contract": selected_contract,
            "production_contract": selected_modifier == "dv",
            "range": selected_range,
            "download_hops": download_hops,
        },
        "source_size_bytes": source_size,
        "downloaded_size_bytes": downloaded_size,
        "sha256_match": sha256_matches,
        "source_sha256": source_sha256,
        "downloaded_sha256": downloaded_sha256,
        "media_match": comparison,
        "source_media": source_media,
        "downloaded_media": downloaded_media,
    }


async def _resume_after_share(
    args: argparse.Namespace,
    *,
    fixture: Path,
    evidence_root: Path,
) -> None:
    """Finish a previously interrupted run without recreating a logged-in device."""
    evidence_dir = _resume_evidence_dir(evidence_root, args.resume_after_share)
    screenshot_path = evidence_dir / "photos-after-share.png"
    screenshot = _validate_screenshot(screenshot_path)
    download_path = evidence_dir / "download-back.mp4"
    report_path = evidence_dir / "report.json"
    checkpoint_path = evidence_dir / "checkpoint.json"
    part_path = download_path.with_suffix(download_path.suffix + ".part")

    if download_path.exists() or report_path.exists():
        raise RuntimeError("resume refuses to overwrite existing download-back.mp4 or report.json")
    if part_path.exists():
        if not part_path.is_file() or part_path.is_symlink():
            raise RuntimeError("resume found an unsafe download-back.mp4.part entry")
        part_path.unlink()
        logger.warning("removed the exact incomplete download-back.mp4.part from the resumed run")

    quota_before = input("Re-enter the exact Photos storage usage observed before upload: ").strip()
    quota_after = input("Re-enter the exact Photos storage usage observed after upload: ").strip()
    if not _quota_unchanged(quota_before, quota_after):
        raise RuntimeError("Photos quota changed (or an observation was empty); entitlement gate failed")
    share_url = _normalize_share_url(getpass.getpass("Paste the existing Google Photos share URL (input hidden): "))

    settings = get_settings()
    checkpoint = {
        "status": "READY_TO_RESUME",
        "profile": settings.redroid_profile,
        "resumed_after_share": True,
        "share_url_stored": False,
        "quota_before": quota_before,
        "quota_after": quota_after,
        "quota_unchanged": True,
        "screenshot": screenshot_path.name,
        "screenshot_evidence": screenshot,
    }
    _write_private_json(checkpoint_path, checkpoint)

    resolver = GooglePhotosResolver(timeout=60, concurrency=1)
    try:
        cdn_url = await resolver.resolve(share_url)
        download_result = await _download_back(
            cdn_url,
            source_path=fixture,
            destination=download_path,
        )
    finally:
        await resolver.close()

    report = {
        "status": "COMPLETE",
        "conclusion": download_result["verdict"],
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "profile": settings.redroid_profile,
        "resumed_after_share": True,
        "redroid_container_id_prefix": (args.redroid_container_id_prefix or "unavailable_after_cleanup"),
        "photos_package": {
            "verified_before_upload": True,
            "details_recovered_after_cleanup": False,
        },
        "quota_before": quota_before,
        "quota_after": quota_after,
        "quota_unchanged": True,
        "share_url_present": True,
        "share_url_redacted": True,
        "cdn_url_redacted": True,
        "download_back": download_result,
        "screenshot": screenshot_path.name,
        "screenshot_evidence": screenshot,
        "downloaded_file": download_path.name,
    }
    _write_private_json(report_path, report)
    logger.info("Google Photos resumed experiment COMPLETE; private evidence: %s", evidence_dir)


async def _main(args: argparse.Namespace) -> None:  # noqa: C901
    fixture = Path(args.fixture).expanduser().resolve(strict=True)
    if not fixture.is_file() or fixture.stat().st_size <= 0:
        raise RuntimeError("--fixture must be a non-empty regular file")

    evidence_root = Path(args.evidence_dir).expanduser().resolve()
    if args.resume_after_share:
        await _resume_after_share(args, fixture=fixture, evidence_root=evidence_root)
        return

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    evidence_dir = evidence_root / stamp
    if not args.readiness_only and not args.media_registration_only:
        _private_dir(evidence_dir)
    screenshot_path = evidence_dir / "photos-after-share.png"
    download_path = evidence_dir / "download-back.mp4"
    report_path = evidence_dir / "report.json"

    settings = get_settings()
    manager = DockerRedroidManager.from_profile_name(
        settings.redroid_profile,
        profiles_path=settings.redroid_profiles_path or None,
        adb_host=settings.redroid_adb_host,
        adb_port_start=settings.redroid_adb_port_start,
        network=settings.redroid_network or None,
    )
    adb = AdbConnection(timeout=args.adb_timeout)
    resolver = GooglePhotosResolver(timeout=60, concurrency=1)
    session = None

    try:
        session = await manager.create(f"photos-{uuid.uuid4().hex[:8]}")
        if not await manager.wait_ready(session.container_id, timeout=args.boot_timeout):
            raise RuntimeError("Redroid did not pass the named-profile readiness checks")
        await adb.connect(session.adb_host, session.adb_port)

        if args.readiness_only:
            features = await adb.shell("pm list features")
            identity = {
                "status": "PASS",
                "profile": settings.redroid_profile,
                "model": (await adb.shell("getprop ro.product.model")).strip(),
                "device": (await adb.shell("getprop ro.product.device")).strip(),
                "fingerprint": (await adb.shell("getprop ro.build.fingerprint")).strip(),
                "sdk": (await adb.shell("getprop ro.build.version.sdk")).strip(),
                "pixel_2016_feature": "com.google.android.feature.PIXEL_2016_EXPERIENCE" in features,
            }
            print(json.dumps(identity, indent=2, sort_keys=True))
            return

        if args.media_registration_only:
            await _push_and_register_fixture(
                adb,
                fixture,
                adb_timeout=args.adb_timeout,
                media_scan_timeout=args.media_scan_timeout,
            )
            print(
                json.dumps(
                    {
                        "status": "PASS",
                        "fixture_size_bytes": fixture.stat().st_size,
                        "media_registered": True,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return

        await adb.shell(f"am start -a android.intent.action.VIEW -d '{_PLAY_STORE_URI}'")
        print(
            "\nManual checkpoint:\n"
            f"  1. On Windows run: adb connect 127.0.0.1:{session.adb_port}\n"
            f"  2. Then run:       scrcpy --serial 127.0.0.1:{session.adb_port}\n"
            "  3. In Play Store install Google Photos, sign into the dedicated test account,\n"
            "     handle 2FA/CAPTCHA, enable Original quality backup, and note the displayed\n"
            "     storage usage before this upload. Do not paste credentials here.\n"
        )
        input("Press Enter only after Photos is installed, signed in, and quota-before is recorded: ")

        package_path = await adb.shell(f"pm path {_PHOTOS_PACKAGE}")
        if not package_path.startswith("package:"):
            raise RuntimeError("Google Photos package is not installed")
        package = _extract_package_evidence(await adb.shell(f"dumpsys package {_PHOTOS_PACKAGE}"))
        quota_before = input("Enter the exact Photos storage-usage text observed before upload: ").strip()
        if not quota_before:
            raise RuntimeError("quota-before evidence cannot be empty")

        await _push_and_register_fixture(
            adb,
            fixture,
            adb_timeout=args.adb_timeout,
            media_scan_timeout=args.media_scan_timeout,
        )
        print(
            "\nIn scrcpy, return to Google Photos, wait for backup to finish, and create a public\n"
            "share link for only this fixture. Then revisit Photos storage usage and record\n"
            "the exact displayed value.\n"
        )
        quota_after = input("Enter the exact Photos storage-usage text observed after upload: ").strip()
        if not _quota_unchanged(quota_before, quota_after):
            raise RuntimeError("Photos quota changed (or quota-after was empty); Pixel entitlement gate failed")
        share_url = _normalize_share_url(getpass.getpass("Paste the Google Photos share URL (input hidden): "))

        await _capture_screenshot(adb, screenshot_path)
        _write_private_json(
            evidence_dir / "checkpoint.json",
            {
                "status": "READY_TO_RESUME",
                "profile": settings.redroid_profile,
                "redroid_container_id_prefix": session.container_id[:12],
                "photos_package": package,
                "quota_before": quota_before,
                "quota_after": quota_after,
                "quota_unchanged": True,
                "share_url_stored": False,
                "screenshot": screenshot_path.name,
                "screenshot_evidence": _validate_screenshot(screenshot_path),
            },
        )
        cdn_url = await resolver.resolve(share_url)
        download_result = await _download_back(
            cdn_url,
            source_path=fixture,
            destination=download_path,
        )

        report = {
            "status": "COMPLETE",
            "conclusion": download_result["verdict"],
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "profile": settings.redroid_profile,
            "redroid_container_id_prefix": session.container_id[:12],
            "photos_package": package,
            "quota_before": quota_before,
            "quota_after": quota_after,
            "quota_unchanged": True,
            "share_url_present": True,
            "share_url_redacted": True,
            "cdn_url_redacted": True,
            "download_back": download_result,
            "screenshot": screenshot_path.name,
            "downloaded_file": download_path.name,
        }
        _write_private_json(report_path, report)
        logger.info("Google Photos experiment COMPLETE; private evidence: %s", evidence_dir)
    finally:
        # Cleanup failures must be visible, especially for a credentialed
        # container, but they must not replace the ADB/Google failure that
        # caused cleanup in the first place.
        primary_error = sys.exc_info()[1]
        cleanup_errors: list[Exception] = []
        try:
            await resolver.close()
        except Exception as exc:  # pragma: no cover - defensive external cleanup
            cleanup_errors.append(exc)
            logger.exception("failed to close Google Photos resolver")
        if session is not None:
            if args.keep_container:
                logger.warning(
                    "keeping credentialed Redroid container %s by explicit request; remove it manually when finished",
                    session.container_id[:12],
                )
            else:
                try:
                    await manager.destroy(session.container_id)
                except Exception as exc:
                    cleanup_errors.append(exc)
                    logger.exception(
                        "failed to destroy credentialed Redroid container %s; remove it manually",
                        session.container_id[:12],
                    )
        if cleanup_errors and primary_error is None:
            raise cleanup_errors[0]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", required=True, help="real non-empty media file visible inside this container")
    parser.add_argument(
        "--evidence-dir",
        default="data/phase0/google-photos",
        help="gitignored private evidence root",
    )
    parser.add_argument("--boot-timeout", type=int, default=180)
    parser.add_argument(
        "--adb-timeout",
        type=int,
        default=900,
        help="per-command ADB transfer timeout in seconds (default: 900)",
    )
    parser.add_argument(
        "--media-scan-timeout",
        type=int,
        default=900,
        help="timeout for Android to register the multi-gigabyte fixture (default: 900)",
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--readiness-only",
        action="store_true",
        help="verify post-restart Redroid boot, ADB and Pixel identity, then destroy it without opening Google UI",
    )
    modes.add_argument(
        "--media-registration-only",
        action="store_true",
        help="also transfer and register the fixture without opening Google UI, then destroy the container",
    )
    modes.add_argument(
        "--resume-after-share",
        metavar="RUN_DIR",
        help="reuse a private run directory containing photos-after-share.png and skip Redroid/login/upload",
    )
    parser.add_argument(
        "--redroid-container-id-prefix",
        help="optional 12-hex-character container prefix recovered from the interrupted run",
    )
    parser.add_argument(
        "--keep-container",
        action="store_true",
        help="leave the credentialed Redroid running for debugging (default destroys it)",
    )
    args = parser.parse_args()
    if args.boot_timeout <= 0:
        parser.error("--boot-timeout must be positive")
    if args.adb_timeout <= 0:
        parser.error("--adb-timeout must be positive")
    if args.media_scan_timeout <= 0:
        parser.error("--media-scan-timeout must be positive")
    if args.redroid_container_id_prefix and not re.fullmatch(r"[0-9a-f]{12}", args.redroid_container_id_prefix):
        parser.error("--redroid-container-id-prefix must contain exactly 12 lowercase hex characters")
    if args.redroid_container_id_prefix and not args.resume_after_share:
        parser.error("--redroid-container-id-prefix is only valid with --resume-after-share")
    if args.keep_container and args.resume_after_share:
        parser.error("--keep-container is not meaningful with --resume-after-share")
    return args


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    asyncio.run(_main(_parse_args()))
