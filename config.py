"""Central configuration: paths, environment variables, Flask config."""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# download directory used by all operations; always placed next to this file
DOWNLOADS_DIR = os.path.join(BASE_DIR, "downloads")
# fonts directory: drop .ttf/.otf files here and the editor will expose them
FONTS_DIR = os.path.join(BASE_DIR, "fonts")
TEMPLATE_ASSETS_DIR = os.path.join(BASE_DIR, "template_assets")

CACHE_DB = os.path.join(BASE_DIR, "cache.db")
TEMPLATES_DB = os.path.join(BASE_DIR, "templates.db")
JOBS_FILE = os.path.join(BASE_DIR, "jobs.json")

SECRET_KEY = os.getenv("SECRET_KEY", "dev_key")
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "password")

# Token for the REST API (/api/v1). If empty the API answers 503.
API_TOKEN = os.getenv("API_TOKEN", "")

for _d in (DOWNLOADS_DIR, FONTS_DIR, TEMPLATE_ASSETS_DIR):
    os.makedirs(_d, exist_ok=True)


class Config:
    SCHEDULER_API_ENABLED = True
    SCHEDULER_TIMEZONE = "Europe/Bucharest"
    SCHEDULER_EXECUTORS = {'default': {'type': 'threadpool', 'max_workers': 5}}
    SCHEDULER_JOB_DEFAULTS = {'coalesce': False, 'max_instances': 3}
