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
app.add_middleware(GZipMiddleware, minimum_size=1000)

# Mount Static Files & Templates
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


async def _fetch_media(client, message_id: int):
    """
    Fetch a message from the configured log channel and unwrap its media.
    Every route needs this, so it's centralized here instead of repeated
    four times like in the original short_code-based version.
    """
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

    return msg, media


@app.get("/watch/{message_id}")
async def watch_page(request: Request, message_id: int):
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
@app.get("/stream/{message_id}")
async def stream_file(request: Request, message_id: int):
    clients = session_manager.get_all_clients()
    client = clients[0]  # first client just to resolve the message/metadata
    msg, media = await _fetch_media(client, message_id)
    file_info = msg.file

    return await get_streaming_response(
        clients,  # ALL clients, for parallel downloading
        file=media,
        file_size=file_info.size,
        filename=file_info.name or f"file_{message_id}",
        mime_type=file_info.mime_type or "application/octet-stream",
        request=request
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

    try:
        tracks_info = await probe_tracks(client, msg, msg.file.size)
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
