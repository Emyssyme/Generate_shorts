import os
import re
import sys
import datetime
import zipfile
import requests
import json
import asyncio
import threading
import subprocess
import unicodedata
import shutil
import sqlite3
import traceback
from flask import Flask, render_template, request, flash, redirect, url_for, send_from_directory
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from flask_socketio import SocketIO, emit

# --- basic configuration --------------------------------------------------
class Config:
    SCHEDULER_API_ENABLED = True
    SCHEDULER_TIMEZONE = "Europe/Bucharest"
    SCHEDULER_EXECUTORS = {'default': {'type': 'threadpool', 'max_workers': 5}}
    SCHEDULER_JOB_DEFAULTS = {'coalesce': False, 'max_instances': 3}

app = Flask(__name__)
app.config.from_object(Config())
app.secret_key = os.getenv("SECRET_KEY", "dev_key")

socketio = SocketIO(app, cors_allowed_origins="*")

# download directory used by all operations; always placed next to this file
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOADS_DIR = os.path.join(BASE_DIR, "downloads")
if not os.path.exists(DOWNLOADS_DIR):
    os.makedirs(DOWNLOADS_DIR)

# fonts directory (optional).  Users can drop .ttf/.otf files here and the
# editor will expose them by basename.
FONTS_DIR = os.path.join(BASE_DIR, 'fonts')
if not os.path.exists(FONTS_DIR):
    os.makedirs(FONTS_DIR)

FONT_DOWNLOAD_URLS = {
    'Inter':      ('Inter-Regular.ttf', 'https://github.com/rsms/inter/releases/download/v14.0/Inter-Regular.ttf'),
    'Inter Bold': ('Inter-Bold.ttf',   'https://github.com/rsms/inter/releases/download/v14.0/Inter-Bold.ttf'),
}
ALLOWED_FONT_EXTENSIONS = {'ttf', 'otf'}

def allowed_font_file(filename):
    ext = os.path.splitext(filename)[1].lower().lstrip('.')
    return ext in ALLOWED_FONT_EXTENSIONS

def download_font_file(dest_path, url):
    try:
        response = requests.get(url, stream=True, timeout=30)
        response.raise_for_status()
        with open(dest_path, 'wb') as out_file:
            shutil.copyfileobj(response.raw, out_file)
        return True
    except Exception as exc:
        print(f"Warning: could not download font from {url}: {exc}")
        return False


def _add_common_linux_fonts(font_map):
    candidates = [
        ('Inter', '/usr/share/fonts/truetype/inter/Inter-Regular.ttf'),
        ('Inter Bold', '/usr/share/fonts/truetype/inter/Inter-Bold.ttf'),
        ('Inter', '/usr/share/fonts/truetype/Inter/Inter-Regular.ttf'),
        ('Inter Bold', '/usr/share/fonts/truetype/Inter/Inter-Bold.ttf'),
        ('Inter', '/usr/share/fonts/truetype/ttf-inter/Inter-Regular.ttf'),
        ('Inter Bold', '/usr/share/fonts/truetype/ttf-inter/Inter-Bold.ttf'),
        ('Inter', os.path.expanduser('~/.local/share/fonts/Inter-Regular.ttf')),
        ('Inter Bold', os.path.expanduser('~/.local/share/fonts/Inter-Bold.ttf')),
    ]
    for name, path in candidates:
        if os.path.exists(path):
            font_map.setdefault(name, path)


def build_font_map():
    font_map = {}
    if os.path.isdir(FONTS_DIR):
        for fname in os.listdir(FONTS_DIR):
            if fname.lower().endswith(('.ttf', '.otf')):
                name = os.path.splitext(fname)[0]
                path = os.path.join(FONTS_DIR, fname)
                if name in ('Inter-Regular', 'Inter'):
                    font_map['Inter'] = path
                    continue
                if name in ('Inter-Bold', 'InterBold'):
                    font_map['Inter Bold'] = path
                    continue
                font_map[name] = path
    if not font_map:
        if sys.platform.startswith('win'):
            font_map = {
                'Arial':           'C:/Windows/Fonts/arial.ttf',
                'Arial Bold':      'C:/Windows/Fonts/arialbd.ttf',
                'Impact':          'C:/Windows/Fonts/impact.ttf',
                'Georgia':         'C:/Windows/Fonts/georgia.ttf',
                'Verdana':         'C:/Windows/Fonts/verdana.ttf',
                'Courier New':     'C:/Windows/Fonts/cour.ttf',
                'Times New Roman': 'C:/Windows/Fonts/times.ttf',
                'Trebuchet MS':    'C:/Windows/Fonts/trebuc.ttf',
                'Calibri':         'C:/Windows/Fonts/calibri.ttf',
                'Segoe UI':        'C:/Windows/Fonts/segoeui.ttf',
                'Comic Sans MS':   'C:/Windows/Fonts/comic.ttf',
            }
        else:
            font_map = {
                'DejaVu Sans':      '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
                'DejaVu Sans Bold': '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
                'Liberation Sans':  '/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf',
                'Liberation Sans Bold': '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf',
                'FreeSerif':        '/usr/share/fonts/truetype/freefont/FreeSerif.ttf',
                'FreeSans':         '/usr/share/fonts/truetype/freefont/FreeSans.ttf',
            }
            _add_common_linux_fonts(font_map)
    else:
        if sys.platform.startswith('win'):
            font_map.update({
                'Arial':           'C:/Windows/Fonts/arial.ttf',
                'Arial Bold':      'C:/Windows/Fonts/arialbd.ttf',
                'Impact':          'C:/Windows/Fonts/impact.ttf',
                'Georgia':         'C:/Windows/Fonts/georgia.ttf',
                'Verdana':         'C:/Windows/Fonts/verdana.ttf',
                'Courier New':     'C:/Windows/Fonts/cour.ttf',
                'Times New Roman': 'C:/Windows/Fonts/times.ttf',
                'Trebuchet MS':    'C:/Windows/Fonts/trebuc.ttf',
                'Calibri':         'C:/Windows/Fonts/calibri.ttf',
                'Segoe UI':        'C:/Windows/Fonts/segoeui.ttf',
                'Comic Sans MS':   'C:/Windows/Fonts/comic.ttf',
            })
        else:
            _add_common_linux_fonts(font_map)
    return font_map


def refresh_font_map():
    global FONT_MAP
    FONT_MAP = build_font_map()


def ensure_default_fonts():
    for font_name, (filename, url) in FONT_DOWNLOAD_URLS.items():
        dest_path = os.path.join(FONTS_DIR, filename)
        if not os.path.exists(dest_path):
            if download_font_file(dest_path, url):
                print(f"Downloaded default font: {font_name}")

ensure_default_fonts()
refresh_font_map()

# simple sqlite cache to avoid reprocessing identical url/time ranges
CACHE_DB = os.path.join(BASE_DIR, "cache.db")
TEMPLATES_DB = os.path.join(BASE_DIR, "templates.db")
TEMPLATE_ASSETS_DIR = os.path.join(BASE_DIR, "template_assets")
os.makedirs(TEMPLATE_ASSETS_DIR, exist_ok=True)

def init_cache():
    conn = sqlite3.connect(CACHE_DB)
    c = conn.cursor()
    # old versions may lack skip_unsilence column -- add if necessary
    c.execute("PRAGMA table_info(cache)")
    columns = [row[1] for row in c.fetchall()]
    if 'skip_unsilence' not in columns:
        try:
            c.execute('ALTER TABLE cache ADD COLUMN skip_unsilence INTEGER DEFAULT 0')
        except Exception:
            pass
    if 'skip_cropping' not in columns:
        try:
            c.execute('ALTER TABLE cache ADD COLUMN skip_cropping INTEGER DEFAULT 0')
        except Exception:
            pass
    c.execute('''
        CREATE TABLE IF NOT EXISTS cache (
            url TEXT,
            start TEXT,
            end TEXT,
            video TEXT,
            srt TEXT,
            skip_unsilence INTEGER DEFAULT 0,
            skip_cropping INTEGER DEFAULT 0,
            UNIQUE(url, start, end, skip_unsilence, skip_cropping)
        )
    ''')
    conn.commit()
    conn.close()

def init_templates_db():
    """Create templates table for storing editor presets."""
    conn = sqlite3.connect(TEMPLATES_DB)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            config TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()


def sanitize_template_filename(name):
    return re.sub(r'[^A-Za-z0-9_.-]', '_', name)


def copy_overlay_to_template_assets(overlay_name, template_name):
    if not overlay_name:
        return None
    overlay_name = os.path.basename(overlay_name)
    source_paths = [
        os.path.join(DOWNLOADS_DIR, overlay_name),
        os.path.join(TEMPLATE_ASSETS_DIR, overlay_name),
    ]
    source = next((p for p in source_paths if os.path.exists(p)), None)
    if not source:
        return None
    base, ext = os.path.splitext(overlay_name)
    safe_name = sanitize_template_filename(f"{template_name}_{base}{ext}")
    dest = os.path.join(TEMPLATE_ASSETS_DIR, safe_name)
    if os.path.abspath(source) != os.path.abspath(dest):
        shutil.copy2(source, dest)
    return safe_name


def resolve_overlay_path(overlay_name):
    if not overlay_name:
        return None
    overlay_name = os.path.basename(overlay_name)
    candidates = [
        os.path.join(DOWNLOADS_DIR, overlay_name),
        os.path.join(TEMPLATE_ASSETS_DIR, overlay_name),
    ]
    return next((p for p in candidates if os.path.exists(p)), None)


def get_all_templates():
    """Return list of template names."""
    conn = sqlite3.connect(TEMPLATES_DB)
    c = conn.cursor()
    c.execute('SELECT name, created_at FROM templates ORDER BY created_at DESC')
    rows = c.fetchall()
    conn.close()
    return [{'name': r[0], 'created_at': r[1]} for r in rows]

def get_template(name):
    """Return config dict for a named template, or None."""
    conn = sqlite3.connect(TEMPLATES_DB)
    c = conn.cursor()
    c.execute('SELECT config FROM templates WHERE name=?', (name,))
    row = c.fetchone()
    conn.close()
    if row:
        return json.loads(row[0])
    return None

def save_template(name, config):
    """Insert or update a template. Returns True on success."""
    config = dict(config)
    overlay_name = config.get('overlay')
    if overlay_name:
        copied_name = copy_overlay_to_template_assets(overlay_name, name)
        if copied_name:
            config['overlay'] = copied_name
        else:
            config.pop('overlay', None)

    conn = sqlite3.connect(TEMPLATES_DB)
    c = conn.cursor()
    try:
        c.execute('INSERT OR REPLACE INTO templates (name, config, created_at) VALUES (?, ?, CURRENT_TIMESTAMP)',
                  (name, json.dumps(config, ensure_ascii=False)))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        conn.close()
        print(f"save_template error: {e}")
        return False

def delete_template(name):
    """Delete a template by name."""
    cfg = get_template(name)
    overlay_name = cfg.get('overlay') if cfg else None

    conn = sqlite3.connect(TEMPLATES_DB)
    c = conn.cursor()
    c.execute('DELETE FROM templates WHERE name=?', (name,))
    conn.commit()
    conn.close()

    if overlay_name:
        # only remove the saved asset if no other template references it
        conn = sqlite3.connect(TEMPLATES_DB)
        c = conn.cursor()
        c.execute('SELECT config FROM templates')
        rows = c.fetchall()
        conn.close()
        still_used = any(json.loads(row[0]).get('overlay') == overlay_name for row in rows)
        if not still_used:
            asset_path = os.path.join(TEMPLATE_ASSETS_DIR, overlay_name)
            try:
                if os.path.exists(asset_path):
                    os.remove(asset_path)
            except Exception:
                pass

# initialize databases when the module loads
init_cache()
init_templates_db()

def find_cache(url, start, end, skip_unsilence=False, skip_cropping=False):
    conn = sqlite3.connect(CACHE_DB)
    c = conn.cursor()
    c.execute('SELECT video, srt FROM cache WHERE url=? AND start=? AND end=? AND skip_unsilence=? AND skip_cropping=?',
              (url or '', start or '', end or '', int(skip_unsilence), int(skip_cropping)))
    row = c.fetchone()
    conn.close()
    return row  # either None or (video, srt)

def store_cache(url, start, end, video, srt, skip_unsilence=False, skip_cropping=False):
    conn = sqlite3.connect(CACHE_DB)
    c = conn.cursor()
    try:
        if video is None or srt is None:
            # remove stale entry for this configuration
            c.execute('DELETE FROM cache WHERE url=? AND start=? AND end=? AND skip_unsilence=? AND skip_cropping=?',
                      (url or '', start or '', end or '', int(skip_unsilence), int(skip_cropping)))
        else:
            c.execute('INSERT OR REPLACE INTO cache (url, start, end, video, srt, skip_unsilence, skip_cropping) VALUES (?,?,?,?,?,?,?)',
                      (url or '', start or '', end or '', video, srt, int(skip_unsilence), int(skip_cropping)))
        conn.commit()
    finally:
        conn.close()

# initialize the cache when the module loads
init_cache()
init_templates_db()

# in‑memory job state (persisted to disk)
JOBS_FILE = os.path.join(BASE_DIR, "jobs.json")

def load_jobs():
    if os.path.exists(JOBS_FILE):
        try:
            with open(JOBS_FILE, encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"warning: failed to load jobs file: {e}")
    return {}

def save_jobs():
    try:
        with open(JOBS_FILE, 'w', encoding='utf-8') as f:
            json.dump(active_jobs, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"warning: failed to save jobs file: {e}")

active_jobs = load_jobs()
jobs_lock = threading.Lock()  # Thread-safe access to active_jobs

# ---------------------------------------------------------------------------
# Editor: colour helpers
# ---------------------------------------------------------------------------

def html_to_ass_color(html_color: str) -> str:
    """Convert #RRGGBB HTML colour to ASS &H00BBGGRR format."""
    h = html_color.lstrip('#')
    try:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except (ValueError, IndexError):
        return '&H00FFFFFF'
    return f'&H00{b:02X}{g:02X}{r:02X}'


def html_to_drawtext_color(html_color: str) -> str:
    """Convert #RRGGBB to ffmpeg drawtext colour 0xRRGGBB."""
    return '0x' + html_color.lstrip('#').upper()


def get_video_size(video_path: str):
    """Return (width, height) of a video via ffprobe. Falls back to 1080×1920."""
    try:
        r = subprocess.run(
            ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
             '-show_entries', 'stream=width,height', '-of', 'csv=p=0', video_path],
            capture_output=True, text=True
        )
        if r.returncode == 0:
            parts = r.stdout.strip().split(',')
            if len(parts) == 2:
                return int(parts[0]), int(parts[1])
    except Exception:
        pass
    return 1080, 1920


# --- login support (minimal) ---------------------------------------------
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

class User(UserMixin):
    def __init__(self, id):
        self.id = id

# dummy credentials for simplicity, can be replaced by environment variables
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "password")

@login_manager.user_loader
def load_user(user_id):
    if user_id == ADMIN_USER:
        return User(ADMIN_USER)
    return None

# simple login/logout routes
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        if username == ADMIN_USER and password == ADMIN_PASS:
            user = User(ADMIN_USER)
            login_user(user)
            flash("Logged in successfully", "success")
            return redirect(url_for('video_cut'))
        flash("Invalid credentials", "danger")
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash("Logged out", "info")
    return redirect(url_for('login'))


# ---------- end of new header ------------------------------------------------

# helper routines -----------------------------------------------------------

def is_h264(video_path: str) -> bool:
    """Return True if the given file's first video stream uses h264."""
    try:
        r = subprocess.run(
            ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
             '-show_entries', 'stream=codec_name',
             '-of', 'default=noprint_wrappers=1:nokey=1', video_path],
            capture_output=True, text=True
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
    ], check=True, timeout=600)  # 10-minute timeout
    return dst_path


def srt_to_ass(srt_content: str, ass_path: str):
    """Convert SRT content to an ASS file with per-word karaoke highlighting.

    Word timing is computed **proportionally** within each SRT entry so
    the rendered video matches the canvas preview exactly.  Each entry
    is split into words, and each word gets its own Dialogue event with
    ``##HLBG##`` / ``##HLFG##`` / ``##BORD##`` placeholders that are
    replaced at render time with the user's chosen colours.

    This function is called whenever the user edits the SRT by hand.
    """
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,48,&H0000FFFF,&H00FFFFFF,&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,2,0,2,10,10,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    def _to_sec(ts):
        """Convert HH:MM:SS.mmm or HH:MM:SS,mmm to seconds."""
        ts = ts.replace(',', '.')
        h, m, s = ts.split(':')
        return int(h) * 3600 + int(m) * 60 + float(s)

    def _ass_ts(sec):
        """Convert seconds to ASS timestamp H:MM:SS.cc."""
        total = int(sec)
        h, rem = divmod(total, 3600)
        m, s = divmod(rem, 60)
        cs = int((sec - total) * 100)
        return f"{h}:{m:02d}:{s:02d}.{cs:02d}"

    blocks = re.split(r"\n\s*\n", srt_content.strip())
    lines_out = [header]

    for block in blocks:
        block = block.strip()
        if not block:
            continue
        parts = block.split("\n")
        if len(parts) < 3:
            continue
        ts_match = re.match(
            r"(\d{2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,.]\d{3})",
            parts[1])
        if not ts_match:
            continue
        seg_start = _to_sec(ts_match.group(1))
        seg_end = _to_sec(ts_match.group(2))
        seg_duration = seg_end - seg_start
        if seg_duration <= 0:
            continue

        # Join all text lines, split into words
        full_text = " ".join(parts[2:])
        full_text = re.sub(r"\s+", " ", full_text).strip()
        if not full_text:
            continue
        words = full_text.split()
        word_count = len(words)
        word_duration = seg_duration / word_count

        # Generate one Dialogue event per word with proportional timing
        for wi, current_word in enumerate(words):
            ev_start = seg_start + wi * word_duration
            ev_end = seg_start + (wi + 1) * word_duration
            if ev_end - ev_start < 0.02:
                continue

            # Build the line with highlight on the current word
            word_parts = []
            for wj, w_text in enumerate(words):
                if wj == wi:
                    word_parts.append(
                        f"{{\\3c&H##HLBG##&\\bord##BORD##\\1c&H##HLFG##&}}"
                        f"{w_text}"
                        f"{{\\r}}"
                    )
                else:
                    word_parts.append(w_text)

            line_text = " ".join(word_parts)
            lines_out.append(
                f"Dialogue: 0,{_ass_ts(ev_start)},{_ass_ts(ev_end)},"
                f"Default,,0,0,0,,{line_text}")

    content = "\n".join(lines_out) + "\n"
    with open(ass_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)


def find_script(name):
    """Locate a helper script by name in the current directory or its parent.

    This project historically has helpers either next to ``app.py`` or in the
    workspace root.  ``find_script`` tries both places and raises if the file
    cannot be found so that the caller can surface a useful error.
    """
    base = os.path.dirname(__file__)
    candidates = [os.path.join(base, name)]
    for path in candidates:
        if os.path.exists(path):
            return os.path.abspath(path)
    raise FileNotFoundError(f"helper script not found: {name}")


def update_job(job_id, status=None, log=None, **kwargs):
    """Mutate the job dictionary stored in ``active_jobs``.

    * ``status`` (optional) replaces the current status string.
    * ``log`` (optional) appends a line to a list stored under ``log`` and
      echoes it to the server console so that developers can follow progress
      without opening the web UI.

    The previous implementation replaced the whole dictionary every time,
    which made it impossible to keep information such as the generated video
    name while updating status.  This helper merges fields instead.
    """
    with jobs_lock:
        job = active_jobs.setdefault(job_id, {})
        if status is not None:
            job['status'] = status
        if log is not None:
            job.setdefault('log', []).append(log)
            # also print to console for visibility
            try:
                print(f"[job {job_id}] {log}")
            except Exception:
                pass
        job.update(kwargs)
        # persist immediately
        save_jobs()


def run_unsilence(input_video, output_video, job_id=None):
    """Call the unsilence script on a single file.

    If ``job_id`` is provided the output from the helper script will be
    appended to the job's log so the web UI can display live progress.
    """
    script = find_script("_unsilence_files.py")
    cmd = [sys.executable, script, input_video, output_video]
    if job_id:
        update_job(job_id, log="starting unsilence script")
        # stream output line-by-line
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            for line in proc.stdout:
                update_job(job_id, log=line.rstrip())
            proc.wait(timeout=3600)  # 1 hour max timeout
        except subprocess.TimeoutExpired:
            proc.kill()
            raise RuntimeError("unsilence script timed out after 1 hour")
        if proc.returncode != 0:
            raise RuntimeError(f"unsilence failed (see logs) returncode={proc.returncode}")
    else:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        if result.returncode != 0:
            raise RuntimeError(f"unsilence failed: {result.stderr}")


def run_crop(input_video, output_dir, overlay=None, job_id=None):
    """Crop the face vertically; returns path of resulting video.

    The helper script produces MP4 video using OpenCV's "mp4v" codec which
    browsers often cannot decode (hence the preview would show only audio).
    After the helper finishes we transcode the result to h264 so the HTML5
    <video> element can play it reliably.
    
    Falls back to CPU-only mode if GPU acceleration fails.
    """
    script = find_script("_crop_face_vertical.py")
    
    # Try with GPU first if available, then fallback to CPU
    for use_gpu in [True, False]:
        if use_gpu and not GPU_AVAILABLE:
            continue  # Skip GPU attempt if no GPU detected
            
        cmd = [sys.executable, script, "--input", input_video, "--output", output_dir]
        if overlay:
            cmd.extend(["--overlay", overlay])
        if not use_gpu:
            cmd.append("--cpu-only")  # Pass CPU-only flag to helper script
        
        try:
            if job_id and use_gpu:
                update_job(job_id, log="attempting crop with GPU acceleration")
            elif job_id and not use_gpu:
                update_job(job_id, log="attempting crop with CPU only")
                
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            
            if result.returncode != 0:
                error_msg = result.stderr
                if "DNN_BACKEND_CUDA" in error_msg or "CUDA" in error_msg:
                    # GPU-related error, try again with CPU
                    if use_gpu:
                        if job_id:
                            update_job(job_id, log="GPU acceleration not available, retrying with CPU")
                        continue  # Try again with use_gpu=False
                    else:
                        # Already tried CPU, this is a real error
                        raise RuntimeError(f"crop failed: {error_msg}")
                else:
                    raise RuntimeError(f"crop failed: {error_msg}")
            
            # Success - process the output
            break  # Exit retry loop on success
            
        except subprocess.TimeoutExpired:
            if use_gpu and GPU_AVAILABLE:
                if job_id:
                    update_job(job_id, log="GPU crop timed out, retrying with CPU")
                continue  # Try again with CPU
            else:
                raise RuntimeError("crop script timed out after 10 minutes")
    
    # script names output as <basename>_processed.mp4
    base = os.path.splitext(os.path.basename(input_video))[0]
    cropped = os.path.join(output_dir, base + "_processed.mp4")
    # verify output file exists before trying to transcode
    if not os.path.exists(cropped):
        raise RuntimeError(f"crop script did not produce output file: {cropped}")
    # always transcode to h264 for browser compatibility
    trans = os.path.join(output_dir, base + "_processed_h264.mp4")
    try:
        subprocess.run([
            'ffmpeg', '-nostdin', '-y', '-i', cropped,
            '-c:v', 'libx264', '-crf', '18', '-preset', 'fast',
            '-g', '30', '-keyint_min', '30', '-sc_threshold', '0',
            '-pix_fmt', 'yuv420p', '-movflags', '+faststart',
            '-c:a', 'copy', trans
        ], check=True, timeout=600)
        return trans
    except subprocess.CalledProcessError as e:
        # if transcoding fails fall back to original cropped file
        if job_id:
            update_job(job_id, log=f"warning: h264 transcode failed, using original: {e}")
        return cropped


def run_subtitles(input_video, output_dir, model="large", max_length=22, job_id=None):
    """Generate subtitles for a single video.

    If ``job_id`` is passed, stream the helper script's output into the job log.

    Returns a tuple ``(srt_path, ass_path)`` — the ASS file contains word-level
    karaoke tags for per-word highlighting during video rendering.
    """
    script = find_script("_generate_subtitles.py")
    cmd = [sys.executable, script, "--input", input_video, "--output", output_dir,
           "--model", model, "--max-length", str(max_length)]
    if job_id:
        update_job(job_id, log="starting subtitle generation")
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in proc.stdout:
            update_job(job_id, log=line.rstrip())
        proc.wait(timeout=3600)  # 1 hour max for subtitle generation
        if proc.returncode != 0:
            raise RuntimeError(f"subtitle generation failed (see logs); rc={proc.returncode}")
    else:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        if result.returncode != 0:
            raise RuntimeError(f"subtitle generation failed: {result.stderr}")
    # output files use same base name convention as the helper script
    base = os.path.splitext(os.path.basename(input_video))[0]
    srt_path = os.path.join(output_dir, base + ".srt")
    ass_path = os.path.join(output_dir, base + ".ass")
    return srt_path, ass_path

# ── face detection helpers ────────────────────────────────────────────────

def detect_faces_in_video(video_path: str, sample_frames: int = 5):
    """Detect faces in the first few frames of a video using OpenCV Haar cascade.

    Returns a dict with:
        - has_face: bool, whether at least one frontal face was detected
        - face_count: average number of faces per frame
        - facing: 'frontal' if faces detected, 'none' otherwise
        - confidence: rough percentage of frames where a face was found
    """
    try:
        import cv2
    except ImportError:
        return {'has_face': False, 'face_count': 0, 'facing': 'none',
                'confidence': 0, 'error': 'OpenCV not installed'}

    cascade_path = cv2.data.haarcascades + 'haarcascade_frontalface_default.xml'
    if not os.path.exists(cascade_path):
        return {'has_face': False, 'face_count': 0, 'facing': 'none',
                'confidence': 0, 'error': 'Haar cascade not found'}

    face_cascade = cv2.CascadeClassifier(cascade_path)
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return {'has_face': False, 'face_count': 0, 'facing': 'none',
                'confidence': 0, 'error': 'Cannot open video'}

    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if total_frames <= 0:
        cap.release()
        return {'has_face': False, 'face_count': 0, 'facing': 'none',
                'confidence': 0, 'error': 'No frames in video'}

    # sample evenly across the first half of the video
    step = max(1, (total_frames // 2) // sample_frames)
    frames_with_faces = 0
    total_faces = 0
    frames_checked = 0

    for i in range(0, min(total_frames // 2, sample_frames * step), step):
        cap.set(cv2.CAP_PROP_POS_FRAMES, i)
        ret, frame = cap.read()
        if not ret:
            break
        frames_checked += 1
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = face_cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(80, 80))
        if len(faces) > 0:
            frames_with_faces += 1
            total_faces += len(faces)

    cap.release()

    if frames_checked == 0:
        return {'has_face': False, 'face_count': 0, 'facing': 'none', 'confidence': 0}

    confidence = (frames_with_faces / frames_checked) * 100
    has_face = frames_with_faces >= frames_checked * 0.4  # at least 40% of frames
    avg_faces = total_faces / frames_checked if frames_checked > 0 else 0

    return {
        'has_face': has_face,
        'face_count': round(avg_faces, 1),
        'facing': 'frontal' if has_face else 'none',
        'confidence': round(confidence, 1),
        'frames_checked': frames_checked,
        'frames_with_faces': frames_with_faces,
    }


# convert any time format (seconds float, "MM:SS" or "HH:MM:SS") to pure seconds.
def _time_to_seconds(t_val):
    if isinstance(t_val, (int, float)):
        return float(t_val)
    t_str = str(t_val).strip()
    if ':' in t_str:
        parts = t_str.split(':')
        if len(parts) == 2:  # MM:SS
            return int(parts[0]) * 60 + float(parts[1])
        elif len(parts) == 3:  # HH:MM:SS
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    return float(t_str)

def background_pipeline(job_id, url=None, upload_path=None, start_time="0", end_time=None, skip_unsilence=False, skip_cropping=False):
    """Background thread that cuts/downloads then optionally unsilences, crops, subtitles.

    ``skip_unsilence`` is used when the user knows the clip already has clean audio.
    ``skip_cropping`` is used to bypass the face-crop step entirely (e.g. when no face is present).
    These flags are recorded in the cache so repeated calls behave identically.
    """
    update_job(job_id, status="starting", log="job created")
    try:
        # determine source for cutting
        cut_path = os.path.join(DOWNLOADS_DIR, f"job_{job_id}_cut.mp4")
        if url:
            update_job(job_id, status="downloading", log=f"yt-dlp {url} ({start_time}-{end_time})")
            cmd = [
                "yt-dlp", "--download-sections", f"*{start_time}-{end_time}",
                "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
                "--force-keyframes-at-cuts", "--no-check-certificate", "-o", cut_path, url
            ]
            try:
                subprocess.run(cmd, check=True, timeout=1800)  # 30-minute timeout for YouTube download
            except subprocess.CalledProcessError as e:
                # some videos/datacenters don't support range requests; fall back to full
                update_job(job_id, log="section download failed, falling back to full download and manual trim")
                full = os.path.join(DOWNLOADS_DIR, f"job_{job_id}_full.mp4")
                subprocess.run([
                    "yt-dlp", "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
                    "-o", full, url
                ], check=True, timeout=1800)
                ff = ["ffmpeg", "-y", "-i", full, "-ss", start_time]
                if end_time:
                    ff += ["-to", end_time]
                ff += ["-c", "copy", cut_path]
                subprocess.run(ff, check=True, timeout=600)
        else:
            # Verificăm dacă avem nevoie de o tăiere sau folosim direct fișierul original
            start_sec = _time_to_seconds(start_time)
            
            if start_sec == 0 and not end_time:
                update_job(job_id, status="skipping cutting", log="no time limits provided, using original uploaded file")
                # Pasăm fișierul original mai departe în pipeline fără re-encodare/tăiere
                cut_path = upload_path
            else:
                update_job(job_id, status="cutting", log=f"trimming {upload_path}")
                # trim local file
        
                # Pornim comanda cu -ss ÎNAINTE de -i pentru căutare ultra-rapidă în fișiere mari
                ff = ["ffmpeg", "-y", "-ss", f"{start_sec:.3f}", "-i", upload_path]
                
                if end_time:
                    end_sec = _time_to_seconds(end_time)
                    duration = end_sec - start_sec
                    if duration > 0:
                        # Folosim -t (durata) în loc de -to, deoarece axa timpului s-a resetat prin mutarea lui -ss
                        ff += ["-t", f"{duration:.3f}"]

                # Înlocuim "-c", "copy" cu re-encodare rapidă și curățare de timestamp-uri
                ff += [
                    "-c:v", "libx264",        # Codec video compatibil oriunde
                    "-c:a", "aac",            # Codec audio standard
                    "-preset", "fast",        # Randare rapidă
                    "-crf", "22",             # Calitate vizuală excelentă
                    "-avoid_negative_ts", "make_zero", # Resetează indicii de timp la 0 (rezolvă freeze-ul pe TikTok/VLC)
                    cut_path
                ]
                subprocess.run(ff, check=True, timeout=600)

        if skip_unsilence:
            update_job(job_id, status="skipping unsilence", log="user requested no audio cleaning")
            unsilenced = cut_path
        else:
            update_job(job_id, status="unsilencing", log="calling unsilence script")
            unsilenced = os.path.join(DOWNLOADS_DIR, f"job_{job_id}_unsilenced.mp4")
            run_unsilence(cut_path, unsilenced, job_id=job_id)

        if skip_cropping:
            update_job(job_id, status="skipping cropping", log="user requested no face cropping")
            cropped = unsilenced
        else:
            update_job(job_id, status="cropping", log="running crop script")
            cropped = run_crop(unsilenced, DOWNLOADS_DIR, job_id=job_id)

        update_job(job_id, status="subtitling", log="generating subtitles")
        srtfile, assfile = run_subtitles(cropped, DOWNLOADS_DIR, job_id=job_id)

        # sanity check: ensure subtitles were actually written
        if not os.path.exists(srtfile):
            raise RuntimeError(f"subtitle file not found after generation: {srtfile}")

        update_job(job_id, status="completed",
                   video=os.path.basename(cropped),
                   srt=os.path.basename(srtfile),
                   ass=os.path.basename(assfile) if os.path.exists(assfile) else None,
                   log="all steps finished")
        # cache this result for future identical requests
        if url:
            store_cache(url, start_time, end_time,
                        os.path.basename(cropped), os.path.basename(srtfile),
                        skip_unsilence=skip_unsilence, skip_cropping=skip_cropping)
    except Exception as e:
        error_trace = traceback.format_exc()
        update_job(job_id, status="error", msg=str(e), trace=error_trace)
        print(f"[job {job_id}] Exception: {error_trace}")


@app.route('/video-cut', methods=['GET', 'POST'])
@login_required 
def video_cut():
    if request.method == 'POST':
        url = request.form.get('url', '').strip()
        start_time = request.form.get('start_time') or "0"
        end_time = request.form.get('end_time')
        skip_unsilence = request.form.get('skip_unsilence') == 'on'
        skip_cropping = request.form.get('skip_cropping') == 'on'
        job_id = request.form.get('job_id') or f"J{int(datetime.datetime.now().timestamp())}"
        # Sanitize job_id: only allow alphanumeric, underscore, and dash
        job_id = re.sub(r'[^a-zA-Z0-9_-]', '', job_id)
        if not job_id:
            job_id = f"J{int(datetime.datetime.now().timestamp())}"

        upload_path = None
        if 'file' in request.files and request.files['file'].filename:
            f = request.files['file']
            upload_path = os.path.join(DOWNLOADS_DIR, f"upload_{job_id}_{f.filename}")
            f.save(upload_path)

        # basic validation
        if not url and not upload_path:
            flash('Please provide a YouTube URL or upload a local file', 'danger')
            return redirect(url_for('video_cut'))

        # if this is a URL request, check cache first
        if url and not upload_path:
            cached = find_cache(url, start_time, end_time, skip_unsilence=skip_unsilence, skip_cropping=skip_cropping)
            if cached:
                video_name, srt_name = cached
                video_path = os.path.join(DOWNLOADS_DIR, video_name)
                srt_path = os.path.join(DOWNLOADS_DIR, srt_name) if srt_name else None
                # ensure both files still exist; if either missing, treat as stale
                if os.path.exists(video_path) and (srt_path is None or os.path.exists(srt_path)):
                    with jobs_lock:
                        active_jobs[job_id] = {'status': 'completed',
                                               'video': video_name,
                                               'srt': srt_name}
                    return {"status": "completed", "job_id": job_id}, 200
                else:
                    # cache is stale; remove entry entirely
                    store_cache(url, start_time, end_time, None, None, skip_unsilence=skip_unsilence, skip_cropping=skip_cropping)

        # start background work
        thread = threading.Thread(target=background_pipeline,
                                  kwargs={
                                      'job_id': job_id,
                                      'url': url if url else None,
                                      'upload_path': upload_path,
                                      'start_time': start_time,
                                      'end_time': end_time,
                                      'skip_unsilence': skip_unsilence,
                                      'skip_cropping': skip_cropping,
                                  },
                                  daemon=True)  # Daemon thread won't block app shutdown
        thread.start()
        return {"status": "accepted", "job_id": job_id}, 202
    return render_template('video_cut.html')


@app.route('/check-job/<job_id>')
@login_required
def check_job(job_id):
    with jobs_lock:
        info = active_jobs.get(job_id, {'status': 'not_found'})
    return json.dumps(info)


@app.route('/api/detect-faces/<job_id>')
@login_required
def api_detect_faces(job_id):
    """Run face detection on a job's current video and return the results.

    The detection runs on the current (possibly cropped) video file.
    Use this to decide whether to toggle skip_cropping for future runs.
    """
    job = active_jobs.get(job_id)
    if not job or 'video' not in job:
        return json.dumps({'ok': False, 'error': 'Job not found or no video yet'}), 404

    video_path = os.path.join(DOWNLOADS_DIR, job['video'])
    if not os.path.exists(video_path):
        return json.dumps({'ok': False, 'error': 'Video file not found on disk'}), 404

    result = detect_faces_in_video(video_path)
    result['ok'] = True
    return json.dumps(result)


@app.route('/api/detect-faces-file', methods=['POST'])
@login_required
def api_detect_faces_file():
    """Run face detection on an uploaded video file (temporary).

    Accepts a multipart upload; saves to a temp location, runs detection,
    then cleans up.  Returns the same dict as /api/detect-faces/<job_id>.
    """
    f = request.files.get('file')
    if not f or not f.filename:
        return json.dumps({'ok': False, 'error': 'No file uploaded'}), 400

    tmp_name = f"face_detect_{int(datetime.datetime.now().timestamp())}_{f.filename}"
    tmp_path = os.path.join(DOWNLOADS_DIR, tmp_name)
    try:
        f.save(tmp_path)
        result = detect_faces_in_video(tmp_path)
        result['ok'] = True
        return json.dumps(result)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass


@app.route('/download/<filename>')
@login_required
def download_file(filename):
    # Sanitize filename to prevent path traversal attacks
    filename = os.path.basename(filename)
    if '/' in filename or '\\' in filename or '..' in filename:
        return '', 403
    return send_from_directory(DOWNLOADS_DIR, filename, as_attachment=True)


@app.route('/preview/<filename>')
@login_required
def preview_file(filename):
    """Serve a file inline (no Content-Disposition: attachment).

    The browser may refuse to show video if the contained codec is unsupported
    (e.g. the old "mp4v" streams produced by the crop helper).  In that case we
    transcode to h264 on-the-fly and send the converted file instead.  The new
    file is cached alongside the original so the conversion only happens once.
    """
    # Sanitize filename to prevent path traversal attacks
    filename = os.path.basename(filename)
    if '/' in filename or '\\' in filename or '..' in filename:
        return '', 403
    search_dirs = [DOWNLOADS_DIR, TEMPLATE_ASSETS_DIR]
    for directory in search_dirs:
        path = os.path.join(directory, filename)
        if os.path.exists(path):
            if directory == DOWNLOADS_DIR and not is_h264(path):
                ext = os.path.splitext(path)[1].lower()
                if ext in ('.mp4', '.mov', '.avi', '.mkv', '.webm', '.m4v', '.flv'):
                    try:
                        new_path = transcode_to_h264(path)
                        filename = os.path.basename(new_path)
                        return send_from_directory(DOWNLOADS_DIR, filename, as_attachment=False)
                    except Exception as e:
                        print(f"preview transcode failed: {e}")
            return send_from_directory(directory, filename, as_attachment=False)
    return '', 404


@app.route('/fonts/<path:filename>')
@login_required
def serve_font(filename):
    """Serve font files from the fonts directory for browser preview."""
    # Sanitize filename to prevent path traversal attacks
    filename = os.path.basename(filename)
    if '/' in filename or '\\' in filename or '..' in filename:
        return '', 403
    return send_from_directory(FONTS_DIR, filename, as_attachment=False)


# High-quality encoding flags reused across all editor render commands.
# The previous CRF 18/slow produced decent results but users complained about
# poor quality; bump CRF to 14 and use a slower preset to maximise fidelity.
# These settings will increase file size but give the best possible output
# from libx264.  You can always override by editing this constant or adding a
# UI control later.
#
# -g 30 / -keyint_min 30 force a keyframe every ~1 second so the video
# starts playing immediately instead of freezing for several seconds while
# the decoder waits for the next GOP boundary.
VIDEO_QUALITY = [
    '-c:v', 'libx264', '-crf', '14', '-preset', 'veryslow',
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
}

FFMPEG_ENCODER_CACHE = {}

def detect_gpu_available():
    """Detect if GPU/CUDA is available for accelerated processing."""
    try:
        result = subprocess.run(
            ['nvidia-smi', '--query-gpu=name', '--format=csv,noheader'],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            gpu_name = result.stdout.strip().split('\n')[0]
            print(f"GPU detected: {gpu_name}")
            return True
    except Exception:
        pass
    print("No GPU detected; using CPU")
    return False

GPU_AVAILABLE = detect_gpu_available()

def select_auto_gpu_encoder():
    """Return the first supported GPU encoder, or None if none are available."""
    for encoder in GPU_ENCODER_QUALITY:
        if ffmpeg_supports_encoder(encoder):
            return encoder
    return None

def ffmpeg_supports_encoder(name):
    if name in FFMPEG_ENCODER_CACHE:
        return FFMPEG_ENCODER_CACHE[name]
    try:
        result = subprocess.run(
            ['ffmpeg', '-hide_banner', '-encoders'],
            capture_output=True, text=True, timeout=15
        )
        supported = name in result.stdout
    except Exception as exc:
        print(f"Warning: ffmpeg encoder probe failed for {name}: {exc}")
        supported = False
    FFMPEG_ENCODER_CACHE[name] = supported
    return supported


@app.route('/')
def index():
    # simple landing page that redirects to login or the main editor
    if current_user.is_authenticated:
        return redirect(url_for('video_cut'))
    return redirect(url_for('login'))


@app.route('/projects')
@login_required
def list_projects():
    # show simple table of all jobs with edit/delete links
    with jobs_lock:
        projects = dict(active_jobs)  # Thread-safe snapshot
    return render_template('projects.html', jobs=projects)


@app.route('/delete_project/<job_id>', methods=['POST'])
@login_required
def delete_project(job_id):
    # remove job state and any downloaded files
    with jobs_lock:
        job = active_jobs.pop(job_id, None)
    if job:
        for key in ('video', 'srt', 'ass'):
            if job.get(key):
                try:
                    os.remove(os.path.join(DOWNLOADS_DIR, job[key]))
                except Exception:
                    pass
        flash(f'Project {job_id} deleted', 'info')
        save_jobs()
    else:
        flash(f'Project {job_id} not found', 'warning')
    return redirect(url_for('list_projects'))


# ═══════════════════════════════════════════════════════════════════════════
#  Template / Preset API – save & restore editor layer configurations
# ═══════════════════════════════════════════════════════════════════════════

@app.route('/api/system-info')
@login_required
def api_system_info():
    """Return system information including GPU availability."""
    gpu_info = {
        'available': GPU_AVAILABLE,
        'gpu_encoder': select_auto_gpu_encoder() or 'none'
    }
    
    # Try to get GPU name
    try:
        result = subprocess.run(
            ['nvidia-smi', '--query-gpu=name', '--format=csv,noheader'],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            gpu_info['gpu_name'] = result.stdout.strip().split('\n')[0]
    except Exception:
        pass
    
    return json.dumps({
        'ok': True,
        'gpu': gpu_info,
        'python_version': f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    })


@login_required
def api_list_templates():
    return json.dumps(get_all_templates())

@app.route('/api/templates/save', methods=['POST'])
@login_required
def api_save_template():
    data = request.get_json(force=True)
    name = (data.get('name') or '').strip()
    config = data.get('config', {})
    if not name:
        return json.dumps({'ok': False, 'error': 'Template name is required'}), 400
    ok = save_template(name, config)
    return json.dumps({'ok': ok})

@app.route('/api/templates/load/<name>', methods=['GET'])
@login_required
def api_load_template(name):
    cfg = get_template(name)
    if cfg is None:
        return json.dumps({'ok': False, 'error': 'Template not found'}), 404
    return json.dumps({'ok': True, 'config': cfg})

@app.route('/api/templates/delete/<name>', methods=['DELETE'])
@login_required
def api_delete_template(name):
    delete_template(name)
    return json.dumps({'ok': True})


@app.route('/editor/<job_id>', methods=['GET', 'POST'])
@login_required
def editor(job_id):
    job = active_jobs.get(job_id)
    if not job or 'video' not in job:
        flash('Job not found or not completed yet', 'danger')
        return redirect(url_for('video_cut'))

    # set once so previews always start from the same source
    if 'base_video' not in job:
        job['base_video'] = job['video']
    base_video = job['base_video']

    # default editor settings: Inter is the preferred font family
    job.setdefault('title_font', 'Inter')
    job.setdefault('sub_font', 'Inter')
    job.setdefault('gpu_mode', 'auto')
    job.setdefault('title_bold', False)
    job.setdefault('sub_bold', False)

    srt_path = os.path.join(DOWNLOADS_DIR, job.get('srt', ''))
    srt_text = ''
    if os.path.exists(srt_path):
        with open(srt_path, encoding='utf-8') as f:
            # normalize line endings and collapse excessive blank lines
            raw = f.read()
        srt_text = re.sub(r"\r\n?|\n", "\n", raw).strip()
        srt_text = re.sub(r"\n{3,}", "\n\n", srt_text)

    if request.method == 'POST':
        save_only = request.form.get('save') == '1'
        # ── save SRT edits ────────────────────────────────────────────────
        # strip accumulated leading/trailing whitespace so every save is clean
        new_srt = request.form.get('srt_text', '').strip()
        # normalize before saving to keep file tidy
        new_srt = re.sub(r"\r\n?|\n", "\n", new_srt)
        new_srt = re.sub(r"\n{3,}", "\n\n", new_srt)
        if os.path.exists(srt_path):
            with open(srt_path, 'w', encoding='utf-8', newline='\n') as f:
                f.write(new_srt)
        srt_text = new_srt

        # ── regenerate ASS from edited SRT ───────────────────────────────
        # The original ASS had per-word karaoke timestamps from Whisper.
        # After the user edits the SRT those timestamps no longer match,
        # so we rebuild a clean ASS directly from the SRT content.
        # The new ASS has one Dialogue event per SRT entry (no karaoke)
        # but inherits all styling via force_style at render time.
        if new_srt:
            new_ass_name = os.path.splitext(job.get('srt', ''))[0] + '.ass'
            try:
                srt_to_ass(new_srt, os.path.join(DOWNLOADS_DIR, new_ass_name))
                job['ass'] = new_ass_name
            except Exception as e:
                update_job(job_id, log=f"warning: could not rebuild ASS from SRT: {e}")
                job.pop('ass', None)

        # ── collect all styling fields ────────────────────────────────────
        def parse_int_field(value, default):
            try:
                iv = int(float(value))
                return str(max(1, iv))
            except Exception:
                return str(default)

        title_text   = request.form.get('title_text', '').strip()
        title_font   = request.form.get('title_font', 'Inter')
        title_bold   = request.form.get('title_bold') == '1'
        title_color  = request.form.get('title_color', '#ffffff')
        title_stroke = request.form.get('title_stroke_color', '#000000')
        title_size   = parse_int_field(request.form.get('title_size', '48'), 48)
        title_x      = parse_int_field(request.form.get('title_x', '10'), 10)
        title_y      = parse_int_field(request.form.get('title_y', '80'), 80)
        sub_font     = request.form.get('sub_font', 'Inter')
        sub_bold     = request.form.get('sub_bold') == '1'
        gpu_mode     = request.form.get('gpu_mode', 'auto')
        sub_color    = request.form.get('sub_color', '#ffffff')
        sub_highlight_color = request.form.get('sub_highlight_color', '#ffff00')
        sub_highlight_text_color = request.form.get('sub_highlight_text_color', '#000000')
        sub_stroke   = request.form.get('sub_stroke_color', '#000000')
        sub_size     = parse_int_field(request.form.get('sub_size', '18'), 18)
        sub_y        = parse_int_field(request.form.get('sub_y', job.get('sub_y', '30')), 30)
        sub_outline_w = parse_int_field(request.form.get('sub_outline_w', '2'), 2)
        sub_hl_box    = parse_int_field(request.form.get('sub_hl_box', '16'), 16)
        sub_bg_enabled = request.form.get('sub_bg_enabled') == '1'
        sub_bg_color = request.form.get('sub_bg_color', '#000000')
        sub_bg_opacity = parse_int_field(request.form.get('sub_bg_opacity', '60'), 60)
        overlay_x    = parse_int_field(request.form.get('overlay_x', job.get('overlay_x', '10')), 10)
        overlay_y    = parse_int_field(request.form.get('overlay_y', job.get('overlay_y', '10')), 10)
        overlay_w    = parse_int_field(request.form.get('overlay_w', job.get('overlay_w', '150')), 150)
        overlay_h    = parse_int_field(request.form.get('overlay_h', job.get('overlay_h', '150')), 150)
        # preview dimensions used for scaling
        prev_w       = float(request.form.get('preview_w') or 0)
        prev_h       = float(request.form.get('preview_h') or 0)

        job.update({
            'title_text': title_text,   'title_font': title_font,
            'title_bold': title_bold,   'title_color': title_color,
            'title_stroke_color': title_stroke,
            'title_size': title_size,   'title_x': title_x, 'title_y': title_y,
            'sub_font': sub_font,       'sub_bold': sub_bold,
            'gpu_mode': gpu_mode,
            'sub_color': sub_color,
            'sub_highlight_color': sub_highlight_color,
            'sub_highlight_text_color': sub_highlight_text_color,
            'sub_stroke_color': sub_stroke, 'sub_size': sub_size, 'sub_y': sub_y,
            'sub_outline_w': sub_outline_w,
            'sub_hl_box': sub_hl_box,
            'sub_bg_enabled': sub_bg_enabled,
            'sub_bg_color': sub_bg_color,
            'sub_bg_opacity': sub_bg_opacity,
            'overlay_x': overlay_x,    'overlay_y': overlay_y,
            'overlay_w': overlay_w,    'overlay_h': overlay_h,
            'preview_w': prev_w, 'preview_h': prev_h,
        })

        # ── download or upload fonts ────────────────────────────────────────
        font_file = request.files.get('font_file')
        if font_file and font_file.filename:
            safe_name = os.path.basename(font_file.filename)
            if allowed_font_file(safe_name):
                dest_path = os.path.join(FONTS_DIR, safe_name)
                font_file.save(dest_path)
                refresh_font_map()
                flash(f"Font uploaded: {safe_name}", 'success')
            else:
                flash('Font upload failed: only .ttf and .otf files are allowed.', 'danger')

        if request.form.get('download_fonts') == '1':
            ensure_default_fonts()
            refresh_font_map()
            save_jobs()
            flash('Downloaded missing Inter fonts to the fonts folder.', 'success')
            return redirect(url_for('editor', job_id=job_id))

        # ── handle overlay upload ─────────────────────────────────────────
        overlay_file = request.files.get('overlay')
        if overlay_file and overlay_file.filename:
            safe_name = os.path.basename(overlay_file.filename)
            overlay_file.save(os.path.join(DOWNLOADS_DIR, safe_name))
            job['overlay'] = safe_name
        # If no new file was uploaded but the hidden form field carries an
        # overlay filename (e.g. restored from a template), link it to the
        # job so render uses it — provided the file actually exists on disk.
        if not job.get('overlay'):
            ov_from_field = request.form.get('current_overlay_file', '').strip()
            overlay_path = resolve_overlay_path(ov_from_field)
            if ov_from_field and overlay_path:
                job['overlay'] = os.path.basename(ov_from_field)
        overlay_filename = job.get('overlay')

        # when saving settings without running ffmpeg we still need to persist
        save_jobs()

        # if the request only wanted to save settings, abort before rendering
        if save_only:
            flash('Project settings saved', 'success')
            return redirect(url_for('editor', job_id=job_id))

        # ── build ffmpeg command ──────────────────────────────────────────
        orig_video     = os.path.basename(base_video)
        new_video_name = f"job_{job_id}_final.mp4"

        has_srt     = os.path.exists(srt_path) and os.path.getsize(srt_path) > 0
        overlay_path = resolve_overlay_path(overlay_filename)
        has_overlay = bool(overlay_path)
        has_title   = bool(title_text)

        # before we build filters, scale coords/sizes if preview dimensions are
        # provided and differ from the actual video size.  preview width/height
        # may be filled by client JS when the metadata loads; if empty we skip.
        vid_w, vid_h = get_video_size(os.path.join(DOWNLOADS_DIR, orig_video))
        try:
            prev_w = float(job.get('preview_w') or prev_w)
            prev_h = float(job.get('preview_h') or prev_h)
        except Exception:
            prev_w = prev_h = 0
        if prev_w > 0 and prev_h > 0 and (prev_w != vid_w or prev_h != vid_h):
            sx = vid_w / prev_w
            sy = vid_h / prev_h
            # scale each numeric field appropriately
            overlay_x = str(int(float(overlay_x) * sx))
            overlay_y = str(int(float(overlay_y) * sy))
            overlay_w = str(int(float(overlay_w) * sx))
            overlay_h = str(int(float(overlay_h) * sy))
            title_x   = str(int(float(title_x)   * sx))
            title_y   = str(int(float(title_y)   * sy))
            sub_y     = str(int(float(sub_y)     * sy))
            # persist scaled values so future edits use the same coordinate system
            job.update({
                'overlay_x': overlay_x, 'overlay_y': overlay_y,
                'overlay_w': overlay_w, 'overlay_h': overlay_h,
                'title_x': title_x, 'title_y': title_y, 'sub_y': sub_y,
                'preview_w': vid_w, 'preview_h': vid_h,
            })
            update_job(job_id, log=f"scaling coords from preview {prev_w}x{prev_h}→{vid_w}x{vid_h}")

        # filters applied after optional overlay compositing
        vf_parts = []

        effective_sub_font = sub_font
        if sub_bold and not effective_sub_font.endswith(' Bold'):
            effective_sub_font = f"{effective_sub_font} Bold"

        if has_srt:
            # Use ASS file for word-level background-highlight when available
            ass_name = job.get('ass', '')
            ass_path = os.path.join(DOWNLOADS_DIR, ass_name) if ass_name else ''
            use_karaoke = bool(ass_name) and os.path.isfile(ass_path)

            if use_karaoke:
                sub_highlight = job.get('sub_highlight_color', '#ffff00')
                sub_highlight_text = job.get('sub_highlight_text_color', '#000000')
                # Convert HTML colours to ASS BBGGRR format for \\3c/\\1c tags.
                # html_to_ass_color returns &H00BBGGRR — we strip the &H00 prefix
                # and trailing & to get the 6-char BBGGRR used in override tags.
                ass_hl_full = html_to_ass_color(sub_highlight)
                hl_hex = ass_hl_full[4:10]  # BBGGRR for highlight background
                # Text on highlight background – user-chosen colour
                ass_hl_text_full = html_to_ass_color(sub_highlight_text)
                hl_fg = ass_hl_text_full[4:10]
                # Use user-controlled highlight box thickness instead of computed
                border_px = sub_hl_box

                with open(ass_path, 'r', encoding='utf-8') as _af:
                    ass_content = _af.read()
                ass_content = (ass_content
                    .replace('##HLBG##', hl_hex)
                    .replace('##HLFG##', hl_fg)
                    .replace('##BORD##', border_px))
                patched_ass = os.path.join(DOWNLOADS_DIR, f"job_{job_id}_patched.ass")
                with open(patched_ass, 'w', encoding='utf-8', newline='\n') as _af:
                    _af.write(ass_content)
                ass_abs = patched_ass.replace('\\', '/').replace(':', '\\:')

                # Build ASS style: BorderStyle=3 gives an opaque background box;
                # BorderStyle=1 is the default outline-only mode.
                if sub_bg_enabled:
                    # Convert CSS opacity (0-100) to ASS alpha (00=opaque, FF=transparent)
                    ass_alpha = format(int(255 * (1 - int(sub_bg_opacity) / 100)), '02X')
                    bg_ass = f"&H{ass_alpha}" + html_to_ass_color(sub_bg_color)[3:]  # strip &H00, prepend alpha
                    sub_style = (
                        f"FontName={effective_sub_font},FontSize={sub_size},"
                        f"PrimaryColour={html_to_ass_color(sub_color)},"
                        f"OutlineColour={html_to_ass_color(sub_stroke)},"
                        f"BackColour={bg_ass},"
                        f"BorderStyle=3,Outline={sub_outline_w},Shadow=1,"
                        f"Alignment=2,MarginV={sub_y}"
                    )
                else:
                    sub_style = (
                        f"FontName={effective_sub_font},FontSize={sub_size},"
                        f"PrimaryColour={html_to_ass_color(sub_color)},"
                        f"OutlineColour={html_to_ass_color(sub_stroke)},"
                        f"Outline={sub_outline_w},Alignment=2,MarginV={sub_y}"
                    )
                vf_parts.append(f"subtitles='{ass_abs}':force_style='{sub_style}'")
            else:
                # Fall back to plain SRT when no ASS file exists
                srt_abs = srt_path.replace('\\', '/').replace(':', '\\:')
                if sub_bg_enabled:
                    ass_alpha = format(int(255 * (1 - int(sub_bg_opacity) / 100)), '02X')
                    bg_ass = f"&H{ass_alpha}" + html_to_ass_color(sub_bg_color)[3:]
                    sub_style = (
                        f"FontName={effective_sub_font},FontSize={sub_size},"
                        f"PrimaryColour={html_to_ass_color(sub_color)},"
                        f"OutlineColour={html_to_ass_color(sub_stroke)},"
                        f"BackColour={bg_ass},"
                        f"BorderStyle=3,Outline={sub_outline_w},Shadow=1,"
                        f"Alignment=2,MarginV={sub_y}"
                    )
                else:
                    sub_style = (
                        f"FontName={effective_sub_font},FontSize={sub_size},"
                        f"PrimaryColour={html_to_ass_color(sub_color)},"
                        f"OutlineColour={html_to_ass_color(sub_stroke)},"
                        f"Outline={sub_outline_w},Alignment=2,MarginV={sub_y}"
                    )
                vf_parts.append(f"subtitles='{srt_abs}':force_style='{sub_style}'")

        if has_title:
            effective_title_font = title_font
            if title_bold and not effective_title_font.endswith(' Bold'):
                effective_title_font = f"{effective_title_font} Bold"
            font_file = FONT_MAP.get(effective_title_font) or FONT_MAP.get(title_font) or 'C:/Windows/Fonts/arial.ttf'
            font_esc  = font_file.replace(':', '\\:')
            # Write title text to a UTF-8 file so drawtext handles any Unicode
            # (Romanian diacritics, etc.) without encoding issues on Windows.
            title_txt_name = f"job_{job_id}_title.txt"
            title_txt_path = os.path.join(DOWNLOADS_DIR, title_txt_name)
            with open(title_txt_path, 'w', encoding='utf-8') as _tf:
                _tf.write(title_text)
            title_txt_esc = title_txt_path.replace('\\', '/').replace(':', '\\:')
            tc  = html_to_drawtext_color(title_color)
            sc  = html_to_drawtext_color(title_stroke)
            bw  = max(1, int(title_size) // 14)
            title_y_top = max(0, int(title_y) - round(int(title_size) * 0.78))
            vf_parts.append(
                f"drawtext=fontfile='{font_esc}':textfile='{title_txt_esc}'"
                f":fontsize={title_size}:fontcolor={tc}"
                f":x={title_x}:y={title_y_top}"
                f":bordercolor={sc}:borderw={bw}"
            )

        # Always write the filter graph to a script file and use
        # -filter_complex_script so Windows never interprets special characters
        # (&H colours, Unicode text, semicolons, single-quotes) on the command line.
        fc_script_name = f"job_{job_id}_fc.txt"
        fc_script_path = os.path.join(DOWNLOADS_DIR, fc_script_name)

        render_quality = VIDEO_QUALITY
        if gpu_mode == 'auto':
            selected_gpu = select_auto_gpu_encoder()
            if selected_gpu:
                render_quality = GPU_ENCODER_QUALITY[selected_gpu]
                update_job(job_id, log=f"auto GPU mode selected {selected_gpu}")
            else:
                update_job(job_id, log="auto GPU mode selected but no supported encoder found, using CPU")
                flash('No supported GPU encoder found; using CPU encoder instead.', 'warning')
        elif gpu_mode in GPU_ENCODER_QUALITY:
            if ffmpeg_supports_encoder(gpu_mode):
                render_quality = GPU_ENCODER_QUALITY[gpu_mode]
                update_job(job_id, log=f"using GPU encoder {gpu_mode}")
            else:
                update_job(job_id, log=f"GPU encoder {gpu_mode} unavailable, falling back to CPU")
                flash('Requested GPU render mode unavailable; using CPU encoder instead.', 'warning')

        if has_overlay:
            ov_scale = f"[1:v]scale={overlay_w}:{overlay_h}[ov]"
            if vf_parts:
                vf_chain = ','.join(vf_parts)
                fc = (
                    f"{ov_scale};"
                    f"[0:v][ov]overlay={overlay_x}:{overlay_y}[ovout];"
                    f"[ovout]{vf_chain}[final]"
                )
                out_label = '[final]'
            else:
                fc = (
                    f"{ov_scale};"
                    f"[0:v][ov]overlay={overlay_x}:{overlay_y}[vout]"
                )
                out_label = '[vout]'
            with open(fc_script_path, 'w', encoding='utf-8') as _fc:
                _fc.write(fc)
            update_job(job_id, log=f"filter_complex (overlay): {fc}")
            cmd = [
                'ffmpeg', '-nostdin', '-y', '-fontsdir', FONTS_DIR,
                '-i', orig_video, '-i', overlay_path,
                '-filter_complex_script', fc_script_name,
                '-map', out_label, '-map', '0:a?',
                *render_quality, '-c:a', 'copy', new_video_name,
            ]
        elif vf_parts:
            vf_chain = ','.join(vf_parts)
            fc = f"[0:v]{vf_chain}[vout]"
            with open(fc_script_path, 'w', encoding='utf-8') as _fc:
                _fc.write(fc)
            update_job(job_id, log=f"filter_complex (no overlay): {fc}")
            cmd = [
                'ffmpeg', '-nostdin', '-y', '-fontsdir', FONTS_DIR,
                '-i', orig_video,
                '-filter_complex_script', fc_script_name,
                '-map', '[vout]', '-map', '0:a?',
                *render_quality, '-c:a', 'copy', new_video_name,
            ]
        else:
            cmd = ['ffmpeg', '-nostdin', '-y', '-fontsdir', FONTS_DIR,
                   '-i', orig_video,
                   *render_quality, '-c:a', 'copy', new_video_name]

        update_job(job_id, log=f"ffmpeg: {' '.join(cmd)}")
        result = subprocess.run(cmd, cwd=DOWNLOADS_DIR, capture_output=True, text=True)
        if result.returncode != 0:
            update_job(job_id, log=f"ffmpeg stderr: {result.stderr[-600:]}")
            flash('Render failed — check server logs for details.', 'danger')
        else:
            # sanity checks: output file should exist and be non-trivial size
            out_path = os.path.join(DOWNLOADS_DIR, new_video_name)
            if not os.path.exists(out_path) or os.path.getsize(out_path) < 1000:
                update_job(job_id, status='error',
                           log='output file missing or too small, ffmpeg may have succeeded with warnings')
                flash('Render produced invalid output – check server logs.', 'danger')
            else:
                job['video'] = new_video_name
                update_job(job_id, log="render complete")
                flash('Video rendered successfully!', 'success')

        return redirect(url_for('editor', job_id=job_id))

    sorted_fonts = sorted(
        FONT_MAP.keys(),
        key=lambda name: (0 if name == 'Inter' else 1 if name == 'Inter Bold' else 2, name)
    )
    font_faces = []
    fonts_dir_abs = os.path.abspath(FONTS_DIR)
    for font_name, font_path in FONT_MAP.items():
        abs_path = os.path.abspath(font_path)
        if not abs_path.startswith(fonts_dir_abs):
            continue
        font_file = os.path.basename(abs_path)
        if not font_file:
            continue
        ext = os.path.splitext(font_file)[1].lower()
        font_fmt = 'opentype' if ext == '.otf' else 'truetype'
        font_faces.append({
            'name': font_name,
            'url': url_for('serve_font', filename=font_file),
            'format': font_fmt,
        })
    return render_template('editor.html', job=job, srt_content=srt_text,
                           job_key=job_id, font_list=sorted_fonts,
                           font_faces=font_faces,
                           templates=get_all_templates())


# ---------------------------------------------------------------------------
# entrypoint
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    # Log system information at startup
    print("="*70)
    print("Generate Shorts - Video Processing App")
    print("="*70)
    print(f"Python: {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
    print(f"GPU Available: {GPU_AVAILABLE}")
    gpu_encoder = select_auto_gpu_encoder()
    if gpu_encoder:
        print(f"FFmpeg GPU Encoder: {gpu_encoder}")
    print(f"Base Directory: {BASE_DIR}")
    print(f"Downloads: {DOWNLOADS_DIR}")
    print("="*70)
    # allow port overridable by PORT env variable for hosting platforms
    port = int(os.getenv('PORT', 5015))
    debug = os.getenv('FLASK_DEBUG', '1') == '1'
    # run via socketio to support websockets
    socketio.run(app, host='0.0.0.0', port=port, debug=debug, use_reloader=False)
