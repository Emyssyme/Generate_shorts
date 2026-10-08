# Generate Shorts Web App

This Flask-based application provides a web interface for:

- Cutting a segment from a YouTube link or local video file
- Removing silence from the clip
- Cropping the face vertically
- Generating subtitles
- Editing the subtitles and optionally overlaying images

## Running the app

1. **Install dependencies** (use a virtual environment):
   ```sh
   pip install -r requirements.txt  # or install Flask, flask-login, flask-socketio, etc.
   ```
2. **Set optional environment variables**:
   ```sh
   export PORT=8000         # port to listen on (default 5000)
   export ADMIN_USER=admin  # login user
   export ADMIN_PASS=pass   # login password
   export SECRET_KEY=...    # flask secret key
   export API_TOKEN=...     # enables the REST API (/api/v1); API is disabled if unset

   # ── Gemini API keys (optional, for cloud subtitling via Gemini models) ──
   export GOOGLE_API_KEY_1=your_api_key_here
   export GOOGLE_API_KEY_2=your_second_api_key_here  # (fallback on quota)
   ```
   When Gemini API keys are configured (or the hardcoded fallback keys are set
   in ``_generate_subtitles.py``), subtitles are generated via Gemini models
   (gemini‑2.0‑flash by default) — no local GPU needed, ideal for low‑power
   machines like the HP Mini G3.  If all keys are exhausted or absent, the app
   falls back to local Whisper.
3. **Start the server**:
   ```sh
   python app.py
   ```
4. Open a browser and navigate to `http://localhost:5000` (or the port you configured).

The app will redirect to the login page, then the video cut interface.

## Notes

- The app uses `yt-dlp`, `ffmpeg` and several helper scripts found in the same directory (or parent directory).
- Downloads and job state are stored in a `downloads` directory located in the same folder as `app.py` regardless of the current working directory.  (Older runs may have created a `downloads` elsewhere; you can move those files into the project folder if you want to keep them.)
- A lightweight SQLite cache remembers previous YouTube URL + start/end combinations and will instantly return cached outputs instead of re‑downloading/processing the same segment again.  The cache file `cache.db` is created next to `app.py`.  When a lookup occurs the app checks that both the cached video and subtitle files still exist; if either has been deleted the cache entry is cleared and normal processing resumes.
- A small log pane in the UI shows progress messages; you can monitor status updates as each step completes.  (The subtitle helper and unsilence script now stream their console output here.)
- You can choose the X/Y offset when applying an overlay image from the editor screen.
- Input validation has been added: you must supply either a YouTube URL or upload a file, and start/end times are respected.
- If the web page appears to hang on "downloading video", check the logs or the server console – yt-dlp may still be running and large
  segments can take a long time to fetch.  In some cases yt-dlp has to download more data than the requested slice, there is
  unfortunately no way around that in the general case; the tool tries to use ``--download-sections`` but will fall back to full
  download followed by an ffmpeg trim.
- The application exposes a simple HTML interface; no additional frontend build is required.

## GPU / Hardware Acceleration

The app auto‑detects available hardware and uses the fastest encoder:

| Hardware             | Encoder      | Detection method                |
|----------------------|-------------|---------------------------------|
| NVIDIA GPU           | `h264_nvenc` / `hevc_nvenc` | `nvidia-smi`         |
| **Intel Quick Sync** | `h264_qsv` / `hevc_qsv` | `wmic` (Win) / `/dev/dri/renderD128` (Linux) / ffmpeg encoder list |
| None (CPU fallback)  | `libx264`    | —                               |

Intel Quick Sync (QSV) is ideal for low‑power machines like the HP Mini G3
because the integrated GPU handles encoding without taxing the CPU.

## Subtitle Method

The app supports three subtitle generation modes (configurable per job):

| Method     | Description                                                    |
|------------|----------------------------------------------------------------|
| **Gemini** | Cloud Gemini API (gemini‑2.0‑flash+) — fast, no local GPU needed|
| **Whisper**| Local Whisper model — needs GPU for good performance           |
| **Auto**   | Tries Gemini API first; falls back to Whisper if unavailable   |

### Gemini API key configuration (priority order)

1. **Environment variables** (highest priority):
   ```cmd
   set GOOGLE_API_KEY_1=your_key
   set GOOGLE_API_KEY_2=your_backup_key
   ```
2. **Hardcoded fallback** (edit in `_generate_subtitles.py`):
   ```python
   _HARDCODED_GEMINI_KEY_1 = "your_key_here"
   _HARDCODED_GEMINI_KEY_2 = "your_backup_key_here"
   ```
   These are used automatically when no environment variables are set.
   The app rotates through all four possible keys on quota errors.
  

## Project structure

| File | Responsibility |
|------|----------------|
| `app.py` | Flask application factory (`create_app`) and entrypoint |
| `config.py` | Paths and environment variables |
| `font_manager.py` | Fontconfig setup, font library, `FONT_MAP` |
| `ffmpeg_utils.py` | `ffmpeg_env`, ffprobe helpers |
| `ass_utils.py` | Colour helpers, SRT / title → ASS |
| `gpu.py` | Hardware encoder detection, ffmpeg capability probes |
| `db.py` | SQLite URL cache and editor templates |
| `jobs.py` | Job state (`jobs.json`), project naming, cancel / delete |
| `pipeline.py` | cut → unsilence → crop → subtitles, and `submit_job()` |
| `auth.py` | Session login for the web UI |
| `routes_ui.py`, `routes_editor.py` | Web UI routes |
| `api_v1.py` | REST API |

The web form and the REST API both go through `pipeline.submit_job()`, so
validation, caching and project naming behave identically.

## Project names

Every project gets a name `<source name>_YYYY-MM-DD_HH-MM-SS`: the uploaded
file name, the YouTube video title, or the optional `name` given to the API.
Downloaded files (video / `.srt` / `.ass`) use this name.

## REST API (`/api/v1`)

Set `API_TOKEN` and send it as `Authorization: Bearer <token>` (or `X-API-Key`).

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/v1/projects` | Create a project (JSON for a URL, multipart for a file) |
| `GET` | `/api/v1/projects` | List projects (`?status=completed&limit=20`) |
| `GET` | `/api/v1/projects/<id>` | Status, name, error, links (`?log=50` for log lines) |
| `POST` | `/api/v1/projects/<id>/cancel` | Cancel a running project (409 if finished) |
| `DELETE` | `/api/v1/projects/<id>` | Delete project and files |
| `GET` | `/api/v1/projects/<id>/files/<video\|srt\|ass>` | Download (`?inline=1` to stream) |

Create fields: `url` or `file`, `name`, `start_time`, `end_time`
(seconds, `MM:SS` or `HH:MM:SS`), `skip_unsilence`, `skip_cropping`,
`skip_subtitles`, `subtitle_method` (`auto` | `google` | `whisper`),
`gemini_api_key`. Creation returns `202` (or `200` with `"cached": true`).

```sh
# from a YouTube link
curl -X POST http://localhost:5015/api/v1/projects \
  -H "Authorization: Bearer $API_TOKEN" -H "Content-Type: application/json" \
  -d '{"url": "https://youtu.be/VIDEO_ID", "start_time": "0:10", "end_time": "0:50"}'

# from a local file
curl -X POST http://localhost:5015/api/v1/projects \
  -H "Authorization: Bearer $API_TOKEN" \
  -F "file=@clip.mp4" -F "skip_cropping=true"

# poll, then download
curl -H "Authorization: Bearer $API_TOKEN" http://localhost:5015/api/v1/projects/<id>
curl -OJ -H "Authorization: Bearer $API_TOKEN" http://localhost:5015/api/v1/projects/<id>/files/video
```

Errors are JSON: `{"error": "..."}` with status 400 / 401 / 404 / 409 / 503.

Enjoy!

