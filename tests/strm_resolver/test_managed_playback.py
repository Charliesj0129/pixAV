"""BDD-064/067–075/119–122: real ASGI transport, isolated storage boundary."""

import hashlib
import json
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from pixav.strm_resolver.app import create_app


@pytest.fixture
def managed(tmp_path):
    token_file = tmp_path / "devices.json"
    token_file.write_text(json.dumps({"synthetic-device": hashlib.sha256(b"synthetic-token").hexdigest()}))
    token_file.chmod(0o600)
    path = tmp_path / "original.mp4"
    path.write_bytes(bytes(range(256)) * 512)

    class Playback:
        root = tmp_path
        prepared = 0
        reading = 0

        async def prepare(self, video_id):
            self.prepared += 1

        @asynccontextmanager
        async def reader(self, video_id):
            self.reading += 1
            try:
                yield {"cache_path": str(path), "size_bytes": path.stat().st_size}
            finally:
                self.reading -= 1

    app = create_app(redis_url=None, db_dsn=None)
    app.state.managed_playback = True
    app.state.playback_settings = SimpleNamespace(playback_tokens_file=str(token_file))
    app.state.db_pool = object()
    app.state.playback = Playback()
    return app, path, token_file


@pytest.mark.parametrize(
    "byte_range,start,end", [("bytes=0-65535", 0, 65535), ("bytes=500-700", 500, 700), ("bytes=-16", 131056, 131071)]
)
async def test_authenticated_ranges_are_exact_without_redirect(managed, byte_range, start, end):
    app, path, _ = managed
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            f"/stream/{uuid.uuid4()}", headers={"Authorization": "Bearer synthetic-token", "Range": byte_range}
        )
    assert response.status_code == 206
    assert response.content == path.read_bytes()[start : end + 1]
    assert response.headers["content-range"] == f"bytes {start}-{end}/131072"
    assert "location" not in response.headers
    assert app.state.playback.reading == 0


async def test_full_get_head_and_unsatisfiable_range(managed):
    app, path, _ = managed
    url = f"/stream/{uuid.uuid4()}"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer synthetic-token"}
    ) as client:
        response = await client.get(url)
        assert response.content == path.read_bytes()
        count = app.state.playback.prepared
        head = await client.head(url)
        assert head.status_code == 200 and head.content == b""
        assert int(head.headers["content-length"]) == len(response.content)
        assert app.state.playback.prepared == count
        invalid = await client.get(url, headers={"Range": "bytes=999999-"})
        assert invalid.status_code == 416


async def test_authentication_and_immediate_device_revocation(managed):
    app, _, token_file = managed
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        url = f"/stream/{uuid.uuid4()}"
        assert (await client.get(url)).status_code == 401
        assert app.state.playback.prepared == 0
        assert (await client.get(url, headers={"Authorization": "Bearer wrong"})).status_code == 403
        token_file.write_text("{}")
        assert (await client.get(url, headers={"Authorization": "Bearer synthetic-token"})).status_code == 403
        assert (await client.get(url.replace("stream", "local"))).status_code == 404


async def test_resolve_never_exposes_provider_url(managed):
    app, _, _ = managed
    video_id = uuid.uuid4()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer synthetic-token"}
    ) as client:
        result = await client.get(f"/resolve/{video_id}")
    assert result.json() == {"video_id": str(video_id), "stream_url": f"/stream/{video_id}"}


async def test_authentication_configuration_fails_closed(managed):
    app, _, token_file = managed
    token_file.chmod(0o644)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer synthetic-token"}
    ) as client:
        assert (await client.get(f"/stream/{uuid.uuid4()}")).status_code == 503
