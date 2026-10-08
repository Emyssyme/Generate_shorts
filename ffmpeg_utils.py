"""Small ffmpeg/ffprobe helpers."""
import os
import subprocess

from config import BASE_DIR


def ffmpeg_env():
    """Return an os.environ copy with FONTCONFIG_PATH set for FFmpeg subprocess calls."""
    env = os.environ.copy()
    fc_conf = os.path.join(BASE_DIR, 'fonts.conf')
    if os.path.exists(fc_conf):
        env['FONTCONFIG_PATH'] = BASE_DIR
        env['FC_CONFIG_DIR'] = BASE_DIR
    # Also force UTF-8 for consistent behaviour
    env.setdefault('PYTHONUTF8', '1')
    env.setdefault('PYTHONIOENCODING', 'utf-8')
    return env


def clean_filter_path(raw_path):
    """
    Transforms standard system paths into secure, cross-platform
    escaped strings for use inside FFmpeg filter scripts.
    """
    # 1. Flip Windows backslashes into standard Unix forward slashes 
    normalized = raw_path.replace('\\', '/')
    # 2. Double-escape the colon (C\:/...) so the filter syntax engine doesn't trip
    return normalized.replace(':', '\\\\:')


def get_video_size(video_path: str):
    """Return (width, height) of a video via ffprobe. Falls back to 1080×1920."""
    try:
        r = subprocess.run(
            ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
             '-show_entries', 'stream=width,height', '-of', 'csv=p=0', video_path],
            capture_output=True, text=True, env=ffmpeg_env()
        )
        if r.returncode == 0:
            parts = r.stdout.strip().split(',')
            if len(parts) == 2:
                return int(parts[0]), int(parts[1])
    except Exception:
        pass
    return 1080, 1920


def is_h264(video_path: str) -> bool:
    """Return True if the given file's first video stream uses h264."""
    try:
        r = subprocess.run(
            ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
             '-show_entries', 'stream=codec_name',
             '-of', 'default=noprint_wrappers=1:nokey=1', video_path],
            capture_output=True, text=True, env=ffmpeg_env()
        )
        return r.stdout.strip() == 'h264'
    except Exception:
        return False


def transcode_to_h264(src_path: str) -> str:
    """Transcode `src_path` to h264 if it isn't already, returning new filename.

    The function will skip re-transcoding if the target file already exists.
    """
    base, ext = os.path.splitext(src_path)
    dst_path = base + '_h264.mp4'
    if os.path.exists(dst_path):
        return dst_path
    subprocess.run([
        'ffmpeg', '-nostdin', '-y', '-i', src_path,
        '-c:v', 'libx264', '-crf', '18', '-preset', 'fast',
        '-g', '30', '-keyint_min', '30', '-sc_threshold', '0',
        '-pix_fmt', 'yuv420p', '-movflags', '+faststart',
        '-c:a', 'copy', dst_path
    ], check=True, timeout=600, env=ffmpeg_env())  # 10-minute timeout
    return dst_path
