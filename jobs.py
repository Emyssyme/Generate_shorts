"""In-memory job state (persisted to jobs.json), naming and cancel/delete."""
import datetime
import json
import os
import re
import subprocess
import threading
import unicodedata

from config import DOWNLOADS_DIR, JOBS_FILE


NON_TERMINAL_STATUSES = {
    'running', 'starting', 'downloading', 'cutting', 'unsilencing',
    'skipping unsilence', 'cropping', 'skipping cropping', 'subtitling',
    'skipping cutting', 'skipping subtitles',
}


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

# ── Cancel support ────────────────────────────────────────────────────
# Each job can be cancelled via the /cancel-job/<job_id> endpoint.
# ``job_cancel_events`` stores a threading.Event per job that the
# background_pipeline thread checks between major steps.
# ``_active_subprocesses`` maps job_id → subprocess.Popen so we can
# kill long-running subprocesses (e.g. subtitle generation) on cancel.
job_cancel_events: dict[str, threading.Event] = {}
_active_subprocesses: dict[str, subprocess.Popen] = {}


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


def make_project_name(label, when=None):
    """Build a project name: <source name>_YYYY-MM-DD_HH-MM-SS."""
    when = when or datetime.datetime.now()
    label = unicodedata.normalize('NFKC', label or '').strip()
    label = re.sub(r'[^\w\- ]', '', label, flags=re.UNICODE)
    label = re.sub(r'\s+', '_', label).strip('_-')[:60] or 'project'
    return f"{label}_{when.strftime('%Y-%m-%d_%H-%M-%S')}"


def fetch_youtube_title(url):
    """Return the video title via yt-dlp, or None if it can't be fetched."""
    try:
        r = subprocess.run(
            ["yt-dlp", "--no-warnings", "--skip-download", "--no-playlist",
             "--print", "title", url],
            capture_output=True, text=True, encoding='utf-8', timeout=30)
        lines = [l.strip() for l in r.stdout.splitlines() if l.strip()]
        return lines[0] if lines else None
    except Exception:
        return None


def request_cancel(job_id):
    """Cancel a running job.

    Sets the threading.Event that the pipeline checks between major steps and
    kills any active subprocess (e.g. subtitle generation) tied to the job.
    """
    cancel_evt = job_cancel_events.get(job_id)
    if cancel_evt:
        cancel_evt.set()

    proc = _active_subprocesses.pop(job_id, None)
    if proc and proc.poll() is None:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass

    with jobs_lock:
        if job_id in active_jobs:
            active_jobs[job_id]['status'] = 'cancelled'
            active_jobs[job_id].setdefault('log', []).append('job cancelled by user')
            save_jobs()


def remove_job(job_id):
    """Delete a job and its files. Returns False if the job doesn't exist."""
    with jobs_lock:
        job = active_jobs.pop(job_id, None)
    if not job:
        return False
    for key in ('video', 'srt', 'ass'):
        if job.get(key):
            try:
                os.remove(os.path.join(DOWNLOADS_DIR, job[key]))
            except Exception:
                pass
    save_jobs()
    return True
