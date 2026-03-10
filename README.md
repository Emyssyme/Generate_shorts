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
   ```
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

Enjoy!  
