import asyncio
import logging
import os

from pixav.pixel_injector.adb import AdbConnection
from pixav.pixel_injector.redroid import DockerRedroidManager
from pixav.pixel_injector.uploader import UIAutomatorUploader
from pixav.shared.models import Account

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

async def main():
    # Attempt to test ADB login flow
    task_id = "test-login-1234"
    image = os.environ.get("PIXAV_REDROID_IMAGE", "redroid/redroid:14.0.0-latest")
    logger.info(f"Using Redroid image: {image}")
    
    redroid_manager = DockerRedroidManager(image=image)
    adb = AdbConnection()
    uploader = UIAutomatorUploader(adb=adb)
    
    # Fake account
    account = Account(email="ren0129b@gmail.com", password="Ren-4568357")  # noqa: S106
    
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
        await adb.pull("/data/local/tmp/screen.png", "screen.png")
        logger.info("Screenshot saved to screen.png")
        
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
