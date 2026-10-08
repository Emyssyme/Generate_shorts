"""GPU / hardware-encoder detection and ffmpeg capability probes."""
import os
import subprocess
import sys

from config import DOWNLOADS_DIR
from ffmpeg_utils import ffmpeg_env


# High-quality encoding flags reused across all editor render commands.
# CRF 14 gives excellent quality; -preset medium is the sweet spot between
# encoding speed and compression efficiency.  Bumping from veryslow→medium
# dramatically reduces render time while the visual quality at CRF 14 is
# indistinguishable.  Use -preset slower/veryslow only for final exports
# where file size matters most.
#
# -g 30 / -keyint_min 30 force a keyframe every ~1 second so the video
# starts playing immediately instead of freezing for several seconds while
# the decoder waits for the next GOP boundary.
VIDEO_QUALITY = [
    '-c:v', 'libx264', '-crf', '14', '-preset', 'medium',
    '-g', '30', '-keyint_min', '30', '-sc_threshold', '0',
    '-pix_fmt', 'yuv420p', '-movflags', '+faststart'
]

GPU_ENCODER_QUALITY = {
    'h264_nvenc': [
        '-c:v', 'h264_nvenc', '-rc:v', 'vbr', '-cq:v', '18',
        '-g', '30', '-keyint_min', '30', '-sc_threshold', '0',
        '-pix_fmt', 'yuv420p', '-movflags', '+faststart'
    ],
    'hevc_nvenc': [
        '-c:v', 'hevc_nvenc', '-rc:v', 'vbr', '-cq:v', '18',
        '-g', '30', '-keyint_min', '30', '-sc_threshold', '0',
        '-pix_fmt', 'yuv420p', '-movflags', '+faststart'
    ],
    # ── Intel Quick Sync (low‑power / HP Mini G3) ─────────────────
    'h264_qsv': [
        '-c:v', 'h264_qsv', '-global_quality', '18',
        '-g', '30', '-keyint_min', '30', '-sc_threshold', '0',
        '-pix_fmt', 'nv12', '-movflags', '+faststart'
    ],
    'hevc_qsv': [
        '-c:v', 'hevc_qsv', '-global_quality', '18',
        '-g', '30', '-keyint_min', '30', '-sc_threshold', '0',
        '-pix_fmt', 'nv12', '-movflags', '+faststart'
    ],
}

FFMPEG_ENCODER_CACHE = {}
DRAWTEXT_LETTER_SPACING_SUPPORTED = None   # tri-state: None → not probed yet
FILTER_COMPLEX_SCRIPT_SUPPORTED = None     # tri-state: None → not probed yet


def _probe_ffmpeg_filter_option(filter_name, option_name, test_value='1'):
    """Return True if *option_name* is accepted by *filter_name*.

    Creates a tiny synthetic input (color source), applies the filter with the
    option set, and checks stderr for 'Option not found'.  Result is cached
    globally so the probe runs only once per process lifetime.
    """
    cmd = [
        'ffmpeg', '-nostdin', '-v', 'error',
        '-f', 'lavfi', '-i', 'color=size=2x2:rate=1:duration=0.01',
        '-vf', f'{filter_name}={option_name}={test_value}',
        '-f', 'null', '-'
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15, env=ffmpeg_env())
        stderr_lower = (result.stderr or '').lower()
        if 'option not found' in stderr_lower:
            return False
        # Also treat "No such filter" as unsupported
        if 'no such filter' in stderr_lower:
            return False
        return True
    except Exception as exc:
        print(f"Warning: ffmpeg option probe failed for {filter_name}/{option_name}: {exc}")
        return False


def drawtext_supports_letter_spacing():
    """Return True if the installed FFmpeg's drawtext filter accepts `letter_spacing`."""
    global DRAWTEXT_LETTER_SPACING_SUPPORTED
    if DRAWTEXT_LETTER_SPACING_SUPPORTED is None:
        DRAWTEXT_LETTER_SPACING_SUPPORTED = _probe_ffmpeg_filter_option('drawtext', 'letter_spacing')
        print(f"drawtext letter_spacing supported: {DRAWTEXT_LETTER_SPACING_SUPPORTED}")
    return DRAWTEXT_LETTER_SPACING_SUPPORTED


def ffmpeg_supports_filter_complex_script():
    """Return True if the installed FFmpeg accepts `-filter_complex_script`."""
    global FILTER_COMPLEX_SCRIPT_SUPPORTED
    if FILTER_COMPLEX_SCRIPT_SUPPORTED is None:
        import tempfile
        td = tempfile.gettempdir()
        script_path = os.path.join(td, '_fc_probe.txt')
        try:
            with open(script_path, 'w', encoding='utf-8') as f:
                f.write('[0:v]null[out]')
            result = subprocess.run(
                ['ffmpeg', '-nostdin', '-v', 'error',
                 '-f', 'lavfi', '-i', 'color=size=2x2:rate=1:duration=0.01',
                 '-filter_complex_script', script_path,
                 '-map', '[out]', '-f', 'null', '-'],
                capture_output=True, text=True, timeout=15, env=ffmpeg_env()
            )
            stderr_lower = (result.stderr or '').lower()
            FILTER_COMPLEX_SCRIPT_SUPPORTED = 'option not found' not in stderr_lower
        except Exception as exc:
            print(f"Warning: ffmpeg filter_complex_script probe failed: {exc}")
            FILTER_COMPLEX_SCRIPT_SUPPORTED = False
        finally:
            try:
                os.remove(script_path)
            except Exception:
                pass
        print(f"filter_complex_script supported: {FILTER_COMPLEX_SCRIPT_SUPPORTED}")
    return FILTER_COMPLEX_SCRIPT_SUPPORTED


def detect_gpu_available():
    """Detect if GPU/CUDA or Intel Quick Sync is available for accelerated processing."""
    # 1. NVIDIA CUDA
    try:
        result = subprocess.run(
            ['nvidia-smi', '--query-gpu=name', '--format=csv,noheader'],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            gpu_name = result.stdout.strip().split('\n')[0]
            print(f"GPU detected (NVIDIA): {gpu_name}")
            return True
    except Exception:
        pass

    # 2. Intel Quick Sync – look for Intel GPU in the system
    try:
        # On Windows Intel GPU shows up via DXGI or wmic
        if sys.platform.startswith('win'):
            r = subprocess.run(
                ['wmic', 'path', 'win32_videocontroller', 'get', 'name'],
                capture_output=True, text=True, timeout=10
            )
            if 'Intel' in r.stdout and ('HD Graphics' in r.stdout or 'UHD Graphics' in r.stdout or 'Iris' in r.stdout):
                print(f"GPU detected (Intel Quick Sync via wmic)")
                return True
        # On Linux check for /dev/dri/renderD128 (Intel GPU)
        if os.path.exists('/dev/dri/renderD128'):
            try:
                r = subprocess.run(
                    ['vainfo'], capture_output=True, text=True, timeout=5
                )
                if r.returncode == 0 and 'Intel' in (r.stdout + r.stderr):
                    print("GPU detected (Intel Quick Sync via vainfo)")
                    return True
            except Exception:
                # vainfo may not be installed – still return True if renderD128 exists
                print("GPU detected (Intel Quick Sync – /dev/dri/renderD128 present)")
                return True
    except Exception:
        pass

    # 3. Check if ffmpeg itself has qsv support
    try:
        r = subprocess.run(
            ['ffmpeg', '-hide_banner', '-encoders'],
            capture_output=True, text=True, timeout=10
        )
        if 'h264_qsv' in r.stdout or 'hevc_qsv' in r.stdout:
            print("GPU detected (Intel Quick Sync encoders available in ffmpeg)")
            return True
    except Exception:
        pass

    print("No GPU detected; using CPU")
    return False

GPU_AVAILABLE = detect_gpu_available()

# Cache hardware capabilities so encoder selection matches actual hardware
_NVIDIA_SMI_OK = False
_INTEL_QSV_OK = False

def _probe_hardware():
    global _NVIDIA_SMI_OK, _INTEL_QSV_OK
    # NVIDIA
    try:
        r = subprocess.run(['nvidia-smi'], capture_output=True, timeout=5)
        _NVIDIA_SMI_OK = r.returncode == 0
    except Exception:
        _NVIDIA_SMI_OK = False
    # Intel QSV
    try:
        if sys.platform.startswith('win'):
            r = subprocess.run(['wmic', 'path', 'win32_videocontroller', 'get', 'name'],
                             capture_output=True, text=True, timeout=10)
            _INTEL_QSV_OK = 'Intel' in r.stdout and ('HD Graphics' in r.stdout or 'UHD Graphics' in r.stdout or 'Iris' in r.stdout)
        else:
            _INTEL_QSV_OK = os.path.exists('/dev/dri/renderD128')
        if not _INTEL_QSV_OK:
            # also check ffmpeg qsv encoders
            r = subprocess.run(['ffmpeg', '-hide_banner', '-encoders'], capture_output=True, text=True, timeout=10)
            _INTEL_QSV_OK = 'h264_qsv' in r.stdout
    except Exception:
        _INTEL_QSV_OK = False

_probe_hardware()

# ── runtime encoder probe ─────────────────────────────────────────────
# ffmpeg -encoders lists every encoder that was compiled in, but the
# actual hardware may be unavailable (e.g. Intel QSV without a working
# VAAPI device).  This cache holds the result of a real encode attempt
# so we never select an encoder that will fail at render time.
_ENCODER_WORKS_CACHE = {}

def _probe_encoder_works(encoder_name):
    """Return True if *encoder_name* can successfully encode a tiny frame."""
    if encoder_name in _ENCODER_WORKS_CACHE:
        return _ENCODER_WORKS_CACHE[encoder_name]

    probe_out = os.path.join(DOWNLOADS_DIR, f'_enc_probe_{encoder_name}.mp4')
    try:
        result = subprocess.run([
            'ffmpeg', '-nostdin', '-v', 'error',
            '-f', 'lavfi', '-i', 'color=size=32x32:rate=1:duration=0.1',
            '-c:v', encoder_name, '-t', '0.1',
            '-f', 'mp4', '-y', probe_out
        ], capture_output=True, text=True, timeout=15, env=ffmpeg_env())
        works = (result.returncode == 0
                 and os.path.exists(probe_out)
                 and os.path.getsize(probe_out) > 0)
        if not works and result.stderr:
            # surface the first line of the error so the admin can diagnose
            first_line = result.stderr.strip().split('\n')[0]
            print(f"Encoder {encoder_name} probe failed: {first_line}")
    except Exception as exc:
        print(f"Encoder {encoder_name} probe crashed: {exc}")
        works = False
    finally:
        try:
            if os.path.exists(probe_out):
                os.remove(probe_out)
        except Exception:
            pass

    _ENCODER_WORKS_CACHE[encoder_name] = works
    return works


def select_auto_gpu_encoder():
    """Return the best supported GPU encoder based on actual hardware, or None.

    Checks real hardware presence, not just ffmpeg compilation support.
    Order: prefer NVIDIA on systems that have it, otherwise Intel QSV.
    """
    # If NVIDIA GPU is actually present, try NVENC first
    if _NVIDIA_SMI_OK:
        for enc in ('h264_nvenc', 'hevc_nvenc'):
            if ffmpeg_supports_encoder(enc) and _probe_encoder_works(enc):
                return enc
    # If Intel QSV is actually present, use it (but verify VAAPI works)
    if _INTEL_QSV_OK:
        for enc in ('h264_qsv', 'hevc_qsv'):
            if ffmpeg_supports_encoder(enc) and _probe_encoder_works(enc):
                return enc
    # Fallback: try any encoder ffmpeg knows about (unlikely but safe)
    for encoder in GPU_ENCODER_QUALITY:
        if ffmpeg_supports_encoder(encoder) and _probe_encoder_works(encoder):
            return encoder
    return None

def ffmpeg_supports_encoder(name):
    if name in FFMPEG_ENCODER_CACHE:
        return FFMPEG_ENCODER_CACHE[name]
    try:
        result = subprocess.run(
            ['ffmpeg', '-hide_banner', '-encoders'],
            capture_output=True, text=True, timeout=15, env=ffmpeg_env()
        )
        supported = name in result.stdout
    except Exception as exc:
        print(f"Warning: ffmpeg encoder probe failed for {name}: {exc}")
        supported = False
    FFMPEG_ENCODER_CACHE[name] = supported
    return supported
