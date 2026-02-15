#!/usr/bin/env python3
"""Interactive demo for Hybrid Search (Semantic + Keyword + RRF)."""

import asyncio
import logging
import uuid
from datetime import datetime, timezone

from pixav.config import get_settings
from pixav.shared.db import create_pool
from pixav.shared.embedding import EmbeddingService
from pixav.shared.models import Video, VideoStatus
from pixav.shared.repository import VideoRepository

logging.basicConfig(level=logging.ERROR)  # Quiet logs
logger = logging.getLogger("demo")
logger.setLevel(logging.INFO)


async def seed_data(repo: VideoRepository, service: EmbeddingService) -> None:
    logger.info("Checking for seed data...")
    test_data = [
        ("Ip Man 4: The Finale", ["action", "martial arts"], "magnet:?xt=urn:btih:ipman4"),
        ("Office Lady Working Late", ["OL", "office", "drama"], "magnet:?xt=urn:btih:ol1"),
        ("Yua Mikami Fan Meeting", ["idol", "event"], "magnet:?xt=urn:btih:yua1"),
        ("IPX-123 The Secret Secretary", ["secretary", "uniform"], "magnet:?xt=urn:btih:ipx123"),
        ("Tokyo Hot n0123", ["uncensored"], "magnet:?xt=urn:btih:tokyo"),
    ]

    for title, tags, magnet in test_data:
        # Check existing
        existing = await repo.find_by_magnet(magnet)
        if existing:
            # Ensure it has embedding and correct status
            text = f"{title} {' '.join(tags)}"
            vector = service.generate(text)
            await repo.update_embedding(existing.id, vector)
            if existing.status != VideoStatus.AVAILABLE:
                await repo.update_status(existing.id, VideoStatus.AVAILABLE)
            continue

        text = f"{title} {' '.join(tags)}"
        vector = service.generate(text)

        video = Video(
            id=uuid.uuid4(),
            title=title,
            magnet_uri=magnet,
            tags=tags,
            embedding=vector,
            status=VideoStatus.AVAILABLE,  # Must be available for search
            created_at=datetime.now(timezone.utc),
        )
        await repo.insert(video)
        logger.info(f"Inserted seed video: {title}")


async def main() -> None:
    settings = get_settings()
    pool = await create_pool(settings)
    repo = VideoRepository(pool)
    service = EmbeddingService()

    logger.info("Initializing model (first run downloads ~400MB)...")
    service.get_model()

    # Seeding
    await seed_data(repo, service)

    print("\n--- Hybrid Search Demo (RRF Fusion) ---")
    print("Try queries like:")
    print("  - 'action movie' (Semantic match for Ip Man)")
    print("  - 'IPX-123' (Exact keyword match)")
    print("  - 'office lady' (Semantic + Keyword match)")
    print("Type a query (or 'q' to quit)")

    try:
        while True:
            query = input("\nQuery: ").strip()
            if not query or query.lower() == "q":
                break

            vector = service.generate(query)

            # Since repository returns Video models (strict schema), we can't see the score easily here
            # but the order is determined by RRF score.
            results = await repo.search(query, vector, limit=5)

            print(f"\nTop {len(results)} Results for '{query}':")
            if not results:
                print("  (No results)")

            for i, video in enumerate(results, 1):
                print(f"  {i}. {video.title} (Tags: {video.tags})")

    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
