import asyncio
import logging
import os
import uuid

import asyncpg
import redis.asyncio as aioredis

from pixav.pixel_injector.service import LocalPixelInjectorService
from pixav.shared.enums import TaskState, VideoStatus
from pixav.shared.models import Task
from pixav.shared.repository import AccountRepository, TaskRepository, VideoRepository

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

async def run_pipeline_demo():
    dsn = os.environ.get("PIXAV_DSN", "postgresql://pixav:pixav@localhost:5432/pixav")
    redis_url = os.environ.get("PIXAV_REDIS_URL", "redis://localhost:6379/0")
    
    logger.info("Connecting to Postgres & Redis...")
    pool = await asyncpg.create_pool(dsn)
    redis = aioredis.from_url(redis_url, decode_responses=True)
    
    video_repo = VideoRepository(pool)
    task_repo = TaskRepository(pool)
    account_repo = AccountRepository(pool)
    
    video_id = uuid.uuid4()
    task_id = uuid.uuid4()
    
    try:
        logger.info(f"[Step 1] Crawler discovered video 'Demo Video {video_id}'")
        await pool.execute(
            """INSERT INTO videos (id, title, magnet_uri, status, local_path) 
               VALUES ($1, $2, $3, $4, $5)""",
            video_id, f"Demo Video {video_id}", "magnet:?xt=urn:btih:DEMO", VideoStatus.DOWNLOADED.value, "/tmp/fake_video.mp4"  # noqa: S108
        )
        
        # Touch a fake file so local_path validation passes
        with open("/tmp/fake_video.mp4", "w") as f:  # noqa: S108
            f.write("fake video content")
            
        logger.info("[Step 2] Queuing upload task")
        # Find the account we created
        account = await pool.fetchrow("SELECT id, email FROM accounts WHERE email = 'ren0129b@gmail.com'")
        if not account:
            logger.error("Could not find the seeded account. Ensure seed_password.py was run.")
            return
            
        account_id = account["id"]
        logger.info(f"[Step 3] Dispatcher assigned account {account['email']} to Task")
        
        await pool.execute(
            """INSERT INTO tasks (id, video_id, account_id, queue_name, state)
               VALUES ($1, $2, $3, $4, $5)""",
            task_id, video_id, account_id, "pixav:upload:demo", TaskState.UPLOADING.value
        )
        
        task = Task(
            id=task_id,
            video_id=video_id,
            account_id=account_id,
            queue_name="pixav:upload:demo",
            state=TaskState.UPLOADING,
            local_path="/tmp/fake_video.mp4"  # noqa: S108
        )
        
        logger.info("[Step 4] Pixel Injector executing upload...")
        service = LocalPixelInjectorService()
        
        # We pass the account object we fetched
        account_obj = await account_repo.find_by_id(account_id)
        
        result = await service.process_task(task, account_obj)
        
        if result.state == TaskState.COMPLETE and result.share_url:
            logger.info(f"[Step 5] Upload successful! Share URL: {result.share_url}")
            await video_repo.update_upload_result(video_id, share_url=result.share_url)
            await task_repo.update_state(task_id, TaskState.COMPLETE)
            
            # Check DB State
            final_video = await video_repo.find_by_id(video_id)
            logger.info(f"[Verification] Final Video Status: {final_video.status.value}, URL: {final_video.share_url}")
        else:
            logger.error(f"Task processing failed: {result.error_message}")
            
    except Exception as e:
        logger.error(f"Pipeline error: {e}")
    finally:
        await pool.close()
        await redis.aclose()
        if os.path.exists("/tmp/fake_video.mp4"):  # noqa: S108
            os.remove("/tmp/fake_video.mp4")  # noqa: S108

if __name__ == "__main__":
    asyncio.run(run_pipeline_demo())
