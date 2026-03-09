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

# in‑memory job state
active_jobs = {}

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

def find_script(name):
    """Locate a helper script by name in the current directory or its parent.

    This project historically has helpers either next to ``app.py`` or in the
    workspace root.  ``find_script`` tries both places and raises if the file
    cannot be found so that the caller can surface a useful error.
    """
    base = os.path.dirname(__file__)
    candidates = [os.path.join(base, name), os.path.join(base, "..", name)]
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


def run_unsilence(input_video, output_video, job_id=None):
    """Call the unsilence script on a single file.

    If ``job_id`` is provided the output from the helper script will be
    appended to the job's log so the web UI can display live progress.
    """
    script = find_script("_unsilece_files_from_folder.py")
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
    """Crop the face vertically; returns path of resulting video."""
    script = find_script("_crop_face_vertical_v5_folder.py")
    cmd = [sys.executable, script, "--input", input_video, "--output", output_dir]
    if overlay:
        cmd.extend(["--overlay", overlay])
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"crop failed: {result.stderr}")
    # script names output as <basename>_processed.mp4
    base = os.path.splitext(os.path.basename(input_video))[0]
    return os.path.join(output_dir, base + "_processed.mp4")


def run_subtitles(input_video, output_dir, model="large", max_length=22, job_id=None):
    """Generate subtitles for a single video.

    If ``job_id`` is passed, stream the helper script's output into the job log.
    """
    script = find_script("_Generate_subtitles_from_video_folder.py")
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


def background_pipeline(job_id, url=None, upload_path=None, start_time="0", end_time=None):
    """Background thread that cuts/downloads then unsilences, crops, subtitles."""
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
    except Exception as e:
        update_job(job_id, status="error", msg=str(e))


@app.route('/video-cut', methods=['GET', 'POST'])
@login_required 
def video_cut():
    if request.method == 'POST':
        url = request.form.get('url', '').strip()
        start_time = request.form.get('start_time') or "0"
        end_time = request.form.get('end_time')
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

        # start background work
        thread = threading.Thread(target=background_pipeline,
                                  kwargs={
                                      'job_id': job_id,
                                      'url': url if url else None,
                                      'upload_path': upload_path,
                                      'start_time': start_time,
                                      'end_time': end_time
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



@app.route('/')
def index():
    # simple landing page that redirects to login or the main editor
    if current_user.is_authenticated:
        return redirect(url_for('video_cut'))
    return redirect(url_for('login'))


@app.route('/editor/<job_id>', methods=['GET', 'POST'])
@login_required
def editor(job_id):
    job = active_jobs.get(job_id)
    if not job or 'video' not in job:
        flash('Job not found or not completed yet', 'danger')
        return redirect(url_for('video_cut'))

    srt_path = os.path.join(DOWNLOADS_DIR, job.get('srt',''))
    srt_text = ''
    if os.path.exists(srt_path):
        with open(srt_path, encoding='utf-8') as f:
            srt_text = f.read()

    if request.method == 'POST':
        # save subtitle edits
        new_text = request.form.get('srt_text', '')
        with open(srt_path, 'w', encoding='utf-8') as f:
            f.write(new_text)

        # optional overlay
        overlay_file = request.files.get('overlay')
        overlay_x = request.form.get('overlay_x', '10')
        overlay_y = request.form.get('overlay_y', '10')
        if overlay_file and overlay_file.filename:
            overlay_path = os.path.join(DOWNLOADS_DIR, overlay_file.filename)
            overlay_file.save(overlay_path)
            orig_video = os.path.join(DOWNLOADS_DIR, job['video'])
            new_video = os.path.join(DOWNLOADS_DIR, job['video'].rsplit('.',1)[0] + '_overlay.mp4')
            filter_str = f"overlay={overlay_x}:{overlay_y}"
            cmd = [
                'ffmpeg', '-y', '-i', orig_video, '-i', overlay_path,
                '-filter_complex', filter_str,
                '-c:a', 'copy', new_video
            ]
            subprocess.run(cmd)
            job['video'] = os.path.basename(new_video)
            flash('Overlay applied and video updated', 'success')
        else:
            flash('Subtitles saved', 'success')

        return redirect(url_for('editor', job_id=job_id))

    return render_template('editor.html', job=job, srt_content=srt_text, job_key=job_id)


# ---------------------------------------------------------------------------
# entrypoint
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    # allow port overridable by PORT env variable for hosting platforms
    port = int(os.getenv('PORT', 5000))
    debug = os.getenv('FLASK_DEBUG', '1') == '1'
    # run via socketio to support websockets
    socketio.run(app, host='0.0.0.0', port=port, debug=debug, use_reloader=False)
