"""Acceptance checks for the rebuilt movie, the last gate before it is called playable."""

from __future__ import annotations

import hashlib
import subprocess

import httpx
import pytest
import respx

from pixav.strm_resolver.movie_acceptance import range_acceptance, vlc_acceptance

BASE = "http://resolver:8000"
VIDEO_ID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def movie(tmp_path):
    path = tmp_path / "movie.mp4"
    path.write_bytes(bytes(range(256)) * 800)
    return path


def _serve(movie):
    """Serve the file the way the resolver's /local route does."""
    size = movie.stat().st_size

    def handler(request: httpx.Request) -> httpx.Response:
        header = request.headers.get("range")
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-length": str(size)})
        if header is None:
            return httpx.Response(200, content=movie.read_bytes())
        start, _, end = header.removeprefix("bytes=").partition("-")
        if start and int(start) >= size:
            return httpx.Response(416, headers={"content-range": f"bytes */{size}"})
        first = int(start) if start else max(0, size - int(end))
        last = min(int(end), size - 1) if start and end else size - 1
        return httpx.Response(
            206,
            content=movie.read_bytes()[first : last + 1],
            headers={"content-range": f"bytes {first}-{last}/{size}"},
        )

    respx.get(f"{BASE}/resolve/{VIDEO_ID}").mock(return_value=httpx.Response(200, json={"source": "local"}))
    respx.route(url=f"{BASE}/stream/{VIDEO_ID}").mock(side_effect=handler)
    respx.route(url=f"{BASE}/local/{VIDEO_ID}").mock(side_effect=handler)
    return handler


class TestRangeAcceptance:
    @respx.mock
    async def test_head_middle_and_tail_ranges_must_match_the_published_bytes(self, movie):
        _serve(movie)
        result = await range_acceptance(BASE, VIDEO_ID, movie)
        assert result == {
            "range": "PASS",
            "suffix": "PASS",
            "416": "PASS",
            "head": "PASS",
            "get": "PASS",
            "sha256": hashlib.sha256(movie.read_bytes()).hexdigest(),
            "size": movie.stat().st_size,
        }
        assert any(call.request.headers.get("range") == "bytes=-65536" for call in respx.calls)

    @respx.mock
    async def test_a_range_serving_the_wrong_bytes_fails(self, movie):
        _serve(movie)
        respx.route(url=f"{BASE}/stream/{VIDEO_ID}").mock(
            return_value=httpx.Response(
                206,
                content=b"\x00" * 65536,
                headers={"content-range": f"bytes 0-65535/{movie.stat().st_size}"},
            )
        )
        with pytest.raises(ValueError, match="range mismatch"):
            await range_acceptance(BASE, VIDEO_ID, movie)

    @respx.mock
    async def test_an_unsatisfiable_range_that_does_not_return_416_fails(self, movie):
        serve = _serve(movie)
        size = movie.stat().st_size

        def handler(request: httpx.Request) -> httpx.Response:
            header = request.headers.get("range", "")
            if header == f"bytes={size}-":
                return httpx.Response(200, content=b"")
            return serve(request)

        respx.route(url=f"{BASE}/stream/{VIDEO_ID}").mock(side_effect=handler)
        with pytest.raises(ValueError, match="416"):
            await range_acceptance(BASE, VIDEO_ID, movie)

    @respx.mock
    @pytest.mark.parametrize("failure", ["corrupt", "truncated", "partial", "length"])
    async def test_full_get_must_match_even_when_ranges_pass(self, movie, failure):
        serve = _serve(movie)

        def handler(request):
            if request.headers.get("range"):
                return serve(request)
            data = movie.read_bytes()
            content = b"!" + data[1:] if failure == "corrupt" else data
            if failure == "truncated":
                content = data[:-1]
            return httpx.Response(
                206 if failure == "partial" else 200,
                content=content,
                headers={"content-length": str(len(data) + (failure == "length"))},
            )

        respx.route(url=f"{BASE}/stream/{VIDEO_ID}").mock(side_effect=handler)
        with pytest.raises(ValueError, match="complete GET"):
            await range_acceptance(BASE, VIDEO_ID, movie)

    @respx.mock
    async def test_suffix_is_required_even_when_explicit_ranges_work(self, movie):
        serve = _serve(movie)

        def handler(request):
            if request.headers.get("range") == "bytes=-65536":
                return httpx.Response(200, content=movie.read_bytes())
            return serve(request)

        respx.route(url=f"{BASE}/stream/{VIDEO_ID}").mock(side_effect=handler)
        with pytest.raises(ValueError, match="range mismatch"):
            await range_acceptance(BASE, VIDEO_ID, movie)


class TestVlcAcceptance:
    async def test_a_decoded_picture_is_required_at_every_seek(self, tmp_path, monkeypatch):
        log = b"main debug: Received first picture\n"
        calls: list[list[str]] = []

        def fake_run(command, **_kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, log, b"")

        monkeypatch.setattr("pixav.strm_resolver.movie_acceptance.subprocess.run", fake_run)
        result = await vlc_acceptance(["docker", "run"], "http://x/stream/1", [0.0, 12.5], tmp_path)

        assert result["vlc"] == "PASS"
        assert [receipt["seconds"] for receipt in result["seeks"]] == [0.0, 12.5]
        assert result["seeks"][0]["log_sha256"] == hashlib.sha256(log).hexdigest()
        assert "12.5" in calls[1]
        assert (tmp_path / "vlc-seek-0.log").read_bytes() == log

    async def test_a_clean_exit_without_a_picture_is_still_a_failure(self, tmp_path, monkeypatch):
        # VLC exits zero after opening a stream it never decoded.
        def fake_run(command, **_kwargs):
            return subprocess.CompletedProcess(command, 0, b"main debug: opening", b"")

        monkeypatch.setattr("pixav.strm_resolver.movie_acceptance.subprocess.run", fake_run)
        with pytest.raises(ValueError, match="did not decode a picture"):
            await vlc_acceptance(["docker", "run"], "http://x/stream/1", [0.0], tmp_path)
        # The log is still kept as evidence of the failure.
        assert (tmp_path / "vlc-seek-0.log").exists()

    async def test_a_non_zero_exit_fails_even_with_a_picture_line(self, tmp_path, monkeypatch):
        def fake_run(command, **_kwargs):
            return subprocess.CompletedProcess(command, 1, b"Received first picture", b"")

        monkeypatch.setattr("pixav.strm_resolver.movie_acceptance.subprocess.run", fake_run)
        with pytest.raises(ValueError, match="did not decode a picture"):
            await vlc_acceptance(["docker", "run"], "http://x/stream/1", [0.0], tmp_path)
