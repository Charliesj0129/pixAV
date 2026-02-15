#!/usr/bin/env python3
"""Backfill embeddings for videos that are missing them."""

from __future__ import annotations

import asyncio
import logging
import signal
from types import FrameType

from pixav.config import get_settings
from pixav.shared.db import create_pool
from pixav.shared.embedding import EmbeddingService
from pixav.shared.repository import VideoRepository

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("backfill")

SHUTDOWN = False


def handle_sigint(sig: int, frame: FrameType | None) -> None:
    global SHUTDOWN
    logger.info("Shutdown signal received, finishing current batch...")
    SHUTDOWN = True


async def main() -> None:
    settings = get_settings()
    pool = await create_pool(settings)
    repo = VideoRepository(pool)
    service = EmbeddingService()  # Lazy loads model on first use

    logger.info("Initializing embedding model (this may take a moment)...")
    service.get_model()

    signal.signal(signal.SIGINT, handle_sigint)

    total_processed = 0

    try:
        while not SHUTDOWN:
            logger.info("Finding videos missing embeddings...")
            videos = await repo.find_missing_embeddings(limit=100)

            if not videos:
                logger.info("No more videos to process.")
                break

            processed_in_batch = 0
            for video in videos:
                if SHUTDOWN:
                    break

                text = f"{video.title} {' '.join(video.tags)}".strip()
                embedding = service.generate(text)
                await repo.update_embedding(video.id, embedding)
                processed_in_batch += 1

            total_processed += processed_in_batch
            logger.info(f"Batch complete. Total processed: {total_processed}")

    finally:
        await pool.close()
        logger.info("Done.")


if __name__ == "__main__":
    asyncio.run(main())
