#!/usr/bin/env python3
"""Idempotently backfill Untitled videos from local media or torrent metadata.

Dry-run is the default. The script never touches a torrent currently in
qBittorrent and removes only metadata-only torrents it created itself.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from pixav.config import get_settings
from pixav.media_loader.metadata import probe_media
from pixav.media_loader.qbittorrent import QBitClient, parse_extra_trackers
from pixav.shared.db import create_pool
from pixav.sht_probe.scoring import QualityScorer


async def _run(*, apply: bool, limit: int, report_path: str | None) -> int:  # noqa: C901
    settings = get_settings()
    pool = await create_pool(settings)
    qbit = QBitClient(
        settings.qbit_url,
        settings.qbit_user,
        settings.qbit_password,
        download_dir=settings.qbit_download_dir,
        local_download_dir=settings.download_dir,
        extra_trackers=parse_extra_trackers(settings.qbit_extra_trackers),
    )
    report: dict[str, Any] = {"mode": "apply" if apply else "dry-run", "resolved": [], "unresolved": []}
    try:
        rows = await pool.fetch(
            """
            SELECT id, title, magnet_uri, info_hash, local_path, metadata_json
              FROM videos
             WHERE btrim(title) = '' OR lower(btrim(title)) = 'untitled'
             ORDER BY created_at ASC
             LIMIT $1
            """,
            limit,
        )
        for row in rows:
            video_id = row["id"]
            local_path = str(row["local_path"] or "")
            title: str | None = None
            media: dict[str, Any] = {}
            source = ""
            created_torrent = False
            # Cleanup must target the hash qBittorrent actually received, which
            # fetch_metadata_name derives from the magnet. The DB info_hash is a
            # different value whenever the row is stale or empty, and cleaning up
            # by it leaves the metadata-only torrent parked in qBittorrent
            # forever, holding an active download slot.
            torrent_hash = ""
            try:
                if local_path and Path(local_path).is_file():
                    media = await probe_media(local_path)
                    title = Path(local_path).stem.strip()
                    source = "local_file"
                elif row["magnet_uri"]:
                    probe = await qbit.fetch_metadata_name(str(row["magnet_uri"]))
                    title, created_torrent, torrent_hash = probe
                    source = "torrent_metadata"
            except Exception as exc:
                report["unresolved"].append({"video_id": str(video_id), "reason": str(exc)})
            finally:
                if created_torrent and torrent_hash:
                    try:
                        await qbit.delete_torrent(torrent_hash, delete_files=False)
                    except Exception as exc:
                        report["unresolved"].append(
                            {"video_id": str(video_id), "reason": f"metadata torrent cleanup failed: {exc}"}
                        )

            if not title or title.casefold() == "untitled":
                if not any(item["video_id"] == str(video_id) for item in report["unresolved"]):
                    report["unresolved"].append({"video_id": str(video_id), "reason": "no usable local/torrent title"})
                continue
            size = int(media.get("size_bytes") or 0)
            score = QualityScorer().score(title, size_bytes=size)
            report["resolved"].append({"video_id": str(video_id), "title": title, "source": source})
            if apply:
                metadata_patch = {"media": media} if media else {"torrent": {"name": title}}
                await pool.execute(
                    """
                    UPDATE videos
                       SET title = $1,
                           quality_score = $2,
                           metadata_json = COALESCE(metadata_json, '{}'::jsonb) || $3::jsonb,
                           updated_at = now()
                     WHERE id = $4
                       AND (btrim(title) = '' OR lower(btrim(title)) = 'untitled')
                    """,
                    title,
                    score,
                    json.dumps(metadata_patch),
                    video_id,
                )

        output = json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True)
        print(output)
        if report_path:
            Path(report_path).write_text(output + "\n", encoding="utf-8")
        return 0 if not report["unresolved"] else 1
    finally:
        await qbit.aclose()
        await pool.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--report")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_run(apply=args.apply, limit=max(1, args.limit), report_path=args.report)))


if __name__ == "__main__":
    main()
