"""Safety-focused tests for the operator-assisted Google Photos experiment."""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock

import httpx
import pytest

from scripts import verify_e2e_pixel_injector as experiment


def test_normalize_share_url_accepts_exact_short_link() -> None:
    assert experiment._normalize_share_url(" https://photos.app.goo.gl/Ab-Cd_12 \n") == (
        "https://photos.app.goo.gl/Ab-Cd_12"
    )


@pytest.mark.parametrize(
    "raw",
    [
        "https://photos.app.goo.gl/abc extra",
        "https://example.test/not-photos",
        "",
    ],
)
def test_normalize_share_url_rejects_ambiguous_or_wrong_input(raw: str) -> None:
    with pytest.raises(RuntimeError, match="complete"):
        experiment._normalize_share_url(raw)


def test_quota_requires_same_nonempty_observation() -> None:
    assert experiment._quota_unchanged("15.0 GB of 15 GB", "15.0 GB of 15 GB") is True
    assert experiment._quota_unchanged("", "") is False
    assert experiment._quota_unchanged("14.9 GB", "15.0 GB") is False


def test_extract_package_evidence_is_sanitized() -> None:
    raw = """
      versionCode=700123 minSdk=23 targetSdk=35
      versionName=7.00.1
      installerPackageName=com.android.vending
      signatures=secret-material-that-must-not-be-copied
    """
    assert experiment._extract_package_evidence(raw) == {
        "version_name": "7.00.1",
        "version_code": "700123",
        "installer": "com.android.vending",
    }


async def test_push_and_register_verifies_remote_size(tmp_path) -> None:
    fixture = tmp_path / "fixture.mp4"
    fixture.write_bytes(b"phase-zero-media")
    adb = AsyncMock()
    adb.shell.side_effect = [
        str(fixture.stat().st_size),
        "Result: Bundle[{android.intent.extra.STREAM=content://media/external/video/media/1}]",
        f"Row: 0 _display_name=pixav-phase0-fixture.mp4, _size={fixture.stat().st_size}",
        "Result: Bundle[EMPTY_PARCEL]",
    ]

    await experiment._push_and_register_fixture(
        adb,
        fixture,
        adb_timeout=600,
        media_scan_timeout=900,
    )

    adb.push.assert_awaited_once_with(
        str(fixture),
        experiment._REMOTE_FIXTURE,
        timeout=600,
    )
    assert adb.shell.await_args_list[1].kwargs == {"timeout": 900}
    assert "--method scan_file" in adb.shell.await_args_list[1].args[0]
    assert "/storage/emulated/0/DCIM/Camera/" in adb.shell.await_args_list[1].args[0]


async def test_push_and_register_rejects_truncated_remote_file(tmp_path) -> None:
    fixture = tmp_path / "fixture.mp4"
    fixture.write_bytes(b"phase-zero-media")
    adb = AsyncMock()
    adb.shell.return_value = "1"

    with pytest.raises(RuntimeError, match="size differs"):
        await experiment._push_and_register_fixture(
            adb,
            fixture,
            adb_timeout=600,
            media_scan_timeout=900,
        )

    assert adb.shell.await_count == 1


class _StaticAsyncStream(httpx.AsyncByteStream):
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.payload


async def test_range_probe_repeats_range_across_allowlisted_redirect() -> None:
    prefix = b"phase-zero"
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "lh3.googleusercontent.com":
            return httpx.Response(
                302,
                headers={"location": "https://video-downloads.googleusercontent.com/object?token=synthetic"},
            )
        return httpx.Response(
            206,
            headers={
                "content-range": f"bytes 0-{len(prefix) - 1}/999",
                "content-length": str(len(prefix)),
            },
            stream=_StaticAsyncStream(prefix),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        final_url, result = await experiment._probe_cdn_range(
            client,
            "https://lh3.googleusercontent.com/media=dv",
            expected_prefix=prefix,
            source_size=999,
        )

    assert final_url.startswith("https://video-downloads.googleusercontent.com/")
    assert [request.headers["range"] for request in requests] == ["bytes=0-9", "bytes=0-9"]
    assert result["range_status"] == 206
    assert result["range_bytes_match"] is True
    assert all("url" not in key for hop in result["redirect_hops"] for key in hop)


class _FailIfReadStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.was_read = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.was_read = True
        raise AssertionError("a non-206 response body must not be read")
        yield b""  # pragma: no cover


async def test_range_probe_does_not_read_body_when_server_returns_200() -> None:
    stream = _FailIfReadStream()

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError, match="body was not read"):
            await experiment._probe_cdn_range(
                client,
                "https://lh3.googleusercontent.com/media=dv",
                expected_prefix=b"prefix",
                source_size=99,
            )

    assert stream.was_read is False


async def test_range_probe_can_record_non_206_without_reading_body() -> None:
    stream = _FailIfReadStream()

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-length": "99", "content-type": "video/mp4"},
            stream=stream,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        _final_url, result = await experiment._probe_cdn_range(
            client,
            "https://lh3.googleusercontent.com/media=dv",
            expected_prefix=b"prefix",
            source_size=99,
            allow_non_206=True,
        )

    assert result["range_status"] == 200
    assert result["range_supported"] is False
    assert result["body_read"] is False
    assert result["redirect_hops"][-1]["content_type"] == "video/mp4"
    assert stream.was_read is False


async def test_range_probe_rejects_redirect_outside_google_media_allowlist() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://example.test/stolen"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError, match="non-allowlisted"):
            await experiment._probe_cdn_range(
                client,
                "https://lh3.googleusercontent.com/media=dv",
                expected_prefix=b"prefix",
                source_size=99,
            )


async def test_stream_download_uses_fresh_redirect_and_hard_size_cap(tmp_path) -> None:
    payload = b"downloaded"
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "lh3.googleusercontent.com":
            return httpx.Response(
                302,
                headers={"location": "https://rr1---synthetic.googlevideo.com/object?token=fake"},
            )
        return httpx.Response(
            200,
            headers={"content-length": str(len(payload)), "content-type": "video/mp4"},
            stream=_StaticAsyncStream(payload),
        )

    part = tmp_path / "download.mp4.part"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        size, digest, hops = await experiment._stream_download_to_part(
            client,
            "https://lh3.googleusercontent.com/media=m22",
            part=part,
            expected_size=len(payload),
        )

    assert size == len(payload)
    assert len(digest) == 64
    assert part.read_bytes() == payload
    assert [request.headers.get("range") for request in requests] == [None, None]
    assert [hop["status"] for hop in hops] == [302, 200]


async def test_stream_download_removes_partial_file_on_oversize(tmp_path) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_StaticAsyncStream(b"too-large"))

    part = tmp_path / "download.mp4.part"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError, match="exceeded"):
            await experiment._stream_download_to_part(
                client,
                "https://rr1---synthetic.googlevideo.com/object",
                part=part,
                expected_size=2,
            )

    assert not part.exists()


def test_media_url_allowlist_accepts_googlevideo_subdomain_only() -> None:
    assert (
        experiment._validated_media_url("https://rr1---synthetic.googlevideo.com/object").host
        == "rr1---synthetic.googlevideo.com"
    )
    with pytest.raises(RuntimeError, match="non-allowlisted"):
        experiment._validated_media_url("https://evilgooglevideo.com/object")


def test_normalize_ffprobe_records_codec_bitrate_and_resolution() -> None:
    raw = {
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "width": 1920,
                "height": 1080,
                "bit_rate": "4000000",
            },
            {"codec_type": "audio", "codec_name": "aac", "bit_rate": "128000"},
        ],
        "format": {
            "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
            "size": "1234",
            "duration": "12.5",
            "bit_rate": "4128000",
        },
    }

    assert experiment._normalize_ffprobe(raw, fallback_size=1) == {
        "size_bytes": 1234,
        "container": "mov,mp4,m4a,3gp,3g2,mj2",
        "duration_seconds": 12.5,
        "overall_bitrate_bps": 4128000,
        "video_codec": "h264",
        "video_bitrate_bps": 4000000,
        "width": 1920,
        "height": 1080,
        "resolution": "1920x1080",
        "audio_codec": "aac",
        "audio_bitrate_bps": 128000,
    }


def test_resume_directory_and_png_must_be_scoped_to_evidence_root(tmp_path) -> None:
    root = tmp_path / "evidence"
    run = root / "20260902T140007Z"
    run.mkdir(parents=True)
    png = run / "photos-after-share.png"
    png.write_bytes(
        experiment._PNG_SIGNATURE + b"\x00\x00\x00\rIHDR" + (720).to_bytes(4, "big") + (1280).to_bytes(4, "big")
    )

    assert experiment._resume_evidence_dir(root, str(run)) == run
    assert experiment._validate_screenshot(png) == {"size_bytes": 24, "width": 720, "height": 1280}

    nested = run / "nested"
    nested.mkdir()
    with pytest.raises(RuntimeError, match="immediate child"):
        experiment._resume_evidence_dir(root, str(nested))


def test_private_json_write_is_atomic_and_rejects_symlink(tmp_path) -> None:
    report = tmp_path / "report.json"
    experiment._write_private_json(report, {"status": "first"})
    experiment._write_private_json(report, {"status": "second"})

    assert report.read_text(encoding="utf-8") == '{\n  "status": "second"\n}\n'
    assert report.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob(".report.json.*.tmp"))

    target = tmp_path / "target.json"
    target.write_text("safe", encoding="utf-8")
    unsafe = tmp_path / "unsafe.json"
    unsafe.symlink_to(target)
    with pytest.raises(RuntimeError, match="unsafe JSON"):
        experiment._write_private_json(unsafe, {"status": "forbidden"})
    assert target.read_text(encoding="utf-8") == "safe"


@pytest.mark.parametrize("payload", [b"original-video", b"changed--video", b"truncated"])
async def test_download_back_http200_without_range_never_selects_m22(tmp_path, monkeypatch, payload):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"original-video")
    destination = tmp_path / "cloud.mp4"
    requests = []

    async def handler(request):
        requests.append(str(request.url))
        assert str(request.url).endswith("=dv")
        return httpx.Response(200, stream=_StaticAsyncStream(payload))

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        experiment.httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs)
    )
    monkeypatch.setattr(experiment, "_probe_media_contract", AsyncMock(return_value={"resolution": "3840x2160"}))
    if payload == b"original-video":
        result = await experiment._download_back(
            "https://lh3.googleusercontent.com/media=dv", source_path=source, destination=destination
        )
        assert result["verdict"] == "PASS_DIRECT_DV_ORIGINAL_NO_RANGE"
        assert result["sha256_match"] is True
        assert result["direct_dv_range"]["range_supported"] is False
        assert destination.read_bytes() == source.read_bytes()
    else:
        with pytest.raises(RuntimeError, match="differs"):
            await experiment._download_back(
                "https://lh3.googleusercontent.com/media=dv", source_path=source, destination=destination
            )
        assert not destination.exists()
        assert not destination.with_suffix(".mp4.part").exists()
    assert len(requests) == 2


async def test_original_download_refuses_m22_before_network_or_file_access(tmp_path):
    with pytest.raises(RuntimeError, match="not a rendition"):
        await experiment._download_back(
            "https://lh3.googleusercontent.com/media=m22",
            source_path=tmp_path / "absent.mp4",
            destination=tmp_path / "cloud.mp4",
        )
