"""Generate Shorts web app: Flask application factory and entrypoint.

Module layout
-------------
config.py         paths and environment variables
font_manager.py   fontconfig setup, font library, FONT_MAP
ffmpeg_utils.py   ffmpeg env, ffprobe helpers
ass_utils.py      colour helpers, SRT/title -> ASS
gpu.py            hardware encoder detection, ffmpeg capability probes
db.py             SQLite cache + editor templates
jobs.py           job state, naming, cancel/delete
pipeline.py       download/cut -> unsilence -> crop -> subtitles, submit_job()
auth.py           session login (web UI)
routes_ui.py      web UI routes
routes_editor.py  editor route
api_v1.py         REST API (/api/v1, token auth)
"""
import os
import sys

from flask import Flask
from flask_socketio import SocketIO

import api_v1
import auth
import db
import font_manager
import gpu
import routes_editor
import routes_ui
from config import BASE_DIR, DOWNLOADS_DIR, SECRET_KEY, Config


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config())
    app.secret_key = SECRET_KEY

    font_manager.init()
    db.init()

    auth.login_manager.init_app(app)
    auth.register(app)
    routes_ui.register(app)
    routes_editor.register(app)
    app.register_blueprint(api_v1.bp)
    return app


app = create_app()
socketio = SocketIO(app, cors_allowed_origins="*")


def main():
    print("=" * 70)
    print("Generate Shorts - Video Processing App")
    print("=" * 70)
    print(f"Python: {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
    print(f"GPU Available: {gpu.GPU_AVAILABLE}")
    gpu_encoder = gpu.select_auto_gpu_encoder()
    if gpu_encoder:
        print(f"FFmpeg GPU Encoder: {gpu_encoder} "
              f"(NVIDIA={gpu._NVIDIA_SMI_OK}, IntelQSV={gpu._INTEL_QSV_OK})")
    else:
        print("No GPU encoder available; using CPU for rendering")
    gemini_keys = [k for k in ('GOOGLE_API_KEY_1', 'GOOGLE_API_KEY_2') if os.getenv(k)]
    if gemini_keys:
        print(f"Gemini API: {len(gemini_keys)} env key(s) configured -> cloud subtitling enabled")
    else:
        print("Gemini API: no env keys set -> checking hardcoded fallback in _generate_subtitles.py")
    print(f"REST API: {'enabled (/api/v1)' if os.getenv('API_TOKEN') else 'disabled (set API_TOKEN)'}")
    print(f"Base Directory: {BASE_DIR}")
    print(f"Downloads: {DOWNLOADS_DIR}")
    print("=" * 70)
    port = int(os.getenv('PORT', 5015))
    debug = os.getenv('FLASK_DEBUG', '1') == '1'
    print(f"Starting Flask on http://0.0.0.0:{port} (debug={debug})")
    sys.stdout.flush()
    socketio.run(app, host='0.0.0.0', port=port, debug=debug, use_reloader=False)


if __name__ == '__main__':
    main()
