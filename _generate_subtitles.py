import os
import glob
import re
import sys
import io
import json
import base64
import time
import requests
from moviepy import VideoFileClip

# ── optional Whisper import (only loaded on demand) ──────────────────────
_whisper_module = None

def _get_whisper():
    global _whisper_module
    if _whisper_module is None:
        import whisper
        _whisper_module = whisper
    return _whisper_module

# ═══════════════════════════════════════════════════════════════════════════
#  Gemini API helpers (generativelanguage.googleapis.com)
#  – models newer than gemini‑1.5‑flash (e.g. gemini‑2.0‑flash, gemini‑2.5‑flash)
# ═══════════════════════════════════════════════════════════════════════════

# ── Hardcoded fallback keys (used when the user does not provide their own) ──
_HARDCODED_GEMINI_KEY_1 = ""  # ← paste your primary key here
_HARDCODED_GEMINI_KEY_2 = ""  # ← paste your secondary (quota‑fallback) key here

GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GEMINI_DEFAULT_MODEL = "gemini-2.5-flash"  # model implicit, > gemini‑1.5‑flash


def _get_gemini_api_keys():
    """Return a list of available Gemini API keys.

    Priority:
      1. GEMINI_USER_KEY env var (from web UI) — highest priority
      2. GOOGLE_API_KEY_1 / GOOGLE_API_KEY_2 env vars (user‑configured)
      3. Hardcoded fallback keys (_HARDCODED_GEMINI_KEY_1 / _2)

    Duplicates are removed.
    """
    keys = []
    seen = set()

    # 1. User‑provided key from the web UI (highest priority)
    ui_key = os.getenv("GEMINI_USER_KEY", "").strip()
    if ui_key and ui_key not in seen:
        keys.append(ui_key)
        seen.add(ui_key)

    # 2. Environment‑variable keys
    for env_name in ("GOOGLE_API_KEY_1", "GOOGLE_API_KEY_2"):
        key = os.getenv(env_name, "").strip()
        if key and key not in seen:
            keys.append(key)
            seen.add(key)

    # 3. Hardcoded fallback keys (used when user hasn't provided any, or as extras)
    for hard_key in (_HARDCODED_GEMINI_KEY_1, _HARDCODED_GEMINI_KEY_2):
        key = hard_key.strip()
        if key and key not in seen:
            keys.append(key)
            seen.add(key)

    return keys


def _transcribe_gemini(audio_path, language="ro-RO", model=None, max_retries=2):
    """Transcribe audio using Gemini API (generativelanguage.googleapis.com).

    Rotates through GOOGLE_API_KEY_1 / GOOGLE_API_KEY_2 / hardcoded keys
    on quota errors.  Returns a list of segment dicts compatible with
    Whisper's output format, or raises RuntimeError if all keys fail.
    """
    if model is None:
        model = GEMINI_DEFAULT_MODEL

    api_keys = _get_gemini_api_keys()
    if not api_keys:
        raise RuntimeError(
            "No Gemini API keys configured. "
            "Set GOOGLE_API_KEY_1 and/or GOOGLE_API_KEY_2 environment variables, "
            "or edit _HARDCODED_GEMINI_KEY_1 / _HARDCODED_GEMINI_KEY_2 in _generate_subtitles.py."
        )

    # Read and encode audio
    with open(audio_path, "rb") as f:
        audio_bytes = f.read()

    file_size_mb = len(audio_bytes) / (1024 * 1024)
    if file_size_mb > 18:
        print(f"Audio file is {file_size_mb:.1f} MB — splitting into chunks for Gemini API")
        return _transcribe_gemini_chunked(audio_path, language, model, api_keys)

    audio_b64 = base64.b64encode(audio_bytes).decode("ascii")

    language_name = "Romanian" if language.startswith("ro") else language
    prompt = (
        f"Transcrie acest audio în limba {language_name}. "
        "Returnează DOAR un array JSON valid, fără text înainte sau după. "
        "Fiecare element din array trebuie să aibă exact aceste chei: "
        '"start" (număr, secunde), "end" (număr, secunde), '
        '"text" (string, textul rostit), '
        '"words" (array de obiecte cu "word", "start", "end" — fiecare cuvânt cu timestamp-ul lui). '
        "Nu inventa text. Dacă nu auzi nimic, returnează []. "
        "IMPORTANT: output-ul trebuie să fie EXACT un array JSON, nimic altceva."
    )

    payload = {
        "contents": [{
            "parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": "audio/wav", "data": audio_b64}},
            ]
        }],
        "generationConfig": {
            "temperature": 0.0,
            "maxOutputTokens": 8192,
        },
    }

    last_error = None
    for key_idx, api_key in enumerate(api_keys):
        for attempt in range(max_retries):
            try:
                url = GEMINI_API_URL.format(model=model) + f"?key={api_key}"
                src = "env" if key_idx < len([k for k in api_keys if k not in (_HARDCODED_GEMINI_KEY_1.strip(), _HARDCODED_GEMINI_KEY_2.strip())]) else "hardcoded"
                print(f"Calling Gemini API ({model}, key {key_idx + 1}/{len(api_keys)} [{src}], attempt {attempt + 1})…")
                resp = requests.post(url, json=payload, timeout=180)
                if resp.status_code == 200:
                    data = resp.json()
                    return _gemini_result_to_segments(data)
                elif resp.status_code in (429, 403):
                    err = resp.json()
                    print(f"Gemini API key {key_idx + 1} returned {resp.status_code}: {err.get('error', {}).get('message', 'unknown')}")
                    break  # break retry loop, go to next key
                else:
                    err = resp.json()
                    msg = err.get("error", {}).get("message", str(err))
                    print(f"Gemini API error ({resp.status_code}): {msg}")
                    if resp.status_code == 400:
                        # Bad request – likely payload issue, don't retry same key
                        last_error = RuntimeError(f"Gemini API error: {msg}")
                        break
                    last_error = RuntimeError(f"Gemini API error: {msg}")
                    break
            except requests.exceptions.RequestException as e:
                print(f"Gemini API network error (key {key_idx + 1}, attempt {attempt + 1}): {e}")
                last_error = e
                time.sleep(2)  # brief backoff before retry

    if last_error:
        raise last_error
    raise RuntimeError("All Gemini API keys exhausted or invalid.")


def _transcribe_gemini_chunked(audio_path, language, model, api_keys):
    """Split long audio into ~50s chunks and transcribe each via Gemini."""
    import tempfile
    from moviepy import AudioFileClip

    audio_clip = AudioFileClip(audio_path)
    total_duration = audio_clip.duration
    chunk_duration = 50.0
    all_segments = []

    chunk_start = 0.0
    chunk_idx = 0
    while chunk_start < total_duration:
        chunk_end = min(chunk_start + chunk_duration, total_duration)
        print(f"Processing Gemini chunk {chunk_idx + 1}: {chunk_start:.1f}s – {chunk_end:.1f}s")

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            chunk_path = tmp.name
        try:
            sub_clip = audio_clip.subclipped(chunk_start, chunk_end)
            sub_clip.write_audiofile(chunk_path, logger=None, fps=16000)
            sub_clip.close()

            # Use same prompt + payload structure, just with chunk audio
            with open(chunk_path, "rb") as f:
                chunk_bytes = f.read()
            audio_b64 = base64.b64encode(chunk_bytes).decode("ascii")

            language_name = "Romanian" if language.startswith("ro") else language
            prompt = (
                f"Transcrie acest audio în limba {language_name}. "
                "Returnează DOAR un array JSON valid, fără text înainte sau după. "
                "Fiecare element din array trebuie să aibă exact aceste chei: "
                '"start" (număr, secunde), "end" (număr, secunde), '
                '"text" (string, textul rostit), '
                '"words" (array de obiecte cu "word", "start", "end"). '
                "Nu inventa text. Dacă nu auzi nimic, returnează []. "
                "IMPORTANT: output-ul trebuie să fie EXACT un array JSON."
            )

            payload = {
                "contents": [{
                    "parts": [
                        {"text": prompt},
                        {"inline_data": {"mime_type": "audio/wav", "data": audio_b64}},
                    ]
                }],
                "generationConfig": {"temperature": 0.0, "maxOutputTokens": 8192},
            }

            success = False
            for key_idx, api_key in enumerate(api_keys):
                try:
                    url = GEMINI_API_URL.format(model=model) + f"?key={api_key}"
                    resp = requests.post(url, json=payload, timeout=180)
                    if resp.status_code == 200:
                        data = resp.json()
                        segments = _gemini_result_to_segments(data)
                        for seg in segments:
                            seg["start"] += chunk_start
                            seg["end"] += chunk_start
                            for w in seg.get("words", []):
                                w["start"] = w.get("start", 0) + chunk_start
                                w["end"] = w.get("end", 0) + chunk_start
                        all_segments.extend(segments)
                        success = True
                        break
                    elif resp.status_code in (429, 403):
                        err = resp.json()
                        print(f"Gemini API key {key_idx + 1} quota: {err.get('error', {}).get('message', '?')}")
                        break
                    else:
                        err = resp.json()
                        print(f"Gemini API error: {err.get('error', {}).get('message', '?')}")
                        break
                except requests.exceptions.RequestException as e:
                    print(f"Gemini API network error: {e}")

            if not success:
                raise RuntimeError(f"Gemini chunk {chunk_idx + 1} transcription failed with all keys.")

        finally:
            try:
                os.remove(chunk_path)
            except Exception:
                pass

        chunk_start = chunk_end
        chunk_idx += 1

    audio_clip.close()
    return all_segments


def _gemini_result_to_segments(data):
    """Convert Gemini API response to Whisper-compatible segment dicts.

    Gemini returns a text response; we expect it to contain a JSON array
    of segments (as instructed by the prompt).  Falls back gracefully if
    the JSON is malformed.
    """
    raw_text = ""
    try:
        candidates = data.get("candidates", [])
        if candidates:
            parts = candidates[0].get("content", {}).get("parts", [])
            raw_text = "".join(p.get("text", "") for p in parts)
    except Exception:
        pass

    if not raw_text.strip():
        print("Gemini returned empty response")
        return []

    # Gemini sometimes wraps the JSON in ```json ... ``` markers
    raw_text = raw_text.strip()
    if raw_text.startswith("```"):
        # Strip code fences
        raw_text = re.sub(r"^```(?:json)?\s*\n?", "", raw_text)
        raw_text = re.sub(r"\n?```\s*$", "", raw_text)

    try:
        segments = json.loads(raw_text)
        if isinstance(segments, list):
            # Validate and normalise each segment
            valid = []
            for seg in segments:
                if not isinstance(seg, dict):
                    continue
                start = seg.get("start", 0)
                end = seg.get("end", 0)
                text = seg.get("text", "").strip()
                if not text:
                    continue
                if start >= end:
                    continue
                words = seg.get("words", [])
                # Ensure words have the right structure
                clean_words = []
                for w in words:
                    if isinstance(w, dict) and w.get("word", "").strip():
                        clean_words.append({
                            "word": w["word"].strip(),
                            "start": float(w.get("start", start)),
                            "end": float(w.get("end", end)),
                        })
                valid.append({
                    "start": float(start),
                    "end": float(end),
                    "text": text,
                    "words": clean_words if clean_words else [],
                })
            # De-overlap
            for i in range(len(valid) - 1):
                if valid[i]["end"] > valid[i + 1]["start"]:
                    valid[i]["end"] = valid[i + 1]["start"]
            return valid
        else:
            print(f"Gemini returned non-array JSON: {type(segments)}")
            return []
    except json.JSONDecodeError as e:
        print(f"Gemini JSON parse error: {e}")
        print(f"Raw response (first 500 chars): {raw_text[:500]}")
        return []


# ═══════════════════════════════════════════════════════════════════════════
#  Whisper helpers (local model, used as fallback)
# ═══════════════════════════════════════════════════════════════════════════

def _transcribe_whisper(audio_path, model_name="large"):
    """Transcribe using local Whisper model."""
    wh = _get_whisper()
    print(f"Loading Whisper model '{model_name}'…")
    model = wh.load_model(model_name)
    print("Transcribing with Whisper…")
    result = model.transcribe(
        audio_path,
        language="ro",
        task="transcribe",
        word_timestamps=True,
    )
    return result.get("segments", [])

# 1. Force standard I/O streams to use UTF-8 cross-platform safely
if sys.platform.startswith('win'):
    # Reconfigure the existing streams rather than replacing the wrapper wrappers entirely
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    else:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# 2. Defensive handling directly on your logging line (Line 277)
# Replace your current print statement with this safely handled variant:
def safe_print_video_path(video_path):
    try:
        print(f"Processing video: {video_path}")
    except UnicodeEncodeError:
        # Fallback for old Windows consoles that fundamentally reject unicode transfers
        clean_path = video_path.encode('utf-8', errors='replace').decode('utf-8', errors='replace')
        # If even that chokes on standard stdout, fallback to ascii sanitization
        try:
            print(f"Processing video: {clean_path}")
        except UnicodeEncodeError:
            print(f"Processing video: {video_path.encode('ascii', errors='replace').decode('ascii')}")

def extract_audio(video_path, audio_path):
    """
    Extracts audio from the video file and saves it as a WAV file.
    """
    video = VideoFileClip(video_path)
    video.audio.write_audiofile(audio_path, logger=None)
    return video.duration


def format_ass_timestamp(seconds):
    """Format seconds as ASS timestamp (H:MM:SS.cc)."""
    total_seconds = int(seconds)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    centiseconds = int((seconds - total_seconds) * 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{centiseconds:02d}"


def generate_ass_karaoke(segments, ass_path, max_length):
    """Generate an ASS file with per-word background-highlight rectangles.

    Word timing is computed **proportionally** within each segment (same
    logic as the canvas preview) so the rendered video matches what the
    user sees in the editor.  This approach completely avoids the
    overlapping-timestamp problems that Whisper's word-level timestamps
    produce when speech is fast.

    Placeholders ``##HLBG##``, ``##HLFG##``, ``##BORD##`` are replaced at
    render time with the user's chosen colours from the editor UI.
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

    lines = [header]

    for seg in segments:
        text = seg.get("text", "").strip()
        seg_start = seg["start"]
        seg_end = seg["end"]
        seg_duration = seg_end - seg_start

        if seg_duration <= 0:
            continue

        # Split the segment text into words (same logic as canvas preview)
        words = text.split()
        if not words:
            start = format_ass_timestamp(seg_start)
            end = format_ass_timestamp(seg_end)
            lines.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{_clean_text(text)}")
            continue

        word_count = len(words)
        word_duration = seg_duration / word_count

        # For each word position, create a Dialogue event where that word
        # is highlighted.  Timing is proportional — word i occupies
        # [seg_start + i*dur, seg_start + (i+1)*dur], matching the
        # canvas preview exactly.
        for wi, current_word in enumerate(words):
            ev_start = seg_start + wi * word_duration
            ev_end = seg_start + (wi + 1) * word_duration

            if ev_end - ev_start < 0.02:
                continue

            # Build the line: highlighted word gets background box via
            # \\3c + \\bord, others are plain text.
            parts = []
            for wj, w_text in enumerate(words):
                if wj == wi:
                    parts.append(
                        f"{{\\3c&H##HLBG##&\\bord##BORD##\\1c&H##HLFG##&}}"
                        f"{w_text}"
                        f"{{\\r}}"
                    )
                else:
                    parts.append(w_text)

            full_text = " ".join(parts)
            start_ts = format_ass_timestamp(ev_start)
            end_ts = format_ass_timestamp(ev_end)
            lines.append(f"Dialogue: 0,{start_ts},{end_ts},Default,,0,0,0,,{full_text}")

    content = "\n".join(lines) + "\n"
    with open(ass_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    print(f"ASS (per-word highlight) saved to: {ass_path}")

def format_timestamp(seconds):
    """
    Formats a time value (in seconds) as an SRT timestamp (HH:MM:SS,mmm).
    """
    total_seconds = int(seconds)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    millis = int((seconds - total_seconds) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"

def split_text_into_segments(text, max_length):
    """
    Splits text into segments so that no segment exceeds max_length characters.
    Attempts to split on word boundaries.
    """
    words = text.split()
    segments = []
    current_line = ""
    for word in words:
        if not current_line:
            current_line = word
        elif len(current_line) + 1 + len(word) <= max_length:
            current_line += " " + word
        else:
            segments.append(current_line)
            current_line = word
    if current_line:
        segments.append(current_line)
    return segments

def subdivide_segments_with_words(segments, max_length):
    """Subdivide segments so each sub-segment has ≤ max_length characters.

    Preserves word-level timestamps by redistributing the ``words`` array
    across sub-segments proportionally.  Returns a flat list of segments
    suitable for both SRT and ASS generators.
    """
    result = []
    for seg in segments:
        text = seg.get("text", "").strip().replace("\n", " ")
        words_data = seg.get("words", [])

        if len(text) <= max_length:
            result.append(seg)
            continue

        # Split the text into character-limited chunks
        text_chunks = split_text_into_segments(text, max_length)
        total_chars = sum(len(c) for c in text_chunks)
        duration = seg["end"] - seg["start"]

        # Build a mapping: for each chunk, which word indices belong to it
        # We track character positions to assign words to chunks
        chunk_word_groups = [[] for _ in text_chunks]
        char_pos = 0
        chunk_idx = 0
        chunk_char_count = 0

        for wi, w in enumerate(words_data):
            word_text = w.get("word", "").strip()
            if not word_text:
                continue
            # Add space before word (except first word in chunk)
            needed = len(word_text) + (1 if chunk_char_count > 0 else 0)

            # If this word would overflow the current chunk, move to next
            if chunk_char_count > 0 and chunk_char_count + needed > max_length and chunk_idx < len(text_chunks) - 1:
                chunk_idx += 1
                chunk_char_count = 0

            chunk_word_groups[chunk_idx].append(w)
            chunk_char_count += needed

        # Build sub-segments
        current_start = seg["start"]
        for ci, chunk_text in enumerate(text_chunks):
            proportion = len(chunk_text) / total_chars if total_chars > 0 else 0
            sub_duration = duration * proportion
            sub_end = current_start + sub_duration

            # Get words for this chunk and include them with absolute timestamps
            chunk_words = chunk_word_groups[ci] if ci < len(chunk_word_groups) else []
            adjusted_words = []
            for w in chunk_words:
                adjusted_words.append({
                    "word": w.get("word", ""),
                    "start": w.get("start", current_start),
                    "end": w.get("end", current_start + 0.1),
                })

            result.append({
                "start": current_start,
                "end": sub_end,
                "text": chunk_text,
                "words": adjusted_words,
            })
            current_start = sub_end

    # ── de-overlap consecutive segments ──────────────────────────────
    # When speech is fast, Whisper may produce overlapping segments
    # (e.g. seg 1 ends at 1.5 and seg 2 starts at 1.3).  Overlapping
    # Dialogue events in ASS/SRT cause two subtitle lines to appear on
    # screen at the same time.  We fix this by clamping each segment's
    # end time so it never exceeds the next segment's start time.
    for i in range(len(result) - 1):
        if result[i]["end"] > result[i + 1]["start"]:
            # clamp current segment's end to the next segment's start
            result[i]["end"] = result[i + 1]["start"]

    return result

def _clean_text(text):
    """Normalize segment text for SRT output.

    - replace any sequence of whitespace (including newlines) with a single space
    - strip leading/trailing whitespace
    """
    # collapse newlines and other whitespace into single spaces
    return re.sub(r"\s+", " ", text).strip()


def generate_srt(segments, srt_path, max_length):
    """
    Subdivides the transcript segments as needed and writes an SRT file.

    After constructing the raw output we perform extra sanitization so
    that the file never contains more than the canonical single blank line
    between entries.  This prevents situations where rendering or the web
    editor ends up showing double/quadruple blank lines.
    """
    all_segments = subdivide_segments_with_words(segments, max_length)

    # build output in memory first so we can normalize line breaks
    lines = []
    for i, seg in enumerate(all_segments):
        start_ts = format_timestamp(seg["start"])
        end_ts = format_timestamp(seg["end"])
        text = _clean_text(seg["text"])

        lines.append(str(i + 1))
        lines.append(f"{start_ts} --> {end_ts}")
        # even if text is empty we include the line so timing is preserved
        lines.append(text)
        lines.append("")  # blank separator

    content = "\n".join(lines).strip() + "\n"
    # collapse runs of 3+ newlines into exactly two (one blank line)
    content = re.sub(r"\n{3,}", "\n\n", content)

    # write with explicit newline to avoid platform conversions
    with open(srt_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)

    print(f"Subtitle saved to: {srt_path}")

def process_video(video_path, model_name, output_folder, max_length, method="auto"):
    """Processes one video:
        - Extracts its audio,
        - Transcribes using Google Speech API or Whisper (based on ``method``),
        - Generates an SRT file and an ASS file with karaoke word highlighting.

    ``method`` values:
      - ``"google"``  – Google Speech-to-Text API (needs GOOGLE_API_KEY_1/_2 env vars)
      - ``"whisper"`` – local Whisper model
      - ``"auto"``    – try Google API first, fall back to Whisper (default)
    """
    base_name = os.path.basename(video_path)
    name, _ = os.path.splitext(base_name)
    srt_filename = f"{name}.srt"
    ass_filename = f"{name}.ass"
    srt_path = os.path.join(output_folder, srt_filename)
    ass_path = os.path.join(output_folder, ass_filename)
    audio_path = os.path.join(output_folder, f"{name}_temp_audio.wav")

    safe_print_video_path(video_path)
    extract_audio(video_path, audio_path)

    segments = []
    used_method = method

    # ── Try Gemini API first ──────────────────────────────────────────
    if method in ("google", "auto"):
        try:
            segments = _transcribe_gemini(audio_path, language="ro-RO")
            used_method = "gemini"
            print(f"Gemini API returned {len(segments)} segments")
        except Exception as e:
            print(f"Gemini API failed: {e}")
            if method == "google":
                # User explicitly requested Google/Gemini-only — raise
                os.remove(audio_path)
                raise RuntimeError(f"Gemini API failed and method is 'google': {e}")
            # method == "auto" → fall through to Whisper
            print("Falling back to Whisper…")

    # ── Fall back to Whisper ──────────────────────────────────────────
    if not segments:
        try:
            segments = _transcribe_whisper(audio_path, model_name)
            used_method = "whisper"
            print(f"Whisper returned {len(segments)} segments")
        except Exception as e:
            os.remove(audio_path)
            raise RuntimeError(f"Whisper transcription also failed: {e}")

    if not segments:
        print(f"No segments were produced for {video_path}.")
        os.remove(audio_path)
        return

    # Generate both SRT (standard) and ASS (karaoke word-highlight)
    subdivided = subdivide_segments_with_words(segments, max_length)
    generate_srt(subdivided, srt_path, max_length)
    generate_ass_karaoke(subdivided, ass_path, max_length)
    os.remove(audio_path)
    print(f"Subtitles generated via {used_method}: {srt_path}, {ass_path}")


def process_folder(input_folder, output_folder, model, max_length, method="auto", video_extensions=[".mp4", ".mov", ".mkv"]):
    """Processes all video files in input_folder (with the specified extensions)
    and writes SRT + ASS files to output_folder.
    """
    if not os.path.exists(output_folder):
        os.makedirs(output_folder)

    video_files = []
    for ext in video_extensions:
        video_files.extend(glob.glob(os.path.join(input_folder, f"*{ext}")))

    if not video_files:
        print("No video files found in the folder.")
        return

    for video_file in video_files:
        process_video(video_file, model, output_folder, max_length, method=method)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(
        description="Generate subtitles for a video or folder of videos using Google Speech API or Whisper."
    )
    parser.add_argument("--input", "-i", required=True,
                        help="input video file or folder")
    parser.add_argument("--output", "-o", required=True,
                        help="output folder for generated SRT files")
    parser.add_argument("--model", default="large",
                        help="Whisper model name (small, base, large, etc.) — used as fallback")
    parser.add_argument("--max-length", type=int, default=22,
                        help="maximum characters per subtitle line")
    parser.add_argument("--method", default="auto",
                        choices=["google", "whisper", "auto"],
                        help="subtitle method: google (API), whisper (local), auto (try google first)")
    args = parser.parse_args()

    print(f"Subtitle method: {args.method}")

    if args.method in ("whisper", "auto"):
        # Only load Whisper if it might be needed
        print("Loading Whisper model… (this may take a while)")
        wh = _get_whisper()
        model = wh.load_model(args.model)
        print("Whisper model loaded.")
    else:
        model = None

    if os.path.isfile(args.input):
        process_video(args.input, args.model, args.output, args.max_length, method=args.method)
    else:
        process_folder(args.input, args.output, args.model, args.max_length, method=args.method)
