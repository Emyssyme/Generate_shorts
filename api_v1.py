"""REST API (v1) for creating and managing projects without the web UI.

Authentication: send the token from the ``API_TOKEN`` environment variable as
``Authorization: Bearer <token>`` or ``X-API-Key: <token>``.
"""
import datetime
import hmac
import os
from functools import wraps

from flask import Blueprint, jsonify, request, send_from_directory, url_for

from config import API_TOKEN, DOWNLOADS_DIR
from jobs import (NON_TERMINAL_STATUSES, active_jobs, jobs_lock, project_dates,
                  remove_job, request_cancel)
from pipeline import submit_job

bp = Blueprint('api_v1', __name__, url_prefix='/api/v1')

FILE_KINDS = ('video', 'srt', 'ass')
TRUE_VALUES = {'1', 'true', 'on', 'yes', 'y'}


def _error(message, status):
    return jsonify({'error': message}), status


def require_token(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not API_TOKEN:
            return _error('API disabled: set the API_TOKEN environment variable', 503)
        supplied = request.headers.get('X-API-Key', '')
        auth = request.headers.get('Authorization', '')
        if auth.lower().startswith('bearer '):
            supplied = auth[7:].strip()
        if not supplied or not hmac.compare_digest(supplied.encode(), API_TOKEN.encode()):
            return _error('invalid or missing API token', 401)
        return view(*args, **kwargs)
    return wrapper


def _as_bool(value):
    if isinstance(value, bool):
        return value
    return str(value or '').strip().lower() in TRUE_VALUES


def _links(project_id, job):
    links = {
        'self': url_for('api_v1.get_project', project_id=project_id),
        'cancel': url_for('api_v1.cancel_project', project_id=project_id),
        'editor': url_for('editor', job_id=project_id),
    }
    for kind in FILE_KINDS:
        if job.get(kind):
            links[kind] = url_for('api_v1.get_file', project_id=project_id, kind=kind)
    return links


def _summary(project_id, job, log_lines=0):
    created, updated = project_dates(project_id, job)
    fmt = '%Y-%m-%d %H:%M:%S'
    data = {
        'id': project_id,
        'name': job.get('name'),
        'created_at': created.strftime(fmt) if created else None,
        'updated_at': updated.strftime(fmt) if updated else None,
        'status': job.get('status'),
        'cancellable': job.get('status', '') in NON_TERMINAL_STATUSES,
        'error': job.get('msg') if job.get('status') == 'error' else None,
        'video': job.get('video'),
        'srt': job.get('srt'),
        'ass': job.get('ass'),
        'links': _links(project_id, job),
    }
    if log_lines:
        data['log'] = (job.get('log') or [])[-log_lines:]
    return data


def _snapshot(project_id):
    with jobs_lock:
        job = active_jobs.get(project_id)
        return dict(job) if job is not None else None


@bp.post('/projects')
@require_token
def create_project():
    """Create a project from a YouTube URL (JSON) or an uploaded file (multipart).

    Fields: url | file, name, start_time, end_time, skip_unsilence,
    skip_cropping, skip_subtitles, subtitle_method (auto|google|whisper),
    gemini_api_key.
    """
    if request.is_json:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return _error('request body must be a JSON object', 400)
        upload = None
    else:
        data = request.form
        upload = request.files.get('file')

    def field(key, default=None):
        value = data.get(key)
        return default if value in (None, '') else value

    try:
        result = submit_job(
            job_id=None,
            url=str(field('url', '')),
            file=upload,
            project_label=field('name'),
            start_time=str(field('start_time', '0')),
            end_time=field('end_time'),
            skip_unsilence=_as_bool(data.get('skip_unsilence')),
            skip_cropping=_as_bool(data.get('skip_cropping')),
            skip_subtitles=_as_bool(data.get('skip_subtitles')),
            subtitle_method=str(field('subtitle_method', 'auto')),
            gemini_user_key=str(field('gemini_api_key', '')),
        )
    except ValueError as exc:
        return _error(str(exc), 400)

    project_id = result['job_id']
    job = _snapshot(project_id) or {}
    body = _summary(project_id, job)
    body['cached'] = result['cached']
    # background thread may not have created the job entry yet
    body['status'] = job.get('status') or 'starting'
    return jsonify(body), (200 if result['cached'] else 202)


@bp.get('/projects')
@require_token
def list_projects():
    """List projects, newest first. Query: ``status``, ``limit`` (default 50)."""
    status = request.args.get('status')
    try:
        limit = max(1, min(int(request.args.get('limit', 50)), 500))
    except ValueError:
        return _error('limit must be an integer', 400)
    with jobs_lock:
        items = [(pid, dict(job)) for pid, job in active_jobs.items()]
    items.sort(key=lambda kv: (project_dates(kv[0], kv[1])[0] or datetime.datetime.min),
               reverse=True)
    if status:
        items = [kv for kv in items if kv[1].get('status') == status]
    return jsonify({'projects': [_summary(pid, job) for pid, job in items[:limit]],
                    'total': len(items)})


@bp.get('/projects/<project_id>')
@require_token
def get_project(project_id):
    """Project status. Query: ``log=N`` returns the last N log lines (default 20)."""
    job = _snapshot(project_id)
    if job is None:
        return _error('project not found', 404)
    try:
        log_lines = max(0, min(int(request.args.get('log', 20)), 1000))
    except ValueError:
        return _error('log must be an integer', 400)
    return jsonify(_summary(project_id, job, log_lines=log_lines))


@bp.post('/projects/<project_id>/cancel')
@require_token
def cancel_project(project_id):
    job = _snapshot(project_id)
    if job is None:
        return _error('project not found', 404)
    if job.get('status', '') not in NON_TERMINAL_STATUSES:
        return _error(f"project is not running (status: {job.get('status')})", 409)
    request_cancel(project_id)
    return jsonify({'ok': True, 'id': project_id, 'status': 'cancelled'})


@bp.delete('/projects/<project_id>')
@require_token
def delete_project(project_id):
    if not remove_job(project_id):
        return _error('project not found', 404)
    return jsonify({'ok': True, 'id': project_id})


@bp.get('/projects/<project_id>/files/<kind>')
@require_token
def get_file(project_id, kind):
    """Download ``video``, ``srt`` or ``ass``; add ``?inline=1`` to stream it."""
    job = _snapshot(project_id)
    if job is None:
        return _error('project not found', 404)
    if kind not in FILE_KINDS:
        return _error(f"kind must be one of: {', '.join(FILE_KINDS)}", 404)
    filename = job.get(kind)
    if not filename or not os.path.exists(os.path.join(DOWNLOADS_DIR, filename)):
        return _error(f'{kind} not available yet', 404)
    download_name = (job['name'] + os.path.splitext(filename)[1]) if job.get('name') else None
    return send_from_directory(DOWNLOADS_DIR, filename,
                               as_attachment=not _as_bool(request.args.get('inline')),
                               download_name=download_name)
