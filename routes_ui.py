"""Web UI routes (upload form, job status, projects, downloads, templates, fonts)."""
import datetime
import json
import os
import subprocess
import sys

from flask import flash, redirect, render_template, request, send_from_directory, url_for
from flask_login import current_user, login_required

import gpu
from config import DOWNLOADS_DIR, FONTS_DIR, TEMPLATE_ASSETS_DIR
from db import delete_template, get_all_templates, get_template, save_template
from ffmpeg_utils import is_h264, transcode_to_h264
from font_manager import FONT_LIBRARY, download_font_by_name, refresh_font_map
from gpu import select_auto_gpu_encoder
from jobs import group_projects, NON_TERMINAL_STATUSES, active_jobs, jobs_lock, remove_job, request_cancel
from pipeline import detect_faces_in_video, submit_job


@login_required
def video_cut():
    if request.method == 'POST':
        form = request.form
        try:
            result = submit_job(
                job_id=form.get('job_id'),
                url=form.get('url', ''),
                file=request.files.get('file'),
                start_time=form.get('start_time') or "0",
                end_time=form.get('end_time'),
                skip_unsilence=form.get('skip_unsilence') == 'on',
                skip_cropping=form.get('skip_cropping') == 'on',
                skip_subtitles=form.get('skip_subtitles') == 'on',
                subtitle_method=form.get('subtitle_method', 'auto').strip(),
                gemini_user_key=form.get('gemini_user_key', '').strip(),
            )
        except ValueError as exc:
            flash(str(exc), 'danger')
            return redirect(url_for('video_cut'))
        if result['cached']:
            return {"status": "completed", "job_id": result['job_id']}, 200
        return {"status": "accepted", "job_id": result['job_id']}, 202
    return render_template('video_cut.html')


@login_required
def check_job(job_id):
    with jobs_lock:
        info = dict(active_jobs.get(job_id, {'status': 'not_found'}))
    # Let the frontend know whether a cancel button should be shown
    info['cancellable'] = info.get('status', '') in NON_TERMINAL_STATUSES
    return json.dumps(info)


@login_required
def cancel_job(job_id):
    request_cancel(job_id)
    return {'ok': True, 'job_id': job_id}


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


@login_required
def download_file(filename):
    # Sanitize filename to prevent path traversal attacks
    filename = os.path.basename(filename)
    if '/' in filename or '\\' in filename or '..' in filename:
        return '', 403
    download_name = None
    with jobs_lock:
        for j in active_jobs.values():
            if j.get('name') and filename in (j.get('video'), j.get('srt'), j.get('ass')):
                download_name = j['name'] + os.path.splitext(filename)[1]
                break
    return send_from_directory(DOWNLOADS_DIR, filename, as_attachment=True,
                               download_name=download_name)


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


@login_required
def serve_font(filename):
    """Serve font files from the fonts directory for browser preview."""
    # Sanitize filename to prevent path traversal attacks
    filename = os.path.basename(filename)
    if '/' in filename or '\\' in filename or '..' in filename:
        return '', 403
    return send_from_directory(FONTS_DIR, filename, as_attachment=False)


def index():
    # simple landing page that redirects to login or the main editor
    if current_user.is_authenticated:
        return redirect(url_for('video_cut'))
    return redirect(url_for('login'))


@login_required
def list_projects():
    # show simple table of all jobs with edit/delete links
    with jobs_lock:
        projects = dict(active_jobs)  # Thread-safe snapshot
    sort = request.args.get('sort', 'created')
    if sort not in ('created', 'updated'):
        sort = 'created'
    return render_template('projects.html', groups=group_projects(sort),
                           sort=sort, total=len(projects), jobs=projects)


@login_required
def delete_project(job_id):
    if remove_job(job_id):
        flash(f'Project {job_id} deleted', 'info')
    else:
        flash(f'Project {job_id} not found', 'warning')
    return redirect(url_for('list_projects'))


@login_required
def api_system_info():
    """Return system information including GPU availability and subtitle method."""
    gpu_info = {
        'available': gpu.GPU_AVAILABLE,
        'gpu_encoder': select_auto_gpu_encoder() or 'none',
        'nvidia_detected': gpu._NVIDIA_SMI_OK,
        'intel_qsv_detected': gpu._INTEL_QSV_OK,
    }
    
    # Try to get GPU name (NVIDIA)
    try:
        result = subprocess.run(
            ['nvidia-smi', '--query-gpu=name', '--format=csv,noheader'],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            gpu_info['gpu_name'] = result.stdout.strip().split('\n')[0]
            gpu_info['gpu_type'] = 'nvidia'
    except Exception:
        pass

    # Detect Intel Quick Sync
    if 'gpu_type' not in gpu_info:
        try:
            if sys.platform.startswith('win'):
                r = subprocess.run(
                    ['wmic', 'path', 'win32_videocontroller', 'get', 'name'],
                    capture_output=True, text=True, timeout=10
                )
                if 'Intel' in r.stdout and ('HD Graphics' in r.stdout or 'UHD Graphics' in r.stdout or 'Iris' in r.stdout):
                    gpu_info['gpu_name'] = 'Intel Quick Sync (iGPU)'
                    gpu_info['gpu_type'] = 'intel_qsv'
            if os.path.exists('/dev/dri/renderD128'):
                gpu_info['gpu_name'] = 'Intel Quick Sync (iGPU)'
                gpu_info['gpu_type'] = 'intel_qsv'
        except Exception:
            pass

    # Subtitle method info (Gemini API)
    gemini_env_keys = [
        k for k in ['GOOGLE_API_KEY_1', 'GOOGLE_API_KEY_2']
        if os.getenv(k)
    ]
    subtitle_info = {
        'gemini_available': len(gemini_env_keys) > 0,
        'gemini_keys_configured': len(gemini_env_keys),
        'has_hardcoded_fallback': True,   # _generate_subtitles.py always has fallback keys
        'whisper_available': True,
        'default_method': 'google' if gemini_env_keys else 'auto',
    }

    return json.dumps({
        'ok': True,
        'gpu': gpu_info,
        'subtitle': subtitle_info,
        'python_version': f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    })


@login_required
def api_list_templates():
    return json.dumps(get_all_templates())

@login_required
def api_save_template():
    data = request.get_json(force=True)
    name = (data.get('name') or '').strip()
    config = data.get('config', {})
    if not name:
        return json.dumps({'ok': False, 'error': 'Template name is required'}), 400
    ok = save_template(name, config)
    return json.dumps({'ok': ok})

@login_required
def api_load_template(name):
    cfg = get_template(name)
    if cfg is None:
        return json.dumps({'ok': False, 'error': 'Template not found'}), 404
    return json.dumps({'ok': True, 'config': cfg})

@login_required
def api_delete_template(name):
    delete_template(name)
    return json.dumps({'ok': True})


# ═══════════════════════════════════════════════════════════════════════════
#  Font library API – list available fonts & trigger downloads
# ═══════════════════════════════════════════════════════════════════════════

@login_required
def api_font_library():
    """Return the list of downloadable fonts with their status (downloaded / available)."""
    result = []
    for name, info in FONT_LIBRARY.items():
        dest_path = os.path.join(FONTS_DIR, info['filename'])
        downloaded = os.path.exists(dest_path)
        result.append({
            'name': name,
            'format': info['format'],
            'downloaded': downloaded,
        })
    return json.dumps(result)


@login_required
def api_download_font(name):
    """Download a specific font from the library."""
    if name not in FONT_LIBRARY:
        return json.dumps({'ok': False, 'error': 'Font not in library'}), 404
    if download_font_by_name(name):
        refresh_font_map()
        return json.dumps({'ok': True, 'name': name})
    return json.dumps({'ok': False, 'error': 'Download failed'}), 500


def register(app):
    """Attach the routes to ``app`` (endpoint names = function names)."""
    app.add_url_rule('/video-cut', methods=['GET', 'POST'], view_func=video_cut)
    app.add_url_rule('/check-job/<job_id>', view_func=check_job)
    app.add_url_rule('/cancel-job/<job_id>', methods=['POST'], view_func=cancel_job)
    app.add_url_rule('/api/detect-faces/<job_id>', view_func=api_detect_faces)
    app.add_url_rule('/api/detect-faces-file', methods=['POST'], view_func=api_detect_faces_file)
    app.add_url_rule('/download/<filename>', view_func=download_file)
    app.add_url_rule('/preview/<filename>', view_func=preview_file)
    app.add_url_rule('/fonts/<path:filename>', view_func=serve_font)
    app.add_url_rule('/', view_func=index)
    app.add_url_rule('/projects', view_func=list_projects)
    app.add_url_rule('/delete_project/<job_id>', methods=['POST'], view_func=delete_project)
    app.add_url_rule('/api/system-info', view_func=api_system_info)
    app.add_url_rule('/api/templates', methods=['GET'], view_func=api_list_templates)
    app.add_url_rule('/api/templates/save', methods=['POST'], view_func=api_save_template)
    app.add_url_rule('/api/templates/load/<name>', methods=['GET'], view_func=api_load_template)
    app.add_url_rule('/api/templates/delete/<name>', methods=['DELETE'], view_func=api_delete_template)
    app.add_url_rule('/api/fonts/library', methods=['GET'], view_func=api_font_library)
    app.add_url_rule('/api/fonts/download/<name>', methods=['POST'], view_func=api_download_font)
