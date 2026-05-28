import os
import glob
import re
import whisper
from moviepy import VideoFileClip

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

    Instead of relying on ASS karaoke tags, we generate individual Dialogue
    events for each word transition.  The highlighted word gets a coloured
    rectangle behind it via ``\\3c`` (outline-as-background) + ``\\bord``
    (large border), with ``\\1c`` setting the foreground text colour.
    Non-highlighted words use the style's default PrimaryColour.

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

    for seg_idx, seg in enumerate(segments):
        words_data = seg.get("words", [])
        text = seg.get("text", "").strip()

        # If no word-level timestamps, fall back to a single event
        if not words_data:
            start = format_ass_timestamp(seg["start"])
            end = format_ass_timestamp(seg["end"])
            clean_text = _clean_text(text)
            lines.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{clean_text}")
            continue

        seg_start = seg["start"]
        seg_end = seg["end"]

        # Collect words with their start times
        word_entries = []
        for w in words_data:
            wt = w.get("word", "").strip()
            if not wt:
                continue
            ws = w.get("start", seg_start)
            word_entries.append((wt, ws))

        if not word_entries:
            start = format_ass_timestamp(seg_start)
            end = format_ass_timestamp(seg_end)
            lines.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{_clean_text(text)}")
            continue

        # For each word position (0..n-1), create an event where that word
        # is highlighted.  The event starts when the word begins and ends
        # when the next word begins (or at segment end for the last word).
        for wi, (word_text, w_start) in enumerate(word_entries):
            # Determine when this highlight ends
            if wi < len(word_entries) - 1:
                highlight_end = word_entries[wi + 1][1]  # next word's start
            else:
                highlight_end = seg_end

            # Build the full line with background-highlight on the current word.
            # The highlighted word gets a coloured rectangle behind it using
            # \\3c (outline colour) + \\bord (large border) = filled background,
            # plus \\1c to set the text colour on top of the background.
            # Placeholders (replaced at render time by app.py):
            #   ##HLBG## → highlight background colour (e.g. yellow)
            #   ##HLFG## → text colour on highlight background (e.g. black)
            #   ##BORD## → border/background thickness in pixels
            parts = []
            for wj, (w_text, _) in enumerate(word_entries):
                if wj == wi:
                    parts.append(
                        f"{{\\3c&H##HLBG##&\\bord##BORD##\\1c&H##HLFG##&}}"
                        f"{w_text}"
                        f"{{\\r}}"
                    )
                else:
                    parts.append(w_text)

            full_text = " ".join(parts)

            start_ts = format_ass_timestamp(w_start)
            end_ts = format_ass_timestamp(min(highlight_end, seg_end))

            # Small gap between consecutive events to avoid flicker
            if highlight_end - w_start < 0.02:
                continue

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

def process_video(video_path, model, output_folder, max_length):
    """
    Processes one video:
        - Extracts its audio,
        - Uses Whisper to transcribe Romanian speech (with word-level timestamps),
        - Generates an SRT file and an ASS file with karaoke word highlighting.
    """
    base_name = os.path.basename(video_path)
    name, _ = os.path.splitext(base_name)
    srt_filename = f"{name}.srt"
    ass_filename = f"{name}.ass"
    srt_path = os.path.join(output_folder, srt_filename)
    ass_path = os.path.join(output_folder, ass_filename)
    audio_path = os.path.join(output_folder, f"{name}_temp_audio.wav")
    
    print(f"Processing video: {video_path}")
    extract_audio(video_path, audio_path)
    
    # Transcribe the audio using Whisper (with word-level timestamps) in Romanian.
    result = model.transcribe(audio_path, language="ro", task="transcribe",
                              word_timestamps=True)
    segments = result.get("segments", [])
    if not segments:
        print(f"No segments were produced for {video_path}.")
        os.remove(audio_path)
        return
    
    # Generate both SRT (standard) and ASS (karaoke word-highlight)
    # Pre-subdivide segments for consistency between both outputs
    subdivided = subdivide_segments_with_words(segments, max_length)
    generate_srt(subdivided, srt_path, max_length)
    generate_ass_karaoke(subdivided, ass_path, max_length)
    os.remove(audio_path)

def process_folder(input_folder, output_folder, model, max_length, video_extensions=[".mp4", ".mov", ".mkv"]):
    """
    Processes all video files in input_folder (with the specified extensions)
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
        process_video(video_file, model, output_folder, max_length)

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(
        description="Generate subtitles for a video or folder of videos using Whisper."
    )
    parser.add_argument("--input", "-i", required=True,
                        help="input video file or folder")
    parser.add_argument("--output", "-o", required=True,
                        help="output folder for generated SRT files")
    parser.add_argument("--model", default="large",
                        help="Whisper model name (small, base, large, etc.)")
    parser.add_argument("--max-length", type=int, default=22,
                        help="maximum characters per subtitle line")
    args = parser.parse_args()

    print("Loading Whisper model... (this may take a while)")
    model = whisper.load_model(args.model)

    if os.path.isfile(args.input):
        process_video(args.input, model, args.output, args.max_length)
    else:
        process_folder(args.input, args.output, model, args.max_length)
