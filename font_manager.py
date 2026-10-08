"""Fontconfig setup, downloadable font library and font map."""
import os
import requests
import shutil
import subprocess
import sys

from config import BASE_DIR, FONTS_DIR


FONT_MAP = {}
FONTCONFIG_CONF = None


def _build_fontconfig_xml(fonts_dir, cache_dir):
    """Generate a fontconfig XML string that works on Linux, macOS and Windows."""
    dirs = []
    if sys.platform.startswith('win'):
        dirs.append('<dir>WINDOWSFONTDIR</dir>')
        # also scan %LOCALAPPDATA%\Microsoft\Windows\Fonts if it exists
        local_fonts = os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Microsoft', 'Windows', 'Fonts')
        if os.path.isdir(local_fonts):
            dirs.append(f'<dir>{local_fonts}</dir>')
    elif sys.platform == 'darwin':
        dirs.append('<dir>/System/Library/Fonts</dir>')
        dirs.append('<dir>/Library/Fonts</dir>')
        dirs.append('<dir>~/Library/Fonts</dir>')
    else:
        dirs.append('<dir>/usr/share/fonts</dir>')
        dirs.append('<dir>/usr/local/share/fonts</dir>')
        dirs.append('<dir>~/.local/share/fonts</dir>')
        dirs.append('<dir>~/.fonts</dir>')
    # Always include the app's local fonts directory
    dirs.append(f'<dir>{fonts_dir}</dir>')

    return f'''<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "fonts.dtd">
<fontconfig>
    <dir>{fonts_dir}</dir>
    {''.join(dirs)}
    <cachedir>{cache_dir}</cachedir>
    <config>
        <rescan>
            <int>30</int>
        </rescan>
    </config>
</fontconfig>'''


def setup_fontconfig():
    """Create fonts.conf and return its path so libass/ffmpeg
    can find fonts on every platform without warnings."""
    fonts_dir_abs = os.path.abspath(FONTS_DIR).replace('\\', '/')
    cache_dir = os.path.join(os.path.abspath(FONTS_DIR), '.fc-cache')
    os.makedirs(cache_dir, exist_ok=True)

    fc_conf_path = os.path.join(BASE_DIR, 'fonts.conf')

    xml = _build_fontconfig_xml(fonts_dir_abs, cache_dir)
    if sys.platform.startswith('win'):
        windir = os.environ.get('WINDIR', 'C:\\Windows')
        xml = xml.replace('WINDOWSFONTDIR', (windir + '\\Fonts').replace('\\', '/'))

    with open(fc_conf_path, 'w', encoding='utf-8') as f:
        f.write(xml)

    return fc_conf_path


FONT_LIBRARY = {
    'Inter': {
        'filename': 'Inter-Variable.ttf',
        'url': 'https://github.com/google/fonts/raw/main/ofl/inter/Inter%5Bopsz%2Cwght%5D.ttf',
        'format': 'variable',
    },
    'Roboto': {
        'filename': 'Roboto-Variable.ttf',
        'url': 'https://github.com/google/fonts/raw/main/apache/roboto/Roboto%5Bwdth%2Cwght%5D.ttf',
        'format': 'variable',
    },
    'Open Sans': {
        'filename': 'OpenSans-Variable.ttf',
        'url': 'https://github.com/google/fonts/raw/main/ofl/opensans/OpenSans%5Bwdth%2Cwght%5D.ttf',
        'format': 'variable',
    },
    'Montserrat': {
        'filename': 'Montserrat-Variable.ttf',
        'url': 'https://github.com/google/fonts/raw/main/ofl/montserrat/Montserrat%5Bwght%5D.ttf',
        'format': 'variable',
    },
    'Poppins': {
        'filename': 'Poppins-Regular.ttf',
        'url': 'https://github.com/google/fonts/raw/main/ofl/poppins/Poppins-Regular.ttf',
        'format': 'static',
    },
    'Oswald': {
        'filename': 'Oswald-Variable.ttf',
        'url': 'https://github.com/google/fonts/raw/main/ofl/oswald/Oswald%5Bwght%5D.ttf',
        'format': 'variable',
    },
    'Bebas Neue': {
        'filename': 'BebasNeue-Regular.ttf',
        'url': 'https://github.com/google/fonts/raw/main/ofl/bebasneue/BebasNeue-Regular.ttf',
        'format': 'static',
    },
    'Anton': {
        'filename': 'Anton-Regular.ttf',
        'url': 'https://github.com/google/fonts/raw/main/ofl/anton/Anton-Regular.ttf',
        'format': 'static',
    },
    'Lato': {
        'filename': 'Lato-Regular.ttf',
        'url': 'https://github.com/google/fonts/raw/main/ofl/lato/Lato-Regular.ttf',
        'format': 'static',
    },
    'Playfair Display': {
        'filename': 'PlayfairDisplay-Variable.ttf',
        'url': 'https://github.com/google/fonts/raw/main/ofl/playfairdisplay/PlayfairDisplay%5Bwght%5D.ttf',
        'format': 'variable',
    },
    'Raleway': {
        'filename': 'Raleway-Variable.ttf',
        'url': 'https://github.com/google/fonts/raw/main/ofl/raleway/Raleway%5Bwght%5D.ttf',
        'format': 'variable',
    },
    'Nunito': {
        'filename': 'Nunito-Variable.ttf',
        'url': 'https://github.com/google/fonts/raw/main/ofl/nunito/Nunito%5Bwght%5D.ttf',
        'format': 'variable',
    },
    'Ubuntu': {
        'filename': 'Ubuntu-Regular.ttf',
        'url': 'https://github.com/google/fonts/raw/main/ufl/ubuntu/Ubuntu-Regular.ttf',
        'format': 'static',
    },
    'Source Sans 3': {
        'filename': 'SourceSans3-Variable.ttf',
        'url': 'https://github.com/google/fonts/raw/main/ofl/sourcesans3/SourceSans3%5Bwght%5D.ttf',
        'format': 'variable',
    },
    'Caveat': {
        'filename': 'Caveat-Variable.ttf',
        'url': 'https://github.com/google/fonts/raw/main/ofl/caveat/Caveat%5Bwght%5D.ttf',
        'format': 'variable',
    },
    'Dancing Script': {
        'filename': 'DancingScript-Variable.ttf',
        'url': 'https://github.com/google/fonts/raw/main/ofl/dancingscript/DancingScript%5Bwght%5D.ttf',
        'format': 'variable',
    },
    'Rubik': {
        'filename': 'Rubik-Variable.ttf',
        'url': 'https://github.com/google/fonts/raw/main/ofl/rubik/Rubik%5Bwght%5D.ttf',
        'format': 'variable',
    },
    'Arimo': {
        'filename': 'Arimo-Variable.ttf',
        'url': 'https://github.com/google/fonts/raw/main/apache/arimo/Arimo%5Bwght%5D.ttf',
        'format': 'variable',
    },
    'Archivo Black': {
        'filename': 'ArchivoBlack-Regular.ttf',
        'url': 'https://github.com/google/fonts/raw/main/ofl/archivoblack/ArchivoBlack-Regular.ttf',
        'format': 'static',
    },
}

ALLOWED_FONT_EXTENSIONS = {'ttf', 'otf'}

def allowed_font_file(filename):
    ext = os.path.splitext(filename)[1].lower().lstrip('.')
    return ext in ALLOWED_FONT_EXTENSIONS

def download_font_file(dest_path, url):
    try:
        response = requests.get(url, stream=True, timeout=30)
        response.raise_for_status()
        with open(dest_path, 'wb') as out_file:
            shutil.copyfileobj(response.raw, out_file)
        return True
    except Exception as exc:
        print(f"Warning: could not download font from {url}: {exc}")
        return False


def is_variable_font(font_path):
    """Detect whether a .ttf/.otf file is an OpenType variable font.

    Uses fontTools when available (reliable), falls back to a quick
    binary scan for the ``fvar`` table tag and filename heuristics.
    """
    try:
        from fontTools.ttLib import TTFont
        font = TTFont(font_path)
        has_fvar = 'fvar' in font
        font.close()
        if has_fvar:
            return True
    except ImportError:
        pass
    except Exception:
        pass

    # Binary scan: look for 'fvar' among the first 60 table records
    try:
        import struct
        with open(font_path, 'rb') as f:
            header = f.read(12)
            if len(header) < 12:
                return False
            sfVersion, numTables = struct.unpack('>IH', header[0:6])
            if sfVersion in (0x00010000, 0x4F54544F):
                for _ in range(min(numTables, 60)):
                    record = f.read(16)
                    if len(record) < 16:
                        break
                    tag = record[0:4].decode('ascii', errors='ignore')
                    if tag == 'fvar':
                        return True
    except Exception:
        pass

    # Fallback: filename heuristics
    basename = os.path.basename(font_path).lower()
    if any(hint in basename for hint in ('-variable', '[wght', 'variablefont')):
        return True

    return False


# Backward-compatible: ensure Inter is available by default
def ensure_default_fonts():
    """Download the Inter variable font if no fonts exist in the fonts directory."""
    existing = [f for f in os.listdir(FONTS_DIR)
                if f.lower().endswith(('.ttf', '.otf'))] if os.path.isdir(FONTS_DIR) else []
    if not existing:
        for font_name in ('Inter',):
            info = FONT_LIBRARY.get(font_name)
            if info:
                dest_path = os.path.join(FONTS_DIR, info['filename'])
                if not os.path.exists(dest_path):
                    if download_font_file(dest_path, info['url']):
                        print(f"Downloaded default font: {font_name}")
        # Also download old-style Inter for backward compatibility
        old_inter = os.path.join(FONTS_DIR, 'Inter-Regular.ttf')
        if not os.path.exists(old_inter):
            info = FONT_LIBRARY['Inter']
            download_font_file(old_inter, info['url'])


def download_font_by_name(font_name):
    """Download a font from the library by its display name. Returns True on success."""
    info = FONT_LIBRARY.get(font_name)
    if not info:
        return False
    dest_path = os.path.join(FONTS_DIR, info['filename'])
    if os.path.exists(dest_path):
        return True  # already downloaded
    return download_font_file(dest_path, info['url'])


def _add_common_linux_fonts(font_map):
    """Add common Linux Inter font paths to *font_map*."""
    candidates = [
        ('Inter', '/usr/share/fonts/truetype/inter/Inter-Regular.ttf'),
        ('Inter Bold', '/usr/share/fonts/truetype/inter/Inter-Bold.ttf'),
        ('Inter', '/usr/share/fonts/truetype/Inter/Inter-Regular.ttf'),
        ('Inter Bold', '/usr/share/fonts/truetype/Inter/Inter-Bold.ttf'),
        ('Inter', '/usr/share/fonts/truetype/ttf-inter/Inter-Regular.ttf'),
        ('Inter Bold', '/usr/share/fonts/truetype/ttf-inter/Inter-Bold.ttf'),
        ('Inter', os.path.expanduser('~/.local/share/fonts/Inter-Regular.ttf')),
        ('Inter Bold', os.path.expanduser('~/.local/share/fonts/Inter-Bold.ttf')),
    ]
    for name, path in candidates:
        if os.path.exists(path):
            font_map.setdefault(name, path)


def _get_true_font_name(path, fallback):
    """Attempt to extract the real font family name from the file."""
    if sys.platform.startswith('linux'):
        try:
            r = subprocess.run(['fc-scan', '--format', '%{family}\\n', path], capture_output=True, text=True, timeout=2)
            if r.stdout.strip():
                return r.stdout.strip().split(',')[0]
        except Exception:
            pass
    try:
        from fontTools.ttLib import TTFont
        font = TTFont(path, fontNumber=0) if path.lower().endswith('.ttc') else TTFont(path)
        name_record = font['name'].getDebugName(1)
        if name_record:
            return name_record
    except Exception:
        pass
    return fallback


def build_font_map():
    """Build a {display_name: filesystem_path} dict for all available fonts.

    Scans the local ``fonts/`` directory first.  For variable fonts the
    single file is exposed under its canonical name.  Falls back to
    system fonts when the local directory is empty.
    """
    font_map = {}

    if os.path.isdir(FONTS_DIR):
        for fname in os.listdir(FONTS_DIR):
            if not fname.lower().endswith(('.ttf', '.otf')):
                continue
            path = os.path.join(FONTS_DIR, fname)
            name = os.path.splitext(fname)[0]

            # ── map well‑known names to their canonical display name ──
            canonical = {
                'Inter-Regular': 'Inter', 'Inter': 'Inter',
                'Inter-Bold': 'Inter Bold', 'InterBold': 'Inter Bold',
                'Inter-Variable': 'Inter',
            }
            if name in canonical:
                font_map.setdefault(canonical[name], path)
                continue

            # ── detect font‑library entries by filename ──
            matched = False
            for lib_name, info in FONT_LIBRARY.items():
                if info['filename'] == fname:
                    font_map[lib_name] = path
                    matched = True
                    break
            if matched:
                continue

            # ── fallback: use the true font name or filename stem ──
            font_map[_get_true_font_name(path, name)] = path

    # ── merge system fonts ─────────────────────────────────────────
    _add_system_fonts(font_map)
    return font_map


def _add_system_fonts(font_map):
    """Add platform system fonts to *font_map* (does not overwrite existing keys)."""
    if sys.platform.startswith('win'):
        candidates = {
            'Arial':           'C:/Windows/Fonts/arial.ttf',
            'Arial Bold':      'C:/Windows/Fonts/arialbd.ttf',
            'Impact':          'C:/Windows/Fonts/impact.ttf',
            'Georgia':         'C:/Windows/Fonts/georgia.ttf',
            'Verdana':         'C:/Windows/Fonts/verdana.ttf',
            'Courier New':     'C:/Windows/Fonts/cour.ttf',
            'Times New Roman': 'C:/Windows/Fonts/times.ttf',
            'Trebuchet MS':    'C:/Windows/Fonts/trebuc.ttf',
            'Calibri':         'C:/Windows/Fonts/calibri.ttf',
            'Segoe UI':        'C:/Windows/Fonts/segoeui.ttf',
            'Comic Sans MS':   'C:/Windows/Fonts/comic.ttf',
        }
    else:
        candidates = {
            'DejaVu Sans':      '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
            'DejaVu Sans Bold': '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
            'Liberation Sans':  '/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf',
            'Liberation Sans Bold': '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf',
            'FreeSerif':        '/usr/share/fonts/truetype/freefont/FreeSerif.ttf',
            'FreeSans':         '/usr/share/fonts/truetype/freefont/FreeSans.ttf',
        }
        _add_common_linux_fonts(candidates)

    for name, path in candidates.items():
        if os.path.exists(path):
            font_map.setdefault(name, path)


def refresh_font_map():
    global FONT_MAP
    FONT_MAP = build_font_map()




def init():
    """Set up fontconfig, download default fonts and build the font map."""
    global FONTCONFIG_CONF
    FONTCONFIG_CONF = setup_fontconfig()
    print(f"Fontconfig set up: FONTCONFIG_PATH={BASE_DIR}")
    ensure_default_fonts()
    refresh_font_map()
