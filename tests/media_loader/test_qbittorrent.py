"""Tests for QBitClient."""

from __future__ import annotations

from collections.abc import AsyncIterator
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from pixav.media_loader.qbittorrent import (
    QBitClient,
    classify_torrent_progress,
    extract_info_hash,
    map_download_path,
    parse_extra_trackers,
)
from pixav.shared.exceptions import DownloadError, SourceUnavailableError


def _legacy_login_response() -> httpx.Response:
    """qBittorrent <= 5.1: 200 with an ``Ok.`` body and a ``SID`` cookie."""
    return httpx.Response(200, text="Ok.", headers={"set-cookie": "SID=legacy-session; path=/"})


def _modern_login_response() -> httpx.Response:
    """qBittorrent >= 5.2: 204 with an empty body and a ``QBT_SID_<port>`` cookie."""
    return httpx.Response(204, headers={"set-cookie": "QBT_SID_8080=modern-session; path=/"})


@pytest.fixture
async def client() -> AsyncIterator[QBitClient]:
    qbit = QBitClient(
        base_url="http://qbit:8080",
        username="admin",
        password="adminadmin",
        download_dir="/downloads",
        timeout=5,
        poll_interval=0,  # skip sleep in tests
    )
    try:
        yield qbit
    finally:
        await qbit.aclose()


class TestExtractHash:
    def test_extracts_40char_hex(self) -> None:
        magnet = "magnet:?xt=urn:btih:da39a3ee5e6b4b0d3255bfef95601890afd80709&dn=Test"
        assert extract_info_hash(magnet) == "da39a3ee5e6b4b0d3255bfef95601890afd80709"

    def test_extracts_base32(self) -> None:
        magnet = "magnet:?xt=urn:btih:3I42H3S6NNFQ2MSVX7XZKYAYSCX5QBYJ&dn=Test"
        result = extract_info_hash(magnet)
        assert result is not None
        assert len(result) == 32

    def test_returns_none_for_invalid(self) -> None:
        assert extract_info_hash("not-a-magnet") is None
        assert extract_info_hash("magnet:?xt=urn:btih:") is None


@respx.mock
@pytest.mark.parametrize("inventory", [[], [{"hash": "A" * 40}], {"hash": "A" * 40}, [{"hash": "invalid"}]])
async def test_inventory_is_read_only_and_validates_hash_contract(client: QBitClient, inventory):
    respx.post("http://qbit:8080/api/v2/auth/login").mock(return_value=_modern_login_response())
    respx.get("http://qbit:8080/api/v2/torrents/info").mock(return_value=httpx.Response(200, json=inventory))
    if isinstance(inventory, list) and (not inventory or inventory[0]["hash"] == "A" * 40):
        assert await client.list_torrent_hashes() == ({"a" * 40} if inventory else set())
    else:
        with pytest.raises(DownloadError, match="inventory contract"):
            await client.list_torrent_hashes()


def test_parse_extra_trackers_filters_invalid_and_deduplicates() -> None:
    raw = "udp://tracker.example:80, https://tracker.example/announce\nudp://tracker.example:80\nfile:///tmp/x"

    assert parse_extra_trackers(raw) == (
        "udp://tracker.example:80",
        "https://tracker.example/announce",
    )


class TestDownloadPathMapping:
    def test_maps_qbit_container_path_to_worker_root(self, tmp_path) -> None:
        result = map_download_path(
            "/downloads/folder/movie.mkv",
            remote_root="/downloads",
            local_root=str(tmp_path),
        )

        assert result == str(tmp_path / "folder" / "movie.mkv")

    @pytest.mark.parametrize("remote_path", ["relative/movie.mkv", "/other/movie.mkv", "/downloads"])
    def test_rejects_unmapped_or_incomplete_paths(self, remote_path: str, tmp_path) -> None:
        with pytest.raises(DownloadError):
            map_download_path(remote_path, remote_root="/downloads", local_root=str(tmp_path))


class TestQBitClient:
    @respx.mock
    async def test_health_check_success(self, client: QBitClient) -> None:
        respx.post("http://qbit:8080/api/v2/auth/login").mock(return_value=_legacy_login_response())
        respx.get("http://qbit:8080/api/v2/app/version").mock(return_value=httpx.Response(200, text="5.0.5"))

        version = await client.health_check()

        assert version == "5.0.5"

    @respx.mock
    async def test_health_check_wrong_endpoint(self, client: QBitClient) -> None:
        respx.post("http://qbit:8080/api/v2/auth/login").mock(return_value=_legacy_login_response())
        respx.get("http://qbit:8080/api/v2/app/version").mock(return_value=httpx.Response(404, text="Not found"))

        with pytest.raises(DownloadError, match="does not expose /api/v2/app/version"):
            await client.health_check()

    @respx.mock
    async def test_health_check_auth_fails(self, client: QBitClient) -> None:
        respx.get("http://qbit:8080/api/v2/app/version").mock(return_value=httpx.Response(200, text="5.0.5"))
        respx.post("http://qbit:8080/api/v2/auth/login").mock(return_value=httpx.Response(200, text="Fails."))

        with pytest.raises(DownloadError, match="login failed"):
            await client.health_check()

    @respx.mock
    async def test_add_magnet_success(self, client: QBitClient) -> None:
        # Login
        login_route = respx.post("http://qbit:8080/api/v2/auth/login").mock(
            return_value=httpx.Response(
                200,
                text="Ok.",
                headers={"Set-Cookie": "SID=abc123; path=/"},
            )
        )
        # Add torrent
        add_route = respx.post("http://qbit:8080/api/v2/torrents/add").mock(
            return_value=httpx.Response(200, text="Ok.")
        )

        magnet = "magnet:?xt=urn:btih:da39a3ee5e6b4b0d3255bfef95601890afd80709&dn=Test"
        result = await client.add_magnet(magnet)

        assert result == "da39a3ee5e6b4b0d3255bfef95601890afd80709"
        assert login_route.called
        assert add_route.called

    @respx.mock
    async def test_reuses_one_authenticated_http_session_across_methods(self, client: QBitClient) -> None:
        """The worker must not create a client and log in again for every call."""
        login_route = respx.post("http://qbit:8080/api/v2/auth/login").mock(return_value=_modern_login_response())
        respx.get("http://qbit:8080/api/v2/app/version").mock(return_value=httpx.Response(200, text="v5.2.3"))
        respx.post("http://qbit:8080/api/v2/torrents/add").mock(return_value=httpx.Response(200, text="Ok."))
        magnet = "magnet:?xt=urn:btih:da39a3ee5e6b4b0d3255bfef95601890afd80709&dn=Test"

        await client.health_check()
        await client.add_magnet(magnet)

        assert login_route.call_count == 1
        assert client._client is not None

        await client.aclose()
        assert client._client is None

    @respx.mock
    async def test_refreshes_an_expired_session_once(self, client: QBitClient) -> None:
        login_route = respx.post("http://qbit:8080/api/v2/auth/login").mock(
            side_effect=[_legacy_login_response(), _modern_login_response()]
        )
        version_route = respx.get("http://qbit:8080/api/v2/app/version").mock(
            side_effect=[httpx.Response(403, text="Forbidden"), httpx.Response(200, text="v5.2.3")]
        )

        assert await client.health_check() == "v5.2.3"
        assert login_route.call_count == 2
        assert version_route.call_count == 2

    @respx.mock
    async def test_add_magnet_appends_configured_trackers_via_official_api(self) -> None:
        client = QBitClient(
            "http://qbit:8080",
            "admin",
            "adminadmin",
            extra_trackers=("udp://tracker.example:80", "https://tracker.example/announce"),
        )
        respx.post("http://qbit:8080/api/v2/auth/login").mock(return_value=_modern_login_response())
        respx.post("http://qbit:8080/api/v2/torrents/add").mock(return_value=httpx.Response(200, text="Ok."))
        tracker_route = respx.post("http://qbit:8080/api/v2/torrents/addTrackers").mock(
            return_value=httpx.Response(200, text="Ok.")
        )
        magnet = "magnet:?xt=urn:btih:da39a3ee5e6b4b0d3255bfef95601890afd80709"

        await client.add_magnet(magnet)

        form = parse_qs(tracker_route.calls.last.request.content.decode())
        assert form["hash"] == ["da39a3ee5e6b4b0d3255bfef95601890afd80709"]
        assert form["urls"] == ["udp://tracker.example:80\nhttps://tracker.example/announce"]

    @respx.mock
    async def test_add_magnet_login_fails(self, client: QBitClient) -> None:
        respx.post("http://qbit:8080/api/v2/auth/login").mock(return_value=httpx.Response(200, text="Fails."))

        magnet = "magnet:?xt=urn:btih:da39a3ee5e6b4b0d3255bfef95601890afd80709&dn=Test"
        with pytest.raises(DownloadError, match="login failed"):
            await client.add_magnet(magnet)

    async def test_add_magnet_invalid_hash(self, client: QBitClient) -> None:
        with pytest.raises(DownloadError, match="Cannot extract hash"):
            await client.add_magnet("not-a-magnet-link")

    @respx.mock
    async def test_wait_complete_success(self, client: QBitClient) -> None:
        respx.post("http://qbit:8080/api/v2/auth/login").mock(return_value=_legacy_login_response())
        respx.get("http://qbit:8080/api/v2/torrents/info").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "hash": "abc123",
                        "progress": 1.0,
                        "state": "uploading",
                        "content_path": "/downloads/test_video",
                        "save_path": "/downloads",
                        "name": "test_video",
                    }
                ],
            )
        )

        result = await client.wait_complete("abc123", timeout=10)
        assert result == "/downloads/test_video"

    @respx.mock
    async def test_wait_complete_returns_worker_visible_path(self, tmp_path) -> None:
        client = QBitClient(
            "http://qbit:8080",
            "admin",
            "adminadmin",
            download_dir="/downloads",
            local_download_dir=str(tmp_path),
            poll_interval=0,
        )
        respx.post("http://qbit:8080/api/v2/auth/login").mock(return_value=_legacy_login_response())
        respx.get("http://qbit:8080/api/v2/torrents/info").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "progress": 1.0,
                        "state": "uploading",
                        "content_path": "/downloads/item/movie.mkv",
                    }
                ],
            )
        )

        try:
            assert await client.wait_complete("abc123", timeout=10) == str(tmp_path / "item" / "movie.mkv")
        finally:
            await client.aclose()

    @respx.mock
    async def test_wait_complete_not_found(self, client: QBitClient) -> None:
        respx.post("http://qbit:8080/api/v2/auth/login").mock(return_value=_legacy_login_response())
        respx.get("http://qbit:8080/api/v2/torrents/info").mock(return_value=httpx.Response(200, json=[]))

        with pytest.raises(DownloadError, match="not found"):
            await client.wait_complete("missing", timeout=10)

    @respx.mock
    async def test_wait_complete_error_state(self, client: QBitClient) -> None:
        respx.post("http://qbit:8080/api/v2/auth/login").mock(return_value=_legacy_login_response())
        respx.get("http://qbit:8080/api/v2/torrents/info").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "hash": "abc123",
                        "progress": 0.5,
                        "state": "error",
                    }
                ],
            )
        )

        with pytest.raises(DownloadError, match="error state"):
            await client.wait_complete("abc123", timeout=10)

    @respx.mock
    async def test_wait_complete_timeout(self, client: QBitClient) -> None:
        respx.post("http://qbit:8080/api/v2/auth/login").mock(return_value=_legacy_login_response())
        respx.get("http://qbit:8080/api/v2/torrents/info").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "hash": "abc123",
                        "progress": 0.5,
                        "state": "downloading",
                    }
                ],
            )
        )

        # poll_interval=0 so it loops fast; timeout=0 means immediate timeout
        with pytest.raises(DownloadError, match="timed out"):
            await client.wait_complete("abc123", timeout=0)

    @pytest.mark.parametrize(
        ("payload", "expected"),
        [([], False), ([{"hash": "different"}], False), ([{"hash": "ABC123"}], True)],
    )
    @respx.mock
    async def test_has_torrent_reads_exact_hash(
        self, client: QBitClient, payload: list[dict[str, str]], expected: bool
    ) -> None:
        respx.post("http://qbit:8080/api/v2/auth/login").mock(return_value=_modern_login_response())
        route = respx.get("http://qbit:8080/api/v2/torrents/info").mock(return_value=httpx.Response(200, json=payload))

        assert await client.has_torrent("abc123") is expected
        assert route.calls.last.request.url.params["hashes"] == "abc123"


class TestLoginContracts:
    """The login contract changed in qBittorrent 5.2; both must keep working.

    A 5.2 server answers 204 with an empty body and renames the session cookie
    to QBT_SID_<port>. Asserting on the old ``Ok.`` body or the ``SID`` name
    made every worker fail to authenticate against a 5.2 server.
    """

    @respx.mock
    async def test_accepts_modern_204_and_renamed_cookie(self, client: QBitClient) -> None:
        respx.post("http://qbit:8080/api/v2/auth/login").mock(return_value=_modern_login_response())
        version_route = respx.get("http://qbit:8080/api/v2/app/version").mock(
            return_value=httpx.Response(200, text="v5.2.3")
        )

        assert await client.health_check() == "v5.2.3"
        # The renamed cookie must be replayed on the follow-up request.
        assert "QBT_SID_8080=modern-session" in version_route.calls.last.request.headers["cookie"]

    @respx.mock
    async def test_accepts_legacy_ok_body_and_sid_cookie(self, client: QBitClient) -> None:
        respx.post("http://qbit:8080/api/v2/auth/login").mock(return_value=_legacy_login_response())
        version_route = respx.get("http://qbit:8080/api/v2/app/version").mock(
            return_value=httpx.Response(200, text="v5.1.4")
        )

        assert await client.health_check() == "v5.1.4"
        assert "SID=legacy-session" in version_route.calls.last.request.headers["cookie"]

    @respx.mock
    async def test_rejects_modern_401_bad_credentials(self, client: QBitClient) -> None:
        respx.post("http://qbit:8080/api/v2/auth/login").mock(return_value=httpx.Response(401, text="Unauthorized"))

        with pytest.raises(DownloadError, match="login failed"):
            await client.health_check()

    @respx.mock
    async def test_rejects_legacy_fails_body(self, client: QBitClient) -> None:
        respx.post("http://qbit:8080/api/v2/auth/login").mock(return_value=httpx.Response(200, text="Fails."))

        with pytest.raises(DownloadError, match="login failed"):
            await client.health_check()

    @respx.mock
    async def test_rejects_ip_ban(self, client: QBitClient) -> None:
        respx.post("http://qbit:8080/api/v2/auth/login").mock(
            return_value=httpx.Response(403, text="Your IP address has been banned")
        )

        with pytest.raises(DownloadError, match="banned"):
            await client.health_check()

    @respx.mock
    async def test_rejects_success_without_session_cookie(self, client: QBitClient) -> None:
        """A cookieless success would 403 on every later call; fail at login instead."""
        respx.post("http://qbit:8080/api/v2/auth/login").mock(return_value=httpx.Response(204))

        with pytest.raises(DownloadError, match="no session cookie"):
            await client.health_check()


class TestClassifyTorrentProgress:
    """Swarm-existence signals, measured against the live client on 2026-08-30."""

    def test_complete_when_progress_is_full(self) -> None:
        assert classify_torrent_progress({"progress": 1.0, "state": "uploading"}) == "complete"

    def test_error_states_are_terminal(self) -> None:
        assert classify_torrent_progress({"progress": 0.0, "state": "error"}) == "error"
        assert classify_torrent_progress({"progress": 0.0, "state": "missingFiles"}) == "error"

    def test_partial_progress_is_viable(self) -> None:
        assert classify_torrent_progress({"progress": 0.01, "state": "stalledDL"}) == "viable"

    def test_num_complete_means_wait_even_when_num_seeds_is_zero(self) -> None:
        """The measured case: tracker reports seeds, but no peer is connected yet.

        Using num_seeds here would declare a live swarm dead.
        """
        info = {"progress": 0.0, "state": "metaDL", "num_seeds": 0, "num_complete": 2}

        assert classify_torrent_progress(info) == "waiting"

    def test_availability_alone_means_wait(self) -> None:
        info = {"progress": 0.0, "state": "metaDL", "num_complete": 0, "availability": 1.4}

        assert classify_torrent_progress(info) == "waiting"

    def test_no_swarm_signal_at_all_is_stalled(self) -> None:
        info = {"progress": 0.0, "state": "metaDL", "num_seeds": 0, "num_complete": 0, "availability": 0.0}

        assert classify_torrent_progress(info) == "stalled"


class TestWatermarkGuard:
    """Watermark magnets must be refused before they can occupy a download slot."""

    WATERMARK_MAGNET = "magnet:?xt=urn:btih:5c2f3934293d283d323b1c3b313d3530723f3331&dn=Fake"

    async def test_add_magnet_rejects_a_watermark(self, client: QBitClient) -> None:
        with pytest.raises(SourceUnavailableError, match="watermark"):
            await client.add_magnet(self.WATERMARK_MAGNET)

    async def test_fetch_metadata_name_rejects_a_watermark(self, client: QBitClient) -> None:
        with pytest.raises(SourceUnavailableError, match="watermark"):
            await client.fetch_metadata_name(self.WATERMARK_MAGNET)

    @respx.mock
    async def test_watermark_never_reaches_qbittorrent(self, client: QBitClient) -> None:
        """The guard runs before login, so no request is issued at all."""
        login = respx.post("http://qbit:8080/api/v2/auth/login").mock(return_value=_modern_login_response())
        add = respx.post("http://qbit:8080/api/v2/torrents/add").mock(return_value=httpx.Response(200, text="Ok."))

        with pytest.raises(SourceUnavailableError):
            await client.add_magnet(self.WATERMARK_MAGNET)

        assert not login.called
        assert not add.called


class TestSourceViability:
    @respx.mock
    async def test_zero_seeds_is_a_retryable_observation_bdd_013(self) -> None:
        client = QBitClient(
            base_url="http://qbit:8080",
            username="admin",
            password="adminadmin",
            poll_interval=1,
            no_peer_grace_seconds=3,
            download_timeout=3600,
        )
        respx.post("http://qbit:8080/api/v2/auth/login").mock(return_value=_modern_login_response())
        respx.get("http://qbit:8080/api/v2/torrents/info").mock(
            return_value=httpx.Response(
                200,
                json=[{"progress": 0.0, "state": "metaDL", "num_seeds": 0, "num_complete": 0, "availability": 0.0}],
            )
        )

        with pytest.raises(DownloadError, match="found no seeds") as error:
            await client.wait_complete("abc123")
        assert not isinstance(error.value, SourceUnavailableError)

    @respx.mock
    async def test_a_live_swarm_is_not_declared_dead(self) -> None:
        """num_complete > 0 keeps waiting; only the overall timeout ends it."""
        client = QBitClient(
            base_url="http://qbit:8080",
            username="admin",
            password="adminadmin",
            poll_interval=1,
            no_peer_grace_seconds=2,
            download_timeout=4,
        )
        respx.post("http://qbit:8080/api/v2/auth/login").mock(return_value=_modern_login_response())
        respx.get("http://qbit:8080/api/v2/torrents/info").mock(
            return_value=httpx.Response(
                200,
                json=[{"progress": 0.0, "state": "metaDL", "num_seeds": 0, "num_complete": 2}],
            )
        )

        with pytest.raises(DownloadError, match="timed out") as exc_info:
            await client.wait_complete("abc123")
        assert not isinstance(exc_info.value, SourceUnavailableError)


class TestMetadataProbeCleanupContract:
    """The leak: cleanup must use the hash the client added, not the caller's."""

    @respx.mock
    async def test_returns_the_hash_it_added_not_the_callers(self, client: QBitClient) -> None:
        magnet = "magnet:?xt=urn:btih:da39a3ee5e6b4b0d3255bfef95601890afd80709&dn=Test"
        respx.post("http://qbit:8080/api/v2/auth/login").mock(return_value=_modern_login_response())
        respx.get("http://qbit:8080/api/v2/torrents/info").mock(
            side_effect=[
                httpx.Response(200, json=[]),
                httpx.Response(200, json=[{"name": "Real Torrent Name"}]),
            ]
        )
        respx.post("http://qbit:8080/api/v2/torrents/add").mock(return_value=httpx.Response(200, text="Ok."))

        probe = await client.fetch_metadata_name(magnet)

        assert probe.name == "Real Torrent Name"
        assert probe.created is True
        assert probe.info_hash == "da39a3ee5e6b4b0d3255bfef95601890afd80709"

    @respx.mock
    async def test_a_torrent_created_then_abandoned_is_removed_before_raising(self, client: QBitClient) -> None:
        """The failure path must not strand a metadata-only torrent in a download slot."""
        magnet = "magnet:?xt=urn:btih:da39a3ee5e6b4b0d3255bfef95601890afd80709&dn=Test"
        respx.post("http://qbit:8080/api/v2/auth/login").mock(return_value=_modern_login_response())
        respx.get("http://qbit:8080/api/v2/torrents/info").mock(
            side_effect=[
                httpx.Response(200, json=[]),
                httpx.ConnectError("qBittorrent went away"),
            ]
        )
        respx.post("http://qbit:8080/api/v2/torrents/add").mock(return_value=httpx.Response(200, text="Ok."))
        delete = respx.post("http://qbit:8080/api/v2/torrents/delete").mock(return_value=httpx.Response(200))

        with pytest.raises(DownloadError):
            await client.fetch_metadata_name(magnet)

        assert delete.called
        assert parse_qs(delete.calls[0].request.content.decode())["hashes"] == [
            "da39a3ee5e6b4b0d3255bfef95601890afd80709"
        ]


class TestAddTorrentFile:
    """The attached .torrent path, preferred because bare magnets carry no trackers."""

    HASH = "da39a3ee5e6b4b0d3255bfef95601890afd80709"
    PAYLOAD = b"d8:announce9:http://t4:infod4:name3:abcee"

    def _login(self) -> None:
        respx.post("http://qbit:8080/api/v2/auth/login").mock(return_value=_modern_login_response())

    @respx.mock
    async def test_uploads_multipart_and_keeps_the_configured_save_path(self, client: QBitClient) -> None:
        self._login()
        add = respx.post("http://qbit:8080/api/v2/torrents/add").mock(return_value=httpx.Response(200, text="Ok."))
        respx.get("http://qbit:8080/api/v2/torrents/info").mock(
            return_value=httpx.Response(200, json=[{"hash": self.HASH}])
        )

        assert await client.add_torrent_file(self.PAYLOAD, self.HASH.upper()) == self.HASH

        body = add.calls.last.request.content
        assert self.PAYLOAD in body
        assert b'name="torrents"' in body
        assert b"/downloads" in body

    @respx.mock
    async def test_rejects_and_removes_a_torrent_that_is_not_the_selected_release(self, client: QBitClient) -> None:
        self._login()
        respx.post("http://qbit:8080/api/v2/torrents/add").mock(return_value=httpx.Response(200, text="Ok."))
        other = "b" * 40
        respx.get("http://qbit:8080/api/v2/torrents/info").mock(
            side_effect=[
                httpx.Response(200, json=[]),
                httpx.Response(200, json=[{"hash": other}]),
                httpx.Response(200, json=[{"hash": other}]),
            ]
        )
        delete = respx.post("http://qbit:8080/api/v2/torrents/delete").mock(return_value=httpx.Response(200))

        with pytest.raises(DownloadError, match="does not match the selected info hash"):
            await client.add_torrent_file(self.PAYLOAD, self.HASH)

        # The wrong release must not keep occupying a download slot.
        assert parse_qs(delete.calls.last.request.content.decode())["hashes"] == [other]

    @respx.mock
    async def test_rejects_a_watermark_hash_before_contacting_the_client(self, client: QBitClient) -> None:
        self._login()
        add = respx.post("http://qbit:8080/api/v2/torrents/add")
        watermark = "73" + "".join(f"{b ^ 0x73:02x}" for b in b"ehuatang@gmail.com!")
        with pytest.raises(SourceUnavailableError):
            await client.add_torrent_file(self.PAYLOAD, watermark)
        assert not add.called

    @respx.mock
    async def test_rejects_a_hash_that_is_not_40_hex(self, client: QBitClient) -> None:
        self._login()
        add = respx.post("http://qbit:8080/api/v2/torrents/add")
        with pytest.raises(DownloadError, match="40-hex info hash"):
            await client.add_torrent_file(self.PAYLOAD, "not-a-hash")
        assert not add.called


class TestMagnetTrackers:
    """Bare magnets must reach the client already carrying trackers."""

    HASH = "da39a3ee5e6b4b0d3255bfef95601890afd80709"
    BARE = f"magnet:?xt=urn:btih:{HASH}&dn=Release"

    def _client(self, trackers: tuple[str, ...]) -> QBitClient:
        return QBitClient(
            base_url="http://qbit:8080",
            username="admin",
            password="adminadmin",
            download_dir="/downloads",
            extra_trackers=trackers,
        )

    def test_trackers_are_appended_to_a_bare_magnet(self) -> None:
        client = self._client(("udp://tracker.example:1337/announce", "http://t2.example/announce"))
        result = client._with_trackers(self.BARE)
        assert result.startswith(self.BARE)
        assert "&tr=udp%3A%2F%2Ftracker.example%3A1337%2Fannounce" in result
        assert "&tr=http%3A%2F%2Ft2.example%2Fannounce" in result

    def test_a_magnet_that_already_names_trackers_is_left_alone(self) -> None:
        client = self._client(("udp://tracker.example:1337/announce",))
        original = self.BARE + "&tr=udp%3A%2F%2Fowner.example%3A80%2Fannounce"
        assert client._with_trackers(original) == original

    def test_without_configured_trackers_the_magnet_is_unchanged(self) -> None:
        assert self._client(())._with_trackers(self.BARE) == self.BARE

    @respx.mock
    async def test_add_magnet_sends_the_tracker_bearing_uri(self) -> None:
        respx.post("http://qbit:8080/api/v2/auth/login").mock(return_value=_modern_login_response())
        add = respx.post("http://qbit:8080/api/v2/torrents/add").mock(return_value=httpx.Response(200, text="Ok."))
        respx.post("http://qbit:8080/api/v2/torrents/addTrackers").mock(return_value=httpx.Response(200))
        client = self._client(("udp://tracker.example:1337/announce",))
        try:
            await client.add_magnet(self.BARE)
        finally:
            await client.aclose()
        sent = parse_qs(add.calls[0].request.content.decode())["urls"][0]
        # Announcing starts on add, rather than one announce cycle later.
        assert "tr=udp%3A%2F%2Ftracker.example%3A1337%2Fannounce" in sent
