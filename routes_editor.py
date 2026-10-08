"""The subtitle / overlay editor route."""
import os
import re
import subprocess

from flask import flash, redirect, render_template, request, url_for
from flask_login import login_required

import font_manager
from ass_utils import html_to_ass_color, srt_to_ass, title_to_ass
from config import DOWNLOADS_DIR, FONTS_DIR
from db import get_all_templates, resolve_overlay_path
from ffmpeg_utils import ffmpeg_env, get_video_size
from font_manager import allowed_font_file, download_font_by_name, is_variable_font, refresh_font_map
from gpu import GPU_ENCODER_QUALITY, VIDEO_QUALITY, _probe_encoder_works, ffmpeg_supports_encoder, ffmpeg_supports_filter_complex_script, select_auto_gpu_encoder
from jobs import active_jobs, save_jobs, update_job
from pipeline import _time_to_seconds


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
    job.setdefault('sub_highlight_opacity', '100')
    job.setdefault('title_outline_w', '2')
    job.setdefault('title_bg_enabled', False)
    job.setdefault('title_bg_color', '#000000')
    job.setdefault('title_bg_opacity', '60')
    job.setdefault('sub_word_spacing', '0')
    job.setdefault('video_scale', '1.0')
    job.setdefault('video_pan_x', '0')
    job.setdefault('video_pan_y', '0')
    job.setdefault('trim_in_start', '')
    job.setdefault('trim_in_end', '')
    job.setdefault('project_res', 'source')
    job.setdefault('project_w', '')
    job.setdefault('project_h', '')

    srt_path = os.path.join(DOWNLOADS_DIR, job.get('srt') or '')
    srt_text = ''
    if job.get('srt') and os.path.isfile(srt_path):
        with open(srt_path, encoding='utf-8') as f:
            # normalize line endings and collapse excessive blank lines
            raw = f.read()
        srt_text = re.sub(r"\r\n?|\n", "\n", raw).strip()
        srt_text = re.sub(r"\n{3,}", "\n\n", srt_text)

    if request.method == 'POST':
        save_only = request.form.get('save') == '1'
        upload_font_only = request.form.get('upload_font') == '1'

        # ── handle font upload (may be standalone action) ───────────
        font_file = request.files.get('font_file')
        if font_file and font_file.filename:
            safe_name = os.path.basename(font_file.filename)
            if allowed_font_file(safe_name):
                dest_path = os.path.join(FONTS_DIR, safe_name)
                font_file.save(dest_path)
                refresh_font_map()
                flash(f"Font uploaded: {safe_name}", 'success')
                if upload_font_only:
                    save_jobs()
                    return redirect(url_for('editor', job_id=job_id))
            else:
                flash('Font upload failed: only .ttf and .otf files are allowed.', 'danger')
                if upload_font_only:
                    return redirect(url_for('editor', job_id=job_id))

        # ── Import SRT/ASS file ──────────────────────────────────────────
        subtitle_file = request.files.get('subtitle_file')
        subtitle_imported = False  # flag to skip SRT-save/ASS-rebuild below
        if subtitle_file and subtitle_file.filename:
            safe_name = os.path.basename(subtitle_file.filename)
            ext = os.path.splitext(safe_name)[1].lower()
            if ext in ('.srt', '.ass'):
                dest_path = os.path.join(DOWNLOADS_DIR, safe_name)
                subtitle_file.save(dest_path)
                # set job's srt/ass to the imported file
                job['srt'] = safe_name
                srt_path = dest_path
                if ext == '.ass':
                    job['ass'] = safe_name
                else:
                    # generate ASS from imported SRT
                    ass_name = os.path.splitext(safe_name)[0] + '.ass'
                    try:
                        with open(dest_path, encoding='utf-8') as f:
                            imported_srt = f.read()
                        srt_to_ass(imported_srt, os.path.join(DOWNLOADS_DIR, ass_name))
                        job['ass'] = ass_name
                    except Exception as e:
                        update_job(job_id, log=f"warning: could not build ASS from imported SRT: {e}")
                # Reload srt_text from the newly imported file so the
                # textarea shows the new content instead of stale old text.
                if ext == '.srt':
                    with open(dest_path, encoding='utf-8') as f:
                        raw = f.read()
                    srt_text = re.sub(r"\r\n?|\n", "\n", raw).strip()
                    srt_text = re.sub(r"\n{3,}", "\n\n", srt_text)
                subtitle_imported = True
                flash(f'Subtitles imported: {safe_name}', 'success')
            else:
                flash('Only .srt and .ass files are supported for subtitle import.', 'warning')

        # ── save SRT edits ────────────────────────────────────────────────
        # Skip this block when a new subtitle file was just imported –
        # the textarea still holds the *old* subtitle text and would
        # overwrite the freshly imported file.
        if not subtitle_imported:
            # strip accumulated leading/trailing whitespace so every save is clean
            new_srt = request.form.get('srt_text', '').strip()
            # normalize before saving to keep file tidy
            new_srt = re.sub(r"\r\n?|\n", "\n", new_srt)
            new_srt = re.sub(r"\n{3,}", "\n\n", new_srt)
            if os.path.isfile(srt_path):
                with open(srt_path, 'w', encoding='utf-8', newline='\n') as f:
                    f.write(new_srt)
            srt_text = new_srt

            # ── regenerate ASS from edited SRT ───────────────────────────
            # The original ASS had per-word karaoke timestamps from Whisper.
            # After the user edits the SRT those timestamps no longer match,
            # so we rebuild a clean ASS directly from the SRT content.
            # The new ASS has one Dialogue event per SRT entry (no karaoke)
            # but inherits all styling via force_style at render time.
            if new_srt:
                new_ass_name = os.path.splitext(job.get('srt') or '')[0] + '.ass'
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
        title_outline_w = parse_int_field(request.form.get('title_outline_w', '2'), 2)
        title_bg_enabled = request.form.get('title_bg_enabled') == '1'
        title_bg_color = request.form.get('title_bg_color', '#000000')
        title_bg_opacity = parse_int_field(request.form.get('title_bg_opacity', '60'), 60)
        sub_font     = request.form.get('sub_font', 'Inter')
        sub_bold     = request.form.get('sub_bold') == '1'
        gpu_mode     = request.form.get('gpu_mode', 'auto')
        sub_color    = request.form.get('sub_color', '#ffffff')
        sub_highlight_color = request.form.get('sub_highlight_color', '#ffff00')
        sub_highlight_text_color = request.form.get('sub_highlight_text_color', '#000000')
        sub_highlight_opacity = parse_int_field(request.form.get('sub_highlight_opacity', '100'), 100)
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
        # ── spacing controls (allow 0 and negative, unlike parse_int_field) ──
        try:    title_line_sp = str(int(float(request.form.get('title_line_spacing', '0'))))
        except: title_line_sp = '0'
        try:    title_letter_sp = str(float(request.form.get('title_letter_spacing', '0')))
        except: title_letter_sp = '0'
        try:    sub_line_sp = str(int(float(request.form.get('sub_line_spacing', '0'))))
        except: sub_line_sp = '0'
        try:    sub_letter_sp = str(float(request.form.get('sub_letter_spacing', '0')))
        except: sub_letter_sp = '0'
        # ── project resolution & export controls ──────────────────────────
        project_res     = request.form.get('project_res', 'source').strip()
        project_w       = request.form.get('project_w', '').strip()
        project_h       = request.form.get('project_h', '').strip()
        export_fps      = request.form.get('export_fps', '').strip()
        export_bitrate  = request.form.get('export_bitrate', '').strip()
        # ── trim / crop / speed / audio controls ─────────────────────────
        trim_start      = request.form.get('trim_start', '').strip()
        trim_end        = request.form.get('trim_end', '').strip()
        trim_in_start   = request.form.get('trim_in_start', '').strip()
        trim_in_end     = request.form.get('trim_in_end', '').strip()
        crop_x          = request.form.get('crop_x', '').strip()
        crop_y          = request.form.get('crop_y', '').strip()
        crop_w          = request.form.get('crop_w', '').strip()
        crop_h          = request.form.get('crop_h', '').strip()
        video_speed     = request.form.get('video_speed', '1.0').strip()
        video_scale     = request.form.get('video_scale', '1.0').strip()
        video_pan_x     = request.form.get('video_pan_x', '0').strip()
        video_pan_y     = request.form.get('video_pan_y', '0').strip()
        audio_boost_db  = request.form.get('audio_boost', '0').strip()
        sub_word_spacing = request.form.get('sub_word_spacing', '0').strip()
        preview_mode    = request.form.get('preview_mode') == '1'

        job.update({
            'title_text': title_text,   'title_font': title_font,
            'title_bold': title_bold,   'title_color': title_color,
            'title_stroke_color': title_stroke,
            'title_size': title_size,   'title_x': title_x, 'title_y': title_y,
            'title_outline_w': title_outline_w,
            'title_bg_enabled': title_bg_enabled,
            'title_bg_color': title_bg_color,
            'title_bg_opacity': title_bg_opacity,
            'sub_font': sub_font,       'sub_bold': sub_bold,
            'gpu_mode': gpu_mode,
            'sub_color': sub_color,
            'sub_highlight_color': sub_highlight_color,
            'sub_highlight_text_color': sub_highlight_text_color,
            'sub_highlight_opacity': sub_highlight_opacity,
            'sub_stroke_color': sub_stroke, 'sub_size': sub_size, 'sub_y': sub_y,
            'sub_outline_w': sub_outline_w,
            'sub_hl_box': sub_hl_box,
            'sub_bg_enabled': sub_bg_enabled,
            'sub_bg_color': sub_bg_color,
            'sub_bg_opacity': sub_bg_opacity,
            'overlay_x': overlay_x,    'overlay_y': overlay_y,
            'overlay_w': overlay_w,    'overlay_h': overlay_h,
            'preview_w': prev_w, 'preview_h': prev_h,
            'title_line_spacing': title_line_sp,
            'title_letter_spacing': title_letter_sp,
            'sub_line_spacing': sub_line_sp,
            'sub_letter_spacing': sub_letter_sp,
            'project_res': project_res,
            'project_w': project_w,
            'project_h': project_h,
            'export_fps': export_fps,
            'export_bitrate': export_bitrate,
            'gpu_mode': gpu_mode,
            'trim_start': trim_start,
            'trim_end': trim_end,
            'trim_in_start': trim_in_start,
            'trim_in_end': trim_in_end,
            'crop_x': crop_x, 'crop_y': crop_y,
            'crop_w': crop_w, 'crop_h': crop_h,
            'video_speed': video_speed,
            'video_scale': video_scale,
            'video_pan_x': video_pan_x,
            'video_pan_y': video_pan_y,
            'audio_boost_db': audio_boost_db,
            'sub_word_spacing': sub_word_spacing,
        })

        # ── download a font from the library (via form POST) ──────────
        download_font_name = request.form.get('download_font_name', '').strip()
        if download_font_name:
            if download_font_by_name(download_font_name):
                refresh_font_map()
                save_jobs()
                flash(f'Font "{download_font_name}" downloaded successfully.', 'success')
            else:
                flash(f'Font "{download_font_name}" not found in library.', 'warning')
            return redirect(url_for('editor', job_id=job_id))

        # ── handle overlay upload ─────────────────────────────────────────
        overlay_file = request.files.get('overlay')
        overlay_clear = request.form.get('overlay_clear') == '1'
        if overlay_clear:
            # remove current overlay
            old_overlay = job.get('overlay')
            if old_overlay:
                old_path = os.path.join(DOWNLOADS_DIR, old_overlay)
                try:
                    if os.path.exists(old_path):
                        os.remove(old_path)
                except Exception:
                    pass
            job.pop('overlay', None)
        elif overlay_file and overlay_file.filename:
            safe_name = os.path.basename(overlay_file.filename)
            # delete previous overlay file if it exists and is different
            old_overlay = job.get('overlay')
            if old_overlay and old_overlay != safe_name:
                old_path = os.path.join(DOWNLOADS_DIR, old_overlay)
                try:
                    if os.path.exists(old_path):
                        os.remove(old_path)
                except Exception:
                    pass
            overlay_file.save(os.path.join(DOWNLOADS_DIR, safe_name))
            job['overlay'] = safe_name
        else:
            # No new file uploaded and not clearing – use the hidden field
            # value (set by applyConfig when loading a template, or from the
            # initial page render).  This must run unconditionally so that
            # loading a template *replaces* any previously-set overlay.
            ov_from_field = request.form.get('current_overlay_file', '').strip()
            if ov_from_field:
                overlay_path = resolve_overlay_path(ov_from_field)
                if overlay_path:
                    job['overlay'] = os.path.basename(ov_from_field)
                else:
                    # File referenced by the hidden field no longer exists
                    job.pop('overlay', None)
            else:
                # Hidden field is empty – user cleared the overlay in the UI
                job.pop('overlay', None)
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

        # Canvas preview dimensions are set to the target resolution by JS.
        # Overlay/title/subtitle positions are in target-space coordinates.
        # The ffmpeg filter chain applies overlay after the video transform,
        # so these coordinates match without any scaling.  We still fetch the
        # source video size for the transform calculations below.
        vid_w, vid_h = get_video_size(os.path.join(DOWNLOADS_DIR, orig_video))

        # ── trim / crop / speed / audio pre-filters ─────────────────────
        pre_filters = []
        audio_filters = []

        # Validate and parse trim times (in seconds or HH:MM:SS)
        trim_start_sec = None
        trim_end_sec = None
        trim_in_start_sec = None
        trim_in_end_sec = None
        try:
            if trim_start:
                trim_start_sec = _time_to_seconds(trim_start)
        except (ValueError, TypeError):
            update_job(job_id, log=f"warning: invalid trim_start '{trim_start}', ignoring")
        try:
            if trim_end:
                trim_end_sec = _time_to_seconds(trim_end)
        except (ValueError, TypeError):
            update_job(job_id, log=f"warning: invalid trim_end '{trim_end}', ignoring")
        try:
            if trim_in_start:
                trim_in_start_sec = _time_to_seconds(trim_in_start)
        except (ValueError, TypeError):
            update_job(job_id, log=f"warning: invalid trim_in_start '{trim_in_start}', ignoring")
        try:
            if trim_in_end:
                trim_in_end_sec = _time_to_seconds(trim_in_end)
        except (ValueError, TypeError):
            update_job(job_id, log=f"warning: invalid trim_in_end '{trim_in_end}', ignoring")

        # ── Video transform (scale + pan) for landscape/portrait adjustment ─
        RESOLUTION_PRESETS = {
            'source': None,
            '1080x1920': (1080, 1920),
            '1920x1080': (1920, 1080),
            '1080x1080': (1080, 1080),
            '1280x720':  (1280, 720),
            '720x1280':  (720, 1280),
        }

        # Determine target resolution
        target_w, target_h = vid_w, vid_h  # default: keep source resolution
        try:
            pw = int(float(project_w)) if project_w else 0
            ph = int(float(project_h)) if project_h else 0
            if project_res == 'custom' and pw > 0 and ph > 0:
                target_w, target_h = pw, ph
            elif project_res in RESOLUTION_PRESETS and RESOLUTION_PRESETS[project_res] is not None:
                target_w, target_h = RESOLUTION_PRESETS[project_res]
        except (ValueError, TypeError):
            pass

        # If target differs from source, build a cover-scale → zoom → pan → crop chain
        if (target_w, target_h) != (vid_w, vid_h):
            try:
                vs = max(0.1, min(10.0, float(video_scale)))
                vpx = float(video_pan_x)
                vpy = float(video_pan_y)
            except (ValueError, TypeError):
                vs, vpx, vpy = 1.0, 0, 0

            # Cover-scale: scale source so it fully covers the target frame
            fill_w = target_w / vid_w
            fill_h = target_h / vid_h
            base_scale = max(fill_w, fill_h)  # cover behavior – use the larger dimension
            total_scale = base_scale * vs

            scaled_w = vid_w * total_scale
            scaled_h = vid_h * total_scale

            # Center crop offset (default: center the source in the target frame)
            center_x = (scaled_w - target_w) / 2
            center_y = (scaled_h - target_h) / 2
            crop_x_val = max(0, center_x + vpx)
            crop_y_val = max(0, center_y + vpy)

            pre_filters.append(
                f"scale=iw*{total_scale:.4f}:ih*{total_scale:.4f}:flags=lanczos,"
                f"crop={target_w}:{target_h}:{crop_x_val:.1f}:{crop_y_val:.1f}"
            )
            update_job(job_id,
                log=f"project: {vid_w}x{vid_h}→{target_w}x{target_h} "
                    f"(cover×{base_scale:.2f}, zoom×{vs:.2f}, pan=({vpx:.0f},{vpy:.0f}))")

            # Update resolution for all subsequent filters (ASS, drawtext, etc.)
            vid_w, vid_h = target_w, target_h
        else:
            # Same resolution – zoom/pan still work but crop to source dims
            try:
                vs = max(0.1, min(10.0, float(video_scale)))
                vpx = float(video_pan_x)
                vpy = float(video_pan_y)
                if vs != 1.0 or vpx != 0 or vpy != 0:
                    pre_filters.append(
                        f"scale=iw*{vs:.4f}:ih*{vs:.4f}:flags=lanczos,"
                        f"crop={vid_w}:{vid_h}:({vs:.4f}*iw-{vid_w})/2+{vpx:.1f}:({vs:.4f}*ih-{vid_h})/2+{vpy:.1f}"
                    )
                    update_job(job_id, log=f"video transform: scale={vs:.2f} pan=({vpx:.0f},{vpy:.0f})")
            except (ValueError, TypeError):
                pass

        # Crop filter (x:y:w:h)
        if all(v for v in [crop_x, crop_y, crop_w, crop_h]):
            try:
                cx, cy, cw, ch = int(crop_x), int(crop_y), int(crop_w), int(crop_h)
                if cw > 0 and ch > 0:
                    pre_filters.append(f"crop={cw}:{ch}:{cx}:{cy}")
                    update_job(job_id, log=f"crop filter: {cw}x{ch}+{cx}+{cy}")
            except (ValueError, TypeError):
                update_job(job_id, log="warning: invalid crop values, ignoring")

        # Speed filter (setpts for video, atempo for audio)
        try:
            speed_val = float(video_speed)
            if speed_val <= 0:
                speed_val = 1.0
            if speed_val != 1.0:
                # setpts adjusts video speed: setpts=PTS/SPEED
                pre_filters.append(f"setpts={1.0/speed_val:.4f}*PTS")
                # atempo adjusts audio speed; atempo range is [0.5, 2.0], chain for extreme values
                remaining = speed_val
                atempo_parts = []
                while remaining > 2.0:
                    atempo_parts.append("atempo=2.0")
                    remaining /= 2.0
                while remaining < 0.5:
                    atempo_parts.append("atempo=0.5")
                    remaining /= 0.5
                atempo_parts.append(f"atempo={remaining:.4f}")
                audio_filters.append(",".join(atempo_parts))
                update_job(job_id, log=f"speed: {speed_val}x (video+audio)")
        except (ValueError, TypeError):
            pass

        # Audio volume boost
        try:
            boost_db = float(audio_boost_db)
            if boost_db != 0:
                audio_filters.append(f"volume={boost_db}dB")
                update_job(job_id, log=f"audio boost: {boost_db}dB")
        except (ValueError, TypeError):
            pass

        # filters: video transforms first, then subtitles/titles after overlay
        vf_parts = list(pre_filters)  # pre_filters go first
        n_video_filters = len(vf_parts)  # split-point: overlay goes between these and subtitle/title
        fonts_dir_esc = FONTS_DIR.replace('\\', '/').replace(':', '\\:')

        # Use base font family + Bold=1 flag.  fontconfig resolves bold by weight.
        # (The canvas @font-face uses "FontName Bold" — that's a separate browser convention.)
        effective_sub_font = sub_font
        sub_bold_flag = 'Bold=1' if sub_bold else ''

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
                        f"Alignment=2,MarginL=0,MarginR=0,MarginV={sub_y}"
                    )
                    if sub_bold_flag:
                        sub_style += f",{sub_bold_flag}"
                    sub_letter_sp = float(job.get('sub_letter_spacing', 0) or 0)
                    if sub_letter_sp:
                        sub_style += f",Spacing={sub_letter_sp}"
                else:
                    sub_style = (
                        f"FontName={effective_sub_font},FontSize={sub_size},"
                        f"PrimaryColour={html_to_ass_color(sub_color)},"
                        f"OutlineColour={html_to_ass_color(sub_stroke)},"
                        f"Outline={sub_outline_w},Alignment=2,MarginL=0,MarginR=0,MarginV={sub_y}"
                    )
                    if sub_bold_flag:
                        sub_style += f",{sub_bold_flag}"
                    sub_letter_sp = float(job.get('sub_letter_spacing', 0) or 0)
                    if sub_letter_sp:
                        sub_style += f",Spacing={sub_letter_sp}"
                vf_parts.append(f"subtitles='{ass_abs}':fontsdir='{fonts_dir_esc}':force_style='{sub_style}'")
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
                        f"Alignment=2,MarginL=0,MarginR=0,MarginV={sub_y}"
                    )
                    if sub_bold_flag:
                        sub_style += f",{sub_bold_flag}"
                else:
                    sub_style = (
                        f"FontName={effective_sub_font},FontSize={sub_size},"
                        f"PrimaryColour={html_to_ass_color(sub_color)},"
                        f"OutlineColour={html_to_ass_color(sub_stroke)},"
                        f"Outline={sub_outline_w},Alignment=2,MarginL=0,MarginR=0,MarginV={sub_y}"
                    )
                    if sub_bold_flag:
                        sub_style += f",{sub_bold_flag}"
                sub_letter_sp = float(job.get('sub_letter_spacing', 0) or 0)
                if sub_letter_sp:
                    sub_style += f",Spacing={sub_letter_sp}"
                vf_parts.append(f"subtitles='{srt_abs}':fontsdir='{fonts_dir_esc}':force_style='{sub_style}'")

        if has_title:
            # ── Title via ASS (same pipeline as subtitles) ──────────────
            # Letter-spacing (ASS Spacing) and line-height are reliable in
            # libass, unlike drawtext's letter_spacing which many FFmpeg
            # builds do not support.  Each line gets its own Dialogue
            # event with \\pos for pixel-precise canvas-style placement.
            title_ass_name = f"job_{job_id}_title.ass"
            title_ass_path = os.path.join(DOWNLOADS_DIR, title_ass_name)
            title_letter_sp = float(job.get('title_letter_spacing', 0) or 0)
            title_line_sp   = int(float(job.get('title_line_spacing', 0) or 0))
            title_outline_w = int(job.get('title_outline_w', '2') or 2)
            title_bg_enabled = job.get('title_bg_enabled', False)
            title_bg_color = job.get('title_bg_color', '#000000')
            title_bg_opacity = int(job.get('title_bg_opacity', '60') or 60)
            # Clamp title position so it never goes out of frame
            safe_title_x = max(0, min(vid_w - 10, int(title_x)))
            safe_title_y = max(0, min(vid_h - 10, int(title_y)))
            title_to_ass(
                title_text=title_text,
                ass_path=title_ass_path,
                font_name=title_font,
                font_size=int(title_size),
                color=title_color,
                stroke_color=title_stroke,
                stroke_width=title_outline_w,
                bold=title_bold,
                letter_spacing=title_letter_sp,
                title_x=safe_title_x,
                title_y=safe_title_y,
                line_spacing=title_line_sp,
                video_width=vid_w,
                video_height=vid_h,
            )
            title_ass_abs = title_ass_path.replace('\\', '/').replace(':', '\\:')
            # force_style must match the ASS Style line: base font name + Bold flag
            # Explicit MarginL/MarginR/MarginV=0 to prevent any hidden renderer defaults
            if title_bg_enabled:
                ass_alpha = format(int(255 * (1 - title_bg_opacity / 100)), '02X')
                bg_ass = f"&H{ass_alpha}" + html_to_ass_color(title_bg_color)[3:]
                title_style = (
                    f"FontName={title_font},FontSize={title_size},"
                    f"PrimaryColour={html_to_ass_color(title_color)},"
                    f"OutlineColour={html_to_ass_color(title_stroke)},"
                    f"BackColour={bg_ass},"
                    f"BorderStyle=3,Outline={title_outline_w},Shadow=1,"
                    f"Alignment=7,Bold={1 if title_bold else 0},"
                    f"MarginL=0,MarginR=0,MarginV=0"
                )
            else:
                title_style = (
                    f"FontName={title_font},FontSize={title_size},"
                    f"PrimaryColour={html_to_ass_color(title_color)},"
                    f"OutlineColour={html_to_ass_color(title_stroke)},"
                    f"Outline={title_outline_w},"
                    f"Alignment=7,Bold={1 if title_bold else 0},"
                    f"MarginL=0,MarginR=0,MarginV=0"
                )
            if title_letter_sp:
                title_style += f",Spacing={title_letter_sp}"
            vf_parts.append(f"subtitles='{title_ass_abs}':fontsdir='{fonts_dir_esc}':force_style='{title_style}'")

        # Always write the filter graph to a script file and use
        # -filter_complex_script so Windows never interprets special characters
        # (&H colours, Unicode text, semicolons, single-quotes) on the command line.
        fc_script_name = f"job_{job_id}_fc.txt"
        fc_script_path = os.path.join(DOWNLOADS_DIR, fc_script_name)

        render_quality = list(VIDEO_QUALITY)
        if gpu_mode == 'auto':
            selected_gpu = select_auto_gpu_encoder()
            if selected_gpu:
                render_quality = list(GPU_ENCODER_QUALITY[selected_gpu])
                update_job(job_id, log=f"auto GPU mode selected {selected_gpu}")
            else:
                update_job(job_id, log="auto GPU mode selected but no supported encoder found, using CPU")
                flash('No supported GPU encoder found; using CPU encoder instead.', 'warning')
        elif gpu_mode in GPU_ENCODER_QUALITY:
            if ffmpeg_supports_encoder(gpu_mode) and _probe_encoder_works(gpu_mode):
                render_quality = list(GPU_ENCODER_QUALITY[gpu_mode])
                update_job(job_id, log=f"using GPU encoder {gpu_mode}")
            else:
                update_job(job_id, log=f"GPU encoder {gpu_mode} unavailable, falling back to CPU")
                flash(f'GPU encoder {gpu_mode} unavailable; using CPU encoder instead.', 'warning')
        # ── preview mode: use ultrafast preset & lower quality ──────────
        if preview_mode:
            # Override with fast preview settings
            render_quality = [
                '-c:v', 'libx264', '-crf', '28', '-preset', 'ultrafast',
                '-g', '30', '-keyint_min', '30', '-sc_threshold', '0',
                '-pix_fmt', 'yuv420p', '-movflags', '+faststart'
            ]
            # also limit resolution for faster preview
            vf_parts.insert(0, 'scale=540:-2')
            update_job(job_id, log="preview mode: fast render with lower quality")
        # ── apply user export overrides ──────────────────────────────────
        export_fps = job.get('export_fps', '').strip()
        export_bitrate = job.get('export_bitrate', '').strip()
        if export_fps:
            render_quality.insert(0, '-r'); render_quality.insert(1, export_fps)
        if export_bitrate:
            render_quality.insert(0, '-b:v'); render_quality.insert(1, export_bitrate)

        # ── Build mid-video trim filter if specified ─────────────────────
        # If trim_in_start and trim_in_end are set, we cut out that middle
        # segment by splitting into two parts and concatenating them.
        has_mid_cut = (trim_in_start_sec is not None and trim_in_end_sec is not None
                       and trim_in_end_sec > trim_in_start_sec)
        mid_cut_video_prefix = ""
        mid_cut_video_suffix = ""
        mid_cut_audio_prefix = ""
        mid_cut_audio_suffix = ""
        mid_cut_input_label = "[0:v]"

        if has_mid_cut:
            its = trim_in_start_sec
            ite = trim_in_end_sec
            mid_cut_video_prefix = (
                f"[0:v]trim=0:{its:.3f},setpts=PTS-STARTPTS[vca];"
                f"[0:v]trim={ite:.3f},setpts=PTS-STARTPTS[vcb];"
                f"[vca][vcb]concat=n=2:v=1:a=0"
            )
            mid_cut_video_suffix = ""
            mid_cut_audio_prefix = (
                f"[0:a]atrim=0:{its:.3f},asetpts=PTS-STARTPTS[aca];"
                f"[0:a]atrim={ite:.3f},asetpts=PTS-STARTPTS[acb];"
                f"[aca][acb]concat=n=2:v=0:a=1"
            )
            mid_cut_audio_suffix = ""
            mid_cut_input_label = "[vcut]"
            update_job(job_id, log=f"mid-video cut: remove {its:.1f}s–{ite:.1f}s")

        if has_overlay:
            ov_scale = f"[1:v]scale={overlay_w}:{overlay_h}[ov]"
            video_filters = vf_parts[:n_video_filters]
            post_filters = vf_parts[n_video_filters:]
            pre_chain = ','.join(video_filters) if video_filters else 'null'
            post_chain = ','.join(post_filters) if post_filters else 'null'

            if has_mid_cut:
                fc = (
                    f"{mid_cut_video_prefix}[vcut];"
                    f"[vcut]{pre_chain}[vpre];"
                    f"{ov_scale};"
                    f"[vpre][ov]overlay={overlay_x}:{overlay_y}[ovout];"
                    f"[ovout]{post_chain}[final]"
                )
            else:
                fc = (
                    f"[0:v]{pre_chain}[vpre];"
                    f"{ov_scale};"
                    f"[vpre][ov]overlay={overlay_x}:{overlay_y}[ovout];"
                    f"[ovout]{post_chain}[final]"
                )
            out_label = '[final]'
            with open(fc_script_path, 'w', encoding='utf-8') as _fc:
                _fc.write(fc)
            update_job(job_id, log=f"filter_complex (overlay after transform): {fc}")
            use_fc_script = ffmpeg_supports_filter_complex_script()
            cmd = ['ffmpeg', '-nostdin', '-y']
            # Trim: -ss before -i for fast seeking
            if trim_start_sec is not None:
                cmd.extend(['-ss', f'{trim_start_sec:.3f}'])
            cmd.extend(['-i', orig_video, '-i', overlay_path])
            if use_fc_script:
                cmd.extend(['-filter_complex_script', fc_script_name])
            else:
                cmd.extend(['-filter_complex', fc])
            cmd.extend(['-map', out_label, '-map', '0:a?'])
            # Trim end (only if no mid-cut; mid-cut handles its own timing)
            if not has_mid_cut:
                if trim_end_sec is not None and trim_start_sec is not None:
                    dur = trim_end_sec - trim_start_sec
                    if dur > 0:
                        cmd.extend(['-t', f'{dur:.3f}'])
                elif trim_end_sec is not None:
                    cmd.extend(['-to', f'{trim_end_sec:.3f}'])
            # Audio filters (including mid-cut audio)
            all_audio_filters = list(audio_filters)
            if has_mid_cut:
                # mid-cut audio is handled via filter_complex, not -af
                pass
            if all_audio_filters:
                af_chain = ','.join(all_audio_filters)
                cmd.extend(['-af', af_chain])
            cmd.extend(render_quality)
            cmd.extend(['-c:a', 'aac', '-b:a', '192k', new_video_name])
        elif vf_parts or audio_filters or has_mid_cut:
            if vf_parts:
                vf_chain = ','.join(vf_parts)
                if has_mid_cut:
                    fc = f"{mid_cut_video_prefix}[vcut];[vcut]{vf_chain}[vout]"
                else:
                    fc = f"[0:v]{vf_chain}[vout]"
                out_label = '[vout]'
            elif has_mid_cut:
                fc = f"{mid_cut_video_prefix}[vout]"
                out_label = '[vout]'
            else:
                # Only audio filters, pass video through
                fc = "[0:v]null[vout]"
                out_label = '[vout]'
            with open(fc_script_path, 'w', encoding='utf-8') as _fc:
                _fc.write(fc)
            update_job(job_id, log=f"filter_complex (no overlay): {fc}")
            use_fc_script = ffmpeg_supports_filter_complex_script()
            cmd = ['ffmpeg', '-nostdin', '-y', '-fontsdir', FONTS_DIR]
            # Trim
            if trim_start_sec is not None:
                cmd.extend(['-ss', f'{trim_start_sec:.3f}'])
            cmd.extend(['-i', orig_video])
            if use_fc_script:
                cmd.extend(['-filter_complex_script', fc_script_name])
            else:
                cmd.extend(['-filter_complex', fc])
            cmd.extend(['-map', out_label, '-map', '0:a?'])
            # Trim end
            if not has_mid_cut:
                if trim_end_sec is not None and trim_start_sec is not None:
                    dur = trim_end_sec - trim_start_sec
                    if dur > 0:
                        cmd.extend(['-t', f'{dur:.3f}'])
                elif trim_end_sec is not None:
                    cmd.extend(['-to', f'{trim_end_sec:.3f}'])
            # Audio filters
            all_audio_filters2 = list(audio_filters)
            if all_audio_filters2:
                af_chain = ','.join(all_audio_filters2)
                cmd.extend(['-af', af_chain])
            cmd.extend(render_quality)
            cmd.extend(['-c:a', 'aac', '-b:a', '192k', new_video_name])
        else:
            cmd = ['ffmpeg', '-nostdin', '-y']
            if trim_start_sec is not None:
                cmd.extend(['-ss', f'{trim_start_sec:.3f}'])
            cmd.extend(['-i', orig_video])
            if has_mid_cut:
                # Use filter_complex for mid-cut even without other filters
                fc = f"{mid_cut_video_prefix}[vout]"
                out_label = '[vout]'
                with open(fc_script_path, 'w', encoding='utf-8') as _fc:
                    _fc.write(fc)
                use_fc_script = ffmpeg_supports_filter_complex_script()
                if use_fc_script:
                    cmd.extend(['-filter_complex_script', fc_script_name])
                else:
                    cmd.extend(['-filter_complex', fc])
                cmd.extend(['-map', out_label, '-map', '0:a?'])
            else:
                if trim_end_sec is not None and trim_start_sec is not None:
                    dur = trim_end_sec - trim_start_sec
                    if dur > 0:
                        cmd.extend(['-t', f'{dur:.3f}'])
                elif trim_end_sec is not None:
                    cmd.extend(['-to', f'{trim_end_sec:.3f}'])
            if audio_filters:
                af_chain = ','.join(audio_filters)
                cmd.extend(['-af', af_chain])
            cmd.extend(render_quality)
            cmd.extend(['-c:a', 'aac', '-b:a', '192k', new_video_name])

        update_job(job_id, log=f"ffmpeg: {' '.join(cmd)}")
        try:
            result = subprocess.run(cmd, cwd=DOWNLOADS_DIR, capture_output=True, text=True,
                                    encoding='utf-8', errors='replace', env=ffmpeg_env(), timeout=1800)
        except subprocess.TimeoutExpired:
            update_job(job_id, status='error', log='Render timed out after 30 minutes')
            flash('Render timed out — the video may be too long or complex.', 'danger')
            return redirect(url_for('editor', job_id=job_id))
        except Exception as exc:
            update_job(job_id, status='error', log=f'Render crashed: {exc}')
            flash(f'Render crashed: {exc}', 'danger')
            return redirect(url_for('editor', job_id=job_id))
        if result.returncode != 0:
            stderr_tail = (result.stderr or '')[-800:]
            update_job(job_id, log=f"ffmpeg stderr: {stderr_tail}")
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
        font_manager.FONT_MAP.keys(),
        key=lambda name: (0 if name == 'Inter' else 1 if name == 'Inter Bold' else 2, name)
    )
    font_faces = []
    fonts_dir_abs = os.path.abspath(FONTS_DIR)
    _var_cache = {}  # cache is_variable_font results per path
    for font_name, font_path in font_manager.FONT_MAP.items():
        abs_path = os.path.abspath(font_path)
        if not abs_path.startswith(fonts_dir_abs):
            continue
        font_file = os.path.basename(abs_path)
        if not font_file:
            continue
        ext = os.path.splitext(font_file)[1].lower()
        font_fmt = 'opentype' if ext == '.otf' else 'truetype'

        # Detect variable vs static so the browser handles weight correctly
        if abs_path not in _var_cache:
            _var_cache[abs_path] = is_variable_font(abs_path)
        is_var = _var_cache[abs_path]

        face = {
            'name': font_name,
            'url': url_for('serve_font', filename=font_file),
            'format': font_fmt,
            'variable': is_var,
        }
        font_faces.append(face)
    return render_template('editor.html', job=job, srt_content=srt_text,
                           job_key=job_id, font_list=sorted_fonts,
                           font_faces=font_faces,
                           templates=get_all_templates())


def register(app):
    """Attach the routes to ``app`` (endpoint names = function names)."""
    app.add_url_rule('/editor/<job_id>', methods=['GET', 'POST'], view_func=editor)
