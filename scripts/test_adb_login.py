import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from pixav.config import get_settings
from pixav.pixel_injector.adb import AdbConnection
from pixav.pixel_injector.redroid import DockerRedroidManager
from pixav.pixel_injector.uploader import UIAutomatorUploader
from pixav.shared.db import create_pool
from pixav.shared.models import Account

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


async def _load_account(settings) -> Account:
    """Use explicit runtime secrets, or select a credentialed DB account."""
    email = os.environ.get("PIXAV_SEED_ACCOUNT_EMAIL", "").strip()
    password = os.environ.get("PIXAV_SEED_ACCOUNT_PASSWORD", "").strip()
    if email or password:
        if not email or not password:
            raise SystemExit("both PIXAV_SEED_ACCOUNT_EMAIL and PIXAV_SEED_ACCOUNT_PASSWORD are required")
        return Account(email=email, password=password)

    pool = await create_pool(settings)
    try:
        row = await pool.fetchrow("""
            SELECT *
              FROM accounts
             WHERE status = 'active'
               AND password IS NOT NULL
               AND btrim(password) <> ''
             ORDER BY last_used_at ASC NULLS FIRST, created_at ASC
             LIMIT 1
            """)
    finally:
        await pool.close()

    if row is None:
        raise SystemExit("no active database account with a password is available")
    logger.info("Using an existing credentialed database account; identity is intentionally redacted")
    return Account.model_validate(dict(row))


async def main():
    # Attempt to test ADB login flow
    task_id = "test-login-1234"
    settings = get_settings()
    logger.info("Using Redroid profile: %s", settings.redroid_profile)

    # The manual experiment must exercise the same profile as the worker. Using
    # DockerRedroidManager(image=...) here silently skipped every ro.* override
    # and the profile readiness checks, so a successful login said nothing about
    # the Pixel XL design under test.
    redroid_manager = DockerRedroidManager.from_profile_name(
        settings.redroid_profile,
        profiles_path=settings.redroid_profiles_path or None,
        adb_host=settings.redroid_adb_host,
        adb_port_start=settings.redroid_adb_port_start,
        network=settings.redroid_network or None,
    )
    adb = AdbConnection()
    uploader = UIAutomatorUploader(adb=adb)

    # Credentials come from explicit runtime environment variables or the
    # production account table. They are never copied into source or logs.
    account = await _load_account(settings)

    session = None
    try:
        logger.info("Creating Redroid container...")
        session = await redroid_manager.create(task_id)

        logger.info(f"Waiting for container {session.container_id} to be ready...")
        ready = await redroid_manager.wait_ready(session.container_id, timeout=120)
        if not ready:
            logger.error("Container failed to become ready.")
            return

        logger.info("Starting login automation...")
        await uploader.login(session, account)

        logger.info("Login automation finished executing.")

        # Test taking a screenshot to see where it ended up
        logger.info("Taking screenshot...")
        await adb.shell("screencap -p /data/local/tmp/screen.png")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        evidence_dir = Path(os.environ.get("PIXAV_PHASE0_EVIDENCE_DIR", "data/phase0/redroid-login"))
        evidence_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        screenshot = evidence_dir / f"screen-{stamp}.png"
        await adb.pull("/data/local/tmp/screen.png", str(screenshot))
        screenshot.chmod(0o600)
        logger.info("Screenshot saved to the private Phase 0 evidence directory")

    except Exception as e:
        logger.error(f"Test failed: {e}")
        if session:
            logger.info("Dumping container logs:")
            os.system(f"docker logs --tail 50 {session.container_id}")  # noqa: S605
    finally:
        if session:
            logger.info("Destroying container...")
            await redroid_manager.destroy(session.container_id)


if __name__ == "__main__":
    asyncio.run(main())
