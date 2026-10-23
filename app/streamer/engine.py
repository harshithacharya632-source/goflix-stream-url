import asyncio
import logging
import os as _os
import time
from collections import OrderedDict, deque

from telethon import TelegramClient
from telethon.tl.types import Document, Photo
from telethon.errors import FloodWaitError
from fastapi import Request, HTTPException
from fastapi.responses import StreamingResponse, Response

logger = logging.getLogger(__name__)

_MB = 1024 * 1024

# Telethon never sends more than 512 KB in one request (its MAX_CHUNK_SIZE). Asking for 1 MB
# chunks looked right but made Telethon quietly split EVERY chunk into two requests sent one
# after the other (the "Starting indirect file download in chunks of 524288 ... stride 1048576"
# lines in the log): twice the waiting per chunk, plus extra CPU copying bytes. With 512 KB chunks
# each chunk is exactly one request ("Starting direct file download").
CHUNK = 512 * 1024

PER_CLIENT = max(1, int(_os.environ.get("STREAM_PARALLEL", "16")))   # requests in flight per Telegram account (1 request = 1 chunk)
MAX_WINDOW = max(2, int(_os.environ.get("STREAM_WINDOW", "24")) * _MB // CHUNK)        # most chunks buffered ahead per viewer (env value is in MB)
START_WINDOW = max(1, int(_os.environ.get("STREAM_START_WINDOW", "6")) * _MB // CHUNK) # chunks a NEW request asks for at first (env value is in MB)
CACHE_BYTES = max(0, int(_os.environ.get("STREAM_CACHE_MB", "64"))) * _MB  # 0 = no chunk cache

_sems = {}         # one limit per Telegram client, shared by ALL viewers (keeps Telegram from throttling us)
_flood_until = {}  # id(client) -> time.monotonic() when its FloodWait ends
_cache = OrderedDict()   # (file id, chunk size, chunk no.) -> bytes   recently downloaded chunks (LRU)
_cache_size = 0
_slots = {}        # same key -> _Slot   chunks that are downloading RIGHT NOW


def _sem(client):
    s = _sems.get(id(client))
    if s is None:
        s = _sems[id(client)] = asyncio.Semaphore(PER_CLIENT)
    return s


def _cache_put(key, data):
    global _cache_size
    if not CACHE_BYTES or len(data) > CACHE_BYTES:
        return
    old = _cache.pop(key, None)
    if old is not None:
        _cache_size -= len(old)
    _cache[key] = data
    _cache_size += len(data)
    while _cache_size > CACHE_BYTES and _cache:
        _, gone = _cache.popitem(last=False)
        _cache_size -= len(gone)


class _Slot:
    """One chunk being downloaded. Every request that wants this chunk shares the
    same download; it is only cancelled when the LAST request that wanted it leaves."""
    __slots__ = ("task", "refs")

    def __init__(self, task):
        self.task = task
        self.refs = 1


def _want(key, make):
    """Get chunk `key`: from the cache, from a download already running (shared), or start one.
    Returns (awaitable, slot). Call _drop(key, slot) when this request no longer needs it."""
    data = _cache.get(key)
    if data is not None:
        _cache.move_to_end(key)
        fut = asyncio.get_running_loop().create_future()
        fut.set_result(data)
        return fut, None
    slot = _slots.get(key)
    if slot is not None and slot.task.done() and (slot.task.cancelled() or slot.task.exception()):
        slot = None   # a dead download must not be handed to a new request
    if slot is None:
        task = asyncio.ensure_future(make())
        slot = _slots[key] = _Slot(task)

        def _finished(t, key=key, slot=slot):
            if _slots.get(key) is slot:
                del _slots[key]
            if not t.cancelled():
                t.exception()   # mark as retrieved so asyncio doesn't log "never retrieved"
        task.add_done_callback(_finished)
    else:
        slot.refs += 1
    return slot.task, slot


def _drop(key, slot):
    if slot is None:
        return
    slot.refs -= 1
    if slot.refs <= 0 and not slot.task.done():
        if _slots.get(key) is slot:
            del _slots[key]   # forget it NOW so a new request can't pick up a task that is being cancelled
        slot.task.cancel()


def _pick(clients, base):
    """Choose a Telegram connection that is not rate-limited right now.
    Returns (index, seconds_to_wait_first)."""
    n, now = len(clients), time.monotonic()
    for k in range(n):
        ci = (base + k) % n
        if _flood_until.get(id(clients[ci]), 0) <= now:
            return ci, 0.0
    ci = min(range(n), key=lambda i: _flood_until.get(id(clients[i]), 0))   # all limited: wait for the first to free up
    return ci, max(0.0, _flood_until[id(clients[ci])] - now)


async def ultra_high_speed_streamer(clients: list, file, start: int, end: int, chunk_size: int = CHUNK,
                                    file_size: int = None):
    """
    Stream bytes [start, end] of a Telegram file in order.

    Why a browser stutters where VLC does not: VLC opens ONE request and reads it
    straight through. A browser opens several -- the start, the end of the file
    (index / moov atom), the resume position, then more every time you seek -- and
    drops them again. If each of those fired a big burst of downloads at Telegram,
    the burst of a request that was already abandoned kept the connection busy and
    the request the player actually needed waited in line behind it. So:

    - A new request starts with a SMALL burst (START_WINDOW chunks) and doubles it
      while the reader is really waiting on the download, up to MAX_WINDOW. Full
      speed is reached after a couple of chunks, but a request the browser drops
      early has asked Telegram for far less than before (a few MB, not a big burst).
    - A chunk that is being downloaded is SHARED by every request that wants it,
      and downloaded chunks are kept in a small LRU cache. The browser's repeated
      requests for the same part of the file (start, end, a little rewind) cost
      nothing and never download anything twice.
    - A chunk is cancelled the moment the last request that wanted it goes away.
    - Chunks are requested on 512 KB boundaries (never crossing Telegram's 1 MB line), the first/last is
      trimmed, so seeking to any byte works. `file` may be a list with one handle per
      client (each Telegram account has its own handle for the same file).
    """
    n = len(clients)
    chunk_size = min(chunk_size, CHUNK)                 # a bigger value silently makes Telethon download twice as slowly
    files = list(file) if isinstance(file, (list, tuple)) else [file] * n
    cap = max(2, min(2 * PER_CLIENT * n, MAX_WINDOW))   # most chunks downloading or waiting to be sent
    win = min(cap, START_WINDOW)
    fid = getattr(files[0], "id", None)                 # Telegram's file id: identical for every account
    if fid is None:
        fid = object()                                  # unknown file: never share anything with other requests
    aligned = start - (start % chunk_size)

    async def download(key, idx: int, pos: int) -> bytes:
        total = file_size if file_size else end + 1
        need = min(chunk_size, total - pos)
        last_err = None
        for attempt in range(5):
            ci, delay = _pick(clients, idx + attempt)    # a retry goes to another connection
            if delay:
                await asyncio.sleep(min(delay, 30))
            try:
                async with _sem(clients[ci]):
                    parts = []
                    # limit=1 -> exactly one chunk (Telethon's limit counts chunks, not bytes)
                    async for part in clients[ci].iter_download(
                            files[ci], offset=pos, limit=1, chunk_size=chunk_size, request_size=chunk_size):
                        parts.append(part)
                data = parts[0] if len(parts) == 1 else b"".join(parts)
                if len(data) >= need:
                    if not isinstance(data, bytes):
                        data = bytes(data)
                    if file_size and len(data) == min(chunk_size, file_size - pos):
                        _cache_put(key, data)            # only COMPLETE chunks are worth keeping
                    return data
                last_err = IOError(f"short chunk at {pos}: {len(data)}/{need}")
            except FloodWaitError as ex:
                # Telegram is rate-limiting this account. Park it and let the retry use
                # another account straight away; with only one account _pick() waits out
                # the real time Telegram asked for (capped so one chunk can't hang forever).
                last_err = ex
                _flood_until[id(clients[ci])] = time.monotonic() + ex.seconds
                logger.warning(f"FloodWait {ex.seconds}s on client {ci} at offset {pos}")
                continue
            except Exception as ex:  # CancelledError is not an Exception, so it passes through
                last_err = ex
            await asyncio.sleep(0.3 * (attempt + 1))
        raise last_err

    positions = iter(range(aligned, end + 1, chunk_size))
    pending = deque()   # (pos, key, slot, awaitable) in file order

    def fill():
        while len(pending) < win:
            pos = next(positions, None)
            if pos is None:
                return
            idx = pos // chunk_size
            key = (fid, chunk_size, idx)
            fut, slot = _want(key, lambda key=key, idx=idx, pos=pos: download(key, idx, pos))
            pending.append((pos, key, slot, fut))

    sent = 0
    t0 = time.monotonic()
    first_byte = None
    try:
        fill()
        while pending:
            pos, key, slot, fut = pending.popleft()
            waited = not fut.done()
            try:
                data = await asyncio.shield(fut)   # shield: cancelling THIS request must not kill a download others share
            finally:
                _drop(key, slot)
            if waited and win < cap:
                win = min(cap, win * 2)
            fill()
            lo = max(start, pos) - pos
            hi = min(end + 1, pos + len(data)) - pos
            if hi > lo:
                if first_byte is None:
                    first_byte = time.monotonic() - t0
                yield data if (lo == 0 and hi == len(data)) else data[lo:hi]
                sent += hi - lo
    finally:
        for _, key, slot, _ in pending:
            _drop(key, slot)
        dt = max(time.monotonic() - t0, 0.001)
        fb = f"{first_byte:.2f}s" if first_byte is not None else "none"
        # Compare MB/s with the video's bitrate: if it is lower, the player WILL stall.
        logger.info(f"Stream closed after {sent / _MB:.1f} MB in {dt:.1f}s "
                    f"({sent / _MB / dt:.1f} MB/s, first byte {fb}, window {win}/{cap})")


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


class _ClosingStreamingResponse(StreamingResponse):
    """StreamingResponse that closes the body generator the moment the client goes away,
    so the Telegram downloads it started are cancelled immediately (not whenever the
    garbage collector gets round to it)."""

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            aclose = getattr(self.body_iterator, "aclose", None)
            if aclose:
                try:
                    await aclose()
                except Exception:
                    pass


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
        "Cache-Control": "public, max-age=31536000, no-transform",  # no-transform: proxies must not re-compress the file
        "Access-Control-Allow-Origin": "*",
        "X-Accel-Buffering": "no",  # tell proxies (nginx) not to hold the stream back
    }
    partial = bool(request.headers.get("Range"))
    if partial:
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
    logger.info(f"Stream open {start}-{end} of {file_size} ({request.headers.get('Range') or 'no Range header'})")

    return _ClosingStreamingResponse(
        ultra_high_speed_streamer(clients, file, start, end, file_size=file_size),
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
