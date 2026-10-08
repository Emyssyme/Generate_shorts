"""Colour helpers and SRT/title -> ASS conversion."""
import re


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


def srt_to_ass(srt_content: str, ass_path: str):
    """Convert SRT content to an ASS file with per-word karaoke highlighting.

    Word timing is computed **proportionally** within each SRT entry so
    the rendered video matches the canvas preview exactly.  Each entry
    is split into words, and each word gets its own Dialogue event with
    ``##HLBG##`` / ``##HLFG##`` / ``##BORD##`` placeholders that are
    replaced at render time with the user's chosen colours.

    This function is called whenever the user edits the SRT by hand.
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

    def _to_sec(ts):
        """Convert HH:MM:SS.mmm or HH:MM:SS,mmm to seconds."""
        ts = ts.replace(',', '.')
        h, m, s = ts.split(':')
        return int(h) * 3600 + int(m) * 60 + float(s)

    def _ass_ts(sec):
        """Convert seconds to ASS timestamp H:MM:SS.cc."""
        total = int(sec)
        h, rem = divmod(total, 3600)
        m, s = divmod(rem, 60)
        cs = int((sec - total) * 100)
        return f"{h}:{m:02d}:{s:02d}.{cs:02d}"

    blocks = re.split(r"\n\s*\n", srt_content.strip())
    lines_out = [header]

    for block in blocks:
        block = block.strip()
        if not block:
            continue
        parts = block.split("\n")
        if len(parts) < 3:
            continue
        ts_match = re.match(
            r"(\d{2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,.]\d{3})",
            parts[1])
        if not ts_match:
            continue
        seg_start = _to_sec(ts_match.group(1))
        seg_end = _to_sec(ts_match.group(2))
        seg_duration = seg_end - seg_start
        if seg_duration <= 0:
            continue

        # Join all text lines, split into words
        full_text = " ".join(parts[2:])
        full_text = re.sub(r"\s+", " ", full_text).strip()
        if not full_text:
            continue
        words = full_text.split()
        word_count = len(words)
        word_duration = seg_duration / word_count

        # Generate one Dialogue event per word with proportional timing
        for wi, current_word in enumerate(words):
            ev_start = seg_start + wi * word_duration
            ev_end = seg_start + (wi + 1) * word_duration
            if ev_end - ev_start < 0.02:
                continue

            # Build the line with highlight on the current word
            word_parts = []
            for wj, w_text in enumerate(words):
                if wj == wi:
                    word_parts.append(
                        f"{{\\3c&H##HLBG##&\\bord##BORD##\\1c&H##HLFG##&}}"
                        f"{w_text}"
                        f"{{\\r}}"
                    )
                else:
                    word_parts.append(w_text)

            line_text = " ".join(word_parts)
            lines_out.append(
                f"Dialogue: 0,{_ass_ts(ev_start)},{_ass_ts(ev_end)},"
                f"Default,,0,0,0,,{line_text}")

    content = "\n".join(lines_out) + "\n"
    with open(ass_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)


def title_to_ass(title_text, ass_path, font_name, font_size, color, stroke_color,
                 stroke_width, bold, letter_spacing, title_x=10, title_y=80,
                 line_spacing=0, video_width=1080, video_height=1920):
    """Generate an ASS file for a static title with adjustable line-spacing.

    Each non-empty line gets its **own** Dialogue event with an explicit
    ``\\pos`` tag — no ``\\N`` newlines, no invisible spacer characters.
    Line height = font_size × 1.2 + line_spacing, matching the canvas preview.
    """
    non_empty = [l for l in title_text.split('\n') if l]
    if not non_empty:
        with open(ass_path, 'w', encoding='utf-8', newline='\n') as f:
            f.write('')
        return

    ass_color = html_to_ass_color(color)
    ass_stroke = html_to_ass_color(stroke_color)
    ass_bold_flag = 1 if bold else 0

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {video_width}
PlayResY: {video_height}
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: TitleStyle,{font_name},{font_size},{ass_color},&H00FFFFFF,{ass_stroke},&H00000000,{ass_bold_flag},0,0,0,100,100,{letter_spacing},0,1,{stroke_width},0,7,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    # One Dialogue per line — no invisible characters, no \N, just pure \pos
    lh = font_size * 1.2 + line_spacing
    dialogues = []
    for i, line in enumerate(non_empty):
        y = int(title_y + i * lh)
        dialogues.append(
            f"Dialogue: 0,0:00:00.00,99:59:59.99,TitleStyle,,0,0,0,,"
            f"{{\\an7}}{{\\pos({title_x},{y})}}{line}"
        )

    content = header + "\n".join(dialogues) + "\n"
    with open(ass_path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(content)
