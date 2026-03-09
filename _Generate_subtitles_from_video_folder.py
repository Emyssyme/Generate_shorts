import os
import glob
import whisper
from moviepy import VideoFileClip

def extract_audio(video_path, audio_path):
    """
    Extracts audio from the video file and saves it as a WAV file.
    """
    video = VideoFileClip(video_path)
    video.audio.write_audiofile(audio_path, logger=None)
    return video.duration

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

def subdivide_segment(segment, max_length):
    """
    Given a transcription segment (with keys "start", "end", "text"),
    subdivides it into one or more subsegments so that each subsegment's text
    has at most max_length characters. The duration is split proportionally.
    """
    text = segment["text"].strip().replace("\n", " ")
    if len(text) <= max_length:
        return [segment]
    
    segments_text = split_text_into_segments(text, max_length)
    total_chars = sum(len(s) for s in segments_text)
    duration = segment["end"] - segment["start"]
    sub_segments = []
    current_start = segment["start"]
    
    for sub_text in segments_text:
        proportion = len(sub_text) / total_chars
        sub_duration = duration * proportion
        sub_segments.append({
            "start": current_start,
            "end": current_start + sub_duration,
            "text": sub_text
        })
        current_start += sub_duration
    return sub_segments

def generate_srt(segments, srt_path, max_length):
    """
    Subdivides the transcript segments as needed and writes an SRT file.
    """
    all_segments = []
    for seg in segments:
        subs = subdivide_segment(seg, max_length)
        all_segments.extend(subs)
    
    with open(srt_path, "w", encoding="utf-8") as f:
        for i, seg in enumerate(all_segments):
            start_ts = format_timestamp(seg["start"])
            end_ts = format_timestamp(seg["end"])
            f.write(f"{i+1}\n")
            f.write(f"{start_ts} --> {end_ts}\n")
            f.write(seg["text"] + "\n\n")
    print(f"Subtitle saved to: {srt_path}")

def process_video(video_path, model, output_folder, max_length):
    """
    Processes one video:
        - Extracts its audio,
        - Uses Whisper to transcribe Romanian speech,
        - Generates an SRT file named <video_basename>_romanian.srt in the output folder.
    """
    base_name = os.path.basename(video_path)
    name, _ = os.path.splitext(base_name)
    srt_filename = f"{name}.srt"
    srt_path = os.path.join(output_folder, srt_filename)
    audio_path = os.path.join(output_folder, f"{name}_temp_audio.wav")
    
    print(f"Processing video: {video_path}")
    extract_audio(video_path, audio_path)
    
    # Transcribe the audio using Whisper (with timing) in Romanian.
    result = model.transcribe(audio_path, language="ro", task="transcribe")
    segments = result.get("segments", [])
    if not segments:
        print(f"No segments were produced for {video_path}.")
        os.remove(audio_path)
        return
    
    generate_srt(segments, srt_path, max_length)
    os.remove(audio_path)

def process_folder(input_folder, output_folder, model, max_length, video_extensions=[".mp4", ".mov", ".mkv"]):
    """
    Processes all video files in input_folder (with the specified extensions)
    and writes SRT files to output_folder.
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
