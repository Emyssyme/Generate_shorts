"""SQLite persistence: URL cache and editor templates."""
import json
import os
import re
import shutil
import sqlite3

from config import CACHE_DB, DOWNLOADS_DIR, TEMPLATES_DB, TEMPLATE_ASSETS_DIR


def init_cache():
    conn = sqlite3.connect(CACHE_DB)
    c = conn.cursor()
    # old versions may lack skip_unsilence column -- add if necessary
    c.execute("PRAGMA table_info(cache)")
    columns = [row[1] for row in c.fetchall()]
    if 'skip_unsilence' not in columns:
        try:
            c.execute('ALTER TABLE cache ADD COLUMN skip_unsilence INTEGER DEFAULT 0')
        except Exception:
            pass
    if 'skip_cropping' not in columns:
        try:
            c.execute('ALTER TABLE cache ADD COLUMN skip_cropping INTEGER DEFAULT 0')
        except Exception:
            pass
    if 'skip_subtitles' not in columns:
        try:
            c.execute('ALTER TABLE cache ADD COLUMN skip_subtitles INTEGER DEFAULT 0')
        except Exception:
            pass
    c.execute('''
        CREATE TABLE IF NOT EXISTS cache (
            url TEXT,
            start TEXT,
            end TEXT,
            video TEXT,
            srt TEXT,
            skip_unsilence INTEGER DEFAULT 0,
            skip_cropping INTEGER DEFAULT 0,
            skip_subtitles INTEGER DEFAULT 0,
            UNIQUE(url, start, end, skip_unsilence, skip_cropping, skip_subtitles)
        )
    ''')
    conn.commit()
    conn.close()

def init_templates_db():
    """Create templates table for storing editor presets."""
    conn = sqlite3.connect(TEMPLATES_DB)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            config TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()


def sanitize_template_filename(name):
    return re.sub(r'[^A-Za-z0-9_.-]', '_', name)


def copy_overlay_to_template_assets(overlay_name, template_name):
    if not overlay_name:
        return None
    overlay_name = os.path.basename(overlay_name)
    source_paths = [
        os.path.join(DOWNLOADS_DIR, overlay_name),
        os.path.join(TEMPLATE_ASSETS_DIR, overlay_name),
    ]
    source = next((p for p in source_paths if os.path.exists(p)), None)
    if not source:
        return None
    base, ext = os.path.splitext(overlay_name)
    safe_name = sanitize_template_filename(f"{template_name}_{base}{ext}")
    dest = os.path.join(TEMPLATE_ASSETS_DIR, safe_name)
    if os.path.abspath(source) != os.path.abspath(dest):
        shutil.copy2(source, dest)
    return safe_name


def resolve_overlay_path(overlay_name):
    if not overlay_name:
        return None
    overlay_name = os.path.basename(overlay_name)
    candidates = [
        os.path.join(DOWNLOADS_DIR, overlay_name),
        os.path.join(TEMPLATE_ASSETS_DIR, overlay_name),
    ]
    return next((p for p in candidates if os.path.exists(p)), None)


def get_all_templates():
    """Return list of template names."""
    conn = sqlite3.connect(TEMPLATES_DB)
    c = conn.cursor()
    c.execute('SELECT name, created_at FROM templates ORDER BY created_at DESC')
    rows = c.fetchall()
    conn.close()
    return [{'name': r[0], 'created_at': r[1]} for r in rows]

def get_template(name):
    """Return config dict for a named template, or None."""
    conn = sqlite3.connect(TEMPLATES_DB)
    c = conn.cursor()
    c.execute('SELECT config FROM templates WHERE name=?', (name,))
    row = c.fetchone()
    conn.close()
    if row:
        return json.loads(row[0])
    return None

def save_template(name, config):
    """Insert or update a template. Returns True on success."""
    config = dict(config)
    overlay_name = config.get('overlay')
    if overlay_name:
        copied_name = copy_overlay_to_template_assets(overlay_name, name)
        if copied_name:
            config['overlay'] = copied_name
        else:
            config.pop('overlay', None)

    conn = sqlite3.connect(TEMPLATES_DB)
    c = conn.cursor()
    try:
        c.execute('INSERT OR REPLACE INTO templates (name, config, created_at) VALUES (?, ?, CURRENT_TIMESTAMP)',
                  (name, json.dumps(config, ensure_ascii=False)))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        conn.close()
        print(f"save_template error: {e}")
        return False

def delete_template(name):
    """Delete a template by name."""
    cfg = get_template(name)
    overlay_name = cfg.get('overlay') if cfg else None

    conn = sqlite3.connect(TEMPLATES_DB)
    c = conn.cursor()
    c.execute('DELETE FROM templates WHERE name=?', (name,))
    conn.commit()
    conn.close()

    if overlay_name:
        # only remove the saved asset if no other template references it
        conn = sqlite3.connect(TEMPLATES_DB)
        c = conn.cursor()
        c.execute('SELECT config FROM templates')
        rows = c.fetchall()
        conn.close()
        still_used = any(json.loads(row[0]).get('overlay') == overlay_name for row in rows)
        if not still_used:
            asset_path = os.path.join(TEMPLATE_ASSETS_DIR, overlay_name)
            try:
                if os.path.exists(asset_path):
                    os.remove(asset_path)
            except Exception:
                pass

# initialize databases when the module loads
init_cache()
init_templates_db()

def find_cache(url, start, end, skip_unsilence=False, skip_cropping=False, skip_subtitles=False):
    conn = sqlite3.connect(CACHE_DB)
    c = conn.cursor()
    c.execute('SELECT video, srt FROM cache WHERE url=? AND start=? AND end=? AND skip_unsilence=? AND skip_cropping=? AND skip_subtitles=?',
              (url or '', start or '', end or '', int(skip_unsilence), int(skip_cropping), int(skip_subtitles)))
    row = c.fetchone()
    conn.close()
    return row  # either None or (video, srt)

def store_cache(url, start, end, video, srt, skip_unsilence=False, skip_cropping=False, skip_subtitles=False):
    conn = sqlite3.connect(CACHE_DB)
    c = conn.cursor()
    try:
        if video is None or srt is None:
            # remove stale entry for this configuration
            c.execute('DELETE FROM cache WHERE url=? AND start=? AND end=? AND skip_unsilence=? AND skip_cropping=? AND skip_subtitles=?',
                      (url or '', start or '', end or '', int(skip_unsilence), int(skip_cropping), int(skip_subtitles)))
        else:
            c.execute('INSERT OR REPLACE INTO cache (url, start, end, video, srt, skip_unsilence, skip_cropping, skip_subtitles) VALUES (?,?,?,?,?,?,?,?)',
                      (url or '', start or '', end or '', video, srt, int(skip_unsilence), int(skip_cropping), int(skip_subtitles)))
        conn.commit()
    finally:
        conn.close()


def init():
    """Create the SQLite tables if needed."""
    init_cache()
    init_templates_db()
