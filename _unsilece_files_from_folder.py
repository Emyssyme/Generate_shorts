import os
import subprocess
import re
import sys
# ensure stdout uses utf-8 to avoid CP1250 errors on Windows consoles
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
# Importăm efectele audio specifice pentru noua sintaxă
from moviepy import VideoFileClip, concatenate_videoclips
from moviepy.audio.fx import AudioFadeIn, AudioFadeOut, AudioNormalize

folder_path = "C:\\Users\\Emil\\Videos\\Secvente\\"

def get_dynamic_silence_level(video_file, offset=-12):
    cmd = f'ffmpeg -i "{video_file}" -af volumedetect -f null -'
    result = subprocess.run(cmd, capture_output=True, shell=True, text=True, encoding='utf-8')
    mean_volume_match = re.search(r"mean_volume: ([\-\d\.]+) dB", result.stderr)
    return str(round(float(mean_volume_match.group(1)) + offset)) if mean_volume_match else "-30"

def remove_silence(input_video, export_video, silence_timestamps, padding=0.15):
    with VideoFileClip(input_video) as video:
        clips = []
        last_ts = 0
        duration = video.duration

        for start, end in silence_timestamps:
            end_point = min(start + padding, duration)
            
            if end_point > last_ts:
                segment = video.subclipped(last_ts, end_point)
                
                # --- MODIFICARE AICI: Folosim with_effects cu clasele importate ---
                segment = segment.with_effects([
                    AudioFadeIn(0.05),
                    AudioFadeOut(0.05)
                ])
                
                clips.append(segment)
            
            last_ts = max(0, end - padding)

        if last_ts < duration:
            last_segment = video.subclipped(last_ts, duration)
            last_segment = last_segment.with_effects([
                AudioFadeIn(0.05),
                AudioFadeOut(0.05)
            ])
            clips.append(last_segment)

        if not clips:
            print(f"Nicio tăietură necesară pentru {input_video}")
            return

        final_clip = concatenate_videoclips(clips, method="compose")
        
        # --- Normalizare Audio la final ---
        if final_clip.audio is not None:
            final_clip = final_clip.with_effects([AudioNormalize()])

        final_clip.write_videofile(
            export_video, 
            codec='libx264', 
            audio_codec="aac", 
            temp_audiofile="temp-audio.m4a",
            remove_temp=True,
            fps=video.fps,
            threads=4
        )
        
def get_video_files(folder_path):
    video_extensions = [".mp4", ".avi", ".mkv", ".mov", ".wmv"]
    video_files = []
    for root, _, files in os.walk(folder_path):
        for file in files:
            # Evităm procesarea fișierelor deja modificate
            if any(file.lower().endswith(ext) for ext in video_extensions) and "_Altered" not in file:
                video_files.append(os.path.join(root, file))
    return video_files

def process_file(input_video, output_video, padding=0.15):
    """Perform silence removal on a single file and save to output_video.

    This utility replicates the behaviour that was previously executed in the
    top‑level loop.  It detects the silence threshold dynamically, runs
    ffmpeg to find the silence segments, and then calls ``remove_silence``.
    """
    print(f"Processing {input_video} -> {output_video}")
    # 1. Detecție automată nivel dB
    silence_db = get_dynamic_silence_level(input_video)
    # print without special characters to avoid encoding issues (simplify diacritics)
    print(f"Nivel de liniste setat la: {silence_db}dB (adaptiv)")

    # 2. Detecție segmente FFmpeg
    cmd = f'ffmpeg -i "{input_video}" -af silencedetect=n={silence_db}dB:d=0.4 -f null -'
    result = subprocess.run(cmd, capture_output=True, shell=True, text=True, encoding='utf-8')

    # Extragem timpii folosind regex
    starts = re.findall(r"silence_start: ([\d\.]+)", result.stderr)
    ends = re.findall(r"silence_end: ([\d\.]+)", result.stderr)
    silence_timestamps = list(zip(map(float, starts), map(float, ends)))

    # 3. Prelucrare și export
    remove_silence(input_video, output_video, silence_timestamps, padding=padding)


# --- Execuție CLI ---
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Remove silence from a video file")
    parser.add_argument("input", help="input video file path")
    parser.add_argument("output", help="output video file path")
    parser.add_argument("--padding", type=float, default=0.15,
                        help="padding seconds around cuts")
    args = parser.parse_args()

    process_file(args.input, args.output, padding=args.padding)