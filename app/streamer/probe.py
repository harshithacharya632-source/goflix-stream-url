"""
Track Probe Service - Discovers audio/video/subtitle tracks in media files.
Uses ffprobe to analyze the file header downloaded from Telegram.
"""
import asyncio
import json
import tempfile
import os
import logging
import traceback
from typing import Optional

logger = logging.getLogger(__name__)

# How much of the file to download for probing (5MB is enough for headers)
PROBE_DOWNLOAD_SIZE = 5 * 1024 * 1024


async def probe_tracks(client, file, file_size: int) -> dict:
    """
    Probe a media file for audio/video/subtitle tracks.
    
    Downloads the first few MB from Telegram, runs ffprobe, and returns
    structured track information.
    
    Returns:
        {
            "video_tracks": [...],
            "audio_tracks": [...],
            "subtitle_tracks": [...],
            "has_multiple_audio": bool
        }
    """
    temp_path = None
    try:
        # Create a temp file to store the header portion
        fd, temp_path = tempfile.mkstemp(suffix=".mkv")
        os.close(fd)
        
        # Download just the header portion from Telegram
        probe_size = min(PROBE_DOWNLOAD_SIZE, file_size)
        downloaded = 0
        
        logger.info(f"Starting probe download: {probe_size/1024:.0f}KB needed, file_size={file_size}, file_type={type(file).__name__}")
        
        try:
            # Method 1: iter_download with request_size
            with open(temp_path, 'wb') as f:
                async for chunk in client.iter_download(
                    file, 
                    offset=0, 
                    request_size=min(524288, probe_size),  # 512KB chunks
                ):
                    if not chunk:
                        continue
                    f.write(bytes(chunk))
                    downloaded += len(chunk)
                    if downloaded >= probe_size:
                        break
        except Exception as dl_err:
            logger.warning(f"iter_download failed ({type(dl_err).__name__}: {dl_err}), trying download_media fallback")
            # Method 2: Fallback - download full file to temp path
            downloaded = 0
            result = await client.download_media(file, file=temp_path)
            if result and os.path.exists(temp_path):
                downloaded = os.path.getsize(temp_path)
            else:
                raise RuntimeError(f"download_media also failed, returned: {result}")
        
        logger.info(f"Downloaded {downloaded / 1024:.0f}KB for probing")
        
        if downloaded == 0:
            logger.error("Downloaded 0 bytes - cannot probe")
            return {
                "video_tracks": [],
                "audio_tracks": [],
                "subtitle_tracks": [],
                "has_multiple_audio": False,
                "error": "Downloaded 0 bytes from Telegram"
            }
        
        # Run ffprobe
        result = await _run_ffprobe(temp_path)
        return result
        
    except Exception as e:
        logger.error(f"Track probing failed: {type(e).__name__}: {e}\n{traceback.format_exc()}")
        return {
            "video_tracks": [],
            "audio_tracks": [],
            "subtitle_tracks": [],
            "has_multiple_audio": False,
            "error": f"{type(e).__name__}: {str(e) or 'Unknown error'}"
        }
    finally:
        # Clean up temp file
        if temp_path and os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except Exception:
                pass


async def _run_ffprobe(file_path: str) -> dict:
    """
    Run ffprobe on a file and parse the output into structured track info.
    Uses asyncio.to_thread + subprocess.run to avoid NotImplementedError on Windows.
    """
    import subprocess
    
    cmd = [
        'ffprobe',
        '-v', 'quiet',
        '-print_format', 'json',
        '-show_streams',
        '-show_format',
        file_path
    ]
    
    try:
        # Run synchronously in a background thread
        result = await asyncio.to_thread(
            subprocess.run,
            cmd,
            capture_output=True,
            timeout=30.0,
            check=False
        )
    except Exception as e:
        logger.error(f"ffprobe execution failed: {e}")
        return {
            "video_tracks": [],
            "audio_tracks": [],
            "subtitle_tracks": [],
            "has_multiple_audio": False,
            "error": f"ffprobe execution failed: {e}"
        }
    
    if result.returncode != 0:
        error_msg = result.stderr.decode(errors='replace').strip()
        logger.error(f"ffprobe failed (code {result.returncode}): {error_msg}")
        return {
            "video_tracks": [],
            "audio_tracks": [],
            "subtitle_tracks": [],
            "has_multiple_audio": False,
            "error": f"ffprobe error: {error_msg}"
        }
    
    try:
        probe_data = json.loads(result.stdout.decode(errors='replace'))
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse ffprobe output: {e}")
        return {
            "video_tracks": [],
            "audio_tracks": [],
            "subtitle_tracks": [],
            "has_multiple_audio": False,
            "error": "Failed to parse ffprobe output"
        }
    
    return _parse_probe_data(probe_data)


def _parse_probe_data(probe_data: dict) -> dict:
    """
    Parse raw ffprobe JSON into organized track lists.
    """
    video_tracks = []
    audio_tracks = []
    subtitle_tracks = []
    
    audio_index = 0  # Separate counter for audio-only index
    
    for stream in probe_data.get('streams', []):
        codec_type = stream.get('codec_type', '')
        tags = stream.get('tags', {})
        
        # Extract common metadata
        language = tags.get('language', 'und')
        title = tags.get('title', '')
        
        if codec_type == 'video':
            video_tracks.append({
                "index": stream.get('index', 0),
                "codec": stream.get('codec_name', 'unknown'),
                "width": stream.get('width', 0),
                "height": stream.get('height', 0),
                "fps": _parse_fps(stream.get('r_frame_rate', '0/1')),
                "language": language,
                "title": title,
            })
            
        elif codec_type == 'audio':
            # Build a display label
            label = _build_audio_label(language, title, stream)
            
            audio_tracks.append({
                "index": stream.get('index', 0),       # Absolute stream index
                "audio_index": audio_index,              # Audio-only index (for -map 0:a:N)
                "codec": stream.get('codec_name', 'unknown'),
                "channels": stream.get('channels', 2),
                "channel_layout": stream.get('channel_layout', ''),
                "sample_rate": stream.get('sample_rate', ''),
                "bit_rate": stream.get('bit_rate', ''),
                "language": language,
                "title": title,
                "label": label,
            })
            audio_index += 1
            
        elif codec_type == 'subtitle':
            subtitle_tracks.append({
                "index": stream.get('index', 0),
                "codec": stream.get('codec_name', 'unknown'),
                "language": language,
                "title": title,
            })
    
    return {
        "video_tracks": video_tracks,
        "audio_tracks": audio_tracks,
        "subtitle_tracks": subtitle_tracks,
        "has_multiple_audio": len(audio_tracks) > 1,
    }


def _build_audio_label(language: str, title: str, stream: dict) -> str:
    """
    Build a user-friendly label for an audio track.
    e.g. "English (AAC 5.1)" or "Hindi - Dubbed (AAC Stereo)"
    """
    # Language name mapping
    lang_names = {
        'eng': 'English', 'hin': 'Hindi', 'jpn': 'Japanese',
        'kor': 'Korean', 'spa': 'Spanish', 'fre': 'French',
        'fra': 'French', 'ger': 'German', 'deu': 'German',
        'ita': 'Italian', 'por': 'Portuguese', 'rus': 'Russian',
        'ara': 'Arabic', 'chi': 'Chinese', 'zho': 'Chinese',
        'tam': 'Tamil', 'tel': 'Telugu', 'kan': 'Kannada',
        'mal': 'Malayalam', 'ben': 'Bengali', 'mar': 'Marathi',
        'guj': 'Gujarati', 'pan': 'Punjabi', 'urd': 'Urdu',
        'und': 'Unknown',
    }
    
    lang_display = lang_names.get(language, language.upper() if language else 'Unknown')
    
    codec = stream.get('codec_name', '').upper()
    channels = stream.get('channels', 2)
    
    # Channel layout label
    if channels >= 6:
        ch_label = '5.1'
    elif channels >= 8:
        ch_label = '7.1'
    elif channels == 2:
        ch_label = 'Stereo'
    elif channels == 1:
        ch_label = 'Mono'
    else:
        ch_label = f'{channels}ch'
    
    parts = [lang_display]
    if title and title.lower() != language:
        parts.append(f'- {title}')
    parts.append(f'({codec} {ch_label})')
    
    return ' '.join(parts)


def _parse_fps(fps_str: str) -> float:
    """Parse FFmpeg frame rate string like '24000/1001' into a float."""
    try:
        if '/' in fps_str:
            num, den = fps_str.split('/')
            return round(float(num) / float(den), 2)
        return float(fps_str)
    except (ValueError, ZeroDivisionError):
        return 0.0
