"""Processing pipeline: download/cut, unsilence, crop, subtitles + submit_job()."""
import datetime
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import traceback

from config import BASE_DIR, DOWNLOADS_DIR
from db import find_cache, store_cache
from ffmpeg_utils import ffmpeg_env
from gpu import GPU_ENCODER_QUALITY, select_auto_gpu_encoder
from jobs import _active_subprocesses, active_jobs, fetch_youtube_title, job_cancel_events, jobs_lock, make_project_name, update_job


def find_script(name):
    """Locate a helper script by name in the current directory or its parent.

    This project historically has helpers either next to ``app.py`` or in the
    workspace root.  ``find_script`` tries both places and raises if the file
    cannot be found so that the caller can surface a useful error.
    """
    base = BASE_DIR
    candidates = [os.path.join(base, name)]
    for path in candidates:
        if os.path.exists(path):
            return os.path.abspath(path)
    raise FileNotFoundError(f"helper script not found: {name}")


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
    
    # Only attempt GPU crop acceleration when NVIDIA CUDA is available
    # (the crop helper uses OpenCV DNN which only supports CUDA, not Intel QSV)
    nvidia_available = False
    try:
        r = subprocess.run(['nvidia-smi'], capture_output=True, timeout=3)
        nvidia_available = r.returncode == 0
    except Exception:
        pass

    # Try with GPU first if available, then fallback to CPU
    for use_gpu in [True, False]:
        if use_gpu and not nvidia_available:
            continue  # Skip GPU attempt if no NVIDIA GPU detected
            
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
                
            result = subprocess.run(
                        cmd, 
                        capture_output=True, 
                        text=True, 
                        encoding='utf-8', 
                        env=ffmpeg_env(), 
                        timeout=600
                    )
            
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
            if use_gpu and nvidia_available:
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
        # Use GPU encoder for the transcode step if available (faster, less CPU)
        gpu_enc = select_auto_gpu_encoder()
        if gpu_enc:
            encode_args = list(GPU_ENCODER_QUALITY[gpu_enc])
            update_job(job_id, log=f"transcoding cropped video with {gpu_enc}")
        else:
            encode_args = [
                '-c:v', 'libx264', '-crf', '18', '-preset', 'fast',
                '-g', '30', '-keyint_min', '30', '-sc_threshold', '0',
                '-pix_fmt', 'yuv420p', '-movflags', '+faststart',
            ]
        subprocess.run([
            'ffmpeg', '-nostdin', '-y', '-i', cropped,
            *encode_args,
            '-c:a', 'copy', trans
        ], check=True, timeout=600, env=ffmpeg_env())
        return trans
    except subprocess.CalledProcessError as e:
        # if transcoding fails fall back to original cropped file
        if job_id:
            update_job(job_id, log=f"warning: h264 transcode failed, using original: {e}")
        return cropped


def run_subtitles(input_video, output_dir, model="large", max_length=22, job_id=None, method="auto", gemini_user_key=""):
    """Generate subtitles for a single video.

    If ``job_id`` is passed, stream the helper script's output into the job log
    and register the subprocess so it can be killed via the cancel mechanism.

    ``method`` can be:
      - ``"google"``  – use Gemini API (requires GOOGLE_API_KEY_1 / GOOGLE_API_KEY_2, or hardcoded keys)
      - ``"whisper"`` – use local faster-whisper model (int8 quantized)
      - ``"auto"``    – try Gemini first, fall back to faster-whisper (default)
    ``gemini_user_key`` – optional user-provided API key from the UI (highest priority)

    Returns a tuple ``(srt_path, ass_path)`` — the ASS file contains word-level
    karaoke tags for per-word highlighting during video rendering.
    """
    script = find_script("_generate_subtitles.py")
    cmd = [sys.executable, script, "--input", input_video, "--output", output_dir,
           "--model", model, "--max-length", str(max_length), "--method", method]

    # 1. Prepare environment variables to force the child process into UTF-8 Mode
    sub_env = os.environ.copy()
    sub_env["PYTHONUTF8"] = "1"
    sub_env["PYTHONIOENCODING"] = "utf-8"
    if gemini_user_key:
        sub_env["GEMINI_USER_KEY"] = gemini_user_key
        if job_id:
            update_job(job_id, log="using user-provided Gemini API key from UI")

    if job_id:
        update_job(job_id, log=f"starting subtitle generation (method={method})")

        # 2. Added encoding='utf-8' and passed the sub_env
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding='utf-8',
            env=sub_env
        )

        # Register subprocess so it can be killed on cancel
        _active_subprocesses[job_id] = proc

        try:
            for line in proc.stdout:
                update_job(job_id, log=line.rstrip())
                # Check for cancellation while reading output
                if _is_cancelled(job_id):
                    update_job(job_id, log="subtitle generation cancelled by user")
                    proc.kill()
                    proc.wait(timeout=10)
                    raise RuntimeError("subtitle generation cancelled by user")

            proc.wait(timeout=300)  # 5-minute timeout for subtitle generation
            if proc.returncode != 0:
                raise RuntimeError(f"subtitle generation failed (see logs); rc={proc.returncode}")
        finally:
            _active_subprocesses.pop(job_id, None)
    else:
        # 3. Added encoding='utf-8' and passed the sub_env here as well
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding='utf-8',
            env=sub_env,
            timeout=300  # 5-minute timeout for subtitle generation
        )
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

def _is_cancelled(job_id):
    """Check if the job has been cancelled.  Should be called periodically
    during long-running pipeline operations."""
    evt = job_cancel_events.get(job_id)
    return evt is not None and evt.is_set()


def background_pipeline(job_id, url=None, upload_path=None, start_time="0", end_time=None, skip_unsilence=False, skip_cropping=False, skip_subtitles=False, subtitle_method="auto", gemini_user_key="", project_label=None, created_at=None):
    """Background thread that cuts/downloads then optionally unsilences, crops, subtitles.

    ``skip_unsilence`` is used when the user knows the clip already has clean audio.
    ``skip_cropping`` is used to bypass the face-crop step entirely (e.g. when no face is present).
    ``skip_subtitles`` is used to skip automatic subtitle generation entirely.
    ``subtitle_method``: ``"google"`` (Gemini API), ``"whisper"``, or ``"auto"`` (default: try Gemini first).
    ``gemini_user_key``: optional user-provided API key from the UI (highest priority).

    These flags are recorded in the cache so repeated calls behave identically.

    Checks ``job_cancel_events[job_id]`` between major steps and aborts gracefully
    if the user has requested cancellation.
    """
    update_job(job_id, status="starting", log="job created")
    try:
        # project name = source file / video title + date and time
        try:
            created = datetime.datetime.fromisoformat(created_at) if created_at else datetime.datetime.now()
        except ValueError:
            created = datetime.datetime.now()
        if url:
            label = project_label or fetch_youtube_title(url) or "youtube"
        else:
            label = project_label or (os.path.splitext(os.path.basename(upload_path))[0] if upload_path else None)
        project_name = make_project_name(label, created)
        update_job(job_id, name=project_name,
                   created_at=created.strftime('%Y-%m-%d %H:%M:%S'),
                   log=f"project name: {project_name}")

        # determine source for cutting
        cut_path = os.path.join(DOWNLOADS_DIR, f"job_{job_id}_cut.mp4")
        if url:
            update_job(job_id, status="downloading", log=f"yt-dlp {url} ({start_time}-{end_time})")
            cmd = [
                "yt-dlp", "--download-sections", f"*{start_time}-{end_time or 'inf'}",
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
                subprocess.run(ff, check=True, timeout=600, env=ffmpeg_env())
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
                subprocess.run(ff, check=True, timeout=600, env=ffmpeg_env())

        # ── cancel check after download/cut ──────────────────────────
        if _is_cancelled(job_id):
            update_job(job_id, status="cancelled", log="cancelled by user after download/cut")
            return

        if skip_unsilence:
            update_job(job_id, status="skipping unsilence", log="user requested no audio cleaning")
            unsilenced = cut_path
        else:
            update_job(job_id, status="unsilencing", log="calling unsilence script")
            unsilenced = os.path.join(DOWNLOADS_DIR, f"job_{job_id}_unsilenced.mp4")
            run_unsilence(cut_path, unsilenced, job_id=job_id)
            # verify unsilence actually produced a file
            if not os.path.exists(unsilenced) or os.path.getsize(unsilenced) < 1000:
                update_job(job_id, log="warning: unsilence output missing or too small, falling back to original")
                unsilenced = cut_path

        # ── cancel check after unsilence ─────────────────────────────
        if _is_cancelled(job_id):
            update_job(job_id, status="cancelled", log="cancelled by user after unsilence")
            return

        if skip_cropping:
            update_job(job_id, status="skipping cropping", log="user requested no face cropping")
            cropped = unsilenced
        else:
            update_job(job_id, status="cropping", log="running crop script")
            cropped = run_crop(unsilenced, DOWNLOADS_DIR, job_id=job_id)

        # ── cancel check after cropping ──────────────────────────────
        if _is_cancelled(job_id):
            update_job(job_id, status="cancelled", log="cancelled by user after cropping")
            return

        if skip_subtitles:
            update_job(job_id, status="skipping subtitles", log="user requested no automatic subtitles")
            srtfile, assfile = None, None
        else:
            update_job(job_id, status="subtitling",
                       log=f"generating subtitles on: {os.path.basename(cropped)} (method={subtitle_method})")
            srtfile, assfile = run_subtitles(cropped, DOWNLOADS_DIR, job_id=job_id, method=subtitle_method, gemini_user_key=gemini_user_key)

            # sanity check: ensure subtitles were actually written
            if not os.path.exists(srtfile):
                raise RuntimeError(f"subtitle file not found after generation: {srtfile}")

        update_job(job_id, status="completed",
                   video=os.path.basename(cropped),
                   srt=os.path.basename(srtfile) if srtfile else None,
                   ass=os.path.basename(assfile) if (assfile and os.path.exists(assfile)) else None,
                   log=f"done: video={os.path.basename(cropped)} srt={os.path.basename(srtfile) if srtfile else 'none'} (pipeline: cut→{'unsilenced→' if not skip_unsilence else ''}{'cropped→' if not skip_cropping else ''}subtitles)")
        # cache this result for future identical requests
        if url:
            store_cache(url, start_time, end_time,
                        os.path.basename(cropped), os.path.basename(srtfile) if srtfile else None,
                        skip_unsilence=skip_unsilence, skip_cropping=skip_cropping, skip_subtitles=skip_subtitles)
    except Exception as e:
        # If the job was already cancelled, don't overwrite with an error
        if _is_cancelled(job_id):
            update_job(job_id, status="cancelled", log=f"cancelled: {e}")
        else:
            error_trace = traceback.format_exc()
            update_job(job_id, status="error", msg=str(e), trace=error_trace)
            print(f"[job {job_id}] Exception: {error_trace}")
    finally:
        # Clean up cancel event and subprocess reference
        job_cancel_events.pop(job_id, None)
        _active_subprocesses.pop(job_id, None)


# ---------------------------------------------------------------------------
# Public service used by both the web UI and the REST API
# ---------------------------------------------------------------------------

def sanitize_job_id(raw):
    """Keep only [A-Za-z0-9_-]; return '' if nothing is left."""
    return re.sub(r'[^a-zA-Z0-9_-]', '', raw or '')


def new_job_id():
    return f"J{int(time.time())}-{secrets.token_hex(2)}"


def safe_upload_name(filename):
    """Strip any path and unsafe characters but keep Unicode letters."""
    name = os.path.basename((filename or '').replace('\\', '/'))
    name = re.sub(r'[^\w.\- ]', '_', name, flags=re.UNICODE).strip(' .')
    return name or 'upload'


def validate_times(start_time, end_time):
    """Raise ValueError for malformed times or end <= start."""
    try:
        start = _time_to_seconds(start_time or "0")
        end = _time_to_seconds(end_time) if end_time else None
    except (ValueError, TypeError):
        raise ValueError("Invalid time format; use seconds, MM:SS or HH:MM:SS")
    if start < 0 or (end is not None and end <= start):
        raise ValueError("end_time must be greater than start_time")


def submit_job(*, job_id=None, url=None, file=None, start_time="0", end_time=None,
               skip_unsilence=False, skip_cropping=False, skip_subtitles=False,
               subtitle_method="auto", gemini_user_key="", project_label=None):
    """Validate a request, then start the pipeline in a background thread.

    ``file`` is a werkzeug ``FileStorage`` (or None). ``project_label`` overrides
    the name taken from the uploaded file / video title.
    Returns ``{'job_id': str, 'cached': bool}``. Raises ``ValueError`` on bad input.
    """
    url = (url or '').strip() or None
    has_file = bool(file is not None and getattr(file, 'filename', ''))
    if not url and not has_file:
        raise ValueError('Please provide a YouTube URL or upload a local file')
    start_time = start_time or "0"
    end_time = end_time or None
    validate_times(start_time, end_time)
    if subtitle_method not in ('google', 'whisper', 'auto'):
        subtitle_method = 'auto'

    job_id = sanitize_job_id(job_id) or new_job_id()
    created_at = datetime.datetime.now()

    upload_path = None
    if has_file:
        upload_path = os.path.join(DOWNLOADS_DIR, f"upload_{job_id}_{safe_upload_name(file.filename)}")
        file.save(upload_path)
        if not project_label:
            project_label = os.path.splitext(safe_upload_name(file.filename))[0]

    # a URL-only request may be answered from the cache
    if url and not upload_path:
        flags = dict(skip_unsilence=skip_unsilence, skip_cropping=skip_cropping,
                     skip_subtitles=skip_subtitles)
        cached = find_cache(url, start_time, end_time, **flags)
        if cached:
            video_name, srt_name = cached
            video_path = os.path.join(DOWNLOADS_DIR, video_name)
            srt_path = os.path.join(DOWNLOADS_DIR, srt_name) if srt_name else None
            if os.path.exists(video_path) and (srt_path is None or os.path.exists(srt_path)):
                with jobs_lock:
                    active_jobs[job_id] = {
                        'status': 'completed',
                        'name': make_project_name(project_label or 'youtube', created_at),
                        'created_at': created_at.strftime('%Y-%m-%d %H:%M:%S'),
                        'video': video_name,
                        'srt': srt_name,
                    }
                return {'job_id': job_id, 'cached': True}
            # stale cache entry: drop it and process normally
            store_cache(url, start_time, end_time, None, None, **flags)

    job_cancel_events[job_id] = threading.Event()
    threading.Thread(
        target=background_pipeline,
        kwargs=dict(job_id=job_id, url=url, upload_path=upload_path,
                    start_time=start_time, end_time=end_time,
                    skip_unsilence=skip_unsilence, skip_cropping=skip_cropping,
                    skip_subtitles=skip_subtitles, subtitle_method=subtitle_method,
                    gemini_user_key=gemini_user_key, project_label=project_label,
                    created_at=created_at.isoformat()),
        daemon=True,
    ).start()
    return {'job_id': job_id, 'cached': False}
