import math
from telethon import TelegramClient
from telethon.tl.types import Document, Photo
from telethon.errors import FloodWaitError
from fastapi import Request, HTTPException
from fastapi.responses import StreamingResponse, Response
import logging

logger = logging.getLogger(__name__)

CHUNK = 1024 * 1024  # Telegram's max request size

import os as _os
PER_CLIENT = max(1, int(_os.environ.get("STREAM_PARALLEL", "8")))   # parallel downloads per Telegram account
MAX_WINDOW = max(2, int(_os.environ.get("STREAM_WINDOW", "24")))    # MB buffered ahead per viewer
_sems = {}  # one limit per Telegram client, shared by ALL viewers (keeps Telegram from throttling us)


def _sem(client):
    import asyncio
    s = _sems.get(id(client))
    if s is None:
        s = _sems[id(client)] = asyncio.Semaphore(PER_CLIENT)
    return s


async def ultra_high_speed_streamer(clients: list, file, start: int, end: int, chunk_size: int = CHUNK):
    """
    Stream bytes [start, end] of a Telegram file in order, as fast as possible.

    - Many 1 MB chunks are downloaded IN PARALLEL (spread over every Telegram
      connection) and handed out in order, so the player gets data as fast as
      the connection allows instead of one slow round trip at a time.
    - `file` may be a list with one file handle per client (each Telegram account
      has its own handle for the same file).
    - Chunks are requested on 1 MB boundaries (Telegram's rule), then the
      first/last one is trimmed, so seeking to any byte works.
    - Only a small window of chunks is kept ahead, so a slow phone can't make the
      server run out of memory.
    """
    import asyncio
    from collections import deque

    n = len(clients)
    files = list(file) if isinstance(file, (list, tuple)) else [file] * n
    window = min(2 * PER_CLIENT * n, MAX_WINDOW)   # chunks downloading or waiting to be sent
    aligned = start - (start % chunk_size)

    async def fetch(i: int, pos: int) -> bytes:
        need = min(chunk_size, end + 1 - pos)
        last_err = None
        for attempt in range(4):
            ci = (i + attempt) % n   # a retry goes to the next connection
            try:
                async with _sem(clients[ci]):
                    buf = bytearray()
                    # limit=1 -> exactly one chunk (Telethon's limit counts chunks, not bytes)
                    async for part in clients[ci].iter_download(
                            files[ci], offset=pos, limit=1, chunk_size=chunk_size, request_size=chunk_size):
                        buf += part
                if len(buf) >= need:
                    return bytes(buf)
                last_err = IOError(f"short chunk at {pos}: {len(buf)}/{need}")
            except FloodWaitError as ex:
                # Telegram is rate-limiting this account -- almost always from
                # firing PER_CLIENT concurrent requests on a single connection
                # right after a seek. Previously this fell into the generic
                # except below and only waited ~0.3-1.2s total before giving
                # up, which is far shorter than a real FloodWait, so it kept
                # re-hitting the limit in a tight loop until it happened to
                # clear on its own -- that's what made a seek feel like it
                # took up to a minute to recover. Wait the actual time
                # Telegram asks for instead (capped so one slow chunk can't
                # hang the whole stream indefinitely).
                last_err = ex
                wait_for = min(ex.seconds, 30)
                logger.warning(f"FloodWait {ex.seconds}s on client {ci} at offset {pos} -- waiting {wait_for}s.")
                await asyncio.sleep(wait_for)
                continue
            except Exception as ex:  # CancelledError is not an Exception, so it passes through
                last_err = ex
            await asyncio.sleep(0.3 * (attempt + 1))
        raise last_err

    positions = iter(enumerate(range(aligned, end + 1, chunk_size)))
    tasks = deque()

    def fill():
        while len(tasks) < window:
            nxt = next(positions, None)
            if nxt is None:
                return
            i, pos = nxt
            tasks.append((pos, asyncio.ensure_future(fetch(i, pos))))

    sent = 0
    try:
        fill()
        while tasks:
            pos, task = tasks.popleft()
            data = await task
            fill()
            lo = max(start, pos) - pos
            hi = min(end + 1, pos + len(data)) - pos
            if hi > lo:
                yield data[lo:hi]
                sent += hi - lo
    finally:
        for _, task in tasks:
            task.cancel()
        logger.info(f"Stream closed after {sent / 1048576:.1f} MB")

async def media_streamer(clients: list[TelegramClient], file, start: int, end: int):
    """
    Parallel media streamer that fetches multiple chunks from Telegram simultaneously
    using multiple sessions for load balancing.
    """
    import asyncio
    total_to_send = end - start + 1
    bytes_sent = 0
    
    # Adaptive Chunk Sizing
    if total_to_send < 10 * 1024 * 1024:  # < 10MB
        chunk_size = 512 * 1024
    elif total_to_send < 100 * 1024 * 1024:  # < 100MB
        chunk_size = 1024 * 1024
    elif total_to_send < 500 * 1024 * 1024:  # < 500MB
        chunk_size = 2 * 1024 * 1024
    else:
        chunk_size = 4 * 1024 * 1024

    concurrency = 16 if total_to_send < 100 * 1024 * 1024 else 32
    client_count = len(clients)
    
    # Divide the requested range into smaller chunks for parallel fetching
    offsets = list(range(start, end + 1, chunk_size))
    
    for i in range(0, len(offsets), concurrency):
        batch = offsets[i:i + concurrency]
        
        # Helper to fetch a single chunk with retries and session rotation
        async def fetch_part(offset, task_idx):
            # Rotate clients per task for multi-session load balancing
            client = clients[task_idx % client_count]
            remaining = end - offset + 1
            current_chunk_size = min(chunk_size, remaining)
            
            for attempt in range(3):
                try:
                    chunk = b""
                    async for part in client.iter_download(file, offset=offset, limit=current_chunk_size):
                        chunk += part
                    return chunk
                except Exception as e:
                    logger.warning(f"Fetch failed (offset {offset}, attempt {attempt+1}): {e}")
                    if attempt == 2: return None
                    await asyncio.sleep(0.5)
            return None

        # Fetch batch in parallel with distributed clients
        tasks = [fetch_part(offset, i + idx) for idx, offset in enumerate(batch)]
        chunks = await asyncio.gather(*tasks)
        
        for chunk in chunks:
            if not chunk: continue
            
            if bytes_sent + len(chunk) > total_to_send:
                chunk = chunk[:total_to_send - bytes_sent]
            
            yield bytes(chunk)
            bytes_sent += len(chunk)
            
            if bytes_sent >= total_to_send:
                break

def get_range_header(request: Request, file_size: int):
    """Return (start, end) for the request, or None if the range can't be satisfied."""
    range_header = request.headers.get("Range")
    if not range_header:
        return 0, file_size - 1
    try:
        spec = range_header.replace("bytes=", "").split(",")[0].strip()
        start_str, end_str = spec.split("-")
        if start_str == "":                      # "-500" = the last 500 bytes
            start = max(file_size - int(end_str), 0)
            end = file_size - 1
        else:
            start = int(start_str)
            end = int(end_str) if end_str else file_size - 1
    except ValueError:
        return 0, file_size - 1
    end = min(end, file_size - 1)
    if start > end or start >= file_size:
        return None
    return start, end


def _content_disposition(filename: str, inline: bool) -> str:
    """Header-safe filename (emoji / non-English names used to crash the response)."""
    from urllib.parse import quote
    ascii_name = filename.encode("ascii", "ignore").decode().replace('"', "").replace("\\", "").strip() or "video"
    return f"{'inline' if inline else 'attachment'}; filename=\"{ascii_name}\"; filename*=UTF-8\'\'{quote(filename)}"


async def get_streaming_response(clients: list[TelegramClient], file, file_size: int, filename: str, mime_type: str, request: Request, inline: bool = False):
    rng = get_range_header(request, file_size)
    if rng is None:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{file_size}"})
    start, end = rng

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(end - start + 1),
        "Content-Type": mime_type,
        "Content-Disposition": _content_disposition(filename, inline),
        "Cache-Control": "public, max-age=31536000",
        "Access-Control-Allow-Origin": "*",
        "X-Accel-Buffering": "no",  # tell proxies (nginx) not to hold the stream back
    }
    partial = bool(request.headers.get("Range"))
    if partial:
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"

    return StreamingResponse(
        ultra_high_speed_streamer(clients, file, start, end),
        status_code=206 if partial else 200,
        headers=headers
    )


# ─── FFmpeg Remux Streaming (Audio Track Switching) ───────────────────────────

async def remux_streamer(client: TelegramClient, file, file_size: int, audio_track: int = 0):
    """
    Stream media through FFmpeg to select a specific audio track.
    
    Pipes: Telegram download → FFmpeg stdin → FFmpeg stdout → HTTP response
    
    FFmpeg remuxes (stream copy, no transcoding) the video + selected audio
    into fragmented MP4 for native browser playback.
    """
    import asyncio
    import subprocess
    import threading
    
    ffmpeg_cmd = [
        'ffmpeg',
        '-hide_banner',
        '-loglevel', 'error',
        '-i', 'pipe:0',                    # Read from stdin
        '-map', '0:v:0',                   # First video stream
        '-map', f'0:a:{audio_track}',      # Selected audio stream
        '-c', 'copy',                      # No transcoding
        '-f', 'mp4',                       # Output MP4 container
        '-movflags', 'frag_keyframe+empty_moov+default_base_moof',  # Fragmented MP4
        'pipe:1'                           # Write to stdout
    ]
    
    logger.info(f"Starting FFmpeg remux: audio_track={audio_track}, file_size={file_size/1024/1024:.1f}MB")
    
    process = None
    try:
        # Start FFmpeg using standard subprocess (bypasses Windows asyncio issues)
        process = subprocess.Popen(
            ffmpeg_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        
        # Task to feed Telegram data into FFmpeg stdin
        feed_error = None
        
        async def feed_ffmpeg():
            nonlocal feed_error
            bytes_fed = 0
            try:
                async for chunk in client.iter_download(file, offset=0, limit=file_size):
                    if not chunk:
                        continue
                    if process.poll() is not None:
                        break
                    # Write synchronously in a thread to avoid blocking the event loop
                    await asyncio.to_thread(process.stdin.write, bytes(chunk))
                    bytes_fed += len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:
                feed_error = e
                logger.error(f"Feed error: {e}")
            finally:
                try:
                    if process.poll() is None:
                        process.stdin.close()
                except Exception:
                    pass
                logger.info(f"Feed complete: {bytes_fed/1024/1024:.1f}MB fed to FFmpeg")
        
        # Start feeding in the background
        feed_task = asyncio.create_task(feed_ffmpeg())
        
        # Read FFmpeg stdout and yield chunks
        bytes_sent = 0
        read_size = 256 * 1024  # 256KB read chunks
        
        while True:
            # Read synchronously in a thread
            chunk = await asyncio.to_thread(process.stdout.read, read_size)
            if not chunk:
                break
            yield chunk
            bytes_sent += len(chunk)
        
        # Wait for feed task to finish
        await feed_task
        
        # Check for FFmpeg errors
        process.wait(timeout=5)
        
        if process.returncode != 0:
            stderr_out = process.stderr.read()
            logger.error(f"FFmpeg exited with code {process.returncode}: {stderr_out.decode(errors='replace')[:500]}")
        
        logger.info(f"Remux complete: {bytes_sent/1024/1024:.1f}MB sent to client")
        
    except GeneratorExit:
        logger.info("Client disconnected during remux")
    except Exception as e:
        logger.error(f"Remux streaming error: {e}")
    finally:
        # Clean up FFmpeg process
        if process and process.poll() is None:
            try:
                process.kill()
                process.wait()
            except Exception:
                pass


async def get_remux_response(
    client: TelegramClient,
    file,
    file_size: int,
    filename: str,
    audio_track: int = 0
):
    """
    Build a StreamingResponse that remuxes media with a selected audio track.
    
    Returns fragmented MP4 which plays natively in all browsers.
    Note: Content-Length is unknown (remuxed size differs from original).
    """
    # Force .mp4 extension for the download filename
    base_name = filename.rsplit('.', 1)[0] if '.' in filename else filename
    remux_filename = f"{base_name}.mp4"
    
    headers = {
        "Content-Type": "video/mp4",
        "Content-Disposition": f'inline; filename="{remux_filename}"',
        "Cache-Control": "no-cache",  # Don't cache remuxed streams (track-specific)
        "Access-Control-Allow-Origin": "*",
        "Accept-Ranges": "none",  # Seeking not supported in remux mode
    }
    
    return StreamingResponse(
        remux_streamer(client, file, file_size, audio_track),
        status_code=200,
        headers=headers,
        media_type="video/mp4"
    )
