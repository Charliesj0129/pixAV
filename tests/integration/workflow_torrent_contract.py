"""Run inside the same isolated Docker network as the synthetic torrent client."""

import asyncio
import base64
import json
import sys
from pathlib import Path
from uuid import uuid4

from pixav.media_loader.qbittorrent import QBitClient
from pixav.shared.exceptions import TorrentOwnershipError


async def main():
    settings = json.load(sys.stdin)
    media = Path(settings["media"])
    async with QBitClient(
        base_url=settings["url"],
        username="admin",
        password=settings["password"],
        download_dir="/downloads",
        local_download_dir=str(media.parent),
        poll_interval=1,
        download_timeout=30,
        no_peer_grace_seconds=30,
    ) as client:
        version = await client.health_check()
        operation = str(uuid4())
        response = await client._request(
            "POST",
            "/api/v2/torrents/add",
            files={
                "torrents": ("synthetic.torrent", base64.b64decode(settings["torrent"]), "application/x-bittorrent")
            },
            data={"savepath": "/downloads", "tags": f"pixav-operation-{operation}"},
        )
        assert response.status_code == 200
        await asyncio.sleep(2)
        first = await client.reconcile_download(settings["hash"], operation)
        second = await client.reconcile_download(settings["hash"], operation)
        assert first == second == str(media)
        assert len(await client.list_torrent_hashes()) == 1
        try:
            await client.reconcile_download(settings["hash"], str(uuid4()))
        except TorrentOwnershipError:
            pass
        else:
            raise AssertionError("foreign owner was not rejected")
        assert media.read_bytes() == b"synthetic torrent contract artifact"
        print(f"qBittorrent {version}: owned artifact reuse and foreign ownership refusal passed")


if __name__ == "__main__":
    asyncio.run(main())
