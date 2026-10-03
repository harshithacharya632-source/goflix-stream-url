from fastapi import FastAPI, Request, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import JSONResponse
from app.database.connection import settings, files_col, create_indexes
from app.streamer.manager import session_manager
from app.streamer.engine import get_streaming_response, get_remux_response
from app.streamer.probe import probe_tracks
from app.bot.main import register_handlers
from app.admin.routes import router as admin_router
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
    # Startup logic
    await create_indexes()
    await session_manager.start()
    register_handlers(session_manager.bot_client)
    logger.info("Application started")
    yield
    # Shutdown logic
    await session_manager.stop()
    logger.info("Application stopped")

app = FastAPI(title="Telegram Direct Media Link Generator", lifespan=lifespan)

# Include Routers
app.include_router(admin_router)

# Enable Compression
app.add_middleware(GZipMiddleware, minimum_size=1000)

# Mount Static Files & Templates
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/watch/{short_code}")
async def watch_page(request: Request, short_code: str):
    file_data = await files_col.find_one({"short_code": short_code})
    if not file_data:
        raise HTTPException(status_code=404, detail="Link not found or expired")
    
    return templates.TemplateResponse("watch.html", {
        "request": request,
        "file": file_data,
        "base_url": settings.BASE_URL.rstrip('/')
    })

@app.get("/dl/{short_code}")
@app.get("/stream/{short_code}")
async def stream_file(request: Request, short_code: str):
    file_data = await files_col.find_one({"short_code": short_code})
    if not file_data:
        raise HTTPException(status_code=404, detail="File not found")

    # Use all available clients for ultra-high-speed downloads
    clients = session_manager.get_all_clients()
    client = clients[0]  # Use first client for initial message fetch
    
    try:
        # Fetch the message that contains the media
        msg = await client.get_messages(file_data['chat_id'], ids=file_data['message_id'])
        if not msg or not msg.media:
            raise HTTPException(status_code=404, detail="Media no longer available on Telegram")
        
        file = msg.media
        # Some media types are nested
        if hasattr(file, 'document'):
            file = file.document
        elif hasattr(file, 'photo'):
            file = file.photo
            
    except Exception as e:
        logger.error(f"Error fetching file: {e}")
        raise HTTPException(status_code=500, detail="Error retrieving file from Telegram")

    return await get_streaming_response(
        clients,  # Pass ALL clients for parallel downloading
        file=file,
        file_size=file_data['file_size'],
        filename=file_data['filename'],
        mime_type=file_data['mime_type'],
        request=request
    )


# ─── Audio Track Discovery API ────────────────────────────────────────────────

@app.get("/api/tracks/{short_code}")
async def get_tracks(short_code: str):
    """Return available audio/video/subtitle tracks for a media file."""
    file_data = await files_col.find_one({"short_code": short_code})
    if not file_data:
        raise HTTPException(status_code=404, detail="File not found")
    
    # Check if tracks are already cached in the database (and not an error result)
    cached = file_data.get('tracks_info')
    if False and cached and not cached.get('error'):
        return JSONResponse(cached)
    
    # Need to probe the file
    client = session_manager.get_client()
    
    try:
        msg = await client.get_messages(file_data['chat_id'], ids=file_data['message_id'])
        if not msg or not msg.media:
            raise HTTPException(status_code=404, detail="Media no longer available")
        
        media = msg.media
        
        # Photos don't have audio tracks
        if hasattr(media, 'photo') and not hasattr(media, 'document'):
            return JSONResponse({
                "video_tracks": [],
                "audio_tracks": [],
                "subtitle_tracks": [],
                "has_multiple_audio": False
            })
        
        # Pass the message for download_media fallback, and media for iter_download
        tracks_info = await probe_tracks(client, msg, file_data['file_size'])
        
        # Only cache if no error
        if not tracks_info.get('error'):
            await files_col.update_one(
                {"short_code": short_code},
                {"$set": {"tracks_info": tracks_info}}
            )
        
        return JSONResponse(tracks_info)
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Track probing error: {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail="Failed to probe tracks")


# ─── Remux Streaming (Audio Track Selection) ──────────────────────────────────

@app.get("/remux/{short_code}")
async def remux_file(request: Request, short_code: str, audio: int = 0):
    """
    Stream media remuxed with a selected audio track.
    Uses FFmpeg to remux (no transcoding) into fragmented MP4.
    
    Query params:
        audio: Audio track index (0-based, default 0)
    """
    file_data = await files_col.find_one({"short_code": short_code})
    if not file_data:
        raise HTTPException(status_code=404, detail="File not found")
    
    # Use a single client for sequential download (FFmpeg needs sequential input)
    client = session_manager.get_client()
    
    try:
        msg = await client.get_messages(file_data['chat_id'], ids=file_data['message_id'])
        if not msg or not msg.media:
            raise HTTPException(status_code=404, detail="Media no longer available")
        
        file = msg.media
        if hasattr(file, 'document'):
            file = file.document
        elif hasattr(file, 'photo'):
            file = file.photo
        
        return await get_remux_response(
            client=client,
            file=file,
            file_size=file_data['file_size'],
            filename=file_data['filename'],
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
