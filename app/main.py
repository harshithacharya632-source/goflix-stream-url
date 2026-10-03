from fastapi import FastAPI, Request, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import JSONResponse
from app.database.connection import settings
from app.streamer.manager import session_manager
from app.streamer.engine import get_streaming_response, get_remux_response
from app.streamer.probe import probe_tracks
from fastapi.middleware.gzip import GZipMiddleware
import uvicorn
import asyncio
import logging
import os
import random
import time
import sys

# Configure Windows Event Loop Policy for subprocess support
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

# Configure Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup logic -- just the Telegram client(s), no bot commands, no DB.
    await session_manager.start()
    logger.info("Application started")
    yield
    # Shutdown logic
    await session_manager.stop()
    logger.info("Application stopped")

app = FastAPI(title="Goflix Stream URL", lifespan=lifespan)

# Enable Compression
class _GZipNotMedia(GZipMiddleware):
    """Gzip pages/JSON only. Gzipping video wastes CPU, breaks Content-Length and stops downloads from resuming."""
    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith(("/stream", "/dl", "/remux")):
            await self.app(scope, receive, send)
            return
        await super().__call__(scope, receive, send)


app.add_middleware(_GZipNotMedia, minimum_size=1000)

# Mount Static Files & Templates
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


# Telegram lookups cost a round trip on EVERY request (the browser makes several
# while buffering/seeking), so remember a message for a few minutes.
_MSG_TTL = 300
_msg_cache = {}  # message_id -> (expires_at, msg, media)


async def _fetch_media(client, message_id: int):
    """
    Fetch a message from the configured log channel and unwrap its media.
    Every route needs this, so it's centralized here instead of repeated
    four times like in the original short_code-based version.
    """
    hit = _msg_cache.get(message_id)
    if hit and hit[0] > time.time():
        return hit[1], hit[2]

    try:
        msg = await client.get_messages(settings.CHANNEL_ID, ids=message_id)
    except Exception as e:
        logger.error(f"Error fetching message {message_id}: {e}")
        raise HTTPException(status_code=500, detail="Error retrieving file from Telegram")

    if not msg or not msg.media:
        raise HTTPException(status_code=404, detail="File not found in log channel")

    media = msg.media
    if hasattr(media, 'document'):
        media = media.document
    elif hasattr(media, 'photo'):
        media = media.photo

    if len(_msg_cache) >= 512:  # keep memory bounded
        _msg_cache.pop(next(iter(_msg_cache)))
    _msg_cache[message_id] = (time.time() + _MSG_TTL, msg, media)
    return msg, media


# Main bot links look like /watch/{id}/{filename}?hash=XXXXXX
# (filename is cosmetic; the plain /watch/{id} form also works)
@app.get("/watch/{message_id}")
@app.get("/watch/{message_id}/{filename:path}")
async def watch_page(request: Request, message_id: int, filename: str | None = None):
    client = session_manager.get_client()
    msg, media = await _fetch_media(client, message_id)

    file_info = msg.file  # Telethon's unified size/name/mime helper
    file_data = {
        "filename": file_info.name or f"file_{message_id}",
        "file_size": file_info.size,
        "mime_type": file_info.mime_type or "application/octet-stream",
        "created_at": msg.date,
    }

    return templates.TemplateResponse("watch.html", {
        "request": request,
        "file": file_data,
        "message_id": message_id,
        "base_url": settings.BASE_URL.rstrip('/')
    })


@app.get("/dl/{message_id}")
@app.get("/dl/{message_id}/{filename:path}")
@app.get("/stream/{message_id}")
@app.get("/stream/{message_id}/{filename:path}")
async def stream_file(request: Request, message_id: int, filename: str | None = None):
    clients = session_manager.get_all_clients()
    client = clients[0]  # first client just to resolve the message/metadata
    msg, media = await _fetch_media(client, message_id)
    file_info = msg.file

    response = await get_streaming_response(
        clients,  # ALL clients, for parallel downloading
        file=media,
        file_size=file_info.size,
        filename=file_info.name or f"file_{message_id}",
        mime_type=file_info.mime_type or "application/octet-stream",
        request=request,
        inline=request.url.path.startswith("/stream")
    )
    if hasattr(response, "body_iterator"):
        response.body_iterator = _count_stream(response.body_iterator)
    return response


# ─── Server load (shown on the watch page) ───────────────────────────────────

_started_at = time.time()
_probe_cache = {}  # message_id -> (expires_at, tracks_info)
_active_streams = 0


async def _count_stream(body):
    """Wrap a streaming body so we know how many viewers are connected."""
    global _active_streams
    _active_streams += 1
    try:
        async for chunk in body:
            yield chunk
    finally:
        _active_streams -= 1


def _mem_percent():
    try:  # container limit first (cgroup v2), then whole machine
        used = int(open("/sys/fs/cgroup/memory.current").read())
        limit = open("/sys/fs/cgroup/memory.max").read().strip()
        if limit != "max":
            return round(used / int(limit) * 100, 1)
    except Exception:
        pass
    try:
        info = {l.split(":")[0]: int(l.split()[1]) for l in open("/proc/meminfo")}
        return round((1 - info["MemAvailable"] / info["MemTotal"]) * 100, 1)
    except Exception:
        return None


# "Active streams" on the page: a number between 1 and 30 that reshuffles every
# 4 seconds (same for every visitor) PLUS the real number of live streams.
# Set STREAM_RANGE = None to show only the real count.
STREAM_RANGE = (1, 30)


def _watching_now():
    if not STREAM_RANGE:
        return _active_streams
    window = int(time.time() // 4)
    return random.Random(window).randint(*STREAM_RANGE) + _active_streams


@app.get("/api/load")
async def server_load():
    try:
        cpu = os.getloadavg()[0] / (os.cpu_count() or 1) * 100
    except (OSError, AttributeError):
        cpu = 0.0
    return JSONResponse(
        {
            "cpu": round(min(cpu, 100), 1),
            "mem": _mem_percent(),
            "streams": _active_streams,
            "watching": _watching_now(),
            "uptime": int(time.time() - _started_at),
        },
        headers={"Cache-Control": "no-store"},
    )


# ─── Audio Track Discovery API ────────────────────────────────────────────────

@app.get("/api/tracks/{message_id}")
async def get_tracks(message_id: int):
    """Return available audio/video/subtitle tracks for a media file."""
    client = session_manager.get_client()
    msg, media = await _fetch_media(client, message_id)

    # Photos don't have audio tracks
    if hasattr(msg.media, 'photo') and not hasattr(msg.media, 'document'):
        return JSONResponse({
            "video_tracks": [],
            "audio_tracks": [],
            "subtitle_tracks": [],
            "has_multiple_audio": False
        })

    cached = _probe_cache.get(message_id)
    if cached and cached[0] > time.time():
        return JSONResponse(cached[1])

    try:
        tracks_info = await probe_tracks(client, msg, msg.file.size)
        if not tracks_info.get("error") and tracks_info.get("audio_tracks"):
            _probe_cache[message_id] = (time.time() + 3600, tracks_info)
            if len(_probe_cache) > 512:
                _probe_cache.pop(next(iter(_probe_cache)))
        return JSONResponse(tracks_info)
    except Exception as e:
        logger.error(f"Track probing error: {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail="Failed to probe tracks")


# ─── Remux Streaming (Audio Track Selection) ──────────────────────────────────

@app.get("/remux/{message_id}")
async def remux_file(request: Request, message_id: int, audio: int = 0):
    """
    Stream media remuxed with a selected audio track.
    Uses FFmpeg to remux (no transcoding) into fragmented MP4.

    Query params:
        audio: Audio track index (0-based, default 0)
    """
    # A single client for sequential download -- FFmpeg needs sequential input.
    client = session_manager.get_client()
    msg, media = await _fetch_media(client, message_id)
    file_info = msg.file

    try:
        return await get_remux_response(
            client=client,
            file=media,
            file_size=file_info.size,
            filename=file_info.name or f"file_{message_id}",
            audio_track=audio
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Remux error: {e}")
        raise HTTPException(status_code=500, detail="Error starting remux stream")


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 8000))
    # use loop="asyncio" to prevent Uvicorn from forcing SelectorEventLoop on Windows,
    # which causes NotImplementedError with asyncio.create_subprocess_exec
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, reload=True, loop="asyncio")
