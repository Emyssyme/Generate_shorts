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

def _fingerprint(job):
    return json.dumps({k: v for k, v in job.items() if k != 'updated_at'},
                      sort_keys=True, default=str)


def save_jobs():
    """Persist jobs to disk, stamping ``updated_at`` on every job that changed.

    Change detection compares a fingerprint of each job with the last saved
    one, so edits made directly on the job dict (e.g. in the editor) count too.
    """
    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    try:
        for job_id in list(_job_fingerprints):
            if job_id not in active_jobs:
                del _job_fingerprints[job_id]
        for job_id, job in list(active_jobs.items()):
            fp = _fingerprint(job)
            if _job_fingerprints.get(job_id) != fp:
                job['updated_at'] = now
                _job_fingerprints[job_id] = fp
        with open(JOBS_FILE, 'w', encoding='utf-8') as f:
            json.dump(active_jobs, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"warning: failed to save jobs file: {e}")

active_jobs = load_jobs()
# fingerprints of the jobs as loaded: untouched legacy jobs keep their dates
_job_fingerprints = {jid: _fingerprint(j) for jid, j in active_jobs.items()}
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


# ---------------------------------------------------------------------------
# Dates and grouping for the Projects tab
# ---------------------------------------------------------------------------

_DATE_FMT = '%Y-%m-%d %H:%M:%S'
_MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July',
           'August', 'September', 'October', 'November', 'December']
_WEEKDAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']


def _parse_ts(value):
    try:
        return datetime.datetime.strptime(value, _DATE_FMT)
    except (TypeError, ValueError):
        return None


def _file_mtime(job):
    for key in ('video', 'srt', 'ass'):
        if job.get(key):
            path = os.path.join(DOWNLOADS_DIR, job[key])
            if os.path.exists(path):
                return datetime.datetime.fromtimestamp(os.path.getmtime(path))
    return None


def project_dates(job_id, job):
    """Return ``(created, updated)`` datetimes (either may be None).

    Older projects have no stored dates, so fall back to the timestamp in the
    job id (``J<epoch>``) and then to the modification time of the video file.
    """
    mtime = _file_mtime(job)
    created = _parse_ts(job.get('created_at'))
    if created is None:
        m = re.fullmatch(r'J(\d{9,10})(?:-\w+)?', job_id or '')
        if m:
            created = datetime.datetime.fromtimestamp(int(m.group(1)))
    created = created or mtime
    updated = _parse_ts(job.get('updated_at')) or mtime or created
    if created and updated and updated < created:
        updated = created
    return created, updated


def date_label(day, today=None):
    if day is None:
        return 'Unknown date'
    today = today or datetime.date.today()
    text = f"{day.day} {_MONTHS[day.month - 1]} {day.year}"
    if day == today:
        return f"Today · {text}"
    if day == today - datetime.timedelta(days=1):
        return f"Yesterday · {text}"
    return f"{_WEEKDAYS[day.weekday()]}, {text}"


def group_projects(sort='created'):
    """Projects grouped by day, newest first.

    ``sort`` is ``'created'`` or ``'updated'`` and decides both the order and
    which date the groups are based on.
    """
    with jobs_lock:
        items = [(jid, dict(job)) for jid, job in active_jobs.items()]
    rows = []
    for jid, job in items:
        created, updated = project_dates(jid, job)
        rows.append({'id': jid, 'job': job, 'name': job.get('name') or jid,
                     'created': created, 'updated': updated,
                     'key': updated if sort == 'updated' else created})
    rows.sort(key=lambda r: r['key'] or datetime.datetime.min, reverse=True)
    groups = []
    for r in rows:
        day = r['key'].date() if r['key'] else None
        if not groups or groups[-1]['day'] != day:
            groups.append({'day': day, 'label': date_label(day), 'items': []})
        groups[-1]['items'].append(r)
    return groups
