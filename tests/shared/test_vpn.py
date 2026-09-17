"""Tests for VPN egress detection.

The failure this guards against is not "the tunnel dropped" — that is loud and
the kill switch already handles it. It is the quiet one: something answers the
probe, the comparison reads as fine, and nobody notices that qBittorrent's
packets have been leaving by the host's own address.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from pixav.config import Settings
from pixav.media_loader.worker import build_egress_monitor
from pixav.shared import vpn_sentinel
from pixav.shared.metrics import set_vpn_egress_state, vpn_egress_state
from pixav.shared.vpn import (
    EGRESS_ISOLATED,
    EGRESS_LEAKING,
    EGRESS_STATES,
    EGRESS_UNKNOWN,
    PublicIpProbe,
    TunnelEgressReporter,
    VpnEgressMonitor,
    classify_egress,
    parse_public_ip,
)

ECHO_URL = "https://echo.example/ip"
TUNNEL_KEY = "pixav:vpn:tunnel_ip"


class TestParsePublicIp:
    def test_accepts_a_bare_ipv4_with_trailing_newline(self) -> None:
        assert parse_public_ip("203.0.113.7\n") == "203.0.113.7"

    def test_accepts_ipv6(self) -> None:
        assert parse_public_ip("2001:db8::1") == "2001:db8::1"

    def test_rejects_an_html_page(self) -> None:
        """A captive portal answers HTTP 200 with a page, not an address."""
        assert parse_public_ip("<html><body>Sign in to continue</body></html>") is None

    def test_rejects_empty_body(self) -> None:
        assert parse_public_ip("   \n") is None

    def test_rejects_a_hostname(self) -> None:
        assert parse_public_ip("vpn.example.com") is None


class TestClassifyEgress:
    def test_identical_addresses_are_a_leak(self) -> None:
        status = classify_egress("203.0.113.7", "203.0.113.7")

        assert status.state == EGRESS_LEAKING
        assert status.leaking is True

    def test_different_addresses_are_isolated(self) -> None:
        status = classify_egress("198.51.100.4", "203.0.113.7")

        assert status.state == EGRESS_ISOLATED
        assert status.leaking is False

    def test_missing_tunnel_observation_is_unknown_not_isolated(self) -> None:
        """A dead sentinel must not read as proof of isolation."""
        status = classify_egress(None, "203.0.113.7")

        assert status.state == EGRESS_UNKNOWN
        assert status.leaking is False
        assert "tunnel" in status.detail

    def test_missing_reference_observation_is_unknown(self) -> None:
        status = classify_egress("198.51.100.4", None)

        assert status.state == EGRESS_UNKNOWN
        assert "reference" in status.detail

    def test_two_missing_observations_are_unknown(self) -> None:
        assert classify_egress(None, None).state == EGRESS_UNKNOWN


class TestPublicIpProbe:
    @respx.mock
    async def test_returns_the_observed_address(self) -> None:
        respx.get(ECHO_URL).mock(return_value=httpx.Response(200, text="203.0.113.7\n"))

        assert await PublicIpProbe(ECHO_URL).observe() == "203.0.113.7"

    @respx.mock
    async def test_transport_error_yields_no_observation(self) -> None:
        """Inside the tunnel this is what a VPN outage looks like, not a leak."""
        respx.get(ECHO_URL).mock(side_effect=httpx.ConnectError("no route"))

        assert await PublicIpProbe(ECHO_URL).observe() is None

    @respx.mock
    async def test_http_error_status_yields_no_observation(self) -> None:
        respx.get(ECHO_URL).mock(return_value=httpx.Response(503, text="203.0.113.7"))

        assert await PublicIpProbe(ECHO_URL).observe() is None

    @respx.mock
    async def test_non_address_body_yields_no_observation(self) -> None:
        respx.get(ECHO_URL).mock(return_value=httpx.Response(200, text="<html>portal</html>"))

        assert await PublicIpProbe(ECHO_URL).observe() is None


class TestTunnelEgressReporter:
    async def test_report_carries_an_expiry(self) -> None:
        """Without a TTL a dead sentinel leaves the last good answer standing."""
        redis = AsyncMock()

        await TunnelEgressReporter(redis, key=TUNNEL_KEY, ttl_seconds=600).publish("198.51.100.4")

        _args, kwargs = redis.set.await_args
        assert kwargs["ex"] == 600
        assert json.loads(redis.set.await_args.args[1])["ip"] == "198.51.100.4"


def _monitor(redis: Any, probe: Any, *, interval_seconds: int = 300) -> VpnEgressMonitor:
    return VpnEgressMonitor(redis, probe=probe, key=TUNNEL_KEY, interval_seconds=interval_seconds)


class TestVpnEgressMonitor:
    @pytest.fixture
    def redis(self) -> AsyncMock:
        client = AsyncMock()
        client.get.return_value = json.dumps({"ip": "198.51.100.4", "observed_at": 1.0})
        return client

    @pytest.fixture
    def probe(self) -> AsyncMock:
        stub = AsyncMock()
        stub.observe.return_value = "203.0.113.7"
        return stub

    async def test_reports_isolated_when_the_addresses_differ(self, redis: AsyncMock, probe: AsyncMock) -> None:
        status = await _monitor(redis, probe).observe()

        assert status.state == EGRESS_ISOLATED

    async def test_first_call_probes_even_when_host_just_booted(
        self, redis: AsyncMock, probe: AsyncMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A low monotonic clock must not suppress initial state/metrics."""
        monkeypatch.setattr("pixav.shared.vpn.time.monotonic", lambda: 1.0)

        status = await _monitor(redis, probe, interval_seconds=300).observe()

        assert status.state == EGRESS_ISOLATED
        probe.observe.assert_awaited_once()

    async def test_reports_a_leak_when_the_addresses_match(self, redis: AsyncMock, probe: AsyncMock) -> None:
        probe.observe.return_value = "198.51.100.4"

        status = await _monitor(redis, probe).observe()

        assert status.state == EGRESS_LEAKING

    async def test_expired_tunnel_report_is_unknown(self, redis: AsyncMock, probe: AsyncMock) -> None:
        """Redis returns None once the sentinel's TTL lapses."""
        redis.get.return_value = None

        status = await _monitor(redis, probe).observe()

        assert status.state == EGRESS_UNKNOWN

    async def test_corrupt_tunnel_report_is_unknown(self, redis: AsyncMock, probe: AsyncMock) -> None:
        redis.get.return_value = "not json"

        assert (await _monitor(redis, probe).observe()).state == EGRESS_UNKNOWN

    async def test_non_address_in_tunnel_report_is_unknown(self, redis: AsyncMock, probe: AsyncMock) -> None:
        redis.get.return_value = json.dumps({"ip": "vpn.example.com"})

        assert (await _monitor(redis, probe).observe()).state == EGRESS_UNKNOWN

    async def test_second_call_within_the_interval_does_not_reprobe(self, redis: AsyncMock, probe: AsyncMock) -> None:
        """The caller is a loop spinning every few seconds; the probe is not."""
        monitor = _monitor(redis, probe)

        first = await monitor.observe()
        second = await monitor.observe()

        assert probe.observe.await_count == 1
        assert second == first

    async def test_a_zero_interval_is_clamped_rather_than_probing_every_iteration(
        self, redis: AsyncMock, probe: AsyncMock
    ) -> None:
        """Misconfiguring the interval to 0 must not turn the worker loop into a
        request generator against a third-party endpoint."""
        monitor = _monitor(redis, probe, interval_seconds=0)

        await monitor.observe()
        await monitor.observe()

        assert probe.observe.await_count == 1


class TestEgressMetric:
    """Exactly one state series is 1 at a time."""

    def _states(self) -> dict[str, float]:
        return {state: vpn_egress_state.labels(state=state)._value.get() for state in EGRESS_STATES}

    def test_publishing_a_state_zeroes_the_others(self) -> None:
        set_vpn_egress_state(EGRESS_LEAKING, EGRESS_STATES)

        assert self._states() == {EGRESS_ISOLATED: 0.0, EGRESS_LEAKING: 1.0, EGRESS_UNKNOWN: 0.0}

    def test_recovering_from_a_leak_clears_the_leak_series(self) -> None:
        """Otherwise an alert on the leak series latches on forever."""
        set_vpn_egress_state(EGRESS_LEAKING, EGRESS_STATES)
        set_vpn_egress_state(EGRESS_ISOLATED, EGRESS_STATES)

        assert self._states() == {EGRESS_ISOLATED: 1.0, EGRESS_LEAKING: 0.0, EGRESS_UNKNOWN: 0.0}


class TestSentinelLoop:
    async def test_refuses_to_start_without_an_echo_endpoint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No endpoint means no observation; connecting to Redis to publish
        nothing forever would look healthy while reporting nothing."""
        create_redis = AsyncMock()
        monkeypatch.setattr("pixav.shared.vpn_sentinel.create_redis", create_redis)

        await vpn_sentinel.run_loop(Settings(vpn_egress_echo_url=""))

        create_redis.assert_not_awaited()


class TestBuildEgressMonitor:
    def test_no_echo_url_disables_the_detector(self) -> None:
        assert build_egress_monitor(Settings(vpn_egress_echo_url=""), AsyncMock()) is None

    def test_configured_echo_url_builds_a_monitor(self) -> None:
        settings = Settings(vpn_egress_echo_url=ECHO_URL)

        assert isinstance(build_egress_monitor(settings, AsyncMock()), VpnEgressMonitor)
