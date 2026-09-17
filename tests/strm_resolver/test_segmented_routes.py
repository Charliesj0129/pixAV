import uuid
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from pixav.strm_resolver.app import create_app


@pytest.mark.parametrize("path", ["resolve", "stream", "local"])
@pytest.mark.parametrize("failure", ["missing", "version", "size", "symlink"])
async def test_incomplete_manifest_never_serves_cached_first_part(tmp_path, path, failure):
    app = create_app(redis_url=None, db_dsn=None)
    video = uuid.uuid4()
    movie = tmp_path / "movie.mp4"
    movie.write_bytes(b"complete movie")
    row = {
        "id": video,
        "local_path": str(movie),
        "manifest_version": 1,
        "playback_manifest_version": 1,
        "share_url": "https://photos.app.goo.gl/first-part",
        "metadata_json": {
            "segmented_playback": {"content": "PASS", "cold_inputs": "photos-only", "size": movie.stat().st_size}
        },
    }
    if failure == "missing":
        movie.unlink()
    elif failure == "version":
        row["playback_manifest_version"] = None
    elif failure == "size":
        movie.write_bytes(b"truncated")
    else:
        link = tmp_path / "link.mp4"
        link.symlink_to(movie)
        row["local_path"] = str(link)
    app.state.db_pool = AsyncMock()
    app.state.db_pool.fetchrow.return_value = row
    app.state.redis = AsyncMock()
    app.state.redis.get.return_value = "https://cdn.invalid/first-part"
    app.state.resolver = AsyncMock()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/{path}/{video}")
    assert response.status_code == 409
    app.state.resolver.resolve.assert_not_awaited()


async def test_complete_movie_head_range_and_416(tmp_path):
    app = create_app(redis_url=None, db_dsn=None)
    movie = tmp_path / "movie.mp4"
    movie.write_bytes(b"0123456789")
    video = uuid.uuid4()
    app.state.db_pool = AsyncMock()
    app.state.db_pool.fetchrow.return_value = {
        "id": video,
        "local_path": str(movie),
        "manifest_version": 1,
        "playback_manifest_version": 1,
        "metadata_json": {"segmented_playback": {"content": "PASS", "cold_inputs": "photos-only", "size": 10}},
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", follow_redirects=True) as client:
        response = await client.get(f"/stream/{video}", headers={"Range": "bytes=3-6"})
        assert response.status_code == 206 and response.content == b"3456"
        assert (await client.get(f"/stream/{video}", headers={"Range": "bytes=10-"})).status_code == 416
        head = await client.head(f"/local/{video}")
        assert head.status_code == 200 and head.content == b"" and head.headers["content-length"] == "10"


SEGMENTED_DETAIL = "segmented playback requires prepare-playback"
NOT_UPLOADED_DETAIL = "video is not uploaded yet (share_url missing)"


def _segmented_app(manifest_version=1, share_url=None):
    app = create_app(redis_url=None, db_dsn=None)
    row = {
        "id": uuid.uuid4(),
        "local_path": None,
        "manifest_version": manifest_version,
        "playback_manifest_version": None,
        "share_url": share_url,
        "cdn_url": None,
        "metadata_json": {},
    }
    app.state.db_pool = AsyncMock()
    app.state.db_pool.fetchrow.return_value = row
    app.state.redis = AsyncMock()
    app.state.redis.get.return_value = None
    app.state.resolver = AsyncMock()
    return app, row


class TestSegmentedGuardIsIdentifiedByItsDetail:
    """The status code alone is not evidence for this acceptance.

    Two different 409s sit on the same path: this guard, and the not-uploaded-yet
    guard inside _resolve_cdn. Asserting only the code lets the second
    impersonate the first, so the live capture and these tests both have to
    compare the detail text.
    """

    @pytest.mark.parametrize("path", ["resolve", "stream", "local"])
    async def test_every_entry_point_names_prepare_playback(self, path):
        app, row = _segmented_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(f"/{path}/{row['id']}")
        assert response.status_code == 409
        assert response.json()["detail"] == SEGMENTED_DETAIL

    @pytest.mark.parametrize("path", ["resolve", "stream"])
    async def test_the_not_uploaded_guard_is_a_different_message(self, path):
        # Before the manifest is committed the same request reaches the other
        # 409. The two must stay textually distinct or neither proves anything.
        app, row = _segmented_app(manifest_version=None)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(f"/{path}/{row['id']}")
        assert response.status_code == 409
        assert response.json()["detail"] == NOT_UPLOADED_DETAIL
        assert response.json()["detail"] != SEGMENTED_DETAIL

    @pytest.mark.parametrize("path", ["resolve", "stream", "local"])
    async def test_no_entry_point_reads_the_parts_table(self, path):
        # "Never fall back to playing the first segment" is structural: the
        # routes only ever read the parent row, so no part can be served.
        app, row = _segmented_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await client.get(f"/{path}/{row['id']}")
        statements = " ".join(str(call.args[0]) for call in app.state.db_pool.fetchrow.await_args_list)
        assert statements, "the route must have queried the parent row"
        assert "video_parts" not in statements.lower()

    @pytest.mark.parametrize("path", ["resolve", "stream", "local"])
    async def test_an_unprepared_request_never_starts_a_retrieval(self, path):
        app, row = _segmented_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await client.get(f"/{path}/{row['id']}")
        app.state.resolver.resolve.assert_not_awaited()
        app.state.db_pool.execute.assert_not_awaited()
