"""The tunnel-side half of VPN egress detection.

Runs inside gluetun's network namespace — the same namespace qBittorrent uses —
and reports what the public internet sees as the source of that traffic. It is
deliberately the only pixAV process that lives there, and it does nothing but
observe: it holds no torrent state, and cannot pause or resume anything.

Its report is written to Redis with a TTL slightly longer than the observation
interval. That expiry is the point. A sentinel that dies, or a tunnel that stops
carrying traffic, must make the comparison decay to "unknown" rather than leave
the last reassuring answer standing indefinitely.

See :mod:`pixav.shared.vpn` for the comparison this feeds.
"""

from __future__ import annotations

import asyncio
import logging

from pixav.config import Settings, get_settings
from pixav.shared.redis_client import create_redis
from pixav.shared.vpn import TUNNEL_SIDE, PublicIpProbe, TunnelEgressReporter

logger = logging.getLogger(__name__)

# The report must outlive one interval so an ordinary slow probe does not read
# as a dead sentinel, but must not outlive two, or a genuinely dead sentinel
# stays invisible for longer than one missed observation.
_TTL_MULTIPLIER = 2


async def run_loop(settings: Settings, *, health_state: object = None) -> None:
    """Publish the tunnel's public IP once per interval, forever."""
    if not settings.vpn_egress_echo_url:
        logger.error("vpn_egress_echo_url is empty; the sentinel has nothing to observe")
        return

    interval = max(1, settings.vpn_egress_interval_seconds)
    redis = await create_redis(settings)
    probe = PublicIpProbe(settings.vpn_egress_echo_url, side=TUNNEL_SIDE)
    reporter = TunnelEgressReporter(
        redis,
        key=settings.vpn_egress_redis_key,
        ttl_seconds=interval * _TTL_MULTIPLIER,
    )

    mark_ready = getattr(health_state, "mark_ready", None)
    if callable(mark_ready):
        mark_ready()

    logger.info("vpn sentinel started, observing every %d seconds", interval)
    try:
        while True:
            observed = await probe.observe()
            if observed is not None:
                # The address itself never reaches the logs; only the fact that
                # an observation happened.
                await reporter.publish(observed)
                logger.debug("published tunnel egress observation")
            await asyncio.sleep(interval)
    finally:
        await redis.aclose()


def main() -> None:
    """Entry point for ``python -m pixav.shared.vpn_sentinel``."""
    from pixav.shared.health import HealthState, create_health_app
    from pixav.shared.health_server import run_with_health

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    health_state = HealthState("vpn_sentinel", stale_after_seconds=settings.heartbeat_stale_seconds)
    health_app = create_health_app("vpn_sentinel", state=health_state)

    async def _run() -> None:
        await run_with_health(
            worker_coro=run_loop(settings, health_state=health_state),
            health_app=health_app,
            host=settings.health_host,
            port=settings.vpn_sentinel_health_port,
            health_state=health_state,
            heartbeat_interval_seconds=settings.heartbeat_interval_seconds,
        )

    asyncio.run(_run())


if __name__ == "__main__":
    main()
