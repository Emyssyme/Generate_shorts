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
import sqlite3
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

# build font map from folder, fall back to Windows system fonts
FONT_MAP = {}
for fname in os.listdir(FONTS_DIR):
    if fname.lower().endswith(('.ttf', '.otf')):
        name = os.path.splitext(fname)[0]
        FONT_MAP[name] = os.path.join(FONTS_DIR, fname)

# default system fonts if folder is empty or missing entries
if not FONT_MAP:
    if sys.platform.startswith('win'):
        FONT_MAP = {
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
        # common Linux fonts
        FONT_MAP = {
            'DejaVu Sans':      '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
            'DejaVu Sans Bold': '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
            'Liberation Sans':  '/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf',
            'Liberation Sans Bold': '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf',
            'FreeSerif':        '/usr/share/fonts/truetype/freefont/FreeSerif.ttf',
            'FreeSans':         '/usr/share/fonts/truetype/freefont/FreeSans.ttf',
        }

# simple sqlite cache to avoid reprocessing identical url/time ranges
CACHE_DB = os.path.join(BASE_DIR, "cache.db")
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
    c.execute('''
        CREATE TABLE IF NOT EXISTS cache (
            url TEXT,
            start TEXT,
            end TEXT,
            video TEXT,
            srt TEXT,
            skip_unsilence INTEGER DEFAULT 0,
            UNIQUE(url, start, end, skip_unsilence)
        )
    ''')
    conn.commit()
    conn.close()

def find_cache(url, start, end, skip_unsilence=False):
    conn = sqlite3.connect(CACHE_DB)
    c = conn.cursor()
    c.execute('SELECT video, srt FROM cache WHERE url=? AND start=? AND end=? AND skip_unsilence=?',
              (url or '', start or '', end or '', int(skip_unsilence)))
    row = c.fetchone()
    conn.close()
    return row  # either None or (video, srt)

def store_cache(url, start, end, video, srt, skip_unsilence=False):
    conn = sqlite3.connect(CACHE_DB)
    c = conn.cursor()
    try:
        if video is None or srt is None:
            # remove stale entry for this configuration
            c.execute('DELETE FROM cache WHERE url=? AND start=? AND end=? AND skip_unsilence=?',
                      (url or '', start or '', end or '', int(skip_unsilence)))
        else:
            c.execute('INSERT OR REPLACE INTO cache (url, start, end, video, srt, skip_unsilence) VALUES (?,?,?,?,?,?)',
                      (url or '', start or '', end or '', video, srt, int(skip_unsilence)))
        conn.commit()
    finally:
        conn.close()

# initialize the cache when the module loads
init_cache()

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

# ---------------------------------------------------------------------------
# Editor: font mapping (Windows system fonts) and colour helpers
# ---------------------------------------------------------------------------
FONT_MAP = {
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
        '-c:a', 'copy', dst_path
    ], check=True)
    return dst_path


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
        for line in proc.stdout:
            update_job(job_id, log=line.rstrip())
        proc.wait()
        if proc.returncode != 0:
            raise RuntimeError(f"unsilence failed (see logs) returncode={proc.returncode}")
    else:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"unsilence failed: {result.stderr}")


def run_crop(input_video, output_dir, overlay=None):
    """Crop the face vertically; returns path of resulting video.

    The helper script produces MP4 video using OpenCV's "mp4v" codec which
    browsers often cannot decode (hence the preview would show only audio).
    After the helper finishes we transcode the result to h264 so the HTML5
    <video> element can play it reliably.
    """
    script = find_script("_crop_face_vertical.py")
    cmd = [sys.executable, script, "--input", input_video, "--output", output_dir]
    if overlay:
        cmd.extend(["--overlay", overlay])
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"crop failed: {result.stderr}")
    # script names output as <basename>_processed.mp4
    base = os.path.splitext(os.path.basename(input_video))[0]
    cropped = os.path.join(output_dir, base + "_processed.mp4")
    # always transcode to h264 for browser compatibility
    trans = os.path.join(output_dir, base + "_processed_h264.mp4")
    try:
        subprocess.run([
            'ffmpeg', '-nostdin', '-y', '-i', cropped,
            '-c:v', 'libx264', '-crf', '18', '-preset', 'fast',
            '-c:a', 'copy', trans
        ], check=True)
        return trans
    except subprocess.CalledProcessError as e:
        # if transcoding fails fall back to original cropped file
        update_job(0, log=f"warning: h264 transcode failed, using original: {e}")
        return cropped


def run_subtitles(input_video, output_dir, model="large", max_length=22, job_id=None):
    """Generate subtitles for a single video.

    If ``job_id`` is passed, stream the helper script's output into the job log.
    """
    script = find_script("_generate_subtitles.py")
    cmd = [sys.executable, script, "--input", input_video, "--output", output_dir,
           "--model", model, "--max-length", str(max_length)]
    if job_id:
        update_job(job_id, log="starting subtitle generation")
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in proc.stdout:
            update_job(job_id, log=line.rstrip())
        proc.wait()
        if proc.returncode != 0:
            raise RuntimeError(f"subtitle generation failed (see logs); rc={proc.returncode}")
    else:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"subtitle generation failed: {result.stderr}")
    # output srt name (the helper uses same convention)
    base = os.path.splitext(os.path.basename(input_video))[0]
    return os.path.join(output_dir, base + ".srt")


def background_pipeline(job_id, url=None, upload_path=None, start_time="0", end_time=None, skip_unsilence=False):
    """Background thread that cuts/downloads then optionally unsilences, crops, subtitles.

    ``skip_unsilence`` is used when the user knows the clip already has clean audio.
    This is recorded in the cache so repeated calls with the same parameters will
    behave identically.
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
                subprocess.run(cmd, check=True)
            except subprocess.CalledProcessError as e:
                # some videos/datacenters don't support range requests; fall back to full
                update_job(job_id, log="section download failed, falling back to full download and manual trim")
                full = os.path.join(DOWNLOADS_DIR, f"job_{job_id}_full.mp4")
                subprocess.run([
                    "yt-dlp", "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
                    "-o", full, url
                ], check=True)
                ff = ["ffmpeg", "-y", "-i", full, "-ss", start_time]
                if end_time:
                    ff += ["-to", end_time]
                ff += ["-c", "copy", cut_path]
                subprocess.run(ff, check=True)
        else:
            update_job(job_id, status="cutting", log=f"trimming {upload_path}")
            # trim local file
            ff = ["ffmpeg", "-y", "-i", upload_path, "-ss", start_time]
            if end_time:
                ff += ["-to", end_time]
            ff += ["-c", "copy", cut_path]
            subprocess.run(ff, check=True)

        if skip_unsilence:
            update_job(job_id, status="skipping unsilence", log="user requested no audio cleaning")
            unsilenced = cut_path
        else:
            update_job(job_id, status="unsilencing", log="calling unsilence script")
            unsilenced = os.path.join(DOWNLOADS_DIR, f"job_{job_id}_unsilenced.mp4")
            run_unsilence(cut_path, unsilenced, job_id=job_id)

        update_job(job_id, status="cropping", log="running crop script")
        cropped = run_crop(unsilenced, DOWNLOADS_DIR)

        update_job(job_id, status="subtitling", log="generating subtitles")
        srtfile = run_subtitles(cropped, DOWNLOADS_DIR, job_id=job_id)

        # sanity check: ensure subtitles were actually written
        if not os.path.exists(srtfile):
            raise RuntimeError(f"subtitle file not found after generation: {srtfile}")

        update_job(job_id, status="completed",
                   video=os.path.basename(cropped),
                   srt=os.path.basename(srtfile),
                   log="all steps finished")
        # cache this result for future identical requests
        if url:
            store_cache(url, start_time, end_time,
                        os.path.basename(cropped), os.path.basename(srtfile),
                        skip_unsilence=skip_unsilence)
    except Exception as e:
        update_job(job_id, status="error", msg=str(e))


@app.route('/video-cut', methods=['GET', 'POST'])
@login_required 
def video_cut():
    if request.method == 'POST':
        url = request.form.get('url', '').strip()
        start_time = request.form.get('start_time') or "0"
        end_time = request.form.get('end_time')
        skip_unsilence = request.form.get('skip_unsilence') == 'on'
        job_id = request.form.get('job_id') or f"J{int(datetime.datetime.now().timestamp())}"

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
            cached = find_cache(url, start_time, end_time, skip_unsilence=skip_unsilence)
            if cached:
                video_name, srt_name = cached
                video_path = os.path.join(DOWNLOADS_DIR, video_name)
                srt_path = os.path.join(DOWNLOADS_DIR, srt_name) if srt_name else None
                # ensure both files still exist; if either missing, treat as stale
                if os.path.exists(video_path) and (srt_path is None or os.path.exists(srt_path)):
                    active_jobs[job_id] = {'status': 'completed',
                                           'video': video_name,
                                           'srt': srt_name}
                    return {"status": "completed", "job_id": job_id}, 200
                else:
                    # cache is stale; remove entry entirely
                    store_cache(url, start_time, end_time, None, None, skip_unsilence=skip_unsilence)

        # start background work
        thread = threading.Thread(target=background_pipeline,
                                  kwargs={
                                      'job_id': job_id,
                                      'url': url if url else None,
                                      'upload_path': upload_path,
                                      'start_time': start_time,
                                      'end_time': end_time,
                                      'skip_unsilence': skip_unsilence,
                                  })
        thread.start()
        return {"status": "accepted", "job_id": job_id}, 202
    return render_template('video_cut.html')


@app.route('/check-job/<job_id>')
@login_required
def check_job(job_id):
    info = active_jobs.get(job_id, {'status': 'not_found'})
    return json.dumps(info)


@app.route('/download/<filename>')
@login_required
def download_file(filename):
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
    path = os.path.join(DOWNLOADS_DIR, filename)
    if os.path.exists(path) and not is_h264(path):
        try:
            new_path = transcode_to_h264(path)
            filename = os.path.basename(new_path)
        except Exception as e:
            # conversion failed; log but fall back to original and hope for the best
            print(f"preview transcode failed: {e}")
    return send_from_directory(DOWNLOADS_DIR, filename, as_attachment=False)


# High-quality encoding flags reused across all editor render commands.
# The previous CRF 18/slow produced decent results but users complained about
# poor quality; bump CRF to 14 and use a slower preset to maximise fidelity.
# These settings will increase file size but give the best possible output
# from libx264.  You can always override by editing this constant or adding a
# UI control later.
VIDEO_QUALITY = [
    '-c:v', 'libx264', '-crf', '14', '-preset', 'veryslow',
    '-pix_fmt', 'yuv420p', '-movflags', '+faststart'
]



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
    return render_template('projects.html', jobs=active_jobs)


@app.route('/delete_project/<job_id>', methods=['POST'])
@login_required
def delete_project(job_id):
    # remove job state and any downloaded files
    job = active_jobs.pop(job_id, None)
    if job:
        for key in ('video','srt'):
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

        # ── collect all styling fields ────────────────────────────────────
        title_text   = request.form.get('title_text', '').strip()
        title_font   = request.form.get('title_font', 'Arial')
        title_color  = request.form.get('title_color', '#ffffff')
        title_stroke = request.form.get('title_stroke_color', '#000000')
        title_size   = request.form.get('title_size', '48')
        title_x      = request.form.get('title_x', '10')
        title_y      = request.form.get('title_y', '80')
        sub_font     = request.form.get('sub_font', 'Arial')
        sub_color    = request.form.get('sub_color', '#ffffff')
        sub_stroke   = request.form.get('sub_stroke_color', '#000000')
        sub_size     = request.form.get('sub_size', '18')
        sub_y        = request.form.get('sub_y', job.get('sub_y', '30'))
        overlay_x    = request.form.get('overlay_x', job.get('overlay_x', '10'))
        overlay_y    = request.form.get('overlay_y', job.get('overlay_y', '10'))
        overlay_w    = request.form.get('overlay_w', job.get('overlay_w', '150'))
        overlay_h    = request.form.get('overlay_h', job.get('overlay_h', '150'))
        # preview dimensions used for scaling
        prev_w       = float(request.form.get('preview_w') or 0)
        prev_h       = float(request.form.get('preview_h') or 0)

        job.update({
            'title_text': title_text,   'title_font': title_font,
            'title_color': title_color, 'title_stroke_color': title_stroke,
            'title_size': title_size,   'title_x': title_x, 'title_y': title_y,
            'sub_font': sub_font,       'sub_color': sub_color,
            'sub_stroke_color': sub_stroke, 'sub_size': sub_size, 'sub_y': sub_y,
            'overlay_x': overlay_x,    'overlay_y': overlay_y,
            'overlay_w': overlay_w,    'overlay_h': overlay_h,
            'preview_w': prev_w, 'preview_h': prev_h,
        })
        # when saving settings without running ffmpeg we still need to persist
        save_jobs()

        # ── handle overlay upload ─────────────────────────────────────────
        overlay_file = request.files.get('overlay')
        if overlay_file and overlay_file.filename:
            safe_name = os.path.basename(overlay_file.filename)
            overlay_file.save(os.path.join(DOWNLOADS_DIR, safe_name))
            job['overlay'] = safe_name
        overlay_filename = job.get('overlay')

        # if the request only wanted to save settings, abort before rendering
        if save_only:
            flash('Project settings saved', 'success')
            return redirect(url_for('editor', job_id=job_id))

        # ── build ffmpeg command ──────────────────────────────────────────
        orig_video     = os.path.basename(base_video)
        new_video_name = f"job_{job_id}_final.mp4"

        has_srt     = os.path.exists(srt_path) and os.path.getsize(srt_path) > 0
        has_overlay = bool(overlay_filename and
                           os.path.exists(os.path.join(DOWNLOADS_DIR, overlay_filename)))
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

        if has_srt:
            # Escape drive-letter colon so ffmpeg filter parser treats it literally.
            srt_abs = srt_path.replace('\\', '/').replace(':', '\\:')
            vid_w, vid_h = get_video_size(os.path.join(DOWNLOADS_DIR, orig_video))
            sub_style = (
                f"PlayResX={vid_w},PlayResY={vid_h},"
                f"FontName={sub_font},FontSize={sub_size},"
                f"PrimaryColour={html_to_ass_color(sub_color)},"
                f"OutlineColour={html_to_ass_color(sub_stroke)},"
                f"Outline=2,Alignment=2,MarginV={sub_y}"
            )
            vf_parts.append(f"subtitles='{srt_abs}':force_style='{sub_style}'")

        if has_title:
            font_file = FONT_MAP.get(title_font, 'C:/Windows/Fonts/arial.ttf')
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
                fc = f"{ov_scale};[0:v][ov]overlay={overlay_x}:{overlay_y}[vout]"
                out_label = '[vout]'
            with open(fc_script_path, 'w', encoding='utf-8') as _fc:
                _fc.write(fc)
            update_job(job_id, log=f"filter_complex (overlay): {fc}")
            cmd = [
                'ffmpeg', '-nostdin', '-y',
                '-i', orig_video, '-i', overlay_filename,
                '-filter_complex_script', fc_script_name,
                '-map', out_label, '-map', '0:a?',
                *VIDEO_QUALITY, '-c:a', 'copy', new_video_name,
            ]
        elif vf_parts:
            vf_chain = ','.join(vf_parts)
            fc = f"[0:v]{vf_chain}[vout]"
            with open(fc_script_path, 'w', encoding='utf-8') as _fc:
                _fc.write(fc)
            update_job(job_id, log=f"filter_complex (no overlay): {fc}")
            cmd = [
                'ffmpeg', '-nostdin', '-y', '-i', orig_video,
                '-filter_complex_script', fc_script_name,
                '-map', '[vout]', '-map', '0:a?',
                *VIDEO_QUALITY, '-c:a', 'copy', new_video_name,
            ]
        else:
            cmd = ['ffmpeg', '-nostdin', '-y', '-i', orig_video, '-c', 'copy', new_video_name]

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

    return render_template('editor.html', job=job, srt_content=srt_text,
                           job_key=job_id, font_list=list(FONT_MAP.keys()))


# ---------------------------------------------------------------------------
# entrypoint
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    # allow port overridable by PORT env variable for hosting platforms
    port = int(os.getenv('PORT', 5000))
    debug = os.getenv('FLASK_DEBUG', '1') == '1'
    # run via socketio to support websockets
    socketio.run(app, host='0.0.0.0', port=port, debug=debug, use_reloader=False)
