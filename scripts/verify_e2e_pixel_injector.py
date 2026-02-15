import asyncio
import logging
import os
import subprocess
import time
import uuid
from datetime import datetime, timezone

from pixav.config import get_settings
from pixav.shared.db import create_pool
from pixav.shared.enums import TaskState, VideoStatus
from pixav.shared.models import Task, Video
from pixav.shared.redis_client import create_redis
from pixav.shared.repository import TaskRepository, VideoRepository

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("verify_pixel_injector")

# Override Redis/DB settings for localhost access if needed
# Assuming localhost mapping is correct as per docker-compose.yml
os.environ["PIXAV_DB_HOST"] = "localhost"
os.environ["PIXAV_REDIS_URL"] = "redis://localhost:6379/0"
# Note: pixav-redis has no password by default


async def main():
    settings = get_settings()
    logger.info("Starting Pixel-Injector Verification...")

    # 1. Prepare Resources
    pool = await create_pool(settings)
    redis = await create_redis(settings)
    task_repo = TaskRepository(pool)
    video_repo = VideoRepository(pool)

    try:
        # 2. Check if containers are running
        logger.info("Checking containers...")
        # Simple check via subprocess
        subprocess.run(["docker", "ps"], check=True)

        # 3. Create Dummy Video File
        video_filename = "test_e2e_pixel_injector.mp4"
        with open(video_filename, "wb") as f:
            f.write(b"0" * 1024 * 1024)  # 1MB dummy content
        logger.info(f"Created local dummy video: {video_filename}")

        # 4. Copy to Pixel Injector Container
        # 4. Copy to Pixel Injector Container
        # Use docker lib for robust finding
        import docker

        try:
            client = docker.from_env()
            container = None
            logger.info("Listing all containers from docker lib:")
            for c in client.containers.list(all=True):
                logger.info(f" - {c.name} ({c.id[:12]}) status={c.status}")
                # Match either dash or underscore variant
                if ("pixav-pixel-injector" in c.name or "pixav-pixel_injector" in c.name) and c.status == "running":
                    container = c

            if not container:
                logger.error("Could not find RUNNING pixav-pixel-injector container via docker lib")
                return

            logger.info(f"Using container: {container.name} ({container.id})")

            # Use docker cp via subprocess as put_archive is complex for simple file
            remote_path = f"/app/{video_filename}"
            res = subprocess.run(["docker", "cp", video_filename, f"{container.id}:{remote_path}"], capture_output=True)
            if res.returncode != 0:
                logger.error(f"Failed to copy file: {res.stderr.decode()}")
                return
            logger.info(f"Copied video to container: {container.name}:{remote_path}")

            # Update remote path variable for DB
            remote_path = f"/app/{video_filename}"

        except Exception as e:
            logger.error(f"Docker lib error: {e}")
            return

        # 5. Insert DB Records
        video_id = uuid.uuid4()
        task_id = uuid.uuid4()

        video = Video(
            id=video_id,
            title=f"E2E Pixel Injector Test {video_id}",
            source_url="http://test.com",
            magnet_uri="magnet:?xt=urn:btih:test",
            status=VideoStatus.DOWNLOADED,
            local_path=remote_path,  # Path INSIDE the container
            # quality=VideoQuality.HD_1080P, # Removed
            size_bytes=1024 * 1024,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            tags=["e2e_test"],
        )
        await video_repo.insert(video)
        logger.info(f"Inserted video {video_id} into DB")

        task = Task(
            id=task_id,
            video_id=video_id,
            type="download",
            state=TaskState.PENDING,
            queue_name="pixav:upload",
            local_path=remote_path,  # Important: Path inside container
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        await task_repo.insert(task)
        logger.info(f"Inserted task {task_id} into DB")

        # 6. Push to Upload Queue
        # We manually push the payload because Maxwell usually does this
        payload = {
            "task_id": str(task_id),
            "video_id": str(video_id),
            "local_path": remote_path,
            "queue_name": "pixav:upload",
            "retries": 0,
            "max_retries": 3,
        }
        await redis.rpush("pixav:upload", str(payload).replace("'", '"'))  # JSON string
        # Actually better to use json.dumps
        import json

        await redis.rpush("pixav:upload", json.dumps(payload))
        logger.info("Pushed task to pixav:upload queue")

        # 7. Monitor Task State
        logger.info("Monitoring task state (timeout 60s)...")
        start_time = time.time()
        while time.time() - start_time < 60:
            current_task = await task_repo.find_by_id(task_id)
            logger.info(f"Task state: {current_task.state}")

            if current_task.state == TaskState.UPLOADING:
                logger.info("SUCCESS: Task moved to UPLOADING state! (Worker picked it up)")
                break

            if current_task.state == TaskState.FAILED:
                logger.error(f"FAILURE: Task failed with message: {current_task.error_message}")
                # Failure might be expected if Google Photos login fails, but we want to verify it TRIED.
                # If error is related to upload/adb, then connectivity worked.
                if (
                    "adb" in str(current_task.error_message).lower()
                    or "upload" in str(current_task.error_message).lower()
                ):
                    logger.info(
                        "Task failed on upload step as expected (no Google Auth), but pipeline connectivity confirmed."
                    )
                    break
                return

            await asyncio.sleep(2)
        else:
            logger.error("Timeout waiting for task state change.")

    finally:
        await redis.aclose()
        await pool.close()
        # Cleanup
        if os.path.exists("test_e2e_pixel_injector.mp4"):
            os.remove("test_e2e_pixel_injector.mp4")


if __name__ == "__main__":
    asyncio.run(main())
