"""Policy wrapper around Starlette's existing HTTP range transport."""

from pathlib import Path

from fastapi import HTTPException, Request, Response
from starlette.responses import FileResponse

from pixav.strm_resolver.access import authorize
from pixav.strm_resolver.playback import PlaybackService, safe_file


def service(request: Request) -> PlaybackService:
    settings = request.app.state.playback_settings
    authorize(request, settings.playback_tokens_file)
    pool = request.app.state.db_pool
    if pool is None:
        raise HTTPException(503, "database unavailable")
    if request.app.state.playback is None:
        request.app.state.playback = PlaybackService.configured(pool, settings)
    return request.app.state.playback


class ManagedFileResponse(Response):
    """Locks live for the actual send, including cancellation/disconnection."""

    def __init__(self, playback, video_id):
        super().__init__()
        self.playback = playback
        self.video_id = video_id

    async def __call__(self, scope, receive, send):
        async with self.playback.reader(self.video_id) as asset:
            if scope["method"] == "HEAD":
                response = Response(
                    headers={"Content-Length": str(asset["size_bytes"]), "Accept-Ranges": "bytes"},
                    media_type="video/mp4",
                )
            else:
                path = Path(asset["cache_path"])
                if not safe_file(self.playback.root, path) or path.stat().st_size != asset["size_bytes"]:
                    raise HTTPException(409, "playback cache changed; retry preparation")
                response = FileResponse(path, media_type="video/mp4")
            await response(scope, receive, send)


async def managed_stream(request: Request, video_id) -> Response:
    playback = service(request)
    if request.method != "HEAD":
        try:
            await playback.prepare(video_id)
        except HTTPException:
            raise
        except Exception:
            # External exception text can include provider URLs or credentials.
            raise HTTPException(502, "remote playback preparation failed") from None
    return ManagedFileResponse(playback, video_id)
