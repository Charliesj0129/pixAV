"""Detection for VPN egress isolation — the secondary line, never the first.

The primary defence is prevention. qBittorrent has no network namespace of its
own (``network_mode: service:gluetun``), so a tunnel outage makes downloading
structurally impossible rather than merely alarming. Nothing in this module is
allowed to become the thing that stops a leak, because a detector that can be
bypassed invites exactly the deployment that relies on it.

What it does instead is answer a question the kill switch cannot: *is the
traffic actually leaving where we think it is?* The worst failure mode is not a
tunnel that drops — that is loud — but one that comes up against the wrong exit,
or a topology change that quietly puts qBittorrent back on the bridge while
every health check stays green.

The measurement needs two observers because one cannot see both sides:

* the sentinel runs inside gluetun's namespace and reports the tunnel's public
  IP into Redis with a TTL, so a dead sentinel decays to "unknown" rather than
  leaving a stale "isolated" behind;
* the bridge-side worker observes its own public IP and compares.

Equal addresses mean qBittorrent's packets and the host's packets leave by the
same door — that is the leak. Both observers must use the same echo endpoint, or
they can disagree merely by answering in different address families.

Public IPs are recorded in Redis for the comparison but never written to logs;
the logs carry the verdict only.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import time
from dataclasses import dataclass

import httpx
import redis.asyncio as aioredis

from pixav.shared.metrics import record_vpn_probe_failure, set_vpn_egress_state

logger = logging.getLogger(__name__)

EGRESS_ISOLATED = "isolated"
EGRESS_LEAKING = "leaking"
EGRESS_UNKNOWN = "unknown"

EGRESS_STATES = (EGRESS_ISOLATED, EGRESS_LEAKING, EGRESS_UNKNOWN)

TUNNEL_SIDE = "tunnel"
REFERENCE_SIDE = "reference"


@dataclass(frozen=True)
class EgressStatus:
    """One comparison between the tunnel's exit and the host's exit."""

    state: str
    tunnel_ip: str | None
    reference_ip: str | None
    detail: str

    @property
    def leaking(self) -> bool:
        return self.state == EGRESS_LEAKING


def parse_public_ip(body: str) -> str | None:
    """Return the IP an echo service reported, or None if it is not one.

    Echo endpoints answer with a bare address and a trailing newline, but an
    intercepting proxy or a captive portal answers with an HTML page and HTTP
    200. Parsing through :mod:`ipaddress` rather than trusting the body keeps
    such a page from being compared as if it were an address, which would
    silently read as "isolated" — the reassuring answer.
    """
    candidate = body.strip()
    if not candidate or len(candidate) > 45:
        return None
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None


def classify_egress(tunnel_ip: str | None, reference_ip: str | None) -> EgressStatus:
    """Compare the two observations without deciding what to do about them."""
    if tunnel_ip is None or reference_ip is None:
        missing = TUNNEL_SIDE if tunnel_ip is None else REFERENCE_SIDE
        return EgressStatus(
            state=EGRESS_UNKNOWN,
            tunnel_ip=tunnel_ip,
            reference_ip=reference_ip,
            detail=f"no {missing}-side observation",
        )
    if tunnel_ip == reference_ip:
        return EgressStatus(
            state=EGRESS_LEAKING,
            tunnel_ip=tunnel_ip,
            reference_ip=reference_ip,
            detail="torrent egress matches the host's public address",
        )
    return EgressStatus(
        state=EGRESS_ISOLATED,
        tunnel_ip=tunnel_ip,
        reference_ip=reference_ip,
        detail="torrent egress differs from the host's public address",
    )


class PublicIpProbe:
    """Ask an echo endpoint for the public IP of the caller's namespace."""

    def __init__(self, echo_url: str, *, timeout: float = 10.0, side: str = REFERENCE_SIDE) -> None:
        self._echo_url = echo_url
        self._timeout = timeout
        self._side = side

    async def observe(self) -> str | None:
        """Return the observed IP, or None when the probe cannot answer."""
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(self._echo_url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            # Inside the tunnel this is the expected shape of a VPN outage: the
            # kill switch drops the probe too. It is not evidence of a leak.
            logger.warning("%s-side public IP probe failed: %s", self._side, exc)
            record_vpn_probe_failure(self._side)
            return None

        observed = parse_public_ip(response.text)
        if observed is None:
            logger.warning("%s-side echo endpoint did not return an IP address", self._side)
            record_vpn_probe_failure(self._side)
        return observed


class TunnelEgressReporter:
    """Sentinel side: publish the tunnel's public IP with an expiry."""

    def __init__(self, redis: aioredis.Redis, *, key: str, ttl_seconds: int) -> None:
        self._redis = redis
        self._key = key
        self._ttl_seconds = max(1, ttl_seconds)

    async def publish(self, ip: str) -> None:
        payload = json.dumps({"ip": ip, "observed_at": time.time()}, sort_keys=True)
        await self._redis.set(self._key, payload, ex=self._ttl_seconds)


class VpnEgressMonitor:
    """Bridge side: compare the tunnel's reported exit against our own."""

    def __init__(
        self,
        redis: aioredis.Redis,
        *,
        probe: PublicIpProbe,
        key: str,
        interval_seconds: int,
    ) -> None:
        self._redis = redis
        self._probe = probe
        self._key = key
        self._interval_seconds = max(1, interval_seconds)
        # A host can start this worker less than one interval after boot, when
        # ``time.monotonic()`` is also less than ``interval_seconds``.  Using 0
        # would then suppress the very first probe and, more importantly, leave
        # all three labelled metric series absent until the next interval.
        self._last_checked = float("-inf")
        self._last_status = EgressStatus(
            state=EGRESS_UNKNOWN,
            tunnel_ip=None,
            reference_ip=None,
            detail="not yet observed",
        )

    async def _read_tunnel_ip(self) -> str | None:
        raw = await self._redis.get(self._key)
        if raw is None:
            return None
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return None
        reported = payload.get("ip") if isinstance(payload, dict) else None
        return parse_public_ip(reported) if isinstance(reported, str) else None

    async def observe(self) -> EgressStatus:
        """Re-measure at most once per interval; otherwise return the last verdict.

        The caller is a worker loop that spins every few seconds. Probing an
        external endpoint at that rate would be its own kind of fingerprint, so
        throttling belongs here rather than in every call site.
        """
        now = time.monotonic()
        if now - self._last_checked < self._interval_seconds:
            return self._last_status

        self._last_checked = now
        tunnel_ip = await self._read_tunnel_ip()
        reference_ip = await self._probe.observe()
        status = classify_egress(tunnel_ip, reference_ip)
        set_vpn_egress_state(status.state, EGRESS_STATES)
        if status.leaking:
            logger.error("VPN egress leak: %s", status.detail)
        self._last_status = status
        return status
