"""
Spotify -> Discord Rich Presence (via Spicetify lyrics-bridge extension)
"""

import asyncio, os, json, time, struct, threading, queue, datetime, base64
import socket as _socket
import subprocess
import configparser, winreg
import tkinter as tk
import tkinter.font as tkfont
import tkinter.colorchooser as tkcolor
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as _FutureTimeout
from io import BytesIO
import ctypes, ctypes.wintypes
import sys

# ── Where the app's files live ────────────────────────────────────
# Two different directories, and conflating them breaks the frozen build.
#
# _APP_DIR is for things the USER owns and we must not lose: statusify.cfg,
# .env, history.json, statusify.log, .artcache, exports. In a PyInstaller
# one-file build __file__ points inside sys._MEIPASS — a temp folder that is
# deleted the moment the process exits — so resolving config from __file__
# would silently discard every setting and the whole listening history on each
# run. Frozen builds therefore anchor user data to the .exe's own folder.
#
# _RES_DIR is for read-only files we SHIP (lyrics-bridge.js, statusify.ico).
# Those really do live in _MEIPASS when frozen, because that is where
# PyInstaller unpacks bundled data. Running from source the two are the same
# folder, which is why one variable sufficed until now.
_FROZEN  = bool(getattr(sys, "frozen", False))
_APP_DIR = (os.path.dirname(os.path.abspath(sys.executable)) if _FROZEN
            else os.path.dirname(os.path.abspath(__file__)))
_RES_DIR = (getattr(sys, "_MEIPASS", _APP_DIR) if _FROZEN
            else os.path.dirname(os.path.abspath(__file__)))

# Pure lyric helpers live in their own module so they can be unit-tested
# without importing tkinter/winreg (see tests/test_lyrics.py). Imported up
# here rather than halfway down the file because _track_offset_ms — defined
# long before the old import site — now depends on resolve_offset_ms.
from statusify_lyrics import (join_lines, select_line, resolve_offset_ms,
                              offset_key, _calc_instrumental_gaps)

try:
    import keyboard as _keyboard
    KEYBOARD_AVAILABLE = True
except ImportError:
    KEYBOARD_AVAILABLE = False

def _pip_install(packages):
    """Install `packages` with the running interpreter's pip. Returns True on success.

    Kept above the third-party imports on purpose. _ensure_dependencies() runs
    from __main__, which is far too late for anything imported at module scope:
    a missing `websockets` or `python-dotenv` raised ImportError here, before
    the installer could ever run. Under pythonw.exe that produced a process
    that died instantly with no window, no console and no log — the auto-
    installer's two most important packages were the two it could never fix.

    No-op in a frozen build. sys.executable is Statusify.exe there, not a
    Python interpreter, so `sys.executable -m pip install …` would not install
    anything — it would launch a second copy of the GUI with nonsense
    arguments. Every dependency is bundled into the exe at build time, so
    there is nothing legitimate for this to do."""
    if _FROZEN:
        return False
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", *packages],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return True
    except Exception:
        return False


try:
    from dotenv import load_dotenv
    import websockets
except ImportError:
    if not _pip_install(["python-dotenv", "websockets"]):
        try:
            ctypes.windll.user32.MessageBoxW(
                None,
                "Statusify could not install its required libraries.\n\n"
                "Run this in a terminal, then start Statusify again:\n"
                "    pip install python-dotenv websockets",
                "Statusify — missing dependencies", 0x10)
        except Exception:
            pass
        raise
    from dotenv import load_dotenv
    import websockets

try:
    from PIL import Image, ImageDraw, ImageTk
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

# System tray support (#11). Optional: the app degrades to window-only mode
# if pystray isn't installed rather than refusing to start.
try:
    import pystray
    TRAY_AVAILABLE = PIL_AVAILABLE
except ImportError:
    TRAY_AVAILABLE = False


# ── Logging sink ──────────────────────────────────────────────────
# Defined up here (rather than alongside the executors further down) because
# config loading, the crash handlers, and single-instance checks all run
# during module import and need to be able to log. Previously they ran before
# `log` existed, so any failure in that window raised NameError instead of
# reporting the actual problem.
log_queue = queue.Queue()

# Every log line also goes to disk. The queue above only feeds the in-app log
# panel, which means it lives and dies with the process: once the window is
# closed — or when the app runs under pythonw from the tray, which is the
# normal case — there is no record of what happened at all. Diagnosing "the
# presence stopped working an hour ago" was therefore impossible after the
# fact, and the bridge's own diagnostics (which arrive as "[Bridge] ..."
# lines) were the first thing lost.
_LOG_FILE     = os.path.join(_APP_DIR, "statusify.log")
_LOG_MAX_B    = 512 * 1024      # rotate at 512 KB; one .old kept
_log_fh       = None
_log_fh_lock  = threading.Lock()

def _log_to_disk(line):
    """Append one line to statusify.log, rotating when it gets large.

    Deliberately best-effort: logging must never take the app down, so every
    failure here (read-only folder, file locked by an editor, disk full) is
    swallowed. The in-app panel still works regardless.
    """
    global _log_fh
    try:
        with _log_fh_lock:
            if _log_fh is None:
                if (os.path.exists(_LOG_FILE)
                        and os.path.getsize(_LOG_FILE) > _LOG_MAX_B):
                    old = _LOG_FILE + ".old"
                    try:
                        if os.path.exists(old):
                            os.remove(old)
                        os.replace(_LOG_FILE, old)
                    except OSError:
                        pass
                _log_fh = open(_LOG_FILE, "a", encoding="utf-8", errors="replace")
            _log_fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {line}\n")
            _log_fh.flush()
    except Exception:
        pass

def log(msg):
    log_queue.put(msg)
    _log_to_disk(msg)

# ── App icon (base64-encoded .ico, embedded so no external file needed) ───
import tempfile, atexit as _atexit
from _icon_data import _ICON_B64   # ~145 KB base64 .ico, kept out of this file

def _write_icon():
    """Write the .ico to a temp file and return its path."""
    data = base64.b64decode(_ICON_B64)
    tf   = tempfile.NamedTemporaryFile(suffix=".ico", delete=False)
    tf.write(data); tf.close()
    _atexit.register(lambda p=tf.name: __import__("os").unlink(p) if __import__("os").path.exists(p) else None)
    return tf.name

_ICON_PATH = None  # set on first use

def _ensure_icon_path():
    """Return a filesystem path to the app icon, writing it out once."""
    global _ICON_PATH
    if _ICON_PATH is None or not os.path.exists(_ICON_PATH):
        on_disk = os.path.join(_RES_DIR, "statusify.ico")
        _ICON_PATH = on_disk if os.path.exists(on_disk) else _write_icon()
    return _ICON_PATH

# Set by _install_bridge() when the bridge injected into Spotify's xpui bundle
# differs from the one we ship — i.e. "Spotify is running an old bridge and
# `spicetify apply` has not been run since". See App._check_bridge_version.
_BRIDGE_UPDATED = False

# ── Rotating file writer ──────────────────────────────────────────
# Every append-only file this app writes previously grew without bound.
# health.csv reached 17 GB before anyone noticed. Route ALL diagnostic
# writes through here so a cap is the default rather than something each
# call site has to remember.
_ROTATE_LOCK = threading.Lock()

def _rotating_write(path, text, max_bytes=1_000_000, keep=1):
    """Append `text` to `path`, rotating to `path.1` once it exceeds max_bytes.

    `keep` is how many rotated generations to retain (0 = just truncate).
    Never raises — diagnostics must not be able to take the app down."""
    try:
        with _ROTATE_LOCK:
            try:
                if os.path.exists(path) and os.path.getsize(path) > max_bytes:
                    if keep > 0:
                        old = path + ".1"
                        try:
                            if os.path.exists(old):
                                os.remove(old)
                        except OSError:
                            pass
                        try:
                            os.replace(path, old)
                        except OSError:
                            open(path, "w").close()
                    else:
                        open(path, "w").close()
            except OSError:
                pass
            with open(path, "a", encoding="utf-8", errors="replace") as f:
                f.write(text)
    except Exception:
        pass

# ── Crash handling ────────────────────────────────────────────────
# The app runs under pythonw.exe, which has no console: anything written to
# stderr goes nowhere, which is why stderr.log and error.txt were both 0
# bytes despite repeated crashes. These hooks capture every unhandled
# exception — main thread, worker threads, and Tk callbacks — to crash.log.
_CRASH_LOG = os.path.join(_APP_DIR, "crash.log")

def _record_crash(kind, exc_type, exc_value, exc_tb):
    import traceback
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    body = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    _rotating_write(
        _CRASH_LOG,
        f"\n{'='*72}\n[{stamp}] {kind} — Statusify v{_VERSION}\n{'='*72}\n{body}",
        max_bytes=2_000_000,
    )
    try:
        log(f"❌ {kind}: {exc_type.__name__}: {exc_value}")
    except Exception:
        pass

def _install_crash_handlers():
    """Install excepthooks for the main thread, worker threads, and Tk."""
    def _main_hook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        _record_crash("UNHANDLED EXCEPTION", exc_type, exc_value, exc_tb)
    sys.excepthook = _main_hook

    # threading.excepthook exists on 3.8+; worker-thread crashes were
    # previously invisible (a daemon thread dying silently is exactly how
    # the backend used to disappear while the GUI kept drawing).
    if hasattr(threading, "excepthook"):
        def _thread_hook(args):
            if issubclass(args.exc_type, SystemExit):
                return
            _record_crash(f"THREAD CRASH ({args.thread.name if args.thread else '?'})",
                          args.exc_type, args.exc_value, args.exc_traceback)
        threading.excepthook = _thread_hook

    # Tk swallows callback exceptions by printing to stderr — invisible here.
    # Route them to crash.log too; a raising callback usually means the UI is
    # now in a half-updated state, which is worth knowing about.
    def _tk_hook(self, exc_type, exc_value, exc_tb):
        _record_crash("TK CALLBACK", exc_type, exc_value, exc_tb)
    tk.Tk.report_callback_exception = _tk_hook

# Point dotenv at an explicit path rather than letting it search. Bare
# load_dotenv() walks up from the current working directory, which is whatever
# the shell or the Startup shortcut happened to leave it as — fine when you
# launch `python main.py` from the app folder, unreliable for a double-clicked
# .exe or an autostart entry.
load_dotenv(os.path.join(_APP_DIR, ".env"))

# Version lives in version.py so the README, the runtime and the CI check in
# .github/workflows/version-sync.yml can never drift apart.
from version import VERSION as _VERSION
_GITHUB_REPO  = "KurepaBoss/Statusify"  # GitHub repo for update checks

DISCORD_APP_ID    = os.getenv("DISCORD_APP_ID", "")
WS_HOST           = "127.0.0.1"
WS_PORT           = 8765

# ── Single-instance enforcement ───────────────────────────────────
# Statusify binds a local WebSocket for the Spicetify bridge. Two instances
# cannot share the port. Previously, a bind failure (orphaned previous
# instance still holding the port) raised OSError inside the daemon backend
# thread and KILLED IT SILENTLY — the Tk window kept drawing but no track
# updates ever arrived ("unresponsive"), and re-launches hit the same bind
# failure ("can't be opened again"). These helpers detect & resolve that.
def _is_port_in_use(port, host=WS_HOST):
    """True if something is already bound to (host, port)."""
    s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        # bind() succeeds → port is free; fails → something holds it.
        s.bind((host, port))
        return False
    except OSError:
        return True
    finally:
        try: s.close()
        except Exception: pass

# Only processes whose image name is in this set may be force-killed by the
# orphan cleanup below. Port 8765 is a very common local dev port; the
# original code killed WHATEVER was listening on it, so running Statusify
# could silently taskkill /F an unrelated dev server, a Node app, or a
# database console. Identity is now verified before any kill.
_KILLABLE_IMAGES = {"python.exe", "pythonw.exe", "statusify.exe"}

def _process_image_name(pid):
    """Return the lowercase image name for `pid`, or None if unknown."""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {int(pid)}", "/NH", "/FO", "CSV"],
            capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout.strip()
    except Exception:
        return None
    # CSV row looks like: "pythonw.exe","1234","Console","1","52,000 K"
    if not out or not out.startswith('"'):
        return None
    try:
        return out.split('","')[0].lstrip('"').strip().lower()
    except Exception:
        return None

def _kill_orphan_instance(port=WS_PORT):
    """If another *Statusify* process is holding our WS port, kill it.

    Uses netstat to find the PID, verifies via tasklist that the PID really
    belongs to a Python/Statusify image, then taskkills it. Returns True if
    the port is free after the call. Never kills the current process, and
    never kills a process it cannot positively identify."""
    own_pid = os.getpid()
    try:
        # netstat -ano shows PID in the last column for each connection.
        out = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception as e:
        log(f"orphan-check: netstat failed: {e}")
        return False

    target = f":{port}"
    pids = set()
    for line in out.splitlines():
        if "LISTENING" not in line:
            continue
        parts = line.split()
        # parts[1] is "127.0.0.1:8765"; parts[-1] is the PID
        if len(parts) >= 4 and parts[1].endswith(target):
            try: pids.add(int(parts[-1]))
            except ValueError: pass

    # Safety: never kill ourselves. If the only holder is our own PID, the
    # port is legitimately ours (or a race) — nothing to kill.
    pids.discard(own_pid)
    if not pids:
        return not _is_port_in_use(port)

    for pid in pids:
        image = _process_image_name(pid)
        if image is None:
            log(f"orphan-check: PID {pid} holds port {port} but could not be "
                f"identified — refusing to kill it")
            continue
        if image not in _KILLABLE_IMAGES:
            log(f"orphan-check: port {port} is held by '{image}' (PID {pid}), "
                f"which is not Statusify — refusing to kill it")
            continue
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, timeout=5,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            log(f"orphan-check: killed {image} PID {pid} holding port {port}")
        except Exception as e:
            log(f"orphan-check: could not kill PID {pid}: {e}")
    # Give the OS a moment to actually release the socket.
    time.sleep(0.5)
    return not _is_port_in_use(port)

# ── Single-instance mutex ─────────────────────────────────────────
# A named kernel mutex is the standard Windows idiom for "only one copy of
# this app". It is instant, race-free, and released automatically by the OS
# when the process dies — no netstat parsing, no force-killing, and no
# dependence on whether a socket happened to linger in TIME_WAIT. The port
# check above stays as a second line of defence for the case where a
# genuinely orphaned Statusify still holds the socket.
_MUTEX_NAME   = "Global\\Statusify_SingleInstance_v1"
_MUTEX_HANDLE = None
_ERROR_ALREADY_EXISTS = 183

def _acquire_single_instance():
    """True if we are the only instance; False if another one already holds it."""
    global _MUTEX_HANDLE
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.restype  = ctypes.wintypes.HANDLE
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        handle = kernel32.CreateMutexW(None, False, _MUTEX_NAME)
        if not handle:
            return True  # can't tell — fail open rather than refuse to start
        if kernel32.GetLastError() == _ERROR_ALREADY_EXISTS:
            # Don't close the handle: closing our reference is harmless, but
            # keeping the code simple matters more than one leaked handle in
            # a process that is about to exit.
            return False
        _MUTEX_HANDLE = handle
        return True
    except Exception:
        return True  # fail open — never block startup on the guard itself

# ── "Show the running instance" handshake ─────────────────────────
# When a second copy is launched, the user is asking to see the app. The
# second process drops this sentinel file and exits; the running instance
# polls for it once a second and restores its window. A file is used rather
# than the WebSocket port because that socket belongs to the Spicetify bridge
# and ws_handler treats any connection as Spicetify — a control connection
# there would clobber the live bridge reference.
_SHOW_FLAG = os.path.join(_APP_DIR, ".show-request")

def _request_show():
    """Ask the already-running instance to bring its window to the front.

    Waits briefly to see whether the request is picked up. If the flag is
    still sitting there, the running copy is either an older build that
    doesn't watch for it or is genuinely wedged — say so rather than exiting
    silently and looking like nothing happened at all."""
    try:
        with open(_SHOW_FLAG, "w", encoding="utf-8") as f:
            f.write(str(time.time()))
    except OSError:
        return

    for _ in range(30):                     # up to ~3 s
        time.sleep(0.1)
        if not os.path.exists(_SHOW_FLAG):
            return                          # picked up — done, exit quietly

    try:
        os.remove(_SHOW_FLAG)
    except OSError:
        pass
    try:
        ctypes.windll.user32.MessageBoxW(
            None,
            "Statusify is already running but isn't responding.\n\n"
            "Right-click its system-tray icon and choose Quit, then "
            "start it again.",
            "Statusify", 0x30)
    except Exception:
        pass

RATE_LIMIT_CALLS  = 5
RATE_LIMIT_WINDOW = 20.0
MAX_STATE         = 128
LYRIC_DELAY_MS    = 0       # user-adjustable lyric timing offset (ms)
_ENV_PATH         = os.path.join(_APP_DIR, ".env")

# ── Persistent config ─────────────────────────────────────────────
_CONFIG_PATH = os.path.join(_APP_DIR, "statusify.cfg")
_HIST_FILE   = os.path.join(_APP_DIR, "history.json")

def _load_config():
    """Return the cached ConfigParser, reading from disk only once.

    The original version re-read and re-parsed statusify.cfg from disk on
    EVERY _cfg_get call (10 call sites) and rewrote the whole file on every
    _cfg_set (13 call sites). Building the settings page alone cost a dozen
    synchronous disk round-trips on the Tk thread. The file is small and this
    process is its only writer, so one in-memory copy is authoritative."""
    global _CFG_CACHE
    with _CFG_LOCK:
        if _CFG_CACHE is None:
            cfg = configparser.ConfigParser()
            try:
                cfg.read(_CONFIG_PATH)
            except (OSError, configparser.Error) as e:
                # A corrupt config must not prevent startup — fall back to
                # defaults and say so, rather than dying before the GUI exists.
                log(f"Config unreadable ({e}) — using defaults")
                cfg = configparser.ConfigParser()
            _CFG_CACHE = cfg
        return _CFG_CACHE

_CFG_CACHE = None
_CFG_LOCK  = threading.RLock()

def _save_config(cfg=None):
    """Write the cached config to disk atomically."""
    with _CFG_LOCK:
        cfg = cfg if cfg is not None else _load_config()
        tmp = _CONFIG_PATH + ".tmp"
        try:
            # Write-then-replace: a crash mid-write can no longer leave a
            # truncated statusify.cfg behind.
            with open(tmp, "w", encoding="utf-8") as f:
                cfg.write(f)
            os.replace(tmp, _CONFIG_PATH)
        except (OSError, configparser.Error) as e:
            # configparser.Error was NOT caught here before. Python 3.13+
            # raises InvalidWriteError for an option name containing a
            # delimiter, which the per-track offsets used to produce (see
            # offset_key). It escaped as an unhandled Tk callback exception
            # and, because the bad key stayed in the cached ConfigParser, made
            # every subsequent save fail too — the app quietly stopped
            # persisting ANY setting. Diagnostics and config writes must never
            # be able to take the app down.
            log(f"Could not save config: {type(e).__name__}: {e}")
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass

def _cfg_get(section, key, fallback=""):
    with _CFG_LOCK:
        return _load_config().get(section, key, fallback=fallback)

def _cfg_set(section, key, value):
    with _CFG_LOCK:
        cfg = _load_config()
        if not cfg.has_section(section):
            cfg.add_section(section)
        cfg.set(section, key, str(value))
        _save_config(cfg)

# Load persisted settings at startup
_stored_delay = _cfg_get("preferences", "lyric_delay_ms", "0")
try: LYRIC_DELAY_MS = int(_stored_delay)
except (TypeError, ValueError): LYRIC_DELAY_MS = 0

ACCENT     = _cfg_get("preferences", "accent_color",  "#1db954") or "#1db954"
_DARK_MODE = (_cfg_get("preferences", "dark_mode", "true").lower() == "true")

INSTRUMENTAL_TEXT = _cfg_get("preferences", "instrumental_text", "🎵 ─ ─ ─ ─ ─ ─ ─ ─ ─ 🎵") or "🎵 ─ ─ ─ ─ ─ ─ ─ ─ ─ 🎵"
SHOW_PAUSED_RPC   = (_cfg_get("preferences", "show_paused_rpc", "false").lower() == "true")
SAVE_HISTORY      = (_cfg_get("preferences", "save_history", "true").lower() == "true")
# Closing the window hides to tray instead of quitting (#12).
#
# Defaults to OFF. It was briefly ON, and that was the wrong call: ✕ silently
# leaving a process running is surprising, and it made the app look like it
# had wedged — you close it, it appears gone, and the next launch refuses to
# start. The minimise button and the tray menu both still hide to tray for
# people who want the background behaviour, so nothing is lost by making the
# close button do the obvious thing.
CLOSE_TO_TRAY     = (_cfg_get("preferences", "close_to_tray", "false").lower() == "true")
# Keep the window above other windows — useful when watching lyrics next to a
# game or a browser, which is most of the time for an app like this.
ALWAYS_ON_TOP     = (_cfg_get("preferences", "always_on_top", "false").lower() == "true")
# Launch straight to the tray. Statusify can start with Windows, and an
# autostart app that steals focus and a window on every boot is a nuisance.
START_MINIMIZED   = (_cfg_get("preferences", "start_minimized", "false").lower() == "true")
# Eased transitions (tab underline, hover fades, progress bar). Motion is a
# genuine accessibility concern and this is also the one switch that makes
# the UI cheap on a very weak machine, so it is user-controllable rather
# than compiled in. Off degrades to the old instant snap, never to breakage.
ANIMATIONS_ENABLED = (_cfg_get("preferences", "animations", "true").lower() == "true")
# Point-size bump applied to the big "now on Discord" lyric line only.
try:
    LYRIC_FONT_BOOST = int(_cfg_get("preferences", "lyric_font_boost", "0"))
except (TypeError, ValueError):
    LYRIC_FONT_BOOST = 0
LYRIC_FONT_BOOST = max(-2, min(10, LYRIC_FONT_BOOST))
# Cap on in-memory (and thus UI-rendered) history rows. Prevents unbounded
# growth over a long session — each entry holds full lyrics.
MAX_HISTORY_ROWS  = 500
# Cap on how many history rows exist as actual Tk widgets at once. Data for
# all MAX_HISTORY_ROWS entries is still kept; this only bounds the widget
# tree, which is what actually costs time on every layout pass.
MAX_RENDERED_ROWS = 60

# ── Album art cache ───────────────────────────────────────────────
# Art was refetched over the network on every single track change with no
# caching at all, so replaying an album re-downloaded the same JPEG each
# time. Keyed by (url, size) because the hero image and the history
# thumbnails want different resolutions of the same source.
_ART_CACHE      = {}
_ART_CACHE_LOCK = threading.Lock()
_ART_CACHE_MAX  = 80
_ART_DISK_DIR   = os.path.join(_APP_DIR, ".artcache")

_ART_DISK_MAX_FILES = 400   # ~2 PNGs per track, so roughly 200 albums

def _art_disk_path(url, size):
    import hashlib
    h = hashlib.sha1(f"{url}@{size}".encode("utf-8")).hexdigest()
    return os.path.join(_ART_DISK_DIR, f"{h}.png")

def _prune_art_cache(max_files=_ART_DISK_MAX_FILES):
    """Drop the least-recently-modified PNGs from the on-disk art cache.

    The in-memory cache has had a size cap from the start, but its disk tier
    only ever grew: every distinct album at every distinct size wrote a PNG
    that nothing ever removed. Called once at startup, off the Tk thread."""
    try:
        entries = []
        with os.scandir(_ART_DISK_DIR) as it:
            for de in it:
                if de.is_file() and de.name.endswith(".png"):
                    try:
                        entries.append((de.stat().st_mtime, de.path))
                    except OSError:
                        pass
        if len(entries) <= max_files:
            return
        entries.sort()
        for _, path in entries[: len(entries) - max_files]:
            try:
                os.remove(path)
            except OSError:
                pass
        log(f"Art cache pruned  ·  {len(entries) - max_files} file(s) removed")
    except (FileNotFoundError, OSError):
        pass

def _round_image(img, radius, bg):
    """Return `img` with rounded corners, composited onto solid colour `bg`.

    Applied at display time rather than inside _fetch_art on purpose: the
    disk cache holds the raw square artwork, so the corner radius and the
    surface colour behind it stay free to change (theme switch, different
    panel) without invalidating a single cached PNG.

    Composites onto an opaque background instead of returning RGBA because
    Tk's PhotoImage does not alpha-blend against a Canvas — a transparent
    corner renders as black, which is precisely the artefact this is meant
    to avoid. The caller passes whatever colour sits behind the art."""
    if img is None or not PIL_AVAILABLE:
        return img
    try:
        img = img.convert("RGB")
        w, h = img.size
        radius = max(0, min(int(radius), min(w, h) // 2))
        if radius == 0:
            return img
        # Build the mask at 4× and downsample: PIL's rounded_rectangle is
        # hard-edged, and an un-antialiased 10 px corner on a 120 px image is
        # visibly staircased.
        scale = 4
        mask = Image.new("L", (w * scale, h * scale), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, w * scale - 1, h * scale - 1),
            radius=radius * scale, fill=255)
        mask = mask.resize((w, h), Image.LANCZOS)
        out = Image.new("RGB", (w, h), bg)
        out.paste(img, (0, 0), mask)
        return out
    except Exception:
        return img   # never let decoration break the image path

def _fetch_art(url, size):
    """Return a PIL image of `url` resized to size×size, or None.

    Three tiers: in-memory dict → on-disk PNG cache → network. Always called
    from a worker thread, never the Tk loop."""
    if not PIL_AVAILABLE or not url:
        return None
    key = (url, size)
    with _ART_CACHE_LOCK:
        hit = _ART_CACHE.get(key)
    if hit is not None:
        return hit

    img = None
    disk = _art_disk_path(url, size)
    try:
        if os.path.exists(disk):
            img = Image.open(disk)
            img.load()   # force decode now, while we're off the Tk thread
    except Exception:
        img = None

    if img is None:
        try:
            import urllib.request
            data = urllib.request.urlopen(url, timeout=4).read()
            img  = Image.open(BytesIO(data)).convert("RGB").resize((size, size), Image.LANCZOS)
        except Exception:
            return None
        try:
            os.makedirs(_ART_DISK_DIR, exist_ok=True)
            img.save(disk, "PNG")
        except Exception:
            pass  # disk cache is an optimisation, not a requirement

    with _ART_CACHE_LOCK:
        if len(_ART_CACHE) >= _ART_CACHE_MAX:
            # Cheap FIFO eviction — good enough for a cache this small, and
            # avoids pulling in an LRU dependency.
            for k in list(_ART_CACHE)[: _ART_CACHE_MAX // 4]:
                _ART_CACHE.pop(k, None)
        _ART_CACHE[key] = img
    return img

# ── Per-track lyric offset (#13) ──────────────────────────────────
# LYRIC_DELAY_MS is a single global, but sync drift is a property of the
# individual track and its lyric source, not of the app. One global value is
# always a compromise across a library. Offsets are stored per track_uri in
# the [offsets] config section and fall back to the global when absent.
def _migrate_offset_keys():
    """Drop [offsets] entries written before offset_key existed.

    Those rows parse as the single option "spotify" holding a mangled value;
    they can never match a real track, and leaving them in place means the
    section keeps failing to round-trip. Removing them is lossless — the data
    was already unrecoverable."""
    with _CFG_LOCK:
        cfg = _load_config()
        if not cfg.has_section("offsets"):
            return
        bad = [k for k in cfg.options("offsets") if ":" in k or k == "spotify"]
        if not bad:
            return
        for k in bad:
            cfg.remove_option("offsets", k)
        _save_config(cfg)
        log(f"Cleared {len(bad)} unreadable per-track offset(s) from statusify.cfg")

_OFFSET_CACHE      = {}   # track_uri -> resolved offset in ms
_OFFSET_CACHE_LOCK = threading.Lock()

def _invalidate_offset_cache():
    """Drop the memoised offsets. Call whenever an offset source changes."""
    with _OFFSET_CACHE_LOCK:
        _OFFSET_CACHE.clear()

def _track_offset_ms(uri=None):
    """Effective lyric offset for `uri`, falling back to the global delay.

    Memoised: this is now consulted by every lyric-selection helper on every
    20 Hz RPC tick, and re-parsing the config (under its lock) several times
    per tick to read a value that only changes when the user edits it is pure
    overhead. _invalidate_offset_cache() is called from both writers."""
    uri = uri if uri is not None else getattr(state, "track_uri", "")
    if not uri:
        return LYRIC_DELAY_MS
    with _OFFSET_CACHE_LOCK:
        hit = _OFFSET_CACHE.get(uri)
    if hit is not None:
        return hit
    val = resolve_offset_ms(_cfg_get("offsets", offset_key(uri), ""), LYRIC_DELAY_MS)
    with _OFFSET_CACHE_LOCK:
        _OFFSET_CACHE[uri] = val
    return val

def _set_track_offset_ms(uri, ms):
    """Persist a per-track offset. Passing None clears it back to global."""
    if not uri:
        return
    _invalidate_offset_cache()
    key = offset_key(uri)
    with _CFG_LOCK:
        cfg = _load_config()
        if not cfg.has_section("offsets"):
            cfg.add_section("offsets")
        if ms is None:
            cfg.remove_option("offsets", key)
        else:
            cfg.set("offsets", key, str(int(ms)))
        _save_config(cfg)

# ── Blacklist (#16) ───────────────────────────────────────────────
# Some tracks or artists shouldn't be broadcast to Discord. Stored as a
# newline-separated list of case-insensitive substrings matched against
# "artist" and "title".
def _load_blacklist():
    raw = _cfg_get("preferences", "blacklist", "")
    return [ln.strip().lower() for ln in raw.replace("\\n", "\n").splitlines() if ln.strip()]

_BLACKLIST = _load_blacklist()

def _is_blacklisted(artist, title):
    if not _BLACKLIST:
        return False
    hay = f"{artist} {title}".lower()
    return any(term in hay for term in _BLACKLIST)

# ── Dropped-line counter (#15) ────────────────────────────────────
# The rate limiter silently discards lyric lines and only mentions it in the
# log. Surfacing a per-song count tells the user whether their grouping and
# delay settings are actually keeping up.
_dropped_lines = 0

# ── Session stats ─────────────────────────────────────────────────
_session_songs        = 0
_session_start        = time.monotonic()
_session_listen_secs  = 0.0   # accumulated while playing
_track_start_mono     = None   # monotonic time we started the current track

def _on_track_start():
    global _session_songs, _track_start_mono
    _session_songs += 1
    _track_start_mono = time.monotonic()
    event_queue.put(("stats",))

def _on_track_pause():
    global _session_listen_secs, _track_start_mono
    if _track_start_mono is not None:
        _session_listen_secs += time.monotonic() - _track_start_mono
        _track_start_mono = None
    event_queue.put(("stats",))

def _on_track_resume():
    global _track_start_mono
    _track_start_mono = time.monotonic()
    event_queue.put(("stats",))

def _get_listen_time():
    """Return total listening seconds including current running track."""
    total = _session_listen_secs
    if _track_start_mono is not None:
        total += time.monotonic() - _track_start_mono
    return total

# ── Hotkey helpers ────────────────────────────────────────────────
_hotkey_skip_combo        = _cfg_get("preferences", "hotkey_skip",        "ctrl+alt+n") or "ctrl+alt+n"
_hotkey_toggle_combo      = _cfg_get("preferences", "hotkey_toggle",      "ctrl+alt+s") or "ctrl+alt+s"
_hotkey_skip_instr_combo  = _cfg_get("preferences", "hotkey_skip_instr",  "ctrl+alt+i") or "ctrl+alt+i"
_hotkey_registered = False
_rpc_enabled          = True   # toggle state

def _register_hotkeys(app_ref):
    global _hotkey_registered
    if not KEYBOARD_AVAILABLE or _hotkey_registered:
        return
    try:
        skip_combo   = _hotkey_skip_combo.strip()
        toggle_combo = _hotkey_toggle_combo.strip()
        skip_instr_combo = _hotkey_skip_instr_combo.strip()
        if skip_combo:
            _keyboard.add_hotkey(skip_combo,       lambda: _hotkey_skip(app_ref),       suppress=False)
        if toggle_combo:
            _keyboard.add_hotkey(toggle_combo,     lambda: _hotkey_toggle(app_ref),     suppress=False)
        if skip_instr_combo:
            _keyboard.add_hotkey(skip_instr_combo, lambda: _hotkey_skip_instrumental(), suppress=False)
        _hotkey_registered = True
        log(f"Hotkeys registered  ·  skip={skip_combo or 'none'}  toggle={toggle_combo or 'none'}  skip_instr={skip_instr_combo or 'none'}")
    except Exception as e:
        log(f"Hotkey registration failed: {e}")

def _send_skip():
    """Send skip_track to Spicetify extension over the existing WebSocket."""
    ws = _spicetify_ws
    if ws is None:
        return
    import asyncio, json
    msg = json.dumps({"type": "skip_track"})
    try:
        # Schedule the coroutine on the backend event loop from this thread
        fut = asyncio.run_coroutine_threadsafe(ws.send(msg), _backend_loop)
        fut.result(timeout=1.0)
    except Exception:
        pass

def _send_skip_instrumental():
    """Send skip_instrumental to Spicetify extension — it will seek to the next lyric line."""
    ws = _spicetify_ws
    if ws is None:
        return
    import asyncio, json
    msg = json.dumps({"type": "skip_instrumental"})
    try:
        fut = asyncio.run_coroutine_threadsafe(ws.send(msg), _backend_loop)
        fut.result(timeout=1.0)
    except Exception:
        pass

def _hotkey_skip(app_ref):
    """Send next-track command via Spicetify WebSocket (posts to event queue)."""
    event_queue.put(("hotkey_skip",))

def _hotkey_skip_instrumental():
    """Send skip_instrumental command to Spicetify extension via WebSocket."""
    _send_skip_instrumental()
    log("Skip instrumental → seeking to next lyric")

def _hotkey_toggle(app_ref):
    global _rpc_enabled
    _rpc_enabled = not _rpc_enabled
    event_queue.put(("hotkey_toggle", _rpc_enabled))
    # Deliberately does NOT touch state.is_playing. Faking a pause here was
    # meant to make rpc_loop clear the presence, but the loop checked
    # _rpc_enabled first and never reached the clear — so all it achieved was
    # corrupting playback state: the next position ping flipped is_playing back
    # to True, which _on_track_resume read as a fresh resume and silently
    # discarded the current track's accumulated listening time. rpc_loop now
    # clears the presence itself on the falling edge of _rpc_enabled.

# ── Startup with Windows ──────────────────────────────────────────

def _startup_lnk_path():
    """Path to the Statusify shortcut in the user's Startup folder."""
    startup = os.path.join(os.environ.get("APPDATA", ""),
                           r"Microsoft\Windows\Start Menu\Programs\Startup",
                           "Statusify.lnk")
    return startup

def _get_startup_enabled():
    return os.path.exists(_startup_lnk_path())

def _cleanup_old_startup():
    """Remove any leftover registry Run key entries from previous versions."""
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Run",
                             0, winreg.KEY_SET_VALUE)
        try: winreg.DeleteValue(key, "Statusify")
        except FileNotFoundError: pass
        winreg.CloseKey(key)
    except Exception:
        pass

def _set_startup_enabled(enabled: bool):
    lnk = _startup_lnk_path()
    # Always clean up old registry entry regardless of enable/disable
    _cleanup_old_startup()
    if not enabled:
        try: os.remove(lnk)
        except FileNotFoundError: pass
        except Exception as e: log(f"Startup remove error: {e}")
        return
    try:
        script_dir = _APP_DIR
        ico        = os.path.join(_RES_DIR, "statusify.ico")
        # Use Windows Script Host COM to create a proper .lnk shortcut.
        # Shortcuts show their Description as the name in Task Manager's
        # startup tab.
        statusify_exe = os.path.join(script_dir, "Statusify.exe")
        main_py       = os.path.join(script_dir, "main.py")

        if _FROZEN:
            # We ARE the executable. Point the shortcut at ourselves instead of
            # guessing at a sibling Statusify.exe or a pythonw that the user may
            # not even have — a frozen build is the one case where the target is
            # known exactly. The exe carries its own icon, so use it for that too.
            target = sys.executable
            args   = ""
            ico    = sys.executable
        # Prefer Statusify.exe (compiled launcher — shows correct name+icon in Task Manager)
        # Fall back to pythonw.exe if not yet built
        elif os.path.exists(statusify_exe):
            target = statusify_exe
            args   = ""
        else:
            pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
            if not os.path.exists(pythonw):
                pythonw = sys.executable.replace("python.exe", "pythonw.exe")
            target = pythonw
            args   = f'"{main_py}"'

        # args is "" for the Statusify.exe path. The old template wrapped it
        # unconditionally, producing Arguments = '""' — a literal empty-string
        # argument handed to the launcher on every boot.
        ps = (
            f'$ws = New-Object -ComObject WScript.Shell; '
            f'$lnk = $ws.CreateShortcut("{lnk}"); '
            f'$lnk.TargetPath = "{target}"; '
            f'$lnk.Arguments = \'{args}\'; '
            f'$lnk.WorkingDirectory = "{script_dir}"; '
            f'$lnk.Description = "Statusify"; '
            f'$lnk.IconLocation = "{ico},0"; '
            f'$lnk.WindowStyle = 7; '
            f'$lnk.Save()'
        )
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0  # SW_HIDE
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", ps],
            capture_output=True, timeout=10,
            startupinfo=si,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        log("Startup shortcut created")
    except Exception as e:
        log(f"Startup shortcut error: {e}")

executor        = ThreadPoolExecutor(max_workers=1)
image_executor  = ThreadPoolExecutor(max_workers=2)   # off-thread album-art fetches
_recv_executor  = ThreadPoolExecutor(max_workers=2)   # bounded Discord pipe reads


def _teardown_and_exit(code=0):
    """Exit the process, guaranteeing it actually exits.

    Quitting used to be `self._root.destroy(); sys.exit(0)`, which is not
    enough. Since Python 3.9 ThreadPoolExecutor worker threads are NON-daemon
    and are joined by an interpreter-exit hook, so a single worker blocked in
    a syscall wedges shutdown forever. _recv_executor is exactly that: its
    workers sit in a blocking read() on the Discord named pipe, which returns
    only when Discord sends something or the handle closes.

    The observed failure: closing Statusify left pythonw.exe alive with no
    window. That zombie still held the single-instance mutex and port 8765,
    so the next launch refused to start ("Statusify is already running") and
    the only way back in was to end the task manually.

    Order matters. Close the pipe first — that is what unblocks a stuck
    reader — then abandon the pools without waiting, then leave via os._exit,
    which does not join anything. Everything that must be persisted (history,
    geometry, config) is already written by the caller before we get here.
    """
    # 1. Unblock any worker stuck reading the Discord pipe.
    try:
        rpc = _ACTIVE_RPC.get("rpc")
        if rpc is not None:
            rpc._connected = False
            if rpc.pipe:
                rpc.pipe.close()
    except Exception:
        pass

    # 2. Abandon the pools. wait=False so we never block on a stuck worker;
    #    cancel_futures so queued work is dropped rather than started.
    for pool in (executor, image_executor, _recv_executor):
        try:
            pool.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass

    # 3. Flush the log file by hand — os._exit skips atexit handlers.
    try:
        with _log_fh_lock:
            if _log_fh is not None:
                _log_fh.flush()
    except Exception:
        pass

    # 4. Leave without joining anything. sys.exit() would hand control back to
    #    the interpreter's thread-joining shutdown, which is the hang itself.
    os._exit(code)
event_queue        = queue.Queue()
_spicetify_ws = None  # active WebSocket to Spicetify extension
# NOTE: log_queue / log() are defined near the top of the file — they are
# needed during module import, before this point is reached.

# ── Health monitor ────────────────────────────────────────────────
# Writes one CSV row per ~5s sample to health.csv next to the app. This is
# diagnostic instrumentation to diagnose the "goes unresponsive after some
# time" bug by capturing what's accumulating while the app runs. Safe to
# remove once the root cause is found and fixed.
import threading as _th
_HEALTH_FILE = os.path.join(_APP_DIR, "health.csv")
_HEALTH_HEADER = "ts,uptime_s,threads,event_q,log_q,executor_pending,img_pending,recv_pending,mem_mb,rpc_active"
_health_started = False
# Diagnostics are OFF unless explicitly requested. This instrumentation runs on
# the Tk thread and appends to a file that is never rotated; left enabled it
# produced a multi-gigabyte health.csv and became the dominant cost in the GUI
# loop. Enable with:  set STATUSIFY_HEALTH=1
_HEALTH_ENABLED  = os.environ.get("STATUSIFY_HEALTH", "") == "1"
_HEALTH_MAX_BYTES = 5 * 1024 * 1024   # hard cap: stop writing past 5 MB

def _health_snapshot():
    """Append one CSV row of live health metrics. Called every ~5s from GUI thread."""
    global _health_started, _HEALTH_ENABLED
    if not _HEALTH_ENABLED:
        return
    try:
        import time as _t, os as _os
        uptime = int(_t.monotonic() - _session_start)
        threads = _th.active_count()
        # queue.Queue has no public size-limit; qsize() is best-effort.
        try:    eq = event_queue.qsize()
        except Exception: eq = -1
        try:    lq = log_queue.qsize()
        except Exception: lq = -1
        # Executor pending counts: walk internal work queue (CPython impl detail,
        # but stable enough for diagnostics).
        def _pending(ex):
            try: return ex._work_queue.qsize()
            except Exception: return -1
        img_p = _pending(image_executor); recv_p = _pending(_recv_executor)
        # Main executor (1 worker) backlog is the freeze-warning signal.
        ex_p  = _pending(executor)
        # Memory (best-effort; psutil may not be installed)
        try:
            import psutil
            mem = int(psutil.Process(_os.getpid()).memory_info().rss / (1024*1024))
        except Exception:
            mem = -1
        rpc_active = getattr(getattr(DiscordRPC, "_instance", None), "_active", False)
        row = f"{int(_t.time())},{uptime},{threads},{eq},{lq},{ex_p},{img_p},{recv_p},{mem},{int(rpc_active)}\n"
        if not _health_started:
            row = _HEALTH_HEADER + "\n" + row
            _health_started = True
        # Goes through the shared rotating writer, so it can never run away
        # again the way the original unbounded append did.
        _rotating_write(_HEALTH_FILE, row, max_bytes=_HEALTH_MAX_BYTES, keep=1)
    except Exception:
        pass  # never let diagnostics crash the GUI thread

# ── Shared state ──────────────────────────────────────────────────
class State:
    artist = title = album_art = track_uri = ""
    position_ms = duration_ms = 0
    is_playing  = False
    lyrics_mode        = "none"
    instrumental_gaps  = []  # pre-calculated list of {startMs, endMs, gap_ms, key}
    synced = []; plain = []
    blacklisted        = False  # current track matches the user's blacklist

state = State()

# Session history: [{artist, title, album_art, synced, plain, mode, time}]
history = []

# ── Lyric helpers ─────────────────────────────────────────────────
# NOTE: every helper below offsets by _track_offset_ms(), NOT the raw global
# LYRIC_DELAY_MS. The per-track offset was only ever applied to the
# instrumental-gap position in rpc_loop, while the line actually chosen for
# display used the global — so nudging "Offset for this track" moved the gap
# markers and left the lyrics themselves exactly where they were. The whole
# feature silently did nothing.
def get_current_line():
    """(current, next) lyric for the live playback position.

    The selection itself lives in statusify_lyrics.select_line so it can be
    unit-tested without Tk; this only binds it to the module-level state."""
    return select_line(state.lyrics_mode, state.synced, state.plain,
                       state.position_ms + _track_offset_ms(), state.duration_ms)

def get_line_midpoint(w):
    for i, e in enumerate(state.synced):
        if e["words"] == w:
            s = e["startMs"]
            en = state.synced[i+1]["startMs"] if i+1 < len(state.synced) else state.duration_ms
            return s + (en-s)//2
    return None

def _cur_idx():
    """Index of the current lyric line based on playback position."""
    pos = state.position_ms + _track_offset_ms()
    idx = 0
    for i, e in enumerate(state.synced):
        if e["startMs"] <= pos:
            idx = i
        else:
            break
    return idx

def get_line_dur(w):
    # Find the line by position first (handles repeated lyrics correctly),
    # fall back to text match if needed.
    pos = state.position_ms + _track_offset_ms()
    best = None
    for i, e in enumerate(state.synced):
        if e["words"] == w:
            if best is None or abs(e["startMs"] - pos) < abs(best[0] - pos):
                best = (e["startMs"], i)
    if best is None:
        return 9999
    i = best[1]
    return (state.synced[i+1]["startMs"] if i+1 < len(state.synced) else state.duration_ms) - state.synced[i]["startMs"]

def get_nth(w, n):
    # Find the current occurrence of w by position, then return the nth line after it.
    pos = state.position_ms + _track_offset_ms()
    best = None
    for i, e in enumerate(state.synced):
        if e["words"] == w:
            if best is None or abs(e["startMs"] - pos) < abs(best[0] - pos):
                best = (e["startMs"], i)
    if best is None:
        return ""
    start = best[1]
    count = 0
    for j in range(start + 1, len(state.synced)):
        count += 1
        if count == n:
            return state.synced[j]["words"]
    return ""

# Stays in main.py: unlike join_lines/_calc_instrumental_gaps this reads the
# module-level `state` and MAX_STATE, so it is not independently testable.
def pick_group(line1):
    if state.lyrics_mode != "synced" or not state.synced: return [line1], 0
    dur = get_line_dur(line1); l2 = get_nth(line1,1); l3 = get_nth(line1,2)
    if dur >= 3500: return [line1], 0
    elif dur >= 1500:
        if l2 and len(join_lines([line1,l2])) <= MAX_STATE: return [line1,l2], 1
        return [line1], 0
    else:
        if l2 and l3 and len(join_lines([line1,l2,l3])) <= MAX_STATE: return [line1,l2,l3], 2
        if l2 and len(join_lines([line1,l2])) <= MAX_STATE: return [line1,l2], 1
        return [line1], 0

# Handle on the live DiscordRPC instance so the GUI can act on it (force a
# reconnect, send a test presence). A dict rather than a bare global so the
# backend can swap the instance on every reconnect without the GUI holding a
# stale reference.
_ACTIVE_RPC = {"rpc": None}

# ── Discord RPC ───────────────────────────────────────────────────
class DiscordRPC:
    OP_HANDSHAKE = 0; OP_FRAME = 1
    # If a single pipe operation takes longer than this, consider the pipe dead.
    # Prevents a blocking read() from wedging the worker thread forever.
    PIPE_TIMEOUT_S   = 5.0
    # Drop a pending SET_ACTIVITY if the worker can't keep up (rate-limit /
    # pipe stall). Old lyric lines are stale anyway; we keep only the latest.
    SEND_STALE_S     = 1.0
    def __init__(self, app_id):
        self.app_id = app_id; self.pipe = None
        self._connected = False; self._nonce = 0
        # Pending RPC sends, newest last. We only need the newest, so older
        # unprocessed sends are dropped to avoid an unbounded queue backlog.
        self._pending = []
        self._lock = threading.Lock()
    def _nxt(self): self._nonce += 1; return str(self._nonce)
    async def connect(self):
        for i in range(10):
            try: self.pipe = open(f"\\\\.\\pipe\\discord-ipc-{i}", "r+b", buffering=0); log(f"RPC connected  ·  discord-ipc-{i}"); break
            except OSError: continue
        if not self.pipe: raise RuntimeError("Discord IPC pipe not found")
        self._raw(self.OP_HANDSHAKE, {"v":1,"client_id":self.app_id})
        # Bounded handshake read — a stalled pipe can't hang the backend thread.
        try:
            fut = _recv_executor.submit(self._recv)
            resp = await asyncio.wrap_future(fut)
        except Exception:
            resp = None
        if resp and resp.get("evt") == "READY":
            self._connected = True
            user = resp["data"]["user"]["username"]
            log(f"RPC handshake OK  ·  {user}"); event_queue.put(("rpc_ok", user))
        else: raise RuntimeError(f"Handshake failed: {resp}")
    def _raw(self, op, data):
        p = json.dumps(data).encode()
        self.pipe.write(struct.pack("<II", op, len(p)) + p); self.pipe.flush()
    def _read_exact(self, n):
        """Read exactly n bytes, or return None if the pipe closes first.

        The pipe is opened with buffering=0, so read(n) is a single syscall
        that may legitimately return fewer bytes than asked for. The old code
        assumed a short read never happened; when it did, the header unpack or
        the JSON parse failed on a perfectly healthy connection and the frame
        was dropped as if the pipe had died."""
        chunks = []
        remaining = n
        while remaining > 0:
            chunk = self.pipe.read(remaining)
            if not chunk:
                return None
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _recv(self):
        try:
            h = self._read_exact(8)
            if h is None: return None
            _, n = struct.unpack("<II", h)
            body = self._read_exact(n)
            if body is None: return None
            return json.loads(body)
        except (OSError, ValueError, struct.error) as e:
            # OSError = pipe died, ValueError = malformed JSON frame. These
            # mean very different things; the bare `except:` that used to be
            # here made them indistinguishable and also swallowed
            # KeyboardInterrupt/SystemExit.
            log(f"RPC recv failed: {type(e).__name__}: {e}")
            return None
    def _send(self, payload):
        try:
            self._raw(self.OP_FRAME, payload)
            # Read the response on a SEPARATE bounded worker with a timeout.
            # A blocking read() on a stalled Discord pipe would otherwise sit
            # here forever, wedging the single RPC worker and preventing any
            # reconnection. We don't reuse the RPC executor for this read,
            # because the write above already occupies this thread.
            r = self._recv_timed()
            if r and r.get("evt") == "ERROR": log(f"RPC error: {r.get('data',{}).get('message',r)}")
        except Exception as e:
            log(f"Pipe error: {e}"); self._connected = False

    def _recv_timed(self):
        """Run the blocking pipe read on a worker with a timeout.

        If the read can't complete within PIPE_TIMEOUT_S, treat the pipe as
        dead: mark disconnected and close the pipe handle. Closing the handle
        unblocks the stuck worker thread (its read() raises) so it can't leak,
        and forces _backend to open a fresh pipe on reconnect."""
        fut = _recv_executor.submit(self._recv)
        try:
            return fut.result(timeout=self.PIPE_TIMEOUT_S)
        except _FutureTimeout:
            log("RPC pipe read timed out — forcing reconnect")
            self._connected = False
            # Close the pipe to unblock the stuck worker thread; its read()
            # will raise and the worker becomes available again.
            try:
                if self.pipe:
                    self.pipe.close()
            except Exception:
                pass
            return None
        except Exception:
            return None
    def _activity(self, title, artist, lines, art, position_ms=None, duration_ms=None):
        label = f"{title} — {artist}"[:128]
        act = {"details": label, "assets": {"large_image": art or "spotify", "large_text": label}}
        f = [l for l in lines if l]
        act["state"] = join_lines(f)[:MAX_STATE] if f else "— "
        # Add elapsed/remaining timer — this is part of the activity payload,
        # NOT a separate RPC call, so it does not count against the rate limit.
        if position_ms is not None and duration_ms and duration_ms > 0:
            import time as _time
            now_unix   = int(_time.time())
            start_unix = now_unix - (position_ms // 1000)
            end_unix   = start_unix + (duration_ms // 1000)
            act["timestamps"] = {"start": start_unix, "end": end_unix}
        return act
    async def set_activity(self, title, artist, lines, art, position_ms=None, duration_ms=None):
        if not self._connected: return
        act = self._activity(title, artist, lines, art, position_ms, duration_ms)
        self._enqueue_send({"cmd":"SET_ACTIVITY","args":{"pid":os.getpid(),"activity":act},"nonce":self._nxt()})
    async def clear_activity(self):
        if not self._connected: return
        self._enqueue_send({"cmd":"SET_ACTIVITY","args":{"pid":os.getpid(),"activity":None},"nonce":self._nxt()})

    def _enqueue_send(self, payload):
        """Submit a send to the worker, dropping stale queued sends first.

        Without this guard, every lyric tick queues another SET_ACTIVITY on the
        single-worker executor. If the worker stalls (busy pipe / Discord held
        by a game), the backlog grows without bound and the app freezes. We keep
        only the newest pending send — older lyric lines are stale by the time
        they'd be sent anyway."""
        now = time.monotonic()
        with self._lock:
            # Drop sends that have been sitting in the queue longer than the
            # staleness window — they're no longer worth sending.
            self._pending = [(ts, p) for (ts, p) in self._pending
                             if now - ts < self.SEND_STALE_S]
            fut_payload = (now, payload)
            self._pending.append(fut_payload)
        # Submit directly to the thread pool. A ThreadPoolExecutor accepts
        # submissions from any thread — no event loop required — and is itself
        # the backpressure boundary (max_workers caps concurrent sends).
        fut = executor.submit(self._drain_sends)
        # Don't await — fire-and-forget, but consume exceptions so they can't
        # surface as "exception never retrieved" warnings.
        def _done(f):
            try: f.result()
            except Exception: pass
        fut.add_done_callback(_done)

    def _drain_sends(self):
        """Worker entry: take the newest pending send and dispatch it.

        Runs on the bounded executor so even if every call here were to block,
        only a fixed number of threads are ever tied up — never an unbounded
        queue of fire-and-forget tasks."""
        with self._lock:
            if not self._pending:
                return
            _, payload = self._pending.pop()      # newest only
            self._pending.clear()                  # discard the rest (stale)
        self._send(payload)

# ── WebSocket ─────────────────────────────────────────────────────
async def ws_handler(ws):
    global _spicetify_ws, _dropped_lines
    _spicetify_ws = ws
    log("Spicetify connected"); event_queue.put(("sp", True))
    try:
        # Brief pause so the extension's onmessage handler is wired up
        # before we ask it to report state (onopen and onmessage are set
        # synchronously but the JS event loop needs one turn to process them)
        await asyncio.sleep(0.3)
        await ws.send(json.dumps({"type": "request_state"}))
        async for msg in ws:
            try: data = json.loads(msg)
            except ValueError: continue
            t = data.get("type")
            if t == "paused":
                state.is_playing = False; event_queue.put(("paused",))
                _on_track_pause()
            elif t == "track_change":
                state.artist    = data.get("artist",""); state.title = data.get("title","")
                state.album_art = data.get("album_art","")
                state.duration_ms = int(data.get("duration_ms",0))
                state.track_uri = data.get("track_uri","")
                state.lyrics_mode = "none"; state.synced = []; state.plain = []
                state.is_playing = True
                # Reset the per-song dropped-line counter on every track change.
                _dropped_lines = 0
                event_queue.put(("dropped", 0))
                state.blacklisted = _is_blacklisted(state.artist, state.title)
                if state.blacklisted:
                    log(f"Blacklisted — RPC suppressed  ·  {state.artist} — {state.title}")
                else:
                    log(f"Now playing  ·  {state.artist} — {state.title}")
                event_queue.put(("track", state.artist, state.title, state.album_art))
                _on_track_start()
            elif t == "lyrics":
                mode   = data.get("mode", data.get("lyrics_mode","none"))
                synced = data.get("synced",[]); plain = data.get("plain",[])
                uri    = data.get("track_uri","")
                src    = data.get("source", "Spicy" if mode=="synced" and synced else "fallback")
                if uri == state.track_uri or state.lyrics_mode == "none":
                    state.lyrics_mode = mode; state.synced = synced; state.plain = plain
                    state.instrumental_gaps = _calc_instrumental_gaps(synced, state.duration_ms) if mode == "synced" else []
                    n   = len(synced) or len(plain)
                    log(f"Lyrics ({src})  ·  {mode}  ·  {n} lines")
                    event_queue.put(("lyrics", src, mode, n))
                    _save_history(mode, synced, plain)
            elif t == "position":
                was = state.is_playing
                state.position_ms = int(data.get("position_ms",0))
                state.duration_ms = int(data.get("duration_ms", state.duration_ms))
                state.is_playing  = data.get("is_playing", True)
                if not was and state.is_playing:
                    _on_track_resume()
            elif t == "lyrics_debug":
                msg = data.get("message", "")
                if msg:
                    log(f"[Bridge] {msg}")
    except websockets.exceptions.ConnectionClosed:
        log("Spicetify disconnected")
    except Exception as e:
        # Anything other than a clean close used to escape this handler with
        # the teardown below unreached: _spicetify_ws kept pointing at a dead
        # socket (so the skip hotkeys silently did nothing) and the Spicetify
        # status dot stayed green until the app was restarted.
        log(f"Spicetify handler error: {type(e).__name__}: {e}")
        event_queue.put(("error", f"Spicetify link dropped: {e}"))
    finally:
        # Only disown the socket if it is still the active one — a reconnect
        # may already have installed a newer ws while this handler unwound.
        if _spicetify_ws is ws:
            _spicetify_ws = None
            event_queue.put(("sp", False))
            state.is_playing = False

def _save_history(mode, synced, plain):
    # Update in place if this track is already in history (lyrics can arrive
    # in more than one message for the same URI).
    for entry in history:
        if entry["track_uri"] == state.track_uri:
            entry["synced"] = synced; entry["plain"] = plain; entry["mode"] = mode; return
    entry = {
        "track_uri": state.track_uri, "artist": state.artist, "title": state.title,
        "album_art": state.album_art, "mode": mode, "synced": synced, "plain": plain,
        "time": datetime.datetime.now().strftime("%H:%M"),
    }
    history.append(entry)
    # Actually enforce the cap. The previous version only stopped *notifying
    # the UI* past MAX_HISTORY_ROWS while still appending forever, so two
    # things went wrong at once: `history` grew without bound for the whole
    # session (every entry holds a full lyric sheet, and _persist_history
    # dumps the lot to disk on quit — hence a 600 KB history.json), and every
    # track after the 500th silently never appeared in the History tab.
    while len(history) > MAX_HISTORY_ROWS:
        history.pop(0)
    # The event carries the entry itself rather than its index. Indices shift
    # the moment the front is trimmed, which would repoint every already-
    # rendered row's LYRICS button at the wrong song.
    event_queue.put(("history_add", entry))

def _lrc_timestamp(ms):
    """Format milliseconds as an LRC [mm:ss.xx] tag."""
    ms = max(0, int(ms))
    minutes, rem = divmod(ms, 60_000)
    seconds, hundredths = divmod(rem, 1000)
    return f"[{minutes:02d}:{seconds:02d}.{hundredths // 10:02d}]"

def _export_lyrics(entry, fmt="lrc"):
    """Write one history entry's lyrics to disk. Returns the path, or None.

    Synced lyrics already carry per-line millisecond timestamps, so a proper
    .lrc file is essentially free — the data was there all along with no way
    to get it out of the app."""
    artist = (entry.get("artist") or "Unknown").strip()
    title  = (entry.get("title")  or "Unknown").strip()
    safe   = "".join(c for c in f"{artist} - {title}" if c not in '\\/:*?"<>|').strip()[:120]
    out_dir = os.path.join(_APP_DIR, "exports")
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as e:
        log(f"Export failed: {e}")
        return None

    synced = entry.get("synced") or []
    plain  = entry.get("plain") or []
    lines  = []

    if fmt == "lrc" and synced:
        lines.append(f"[ar:{artist}]")
        lines.append(f"[ti:{title}]")
        lines.append("[re:Statusify]")
        for ln in synced:
            lines.append(f"{_lrc_timestamp(ln.get('startMs', 0))}{ln.get('words', '')}")
    else:
        fmt = "txt"
        lines.append(f"{artist} — {title}")
        lines.append("")
        if synced:
            lines.extend(ln.get("words", "") for ln in synced)
        else:
            lines.extend(plain)

    path = os.path.join(out_dir, f"{safe}.{fmt}")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except OSError as e:
        log(f"Export failed: {e}")
        return None
    log(f"Exported {fmt.upper()}  ·  {os.path.basename(path)}")
    return path

def _load_history():
    """Loads history from JSON if SAVE_HISTORY is enabled."""
    global history
    if not SAVE_HISTORY or not os.path.exists(_HIST_FILE): return
    try:
        with open(_HIST_FILE, "r", encoding="utf-8") as f:
            history = json.load(f)
        # Keep only the most recent entries to prevent memory bloat
        if len(history) > MAX_HISTORY_ROWS:
            history = history[-MAX_HISTORY_ROWS:]
        log(f"Loaded {len(history)} history entries from disk")
    except Exception as e:
        log(f"Could not load history: {e}")

def _persist_history():
    """Saves or deletes history on disk based on SAVE_HISTORY setting."""
    if SAVE_HISTORY:
        try:
            with open(_HIST_FILE, "w", encoding="utf-8") as f:
                json.dump(history, f, indent=2)
            log("History persisted to disk")
        except Exception as e:
            log(f"Could not save history: {e}")
    else:
        if os.path.exists(_HIST_FILE):
            try: os.remove(_HIST_FILE); log("History file deleted (disabled)")
            except OSError as e: log(f"Could not delete history file: {e}")

# ── RPC loop ──────────────────────────────────────────────────────
async def rpc_loop(rpc):
    global _dropped_lines
    rl = {"t":[]}
    last_uri = track_mono = None
    title_sent = False; last_line = None; skip = []
    gap_mono = None; gap_shown_idx = -1; was_playing = False
    calibration_until = 0.0   # Feature 6: don't RPC until this monotonic time
    rpc_was_enabled = _rpc_enabled

    def reset_track_state():
        """Forget everything we've published, so the next tick starts clean."""
        nonlocal last_uri, track_mono, title_sent, last_line, skip
        nonlocal gap_mono, gap_shown_idx, was_playing, calibration_until
        last_uri = track_mono = None; title_sent = False
        last_line = None; skip = []; gap_mono = None; gap_shown_idx = -1
        was_playing = False; calibration_until = 0.0

    def avail():
        now = time.monotonic(); rl["t"] = [x for x in rl["t"] if now-x < RATE_LIMIT_WINDOW]
        return len(rl["t"]) < RATE_LIMIT_CALLS

    def connected():
        return rpc._connected
    def rec(): rl["t"].append(time.monotonic())
    def wait():
        if avail(): return 0.0
        return max(0.0, RATE_LIMIT_WINDOW - (time.monotonic() - min(rl["t"])))

    while True:
        await asyncio.sleep(0.05)
        if not connected():
            return  # pipe broke — let _backend reconnect
        if not _rpc_enabled:
            # Toggling RPC off used to `continue` straight past the clear
            # below, so whatever lyric was last published stayed pinned to the
            # user's Discord profile indefinitely — the one thing the toggle
            # exists to prevent. Clear once on the falling edge.
            if rpc_was_enabled:
                await rpc.clear_activity(); rl["t"].clear()
                reset_track_state()
                event_queue.put(("line", ""))
                log("RPC disabled — presence cleared")
                rpc_was_enabled = False
            continue
        if not rpc_was_enabled:
            # Re-enabled: forget the pre-toggle track so the current one is
            # republished from scratch instead of being suppressed as a dupe.
            reset_track_state()
            rpc_was_enabled = True
            log("RPC enabled")
        if not state.is_playing:
            if was_playing:
                if SHOW_PAUSED_RPC:
                    # Feature 7: show paused indicator instead of clearing
                    if avail():
                        await rpc.set_activity(state.title, state.artist, ["\u23f8 Paused"], state.album_art)
                        rec(); log("RPC paused indicator")
                else:
                    await rpc.clear_activity(); rl["t"].clear()
                reset_track_state()
                event_queue.put(("line",""))
                if not SHOW_PAUSED_RPC:
                    log("RPC cleared")
            continue
        was_playing = True
        if not state.title: continue
        # Blacklist gate (#16): clear any presence we already published for
        # this track, then stay silent for as long as it's playing.
        if getattr(state, "blacklisted", False):
            if last_uri != state.track_uri:
                await rpc.clear_activity(); rl["t"].clear()
                last_uri = state.track_uri
                event_queue.put(("line", "— blacklisted —"))
            continue
        if state.track_uri != last_uri:
            last_uri = state.track_uri; track_mono = time.monotonic()
            title_sent = False; last_line = None; skip = []; gap_mono = None; gap_shown_idx = -1
            calibration_until = time.monotonic() + 1.5  # Feature 6: wait 1.5s to prevent Discord RPC rate-limit on rapid skips
            continue

        # Feature 6: skip RPC until calibration gate has passed
        if time.monotonic() < calibration_until:
            continue

        line1, _ = get_current_line()
        # Per-track offset (#13), falling back to the global when unset.
        pos = state.position_ms + _track_offset_ms()

        # ── Instrumental detection — use pre-calculated gap list ──────
        active_gap = None
        for gap in state.instrumental_gaps:
            if gap["gap_ms"] > 3000 and gap["startMs"] <= pos < gap["endMs"]:
                active_gap = gap
                break

        # Active gap display
        if active_gap:
            if gap_shown_idx != active_gap["key"]:
                if not avail():
                    w = wait()
                    if w > 0 and (active_gap["gap_ms"] / 1000) > w + 1.0:
                        log(f"Instrumental waiting for rate limit  ·  {w:.1f}s")
                        await asyncio.sleep(w + 0.05)
                if avail():
                    gap_shown_idx = active_gap["key"]; title_sent = True; rec()
                    instr_text = INSTRUMENTAL_TEXT  # Feature 5: custom instrumental text
                    await rpc.set_activity(state.title, state.artist, [instr_text], state.album_art, state.position_ms, state.duration_ms)
                    log(f"RPC instrumental  (gap {active_gap['gap_ms']/1000:.1f}s)")
                    event_queue.put(("line", instr_text))
            continue

        if not line1:
            # No lyric line for this instant. That covers a lot of ordinary
            # cases: the track has no lyrics at all, it is an instrumental or
            # a podcast, the fetch failed, or the lyrics simply have not
            # arrived yet (they land a second or two after the track change).
            #
            # This used to be a bare `continue`, and since it was the only
            # thing standing between a playing track and the sole set_activity
            # call below, "no lyrics" meant Statusify published NOTHING —
            # despite already holding the title, artist, album art and
            # timestamps. That is why a lyrics-side failure showed up as
            # "the Rich Presence isn't working at all".
            #
            # `title_sent` has existed all along and was never read; this is
            # the gate it was written for. One publish per track, not one per
            # 50ms tick, so an unlyricked album can't exhaust the rate limit.
            if not title_sent and avail():
                title_sent = True; rec()
                await rpc.set_activity(state.title, state.artist, [],
                                       state.album_art,
                                       state.position_ms, state.duration_ms)
                log(f"RPC title-only  ·  {state.artist} — {state.title}")
                event_queue.put(("line", ""))
            continue
        # ─────────────────────────────────────────────────────────────

        gap_mono = None; title_sent = True
        if line1 == last_line or line1 in skip: continue
        group, _ = pick_group(line1)

        if not avail():
            w = wait(); log(f"Rate limited  ·  {w:.1f}s"); event_queue.put(("rl", w))
            await asyncio.sleep(w + 0.05)
            # After waiting, resync to whatever line is current now.
            # Don't try to send stale line1 — the song has moved on.
            cur, _ = get_current_line()
            if not cur or cur == last_line or cur in skip:
                # Nothing new to send yet
                last_line = line1; skip = group[1:]
                if cur and cur != line1:
                    _dropped_lines += 1
                    log(f"Dropped (moved on)  ·  {line1[:40]}")
                    event_queue.put(("dropped", _dropped_lines))
                continue
            # Send the current line instead of the original stale one
            line1 = cur
            group, _ = pick_group(line1)

        last_line = line1; skip = group[1:]; rec()
        await rpc.set_activity(state.title, state.artist, group, state.album_art, state.position_ms, state.duration_ms)
        display = join_lines(group)
        log(f"RPC ({len(group)}L)  ·  {display[:55]}"); event_queue.put(("line", display))

# ── Colour maths ──────────────────────────────────────────────────
# Small pure helpers, kept module-level so tests can exercise them without
# a Tk root. Everything the UI animates (hover fades, accent tinting) needs
# to interpolate between two hex colours, and the accent is user-chosen so
# nothing that derives from it can be hardcoded.

def _hex_to_rgb(c):
    """'#rrggbb' → (r, g, b). Tolerates '#rgb' and a missing '#'."""
    c = str(c).lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16))

def _rgb_to_hex(rgb):
    r, g, b = (max(0, min(255, int(round(v)))) for v in rgb)
    return f"#{r:02x}{g:02x}{b:02x}"

def _blend(c1, c2, t):
    """Mix c1→c2 by t in 0..1. t=0 is c1, t=1 is c2."""
    t = max(0.0, min(1.0, float(t)))
    a, b = _hex_to_rgb(c1), _hex_to_rgb(c2)
    return _rgb_to_hex(a[i] + (b[i] - a[i]) * t for i in range(3))

def _luminance(c):
    """Perceived luminance 0..1 (Rec. 601 weights — good enough to pick
    between black and white foreground on an arbitrary accent)."""
    r, g, b = _hex_to_rgb(c)
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255.0

def _readable_on(c):
    """Black or white, whichever stays legible on top of `c`.

    The RPC button used to hardcode fg='#000000' on an ACCENT background.
    The accent is a colour picker — choose anything dark (navy, maroon) and
    the button's label went black-on-black."""
    return "#000000" if _luminance(c) > 0.55 else "#ffffff"

# ── GUI colors ────────────────────────────────────────────────────
def _apply_palette(dark: bool, accent: str):
    global BG, BG2, BG3, BG4, ACCENT, MUTED, TEXT, TEXT2, BORDER, _DARK_MODE
    global ACCENT_SOFT, ACCENT_FG, HOVER_BG, SHADOW, DANGER, WARN
    global _PREV_BG, _PREV_BG2, _PREV_BG3, _PREV_BG4
    global _PREV_MUTED, _PREV_TEXT, _PREV_TEXT2, _PREV_BORDER, _PREV_ACCENT
    global _PREV_ACCENT_SOFT, _PREV_ACCENT_FG, _PREV_HOVER_BG, _PREV_SHADOW
    global _PREV_DANGER, _PREV_WARN
    # Snapshot the colours that are about to be replaced so _rebuild_all
    # can build an exact before→after mapping without any hash collisions.
    # Guard against the very first call where these globals don't exist yet.
    _PREV_BG     = globals().get("BG",     "#0a0a0a")
    _PREV_BG2    = globals().get("BG2",    "#111111")
    _PREV_BG3    = globals().get("BG3",    "#181818")
    _PREV_BG4    = globals().get("BG4",    "#1e1e1e")
    _PREV_MUTED  = globals().get("MUTED",  "#535353")
    _PREV_TEXT   = globals().get("TEXT",   "#ffffff")
    _PREV_TEXT2  = globals().get("TEXT2",  "#b3b3b3")
    _PREV_BORDER = globals().get("BORDER", "#2a2a2a")
    _PREV_ACCENT = globals().get("ACCENT", "#1db954")
    _PREV_ACCENT_SOFT = globals().get("ACCENT_SOFT", "#1a2a1a")
    _PREV_ACCENT_FG   = globals().get("ACCENT_FG",   "#000000")
    _PREV_HOVER_BG    = globals().get("HOVER_BG",    "#1e1e1e")
    _PREV_SHADOW      = globals().get("SHADOW",      "#000000")
    _PREV_DANGER      = globals().get("DANGER",      "#e05555")
    _PREV_WARN        = globals().get("WARN",        "#d4a017")
    _DARK_MODE = dark
    ACCENT = accent
    if dark:
        # A neutral-cool ramp rather than pure greys. Each step is a real
        # elevation level: BG is the page, BG2 a card, BG3 an inset/control,
        # BG4 the highest surface. The old ramp (#0a0a0a → #1e1e1e) put only
        # 20 levels of grey between the page and the topmost surface, so every
        # panel edge disappeared and the whole window read as one flat sheet.
        BG     = "#0b0d10"
        BG2    = "#12151a"
        BG3    = "#1a1f26"
        BG4    = "#232932"
        MUTED  = "#6b7480"
        TEXT   = "#f2f4f7"
        TEXT2  = "#a8b0bb"
        BORDER = "#252b34"
        SHADOW = "#05070a"
        # Semantic colours. These were literals scattered through the build
        # methods (a hardcoded red and amber), so they never adapted to the
        # theme —
        # a mid-tone red tuned for a black background sat on white in light
        # mode at roughly 2.4:1 contrast, well under the 4.5:1 floor.
        DANGER = "#ff6b6b"
        WARN   = "#e8b339"
    else:
        # Light mode was four muddy greys (#f0f0f0/#e4e4e4/#d8d8d8/#cccccc)
        # that made cards *darker* than the page — the inverse of how
        # elevation reads. Cards are now white and lift off a tinted page.
        BG     = "#f4f6f8"
        BG2    = "#ffffff"
        BG3    = "#eaeef2"
        BG4    = "#dde3ea"
        MUTED  = "#7b8794"
        TEXT   = "#12161b"
        TEXT2  = "#4a5563"
        BORDER = "#d9e0e7"
        SHADOW = "#c7d0d9"
        DANGER = "#c62f2f"
        WARN   = "#8a6100"
    # Accent-derived tokens. Hover backgrounds used to be the literal
    # "#1a2a1a" — a green tint baked in regardless of the chosen accent, and
    # near-black in light mode.
    ACCENT_SOFT = _blend(BG3, ACCENT, 0.22 if dark else 0.16)
    ACCENT_FG   = _readable_on(ACCENT)
    HOVER_BG    = _blend(BG2, TEXT, 0.06 if dark else 0.05)

_apply_palette(_DARK_MODE, ACCENT)
# After _apply_palette the names BG, BG2 … ACCENT … are module-level strings.

# Hero album-art size — single source of truth shared by the Now-Playing
# canvas, the placeholder art (_default_art) and the live art loader (_set_art).
HERO_ART_PX = 120
# Corner radii. Album art was the one large square in a UI made entirely of
# squares, so it read as an unstyled <img> dropped into the layout.
HERO_ART_RADIUS = 10
THUMB_PX, THUMB_RADIUS = 44, 6

# ── UI scale ──────────────────────────────────────────────────────
# Spacing was previously ad-hoc: padx values of 12, 14 and 16 and pady values
# of 2, 3, 4, 6, 8, 10, 12 and 18 all appeared within the same page, so
# nothing lined up vertically and the density read as accidental. These are a
# 4 px grid; every pad in the UI should be one of them.
SP_XS, SP_SM, SP_MD, SP_LG, SP_XL = 4, 8, 12, 16, 24

# Type scale. Sizes are passed to App._f(), which adds +1 and enforces a
# 10 pt floor, so these are relative steps rather than absolute point sizes.
FS_MICRO, FS_SMALL, FS_BODY, FS_LARGE, FS_TITLE, FS_HERO = 7, 8, 9, 10, 13, 15

# Window geometry defaults. __init__ used to set 540x720 and then _center()
# immediately re-set it to 500x680 — two different sizes, the second silently
# winning. One source of truth now.
WIN_W, WIN_H = 520, 720
WIN_MIN_W, WIN_MIN_H = 460, 580

# Pixels travelled per mouse-wheel notch in the history list.
SCROLL_NOTCH_PX = 90

class App:
    """
    Single Tk() window with overrideredirect(True) to remove the OS titlebar.
    After the window is mapped we use ctypes to set WS_EX_APPWINDOW on the
    HWND, which forces Windows to show it in the taskbar regardless of the
    overrideredirect flag.
    """
    # Windows extended style constants
    GWL_EXSTYLE      = -20
    WS_EX_APPWINDOW  = 0x00040000
    WS_EX_TOOLWINDOW = 0x00000080

    def __init__(self):
        self._root = tk.Tk()
        try:
            import ctypes
            dpi = ctypes.windll.user32.GetDpiForSystem()
            self._root.tk.call('tk', 'scaling', dpi / 72.0)
        except Exception:
            pass
        self._root.title("Statusify")
        self._root.geometry("540x720")
        self._root.resizable(True, True)
        self._root.minsize(460, 580)
        self._root.configure(bg=BG)
        self._root.overrideredirect(True)
        self._root.protocol("WM_DELETE_WINDOW", self._on_close_button)
        # Set window icon (.ico applied before and after overrideredirect)
        self._apply_icon()

        # Alias so the rest of the code can reference self.win uniformly
        self.win = self._root

        self._center()
        self._img = None
        # Progress-bar estimation state. `state.position_ms` is only updated on
        # each WS "position" ping; between pings we advance it ourselves while
        # playing so the bar moves smoothly (mirrors Discord RPC's own timer).
        self._last_pos_ms = 0
        self._last_pos_mono = None
        self._last_dur_ms = 0
        self._pages = {}
        self._cur_page = None
        # (_hist_imgs removed: thumbnail refs now live on their own canvas
        #  widget, so they are freed when the row is destroyed.)
        # Registry of live after() timers, keyed by name. See _schedule().
        self._timers = {}
        self._alive  = True
        self._hidden = False
        # Smooth-scroll chase state for the History list (see _smooth_scroll).
        self._scroll_target = 0.0
        self._scroll_active = False
        self._build()
        self._render_loaded_history()
        self._tray_start()
        self._poll()
        # Drive the Now-Playing progress bar (~4 fps is smooth enough and cheap).
        self._schedule("progress", 250, self._tick_progress)

        # Apply taskbar fix after the event loop starts (needs HWND to exist)
        self._schedule("taskbarfix", 100, self._fix_taskbar)
        self._schedule("hotkeys", 200, lambda: _register_hotkeys(self))
        self._add_resize_handles()
        # Re-show in taskbar whenever the window is focused back (handles tab-out)
        self._root.bind("<FocusIn>", lambda e: self._schedule("focusin", 50, self._on_focus_in))
        self._bind_shortcuts()
        self._apply_topmost()
        # Persist geometry as the window settles, not on every drag pixel.
        self._root.bind("<Configure>",
                        lambda e: self._schedule("savegeo", 800, self._save_geometry))
        if START_MINIMIZED:
            # Defer until after the first draw, or Tk shows a flash of window.
            self._schedule("startmin", 400, self._start_hidden)
        self._schedule("bridgecheck", 2500, self._check_bridge_version)
        # Clear any stale request left by a crash, then start watching.
        try:
            if os.path.exists(_SHOW_FLAG):
                os.remove(_SHOW_FLAG)
        except OSError:
            pass
        self._schedule("showwatch", 1000, self._watch_show_request)

    def _start_hidden(self):
        if getattr(self, "_tray", None):
            self._hide_to_tray()
            log("Started minimised to tray")
        else:
            self._minimize()

    # ── Timer registry ────────────────────────────────────────────
    def _schedule(self, key, ms, fn):
        """after() with a named slot — arming a key cancels its previous timer.

        The freeze that shipped in 1.1.5 was a self-rescheduling method that
        was ALSO called directly from two other places. Each direct call
        started an additional permanent timer chain, nothing ever cancelled
        them, and after a couple of days thousands of chains were firing on
        the Tk thread. Raw after() makes that mistake easy and invisible;
        this makes it impossible — one key can only ever have one live timer,
        so a duplicate chain cannot exist regardless of who calls what.

        Also refuses to arm after shutdown, so a late timer can't fire on a
        destroyed root and leave the app half-torn-down."""
        if not getattr(self, "_alive", False):
            return None
        self._cancel(key)
        try:
            tid = self._root.after(ms, fn)
        except Exception:
            return None
        self._timers[key] = tid
        return tid

    def _cancel(self, key):
        """Cancel the timer registered under `key`, if any."""
        tid = self._timers.pop(key, None)
        if tid is None:
            return
        try:
            self._root.after_cancel(tid)
        except Exception:
            pass

    def _cancel_all_timers(self):
        """Stop every registered timer — called on shutdown."""
        self._alive = False
        for key in list(self._timers):
            self._cancel(key)

    def _on_focus_in(self):
        """Called when main window regains focus — reapply taskbar style."""
        self._ensure_taskbar()

    def _ensure_taskbar(self):
        """Reapply WS_EX_APPWINDOW silently — no withdraw/deiconify flicker."""
        try:
            user32  = ctypes.windll.user32
            hwnd_tk = self._root.winfo_id()
            hwnd    = user32.GetParent(hwnd_tk) or hwnd_tk
            style   = user32.GetWindowLongW(hwnd, self.GWL_EXSTYLE)
            style   = (style & ~self.WS_EX_TOOLWINDOW) | self.WS_EX_APPWINDOW
            user32.SetWindowLongW(hwnd, self.GWL_EXSTYLE, style)
            # SWP_NOMOVE|SWP_NOSIZE|SWP_NOZORDER|SWP_FRAMECHANGED
            user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0001|0x0002|0x0004|0x0020)
        except Exception:
            pass

    def _fix_taskbar(self):
        """
        Find the real top-level HWND via FindWindowW (title match),
        set WS_EX_APPWINDOW, send WM_SETICON on every ancestor HWND,
        then walk up the parent chain to catch the wrapper window too.
        """
        try:
            global _ICON_PATH
            if _ICON_PATH is None:
                _ICON_PATH = _write_icon()

            user32 = ctypes.windll.user32

            LR_LOADFROMFILE = 0x00000010
            IMAGE_ICON      = 1
            WM_SETICON      = 0x0080
            ICON_SMALL      = 0
            ICON_BIG        = 1

            hicon_big = user32.LoadImageW(
                None, _ICON_PATH, IMAGE_ICON, 256, 256, LR_LOADFROMFILE)
            hicon_small = user32.LoadImageW(
                None, _ICON_PATH, IMAGE_ICON, 16, 16, LR_LOADFROMFILE)

            # Collect every HWND that could own the taskbar button:
            # the tk widget id, its parent, and the FindWindowW result by title
            hwnd_tk     = self._root.winfo_id()
            hwnd_parent = user32.GetParent(hwnd_tk)
            hwnd_title  = user32.FindWindowW(None, "Statusify")

            candidates = {h for h in (hwnd_tk, hwnd_parent, hwnd_title) if h}

            # Also walk the ancestor chain from each candidate
            for hwnd in list(candidates):
                h = hwnd
                for _ in range(6):
                    p = user32.GetParent(h)
                    if p: candidates.add(p); h = p
                    else: break

            for hwnd in candidates:
                # Apply WS_EX_APPWINDOW style
                style = user32.GetWindowLongW(hwnd, self.GWL_EXSTYLE)
                style = (style & ~self.WS_EX_TOOLWINDOW) | self.WS_EX_APPWINDOW
                user32.SetWindowLongW(hwnd, self.GWL_EXSTYLE, style)
                # Send icon messages
                if hicon_big:
                    user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG,   hicon_big)
                if hicon_small:
                    user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, hicon_small)

            # Refresh so the shell picks up the new icon.
            # Save Toplevel children first — withdraw() hides them and
            # deiconify() won't restore them automatically.
            popups = [w for w in self._root.winfo_children()
                      if isinstance(w, tk.Toplevel) and w.winfo_viewable()]
            self._root.withdraw()
            def _restore(popups=popups):
                self._root.deiconify()
                for p in popups:
                    try: p.deiconify()
                    except Exception: pass
            self._root.after(10, _restore)
            self._root.after(100, self._apply_icon)
        except Exception as e:
            log(f"Taskbar fix skipped: {e}")

    def _apply_icon(self):
        """Apply the embedded .ico as the window and taskbar icon."""
        try:
            global _ICON_PATH
            if _ICON_PATH is None:
                _ICON_PATH = _write_icon()
            self._root.iconbitmap(default=_ICON_PATH)
            # Also set via iconphoto for the taskbar (Pillow path)
            if PIL_AVAILABLE:
                from io import BytesIO
                data  = base64.b64decode(_ICON_B64)
                img   = Image.open(BytesIO(data))
                sizes = [256, 128, 64, 48, 32, 16]
                photos = []
                for s in sizes:
                    try:
                        resized = img.resize((s, s), Image.LANCZOS)
                        photos.append(ImageTk.PhotoImage(resized))
                    except Exception:
                        pass
                if photos:
                    self._icon_photos = photos  # keep refs
                    self._root.iconphoto(True, *photos)
        except Exception as e:
            log(f"Icon apply skipped: {e}")

    def _center(self, force=False):
        """Restore the last window geometry, or centre on first run.

        Previously this hard-centred a 500x680 window on every single launch,
        discarding wherever you had put it. For an app you leave running on a
        second monitor that's a small papercut you hit every day.

        `force=True` skips the saved geometry and genuinely re-centres. The
        Settings "Reset window position → CENTER" button called this with no
        argument, so it took the saved-geometry path and put the window back
        exactly where it already was — the one thing it must not do, given the
        button exists to rescue a window stranded off-screen."""
        sw = self._root.winfo_screenwidth(); sh = self._root.winfo_screenheight()
        saved = "" if force else _cfg_get("window", "geometry", "")
        if saved:
            try:
                size, x, y = saved.split("+")[0], int(saved.split("+")[1]), int(saved.split("+")[2])
                w, h = (int(v) for v in size.split("x"))
                # Only honour it if the window would land on a visible screen —
                # otherwise unplugging a monitor strands the window off-canvas
                # with no title bar to drag it back by.
                if (w >= WIN_MIN_W and h >= WIN_MIN_H
                        and -w + 80 < x < sw - 80 and -40 < y < sh - 80):
                    self._root.geometry(saved)
                    return
                log("Saved window position is off-screen — recentring")
            except (ValueError, IndexError):
                pass
        w, h = WIN_W, WIN_H
        self._root.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")
        if force:
            # Persist immediately: the <Configure> handler is debounced by
            # 800 ms and would be skipped entirely if the window is hidden.
            _cfg_set("window", "geometry", f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")
            log("Window position reset to centre")

    def _save_geometry(self):
        """Persist the current window geometry (called on quit / hide)."""
        try:
            if getattr(self, "_hidden", False):
                return  # a withdrawn window reports a useless geometry
            geo = self._root.geometry()          # "WxH+X+Y"
            if "x" in geo and "+" in geo:
                _cfg_set("window", "geometry", geo)
        except (tk.TclError, ValueError):
            pass

    # ── Always on top ─────────────────────────────────────────────
    def _apply_topmost(self):
        try:
            self._root.attributes("-topmost", bool(ALWAYS_ON_TOP))
        except tk.TclError:
            pass

    def _toggle_topmost(self, _e=None):
        global ALWAYS_ON_TOP
        ALWAYS_ON_TOP = not ALWAYS_ON_TOP
        _cfg_set("preferences", "always_on_top", str(ALWAYS_ON_TOP).lower())
        self._apply_topmost()
        self._paint_topmost_btn()
        log(f"Always on top {'enabled' if ALWAYS_ON_TOP else 'disabled'}")

    def _paint_topmost_btn(self):
        btn = getattr(self, "_top_btn", None)
        if btn is None:
            return
        try:
            btn.config(fg=ACCENT if ALWAYS_ON_TOP else MUTED)
        except tk.TclError:
            pass

    # ── In-app keyboard shortcuts ─────────────────────────────────
    def _bind_shortcuts(self):
        """Window-scoped keys. The global hotkeys (ctrl+alt+…) are separate —
        these only fire when Statusify itself has focus, so they can use plain
        combos without stealing keys from other apps."""
        W = self._root
        binds = {
            "<Escape>":           lambda e: self._close_lyrics_panel(),
            "<Control-f>":        lambda e: self._focus_history_search(),
            "<Control-Key-1>":    lambda e: self._show("NOW PLAYING"),
            "<Control-Key-2>":    lambda e: self._show("HISTORY"),
            "<Control-Key-3>":    lambda e: self._show("SETTINGS"),
            "<Control-c>":        lambda e: self._copy_current_lyric(),
            "<Control-m>":        lambda e: self._toggle_mini(),
            "<Control-t>":        lambda e: self._toggle_topmost(),
        }
        for seq, fn in binds.items():
            try:
                W.bind(seq, fn)
            except tk.TclError:
                pass

    def _focus_history_search(self):
        try:
            self._show("HISTORY")
            self._hist_search_entry.focus_set()
            self._hist_search_entry.select_range(0, "end")
        except (AttributeError, tk.TclError):
            pass

    # ── Clipboard helpers ─────────────────────────────────────────
    def _to_clipboard(self, text, what="Copied"):
        if not text:
            return
        try:
            self._root.clipboard_clear()
            self._root.clipboard_append(text)
            log(f"{what}: {text[:60]}")
        except tk.TclError as e:
            log(f"Clipboard failed: {e}")

    def _copy_current_lyric(self):
        try:
            self._to_clipboard(self.lbl_lyric.cget("text"), "Copied lyric")
        except (AttributeError, tk.TclError):
            pass

    def _copy_track(self):
        artist = getattr(state, "artist", "")
        title  = getattr(state, "title", "")
        if artist or title:
            self._to_clipboard(f"{artist} — {title}", "Copied track")

    # ── Bridge version check ──────────────────────────────────────
    def _check_bridge_version(self):
        """Warn when Spotify is running a bridge older than the one we ship.

        The advice here matters as much as the detection. This used to say
        "restart Spotify", which is simply wrong: Spicetify injects extensions
        into Spotify's xpui bundle at `spicetify apply` time, and restarting
        Spotify re-runs that injected copy without ever re-reading the source
        folder _install_bridge() writes to. Users who dutifully restarted
        Spotify — repeatedly — kept running an eleven-day-old bridge pinned to
        a Spicy Lyrics API version the server no longer accepted, so lyrics
        came back empty while the official Spicy Lyrics panel showed them
        fine. `spicetify apply` is the step that actually does anything.

        The check itself used to compare the source against the folder it had
        just been copied to, which is always equal, so it never fired."""
        if _BRIDGE_UPDATED:
            msg = "Bridge out of date in Spotify — run: spicetify apply"
            log(f"⚠ {msg}")
            self._set_error(msg)

    # ── Mini mode ─────────────────────────────────────────────────
    def _toggle_mini(self, _e=None):
        if getattr(self, "_mini", None) is not None:
            self._close_mini()
        else:
            self._open_mini()

    def _open_mini(self):
        """A compact always-on-top strip showing just the current lyric.

        The main window is 520x720 — far too big to leave floating over a game
        or a video. Mini mode is the form this app actually wants most of the
        time: one line of text, always visible, out of the way."""
        try:
            m = tk.Toplevel(self._root)
            m.overrideredirect(True)
            m.attributes("-topmost", True)
            m.configure(bg=BG)
            sw = m.winfo_screenwidth()
            w, h = 560, 76
            saved = _cfg_get("window", "mini_geometry", "")
            m.geometry(saved if saved else f"{w}x{h}+{(sw - w) // 2}+40")

            frame = tk.Frame(m, bg=BG2, highlightbackground=BORDER, highlightthickness=1)
            frame.pack(fill="both", expand=True)

            top = tk.Frame(frame, bg=BG2); top.pack(fill="x", padx=SP_MD, pady=(SP_SM, 0))
            self._mini_track = tk.Label(top, text="—", fg=MUTED, bg=BG2,
                                        font=self._f(FS_MICRO), anchor="w")
            self._mini_track.pack(side="left", fill="x", expand=True)
            close = tk.Label(top, text="✕", fg=MUTED, bg=BG2, font=self._f(FS_SMALL),
                             cursor="hand2", padx=SP_XS)
            close.pack(side="right")
            close.bind("<Button-1>", lambda e: self._close_mini())
            self._hoverable(close, fg=lambda: MUTED, hover_fg=lambda: DANGER, duration_ms=90)

            self._mini_lyric = tk.Label(frame, text="—", fg=ACCENT, bg=BG2,
                                        font=self._f(FS_TITLE, True), anchor="w",
                                        wraplength=520, justify="left")
            self._mini_lyric.pack(fill="x", padx=SP_MD, pady=(0, SP_SM))

            # Drag anywhere on the strip to move it.
            for w_ in (frame, top, self._mini_lyric, self._mini_track):
                w_.bind("<ButtonPress-1>", lambda e: (
                    setattr(self, "_mox", e.x_root - m.winfo_x()),
                    setattr(self, "_moy", e.y_root - m.winfo_y())))
                w_.bind("<B1-Motion>", lambda e: m.geometry(
                    f"+{e.x_root - self._mox}+{e.y_root - self._moy}"))

            self._mini = m
            self._refresh_mini()
            log("Mini mode on  ·  Ctrl+M to close")
        except tk.TclError as e:
            self._mini = None
            log(f"Mini mode failed: {e}")

    def _close_mini(self):
        m = getattr(self, "_mini", None)
        if m is None:
            return
        try:
            _cfg_set("window", "mini_geometry", m.geometry())
            m.destroy()
        except (tk.TclError, ValueError):
            pass
        self._mini = None
        self._cancel("mini")
        log("Mini mode off")

    def _refresh_mini(self):
        """Mirror the current lyric into the mini strip."""
        m = getattr(self, "_mini", None)
        if m is None:
            return
        try:
            self._mini_lyric.config(text=self.lbl_lyric.cget("text") or "—")
            artist = getattr(state, "artist", ""); title = getattr(state, "title", "")
            self._mini_track.config(text=f"{artist} — {title}" if title else "Waiting for Spotify…")
        except (AttributeError, tk.TclError):
            pass

    # ── System tray (#11, #12) ────────────────────────────────────
    def _tray_start(self):
        """Create the tray icon, if pystray is available.

        Statusify uses overrideredirect(True), which strips the OS window
        frame AND the taskbar button — hence the hand-rolled Win32 in
        _fix_taskbar/_ensure_taskbar that re-applies WS_EX_APPWINDOW on every
        focus change. A tray icon is the idiomatic home for a background
        presence app and gives a reliable way back to the window that doesn't
        depend on any of that."""
        self._tray = None
        if not TRAY_AVAILABLE:
            log("Tray unavailable (pystray/Pillow not installed) — window-only mode")
            return
        try:
            image = None
            try:
                image = Image.open(_ensure_icon_path())
            except Exception:
                image = Image.new("RGB", (64, 64), ACCENT)

            def _do(fn):
                # pystray callbacks run on the tray's own thread; every Tk
                # call must be marshalled back to the main loop.
                return lambda *_: self._root.after(0, fn)

            menu = pystray.Menu(
                pystray.MenuItem("Show Statusify", _do(self._tray_show), default=True),
                pystray.MenuItem("Hide to tray",   _do(self._hide_to_tray)),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Mini mode",          _do(self._toggle_mini)),
                pystray.MenuItem("Always on top",      _do(self._toggle_topmost)),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Toggle Discord RPC", _do(self._tray_toggle_rpc)),
                pystray.MenuItem("Reconnect RPC",      _do(self._reconnect_rpc)),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit", _do(self._quit)),
            )
            self._tray = pystray.Icon("Statusify", image, "Statusify", menu)
            threading.Thread(target=self._tray.run, name="tray", daemon=True).start()
            log("Tray icon started")
        except Exception as e:
            self._tray = None
            log(f"Tray icon failed: {e}")

    def _tray_stop(self):
        tray = getattr(self, "_tray", None)
        if tray is not None:
            try:
                tray.stop()
            except Exception:
                pass
            self._tray = None

    def _tray_show(self):
        try:
            self._root.deiconify()
            self._ensure_taskbar()
            self._root.lift()
            self._root.focus_force()
            self._hidden = False
        except Exception as e:
            log(f"Tray show failed: {e}")

    def _watch_show_request(self):
        """Restore the window when another launch asks us to.

        Double-clicking Statusify.exe while a copy is already running used to
        hit the single-instance guard and do nothing but show an 'already
        running' box. If that instance was hidden in the tray, the app was
        effectively unopenable — you had to hunt for the tray icon or kill the
        process. The second launch now drops a sentinel file and exits; this
        picks it up and brings the window back."""
        try:
            if os.path.exists(_SHOW_FLAG):
                try:
                    os.remove(_SHOW_FLAG)
                except OSError:
                    pass
                log("Second launch detected — restoring window")
                self._tray_show()
        except OSError:
            pass
        self._schedule("showwatch", 1000, self._watch_show_request)

    def _hide_to_tray(self):
        """Withdraw the window but keep the backend and RPC running."""
        self._save_geometry()   # capture position before it becomes unreadable
        if not getattr(self, "_tray", None):
            # No tray icon means no way to get the window back — minimise
            # instead of withdrawing, or the app becomes unreachable.
            self._minimize()
            return
        try:
            self._root.withdraw()
            self._hidden = True
        except Exception as e:
            log(f"Hide to tray failed: {e}")

    def _tray_toggle_rpc(self):
        _hotkey_toggle(self)

    # ── RPC controls ──────────────────────────────────────────────
    def _reconnect_rpc(self, _e=None):
        """Force the backend to drop the current pipe and reconnect.

        Discord being restarted, or a game grabbing the IPC pipe, leaves the
        presence dead until the next automatic retry. There was no way to
        trigger that from the UI — you had to restart the whole app."""
        rpc = _ACTIVE_RPC.get("rpc")
        if rpc is None:
            log("Reconnect: no active RPC connection to reset")
            self._set_error("No Discord connection to reset")
            return
        try:
            rpc._connected = False
            if rpc.pipe:
                rpc.pipe.close()
            log("Reconnect requested — backend will re-handshake shortly")
            self._set_error("")
        except OSError as e:
            log(f"Reconnect failed: {e}")

    def _test_presence(self, _e=None):
        """Push a dummy activity so Discord can be verified without a song."""
        rpc = _ACTIVE_RPC.get("rpc")
        if rpc is None or not rpc._connected:
            log("Test presence: not connected to Discord")
            self._set_error("Not connected to Discord — cannot send test")
            return
        try:
            rpc._enqueue_send({
                "cmd": "SET_ACTIVITY",
                "args": {"pid": os.getpid(), "activity": rpc._activity(
                    "Statusify test", "If you can see this, RPC works",
                    ["✓ Test presence"], "")},
                "nonce": rpc._nxt(),
            })
            log("Test presence sent — check your Discord profile")
        except Exception as e:
            log(f"Test presence failed: {e}")

    def _toggle_rpc_btn(self, _e=None):
        _hotkey_toggle(self)
        self._paint_rpc_btn()

    def _paint_rpc_btn(self):
        btn = getattr(self, "_rpc_btn", None)
        if btn is None:
            return
        try:
            btn.config(text="RPC ON" if _rpc_enabled else "RPC OFF",
                       bg=ACCENT if _rpc_enabled else BG3,
                       fg=ACCENT_FG if _rpc_enabled else MUTED)
        except tk.TclError:
            pass

    def _on_close_button(self):
        """Window close (X). Honours the close_to_tray preference.

        Statusify's whole job is to run quietly while you listen, so the
        default is to hide rather than exit — closing the window should not
        silently drop your Discord presence."""
        if CLOSE_TO_TRAY and getattr(self, "_tray", None):
            self._hide_to_tray()
            log("Hidden to tray — right-click the tray icon to quit")
        else:
            self._quit()

    def _quit(self):
        # Stop every registered timer FIRST. Otherwise a queued after()
        # callback can fire against a partially destroyed widget tree during
        # teardown and raise from inside Tk's event loop.
        try:
            self._cancel_all_timers()
        except Exception:
            pass
        try:
            self._save_geometry()
        except Exception:
            pass
        try:
            self._tray_stop()
        except Exception:
            pass
        _persist_history()
        # Clear Discord RPC cleanly before exit
        try:
            state.is_playing = False
        except Exception:
            pass
        # Gracefully close the WebSocket server so port 8765 is released NOW.
        # Otherwise the port can linger and the next launch hits bind-error
        # 10048 ("can't be opened again"). Schedule the close on the backend
        # loop (where the server lives) and let it drain briefly.
        global _WS_SERVER, _backend_loop
        if _WS_SERVER is not None and _backend_loop is not None:
            try:
                fut = asyncio.run_coroutine_threadsafe(
                    _WS_SERVER.close(), _backend_loop)
                fut.result(timeout=2.0)
            except Exception as e:
                log(f"WS close on exit: {e}")
        # Destroy any open Toplevel windows (e.g. lyrics popup)
        for w in self._root.winfo_children():
            try:
                if isinstance(w, tk.Toplevel):
                    w.destroy()
            except Exception:
                pass
        self._root.destroy()
        _teardown_and_exit()

    def _minimize(self):
        # Minimize at Win32 level so overrideredirect(True) is never touched.
        try:
            user32  = ctypes.windll.user32
            SW_MINIMIZE = 6
            hwnd = user32.GetParent(self._root.winfo_id()) or self._root.winfo_id()
            user32.ShowWindow(hwnd, SW_MINIMIZE)
        except Exception as e:
            log(f"Minimize failed: {e}")
            self._root.iconify()

    def _add_resize_handles(self):
        """Add resize grips to all edges/corners (overrideredirect removes OS ones)."""
        W = self._root
        sz = 6

        EDGE_MAP = {
            "se": ("bottom_right_corner", dict(relx=1.0, rely=1.0, anchor="se", width=sz*2, height=sz*2)),
            "sw": ("bottom_left_corner",  dict(relx=0.0, rely=1.0, anchor="sw", width=sz*2, height=sz*2)),
            "ne": ("top_right_corner",    dict(relx=1.0, rely=0.0, anchor="ne", width=sz*2, height=sz*2)),
            "nw": ("top_left_corner",     dict(relx=0.0, rely=0.0, anchor="nw", width=sz*2, height=sz*2)),
            "e":  ("right_side",          dict(relx=1.0, rely=0.0, anchor="ne", width=sz,    relheight=1.0)),
            "w":  ("left_side",           dict(relx=0.0, rely=0.0, anchor="nw", width=sz,    relheight=1.0)),
            "s":  ("bottom_side",         dict(relx=0.0, rely=1.0, anchor="sw", relwidth=1.0, height=sz)),
            "n":  ("top_side",            dict(relx=0.0, rely=0.0, anchor="nw", relwidth=1.0, height=sz)),
        }

        def _make_outline(x, y, w, h):
            """Create a dotted-border overlay showing the target resize dimensions."""
            ov = tk.Toplevel(W)
            ov.overrideredirect(True)
            ov.attributes("-topmost", True)
            ov.attributes("-transparentcolor", "#010101")
            ov.configure(bg="#010101")
            ov.geometry(f"{w}x{h}+{x}+{y}")
            # Draw dotted border using a Canvas
            cv = tk.Canvas(ov, bg="#010101", highlightthickness=0,
                           width=w, height=h)
            cv.pack(fill="both", expand=True)
            dash = (4, 4)
            cv.create_rectangle(2, 2, w-2, h-2,
                                 outline=ACCENT, width=2, dash=dash, tags="border")
            cv.create_text(w//2, h//2, text=f"{w} × {h}",
                           fill=ACCENT, font=("Segoe UI", 9), tags="label")
            return ov, cv

        def _start(e, edge):
            W._re  = edge
            W._rx  = e.x_root
            W._ry  = e.y_root
            W._rx0 = W.winfo_x()
            W._ry0 = W.winfo_y()
            W._rw  = W.winfo_width()
            W._rh  = W.winfo_height()
            W._resize_pending = None
            ov, cv = _make_outline(W._rx0, W._ry0, W._rw, W._rh)
            W._resize_ov = ov
            W._resize_cv = cv

        def _drag(e):
            edge = getattr(W, "_re", None)
            if not edge: return
            dx = e.x_root - W._rx
            dy = e.y_root - W._ry
            x, y, w, h = W._rx0, W._ry0, W._rw, W._rh
            if "e" in edge: w = max(420, w + dx)
            if "s" in edge: h = max(520, h + dy)
            if "w" in edge:
                nw = max(420, w - dx); x = W._rx0 + (W._rw - nw); w = nw
            if "n" in edge:
                nh = max(520, h - dy); y = W._ry0 + (W._rh - nh); h = nh
            W._resize_pending = (x, y, w, h)
            # Update outline — move/resize existing items, no delete/recreate
            try:
                ov = W._resize_ov
                cv = W._resize_cv
                ov.geometry(f"{w}x{h}+{x}+{y}")
                cv.config(width=w, height=h)
                cv.coords("border", 2, 2, w-2, h-2)
                cv.itemconfig("label", text=f"{w} × {h}")
                cv.coords("label", w//2, h//2)
            except Exception:
                pass

        def _stop(e):
            W._re = None
            try:
                W._resize_ov.destroy()
                del W._resize_ov, W._resize_cv
            except Exception:
                pass
            pending = getattr(W, "_resize_pending", None)
            if pending:
                x, y, w, h = pending
                W.geometry(f"{w}x{h}+{x}+{y}")
                W._resize_pending = None

        for edge, (cursor, kw) in EDGE_MAP.items():
            f = tk.Frame(W, bg=BG, cursor=cursor)
            f.place(**kw)
            f.bind("<ButtonPress-1>",   lambda e, ed=edge: _start(e, ed))
            f.bind("<B1-Motion>",       _drag)
            f.bind("<ButtonRelease-1>", _stop)
            f.lift()

    def mainloop(self):
        self._root.mainloop()

    def _f(self, size, bold=False):
        """Return a cached Font for (size, bold).

        This used to construct a brand-new tkfont.Font on every call. Each one
        is a real Tcl object that lives until the interpreter dies, and the UI
        calls _f() several hundred times — four per history row alone, so 60
        rendered rows meant 240 redundant font objects. Caching collapses the
        whole app onto roughly a dozen.

        Bump +1 reduces aliasing, but enforce a floor so tiny requests
        (the old _f(7) → size 8, _f(8) → size 9) produce legible text
        instead of pixelated dots. Minimum readable size on any DPI is ~10pt."""
        effective = max(int(size) + 1, 10)
        key = (effective, bool(bold))
        cache = self.__dict__.setdefault("_font_cache", {})
        f = cache.get(key)
        if f is None:
            f = tkfont.Font(family="Segoe UI", size=effective,
                            weight="bold" if bold else "normal")
            cache[key] = f
        return f

    # ── Animation engine ──────────────────────────────────────────
    # Everything in this UI changed state by snapping: tabs jumped, the
    # underline teleported, hovers flipped colour in one frame. These drive
    # short eased tweens on the Tk loop instead. Each tween owns a named
    # _schedule slot, so re-triggering one (spamming hover, clicking tabs
    # fast) replaces its frames rather than stacking a second chain.

    ANIM_FPS = 60

    @staticmethod
    def _ease_out_cubic(t):
        """Fast start, gentle settle. The default for anything that moves."""
        return 1.0 - (1.0 - t) ** 3

    @staticmethod
    def _ease_in_out_sine(t):
        import math
        return -(math.cos(math.pi * t) - 1.0) / 2.0

    def _animate(self, key, duration_ms, apply_fn, ease=None):
        """Call apply_fn(eased_t) each frame for duration_ms, ending at 1.0.

        apply_fn is wrapped so a TclError from a widget destroyed mid-tween
        (theme rebuild, history row trimmed, window closing) quietly ends the
        animation instead of raising on the Tk loop."""
        if not getattr(self, "_alive", False):
            return
        if duration_ms <= 0 or not ANIMATIONS_ENABLED:
            try:
                apply_fn(1.0)
            except (tk.TclError, AttributeError):
                pass
            return
        ease = ease or self._ease_out_cubic
        frame_ms = max(1, int(1000 / self.ANIM_FPS))
        start = time.monotonic()

        def _step():
            elapsed = (time.monotonic() - start) * 1000.0
            t = min(1.0, elapsed / duration_ms)
            try:
                apply_fn(ease(t))
            except (tk.TclError, AttributeError):
                return          # widget went away — stop, don't re-arm
            if t < 1.0:
                self._schedule(key, frame_ms, _step)

        _step()

    @staticmethod
    def _resolve_color(v):
        """Accept either a literal '#rrggbb' or a zero-arg callable returning one.

        Callables matter because the palette globals are *rebound* by
        _apply_palette on every theme or accent change. A colour captured when
        the widget was built is the dark-mode value forever; the original
        inline `lambda e: w.config(fg=ACCENT)` handlers read the global at
        event time and so followed the theme for free. Anything that stores a
        colour for later use has to defer the lookup the same way."""
        return v() if callable(v) else v

    def _fade_colors(self, key, widget, duration_ms=110, **targets):
        """Tween widget options (fg=…, bg=…) from their current value to a target.

        Used for hover feedback. Reads the widget's live colour as the start
        point, so interrupting a fade half-way continues from where it is
        rather than snapping back to the nominal resting colour."""
        try:
            targets = {o: self._resolve_color(v) for o, v in targets.items()}
            starts = {opt: widget.cget(opt) for opt in targets}
        except tk.TclError:
            return
        # Nothing to do if every channel is already at its target.
        if all(starts[o] == targets[o] for o in targets):
            return

        def _apply(t):
            widget.config(**{o: _blend(starts[o], targets[o], t) for o in targets})

        self._animate(key, duration_ms, _apply)

    def _hoverable(self, widget, fg=None, hover_fg=None, bg=None, hover_bg=None,
                   duration_ms=110):
        """Bind an animated hover to `widget`, returning it for chaining.

        Pass the resting and hovered colours; omitted pairs are left alone.
        Colours should be given as callables (`lambda: ACCENT`) so they track
        theme changes — see _resolve_color.

        Each widget gets its own animation slot keyed by its Tk path name, so
        two widgets can fade simultaneously without cancelling each other."""
        key = f"hover:{widget}"
        enter, leave = {}, {}
        if hover_fg is not None:
            enter["fg"] = hover_fg
            leave["fg"] = fg if fg is not None else widget.cget("fg")
        if hover_bg is not None:
            enter["bg"] = hover_bg
            leave["bg"] = bg if bg is not None else widget.cget("bg")
        if not enter:
            return widget
        widget.bind("<Enter>", lambda _e: self._fade_colors(key, widget, duration_ms, **enter))
        widget.bind("<Leave>", lambda _e: self._fade_colors(key, widget, duration_ms, **leave))
        return widget

    @staticmethod
    def _focus_ring(entry):
        """Give an Entry a resting border that turns accent-coloured on focus.

        Every text field in the app was relief='flat' with no border at all,
        so an input was indistinguishable from a slightly-different-coloured
        rectangle, and a focused one was indistinguishable from an unfocused
        one — you could only find the caret by typing. Tk draws the highlight
        ring itself, so this needs no bindings and cannot desynchronise from
        the real focus state."""
        try:
            entry.config(highlightthickness=1, highlightbackground=BORDER,
                         highlightcolor=ACCENT, bd=0)
        except tk.TclError:
            pass
        return entry

    def _hover_surface(self, container, base_bg, hover_bg, duration_ms=120):
        """Fade every widget in `container` currently painted base_bg → hover_bg
        while the pointer is anywhere inside the container.

        Tk makes this less trivial than it looks. Moving the pointer from a
        Frame onto its own child fires <Leave> on the Frame *before* <Enter>
        on the child, so the naive two-binding version un-highlights and
        re-highlights on every internal boundary — a row full of labels
        strobes as you cross it. So: bind both events on every descendant,
        and have <Leave> confirm the pointer really has left the subtree
        before fading back out.

        base_bg/hover_bg may be callables, resolved per event so the hover
        follows a later theme change (see _resolve_color). Which widgets take
        part, though, is decided once here — membership is a fact about the
        row's structure, not about the current palette."""
        base_now = self._resolve_color(base_bg)
        targets = []

        def _collect(w):
            try:
                if w.cget("bg") == base_now:
                    targets.append(w)
            except tk.TclError:
                pass
            for c in w.winfo_children():
                _collect(c)

        _collect(container)
        if not targets:
            return

        key = f"surface:{container}"

        def _still_inside():
            try:
                w = self.win.winfo_containing(self.win.winfo_pointerx(),
                                              self.win.winfo_pointery())
            except (tk.TclError, KeyError):
                return False
            while w is not None:
                if w is container:
                    return True
                w = getattr(w, "master", None)
            return False

        def _fade(to_spec):
            to = self._resolve_color(to_spec)
            try:
                frm = targets[0].cget("bg")
            except (IndexError, tk.TclError):
                return
            if frm == to:
                return

            def _apply(t):
                col = _blend(frm, to, t)
                for w in targets:
                    try:
                        w.config(bg=col)
                    except tk.TclError:
                        pass

            self._animate(key, duration_ms, _apply)

        def _enter(_e):
            _fade(hover_bg)

        def _leave(_e):
            if not _still_inside():
                _fade(base_bg)

        def _bind(w):
            w.bind("<Enter>", _enter, add="+")
            w.bind("<Leave>", _leave, add="+")
            for c in w.winfo_children():
                _bind(c)

        _bind(container)

    def _build(self):
        W = self.win
        # ── Custom title bar ──────────────────────────────────────
        bar = tk.Frame(W, bg=BG, height=42); bar.pack(fill="x"); bar.pack_propagate(False)
        bar.bind("<ButtonPress-1>",
            lambda e: (setattr(self,"_ox",e.x_root-W.winfo_x()), setattr(self,"_oy",e.y_root-W.winfo_y())))
        bar.bind("<B1-Motion>",
            lambda e: W.geometry(f"+{e.x_root-self._ox}+{e.y_root-self._oy}"))

        # Logo in titlebar — replaces the ♪ text symbol
        try:
            from io import BytesIO
            _logo_img = Image.open(BytesIO(base64.b64decode(_ICON_B64))).resize((20,20), Image.LANCZOS)
            self._logo_photo = ImageTk.PhotoImage(_logo_img)
            tk.Label(bar, image=self._logo_photo, bg=BG, bd=0).pack(side="left", padx=(14,6))
        except Exception:
            tk.Label(bar, text="♪", fg=ACCENT, bg=BG, font=self._f(11)).pack(side="left", padx=(14,5))

        tk.Label(bar, text="STATUSIFY", fg=TEXT2, bg=BG, font=self._f(8,True)).pack(side="left")

        # hov is a callable, not a colour: the palette globals get rebound on
        # every theme change, so a value captured here would freeze.
        for txt, cmd, hov in [("✕", self._on_close_button, lambda: DANGER),
                              ("—", self._minimize,        lambda: TEXT)]:
            b = tk.Label(bar, text=txt, fg=MUTED, bg=BG, font=self._f(10),
                         cursor="hand2", padx=SP_MD, pady=SP_XS + 2)
            b.pack(side="right")
            b.bind("<Button-1>", lambda e, c=cmd: c())
            self._hoverable(b, fg=lambda: MUTED, hover_fg=hov, duration_ms=90)

        # ── Tab bar ───────────────────────────────────────────────
        tabs = tk.Frame(W, bg=BG2, height=36); tabs.pack(fill="x"); tabs.pack_propagate(False)
        self._tab_btns = {}
        for name in ("NOW PLAYING", "HISTORY", "SETTINGS"):
            b = tk.Label(tabs, text=name, fg=MUTED, bg=BG2,
                         font=self._f(FS_SMALL, True), cursor="hand2",
                         padx=SP_LG, pady=SP_SM + 2)
            b.pack(side="left")
            b.bind("<Button-1>", lambda e, n=name: self._show(n))
            # Inactive tabs had no hover feedback at all, so there was nothing
            # to tell you they were clickable.
            # Only the inactive tabs respond — the active one already owns
            # ACCENT and must not be dragged off it by a stray hover.
            b.bind("<Enter>", lambda e, w=b, n=name:
                   None if self._cur_page == n
                   else self._fade_colors(f"tabfg:{n}", w, 110, fg=TEXT2))
            b.bind("<Leave>", lambda e, w=b, n=name:
                   None if self._cur_page == n
                   else self._fade_colors(f"tabfg:{n}", w, 110, fg=MUTED))
            self._tab_btns[name] = b

        # Thin accent line under active tab
        self._tab_line = tk.Frame(W, bg=ACCENT, height=2)
        self._tab_line.place(x=0, y=78, width=112)  # approx width of "NOW PLAYING"

        # ── Page container ────────────────────────────────────────
        self._container = tk.Frame(W, bg=BG); self._container.pack(fill="both", expand=True)
        self._build_now_playing()
        self._build_history()
        self._build_settings()
        self._show("NOW PLAYING")

    # ── NOW PLAYING ───────────────────────────────────────────────
    def _build_now_playing(self):
        p = tk.Frame(self._container, bg=BG); self._pages["NOW PLAYING"] = p

        # ── Hero card: large album art + title/artist/info ───────────────
        # Every pad on this page is one of SP_XS…SP_XL. It previously mixed
        # 14/12/10/6/4/2 more or less at random, which is why nothing on the
        # page shared a left edge or a rhythm.
        card = tk.Frame(p, bg=BG2); card.pack(fill="x", padx=SP_LG, pady=(SP_MD, SP_XS))
        self.canvas = tk.Canvas(card, width=HERO_ART_PX, height=HERO_ART_PX,
                                bg=BG2, highlightthickness=0)
        self.canvas.pack(side="left", padx=SP_LG, pady=SP_LG); self._default_art()

        inf = tk.Frame(card, bg=BG2)
        inf.pack(side="left", fill="both", expand=True, padx=(0, SP_LG))
        # Centre the text block against the 120 px artwork instead of pushing
        # it down with a magic 18 px top pad — that only lined up at one font
        # size and drifted the moment the title wrapped to two lines.
        inf.pack_propagate(False)
        spacer_top = tk.Frame(inf, bg=BG2); spacer_top.pack(fill="both", expand=True)
        self.lbl_title  = tk.Label(inf, text="Waiting for Spotify...", fg=TEXT, bg=BG2,
                                   font=self._f(FS_HERO, True), anchor="w",
                                   wraplength=300, justify="left")
        self.lbl_title.pack(fill="x")
        self.lbl_artist = tk.Label(inf, text="", fg=TEXT2, bg=BG2,
                                   font=self._f(FS_LARGE), anchor="w")
        self.lbl_artist.pack(fill="x", pady=(SP_XS // 2, 0))
        self.lbl_info   = tk.Label(inf, text="", fg=MUTED, bg=BG2,
                                   font=self._f(FS_SMALL), anchor="w")
        self.lbl_info.pack(fill="x", pady=(SP_XS, 0))
        tk.Frame(inf, bg=BG2).pack(fill="both", expand=True)

        # ── Progress bar: track + fill, with elapsed / total times ───────
        # Sits directly under the hero card and shares its horizontal inset,
        # so the bar reads as belonging to the track above it.
        prog_outer = tk.Frame(p, bg=BG)
        prog_outer.pack(fill="x", padx=SP_LG, pady=(SP_SM, 0))
        self._prog_cv = tk.Canvas(prog_outer, height=self.PROG_H, bg=BG,
                                  highlightthickness=0)
        self._prog_cv.pack(fill="x")
        # NOTE: still no <Configure> binding here — see _redraw_progress.
        times = tk.Frame(prog_outer, bg=BG); times.pack(fill="x", pady=(SP_XS, 0))
        self._prog_elapsed = tk.Label(times, text="0:00", fg=MUTED, bg=BG,
                                      font=self._f(FS_MICRO), anchor="w")
        self._prog_elapsed.pack(side="left")
        self._prog_total   = tk.Label(times, text="--:--", fg=MUTED, bg=BG,
                                      font=self._f(FS_MICRO), anchor="e")
        self._prog_total.pack(side="right")
        # Draw the empty bar once; _tick_progress / _redraw_progress keep it current.
        self._redraw_progress()

        lbox = tk.Frame(p, bg=BG3); lbox.pack(fill="x", padx=SP_LG, pady=(SP_SM, SP_XS))
        tk.Label(lbox, text="NOW ON DISCORD", fg=MUTED, bg=BG3,
                 font=self._f(FS_MICRO, True)).pack(anchor="w", padx=SP_LG,
                                                    pady=(SP_MD, 0))
        self.lbl_lyric = tk.Label(lbox, text="—", fg=MUTED, bg=BG3,
                                  font=self._f(FS_TITLE + LYRIC_FONT_BOOST, True),
                                  wraplength=430, justify="left", anchor="w",
                                  pady=SP_SM + 2)
        self.lbl_lyric.pack(anchor="w", fill="x", padx=SP_LG, pady=(0, SP_MD))


        # ── Lyric delay control ───────────────────────────────────────
        delay_outer = tk.Frame(p, bg=BG2); delay_outer.pack(fill="x", padx=14, pady=(2,2))

        # Header row: label + RESET
        delay_header = tk.Frame(delay_outer, bg=BG2)
        delay_header.pack(fill="x", padx=12, pady=(8,4))
        tk.Label(delay_header, text="LYRIC DELAY", fg=MUTED, bg=BG2,
                 font=self._f(7,True)).pack(side="left")
        rst = tk.Label(delay_header, text="RESET", fg=MUTED, bg=BG2,
                       font=self._f(7), cursor="hand2")
        rst.pack(side="right")
        rst.bind("<Button-1>", lambda e: _reset_delay())
        self._hoverable(rst, fg=lambda: MUTED, hover_fg=lambda: ACCENT)

        # Control row: [hear first label] [−] [value] [+] [see first label]
        delay_ctrl = tk.Frame(delay_outer, bg=BG2)
        delay_ctrl.pack(fill="x", padx=12, pady=(0,8))

        def _btn(parent, txt, cmd):
            b = tk.Label(parent, text=txt, fg=TEXT2, bg=BG3,
                         font=self._f(11,True), cursor="hand2",
                         width=3, anchor="center", pady=1)
            b.pack(side="left", padx=(0,2))
            b.bind("<Button-1>", lambda e: cmd())
            self._hoverable(b, fg=lambda: TEXT2, hover_fg=lambda: ACCENT, bg=lambda: BG3, hover_bg=lambda: ACCENT_SOFT)
            return b

        def _dec_delay():
            global LYRIC_DELAY_MS
            LYRIC_DELAY_MS = max(-5000, LYRIC_DELAY_MS - 100)
            self._update_delay_label()
        def _inc_delay():
            global LYRIC_DELAY_MS
            LYRIC_DELAY_MS = min(5000, LYRIC_DELAY_MS + 100)
            self._update_delay_label()
        def _reset_delay():
            global LYRIC_DELAY_MS
            LYRIC_DELAY_MS = 0
            self._update_delay_label()

        # Left side: hear first
        hear_frame = tk.Frame(delay_ctrl, bg=BG2)
        hear_frame.pack(side="left", fill="y")
        tk.Label(hear_frame, text="◀ hear first", fg=MUTED, bg=BG2,
                 font=self._f(7), anchor="e").pack(side="left", padx=(0,6))
        _btn(delay_ctrl, "−", _dec_delay)

        # Center: value display — initialise from persisted value
        _init_delay_s = LYRIC_DELAY_MS / 1000
        _init_delay_sign = "+" if _init_delay_s > 0 else ""
        _init_delay_text = f"{_init_delay_sign}{_init_delay_s:.1f}s"
        _init_delay_fg   = ACCENT if LYRIC_DELAY_MS != 0 else TEXT
        self.lbl_delay = tk.Label(delay_ctrl, text=_init_delay_text, fg=_init_delay_fg, bg=BG,
                                  font=self._f(11,True), width=6, anchor="center",
                                  relief="flat", padx=4)
        self.lbl_delay.pack(side="left", padx=4)

        # Right side: see first
        _btn(delay_ctrl, "+", _inc_delay)
        tk.Label(delay_ctrl, text="see first ▶", fg=MUTED, bg=BG2,
                 font=self._f(7), anchor="w").pack(side="left", padx=(6,0))

        # ── Quick actions ─────────────────────────────────────────
        # Everything here was previously either hotkey-only or impossible.
        acts = tk.Frame(p, bg=BG); acts.pack(fill="x", padx=SP_LG, pady=(SP_SM, SP_XS))

        def _act(label, cmd, tip=None, accent=False):
            b = tk.Label(acts, text=label,
                         fg=ACCENT_FG if accent else TEXT2,
                         bg=ACCENT if accent else BG3,
                         font=self._f(FS_MICRO, True), cursor="hand2",
                         padx=SP_MD, pady=SP_XS + 1)
            b.pack(side="left", padx=(0, SP_SM))
            b.bind("<Button-1>", lambda e: cmd())
            if not accent:
                # Tint the chip's surface as well as its label. Recolouring
                # only the text left the button's own shape completely inert
                # under the pointer.
                self._hoverable(b, fg=lambda: TEXT2, hover_fg=lambda: ACCENT,
                                bg=lambda: BG3, hover_bg=lambda: ACCENT_SOFT)
            return b

        # RPC and ON TOP are *state* toggles, not plain buttons: their resting
        # colours depend on whether the feature is on. The generic two-colour
        # hover can't express that — on <Leave> it would repaint an enabled
        # toggle in the disabled colour and silently lie about the state. Both
        # therefore get a hover whose rest position is read from the live flag.
        def _stateful_hover(btn, rest_fg, rest_bg, hov_fg, hov_bg):
            key = f"hover:{btn}"
            btn.bind("<Enter>", lambda e: self._fade_colors(
                key, btn, 110, fg=hov_fg(), bg=hov_bg()))
            btn.bind("<Leave>", lambda e: self._fade_colors(
                key, btn, 110, fg=rest_fg(), bg=rest_bg()))

        self._rpc_btn = _act("RPC ON", self._toggle_rpc_btn, accent=True)
        _stateful_hover(
            self._rpc_btn,
            rest_fg=lambda: ACCENT_FG if _rpc_enabled else MUTED,
            rest_bg=lambda: ACCENT if _rpc_enabled else BG3,
            # Lift the accent toward its own foreground when armed, so the
            # primary action finally has some press-me feedback of its own.
            hov_fg=lambda: ACCENT_FG if _rpc_enabled else ACCENT,
            hov_bg=lambda: (_blend(ACCENT, ACCENT_FG, 0.18) if _rpc_enabled
                            else ACCENT_SOFT),
        )
        self._paint_rpc_btn()
        _act("MINI", self._toggle_mini)
        self._top_btn = _act("ON TOP", self._toggle_topmost)
        _stateful_hover(
            self._top_btn,
            rest_fg=lambda: ACCENT if ALWAYS_ON_TOP else MUTED,
            rest_bg=lambda: BG3,
            hov_fg=lambda: ACCENT,
            hov_bg=lambda: ACCENT_SOFT,
        )
        self._paint_topmost_btn()
        _act("COPY", self._copy_current_lyric)

        sb = tk.Frame(p, bg=BG); sb.pack(fill="x", padx=SP_LG, pady=(SP_SM, SP_XS))
        self.dot_sp = tk.Label(sb, text="●", fg=MUTED, bg=BG, font=self._f(FS_MICRO)); self.dot_sp.pack(side="left")
        tk.Label(sb, text=" Spicetify", fg=MUTED, bg=BG, font=self._f(FS_SMALL)).pack(side="left")
        tk.Label(sb, text="   ", bg=BG).pack(side="left")
        self.dot_dc = tk.Label(sb, text="●", fg=MUTED, bg=BG, font=self._f(FS_MICRO)); self.dot_dc.pack(side="left")
        tk.Label(sb, text=" Discord RPC", fg=MUTED, bg=BG, font=self._f(FS_SMALL)).pack(side="left")
        self.lbl_rl = tk.Label(sb, text="", fg=MUTED, bg=BG, font=self._f(FS_SMALL)); self.lbl_rl.pack(side="right")

        # Second status row (#14, #15). The two dots above are binary: when
        # RPC drops you get a grey dot and have to open the log to find out
        # why. These two labels put the reason and the per-song dropped-line
        # count where you can actually see them.
        sb2 = tk.Frame(p, bg=BG); sb2.pack(fill="x", padx=14, pady=(0,4))
        self.lbl_err = tk.Label(sb2, text="", fg=DANGER, bg=BG, font=self._f(7),
                                anchor="w", wraplength=330, justify="left")
        self.lbl_err.pack(side="left", fill="x", expand=True)
        self.lbl_dropped = tk.Label(sb2, text="", fg=MUTED, bg=BG, font=self._f(7))
        self.lbl_dropped.pack(side="right")

        tk.Frame(p, bg=BORDER, height=1).pack(fill="x", padx=14, pady=(2,6))
        tk.Label(p, text="LOG", fg=MUTED, bg=BG, font=self._f(7,True)).pack(anchor="w", padx=14)
        lf = tk.Frame(p, bg=BG2); lf.pack(fill="both", expand=True, padx=14, pady=(3,14))
        self.log_txt = tk.Text(lf, bg=BG2, fg=TEXT2, font=tkfont.Font(family="Consolas", size=8),
                               relief="flat", state="disabled", wrap="word", padx=8, pady=6)
        self.log_txt.pack(fill="both", expand=True)
        for tag, col in [("g",ACCENT),("m",MUTED),("y",WARN),("ts",BORDER)]:
            self.log_txt.tag_config(tag, foreground=col)

    def _update_delay_label(self):
        s = LYRIC_DELAY_MS / 1000
        sign = "+" if s > 0 else ""
        self.lbl_delay.config(
            text=f"{sign}{s:.1f}s",
            fg=ACCENT if LYRIC_DELAY_MS != 0 else TEXT
        )
        _cfg_set("preferences", "lyric_delay_ms", str(LYRIC_DELAY_MS))
        # Tracks with no per-track override resolve to the global delay and
        # cache that value, so changing the global has to drop the cache.
        _invalidate_offset_cache()
        self._refresh_track_offset()
        log(f"Lyric delay set to {sign}{s:.1f}s")

    # ── HISTORY ───────────────────────────────────────────────────
    def _build_history(self):
        p = tk.Frame(self._container, bg=BG); self._pages["HISTORY"] = p

        head = tk.Frame(p, bg=BG); head.pack(fill="x", padx=SP_LG, pady=(SP_MD, SP_SM))
        # Not "SESSION HISTORY" — it is restored from disk and spans sessions.
        tk.Label(head, text="LISTENING HISTORY", fg=MUTED, bg=BG,
                 font=self._f(FS_SMALL, True)).pack(side="left")
        # There was no way to clear history at all — it just accumulated,
        # persisted to disk, and reloaded on every launch.
        clr = tk.Label(head, text="CLEAR", fg=MUTED, bg=BG,
                       font=self._f(FS_MICRO, True), cursor="hand2")
        clr.pack(side="right")
        clr.bind("<Button-1>", lambda e: self._clear_history())
        self._hoverable(clr, fg=lambda: MUTED, hover_fg=lambda: DANGER)

        # Feature 4: search bar
        sf = tk.Frame(p, bg=BG); sf.pack(fill="x", padx=SP_LG, pady=(0, SP_SM))
        self._hist_search = tk.StringVar()
        ent_s = tk.Entry(sf, textvariable=self._hist_search, bg=BG2, fg=TEXT2,
                         insertbackground=TEXT2, relief="flat",
                         font=self._f(FS_BODY), width=30)
        self._focus_ring(ent_s)
        ent_s.pack(fill="x", ipady=SP_XS)
        self._hist_search_entry = ent_s   # so Ctrl+F can focus it
        tk.Label(sf, text="🔍  filter by title, artist, or lyrics   ·   Ctrl+F",
                 fg=MUTED, bg=BG, font=self._f(FS_MICRO)).pack(anchor="w", pady=(2,0))
        self._hist_search.trace_add("write", lambda *_: self._filter_history())

        outer = tk.Frame(p, bg=BG); outer.pack(fill="both", expand=True, padx=14, pady=(0,14))
        self._hist_vsb = tk.Scrollbar(outer, bg=BG3, troughcolor=BG, relief="flat", width=5, bd=0)
        self.hist_cv = tk.Canvas(outer, bg=BG, highlightthickness=0, yscrollcommand=self._hist_vsb.set)
        self.hist_cv.pack(side="left", fill="both", expand=True)
        self._hist_vsb.config(command=self.hist_cv.yview)
        self.hist_frm = tk.Frame(self.hist_cv, bg=BG)
        self._hw = self.hist_cv.create_window((0,0), window=self.hist_frm, anchor="nw")

        def _update_scroll(e=None):
            self.hist_cv.configure(scrollregion=self.hist_cv.bbox("all"))
            # Only show scrollbar and allow scrolling when content overflows
            content_h = self.hist_frm.winfo_reqheight()
            canvas_h  = self.hist_cv.winfo_height()
            if content_h > canvas_h:
                self._hist_vsb.pack(side="right", fill="y")
                self._scroll_enabled = True
            else:
                self._hist_vsb.pack_forget()
                self.hist_cv.yview_moveto(0)
                self._scroll_enabled = False

        self.hist_frm.bind("<Configure>", _update_scroll)
        self.hist_cv.bind("<Configure>",
            lambda e: (self.hist_cv.itemconfig(self._hw, width=e.width), _update_scroll()))

        def _on_mousewheel(e):
            if getattr(self, "_scroll_enabled", False):
                # One notch ≈ 3 text lines' worth of pixels, glided rather
                # than jumped. yview_scroll("units") teleported the canvas by
                # a whole row per notch, which on a list of 44 px thumbnails
                # meant the content visibly disappeared and reappeared
                # somewhere else with nothing linking the two positions.
                self._smooth_scroll(-(e.delta / 120.0) * SCROLL_NOTCH_PX)

        self.hist_cv.bind("<MouseWheel>", _on_mousewheel)

        def _bind_mw(widget):
            widget.bind("<MouseWheel>", _on_mousewheel)
            for child in widget.winfo_children():
                _bind_mw(child)
        self._bind_hist_mw = _bind_mw

        self.no_hist = tk.Label(self.hist_frm, text="Nothing played yet.",
                                fg=MUTED, bg=BG, font=self._f(9))
        self.no_hist.pack(pady=30)
        self._hist_rows = []  # list of (row_widget, entry_dict) for filtering
        self._bind_hist_mw(self.hist_frm)

    def _smooth_scroll(self, delta_px):
        """Glide the history canvas by delta_px using exponential approach.

        Deliberately not a fixed-duration tween: wheel notches arrive in
        bursts, and each one should extend the same glide rather than restart
        a new one. Keeping a target that the view chases means a fast flick
        accumulates into one long smooth travel, and reversing direction
        mid-scroll turns around immediately instead of finishing the old
        animation first."""
        cv = self.hist_cv
        try:
            total = max(1, self.hist_frm.winfo_reqheight())
            view  = max(1, cv.winfo_height())
        except tk.TclError:
            return
        max_frac = max(0.0, 1.0 - view / total)
        cur = cv.yview()[0]
        # Continue from the in-flight target if we're mid-glide, else from
        # wherever the view actually is.
        base = self._scroll_target if getattr(self, "_scroll_active", False) else cur
        target = max(0.0, min(max_frac, base + delta_px / total))
        self._scroll_target = target

        if not ANIMATIONS_ENABLED:
            cv.yview_moveto(target)
            return

        if getattr(self, "_scroll_active", False):
            return          # the chase loop below is already running

        self._scroll_active = True

        def _step():
            try:
                pos = cv.yview()[0]
            except tk.TclError:
                self._scroll_active = False
                return
            diff = self._scroll_target - pos
            if abs(diff) < 0.0008:
                try: cv.yview_moveto(self._scroll_target)
                except tk.TclError: pass
                self._scroll_active = False
                return
            try:
                cv.yview_moveto(pos + diff * 0.25)
            except tk.TclError:
                self._scroll_active = False
                return
            self._schedule("histscroll", 16, _step)

        _step()

    def _render_loaded_history(self):
        """Draw the entries restored from disk by _load_history().

        Nothing ever rendered them before: rows were only created in response
        to a ("history_add",) event, which fires solely for tracks played in
        the current session. So a user with a full history.json opened the
        History tab and was told "Nothing played yet" — while that same file
        was dutifully reloaded and re-persisted on every single launch.

        Only the newest MAX_RENDERED_ROWS get widgets; _trim_history_rows
        would destroy anything older on the spot anyway."""
        if not history:
            return
        for e in history[-MAX_RENDERED_ROWS:]:
            self._add_history_row(e)
        log(f"History restored  ·  {len(history)} tracks")

    def _filter_history(self):
        """Show/hide history rows based on search query."""
        q = self._hist_search.get().lower()
        for row, e in self._hist_rows:
            if not q:
                match = True
            else:
                match = (q in e.get("title", "").lower() or q in e.get("artist", "").lower())
                if not match:
                    if e.get("plain"):
                        match = any(q in ln.lower() for ln in e["plain"])
                    elif e.get("synced"):
                        match = any(q in ln.get("words", "").lower() for ln in e["synced"])
            if match:
                row.pack(fill="x", pady=(0,2))
            else:
                row.pack_forget()

    def _clear_history(self):
        """Wipe session history, its rendered rows, and the on-disk copy."""
        global history
        for row, _ in list(getattr(self, "_hist_rows", [])):
            try:
                row.destroy()
            except Exception:
                pass
        self._hist_rows = []
        history.clear()
        self._close_lyrics_panel()
        try:
            self.no_hist.pack(pady=30)
        except (AttributeError, tk.TclError):
            pass
        try:
            if os.path.exists(_HIST_FILE):
                os.remove(_HIST_FILE)
        except OSError as e:
            log(f"Could not delete history file: {e}")
        log("History cleared")

    def _trim_history_rows(self):
        """Destroy the oldest rendered rows beyond MAX_RENDERED_ROWS.

        MAX_HISTORY_ROWS is 500 and each row is a Frame plus ~5 children plus
        a decoded thumbnail — roughly 3,000 live Tk widgets at cap. Every
        layout pass, every <Configure>, and every _rebuild_all theme change
        walks all of them, so the History tab got progressively heavier the
        longer a session ran. The underlying `history` list is untouched (so
        search, persistence and lyric indices still cover everything) — this
        only bounds how many rows exist as widgets."""
        while len(self._hist_rows) > MAX_RENDERED_ROWS:
            old_row, _ = self._hist_rows.pop(0)
            try:
                old_row.destroy()   # also drops the thumbnail ref on the canvas
            except Exception:
                pass

    def _add_history_row(self, e):
        """Render one history entry. Takes the entry dict, not a list index —
        see _save_history for why indices can't be trusted here."""
        if not e: return

        # Hide the empty-state label once there's something to show.
        if not self._hist_rows:
            self.no_hist.pack_forget()

        row = tk.Frame(self.hist_frm, bg=BG2, cursor="hand2")
        row.pack(fill="x", pady=(0,2))

        # Thumbnail canvas. Its bg is the row colour, not BG3, so the rounded
        # artwork's corners land on the row rather than on a square patch.
        c = tk.Canvas(row, width=THUMB_PX, height=THUMB_PX, bg=BG2,
                      highlightthickness=0)
        c.pack(side="left", padx=SP_MD, pady=SP_MD)
        self._rounded_rect(c, 0, 0, THUMB_PX - 1, THUMB_PX - 1, THUMB_RADIUS,
                           fill=BG3, outline="")
        c.create_text(THUMB_PX // 2, THUMB_PX // 2, text="♫", fill=MUTED,
                      font=self._f(11))
        if PIL_AVAILABLE and e.get("album_art"):
            self.win.after(80, lambda cv=c, u=e["album_art"]: self._load_thumb(cv, u))

        inf = tk.Frame(row, bg=BG2); inf.pack(side="left", fill="both", expand=True)
        tk.Label(inf, text=e["title"],  fg=TEXT,  bg=BG2, font=self._f(9,True),
                 anchor="w", wraplength=260).pack(fill="x", pady=(10,0), padx=(0,6))
        tk.Label(inf, text=e["artist"], fg=TEXT2, bg=BG2, font=self._f(8),
                 anchor="w").pack(fill="x", padx=(0,6))
        src = "Spicy" if e["mode"]=="synced" else ("Plain" if e["mode"]=="plain" else "No lyrics")
        syn_len = len(e["synced"]) if e.get("synced") else 0
        pln_len = len(e["plain"]) if e.get("plain") else 0
        n = syn_len or pln_len
        tk.Label(inf, text=f"{src}  ·  {n} lines  ·  {e['time']}", fg=MUTED, bg=BG2,
                 font=self._f(7), anchor="w").pack(fill="x", pady=(0,10), padx=(0,6))

        if n > 0:
            btn = tk.Label(row, text="LYRICS ›", fg=MUTED, bg=BG2,
                           font=self._f(7,True), cursor="hand2", padx=SP_MD)
            btn.pack(side="right", padx=(0, SP_MD))
            self._hoverable(btn, fg=lambda: MUTED, hover_fg=lambda: ACCENT)
            # The row has always had cursor="hand2" across its whole width,
            # which promised a click target that only the small LYRICS label
            # actually honoured. Make the whole row do what it looks like it
            # does. (Bound before _hover_surface so the hover bindings, which
            # use add="+", don't have to care about ordering.)
            for _w in (row, inf, *inf.winfo_children()):
                _w.bind("<Button-1>", lambda ev, ent=e: self._show_lyrics(ent))
            c.bind("<Button-1>", lambda ev, ent=e: self._show_lyrics(ent))
            btn.bind("<Button-1>", lambda ev, ent=e: self._show_lyrics(ent))
        else:
            row.config(cursor="")   # nothing to open — don't advertise a click

        # Whole-row hover tint, so the pointer position is always legible in
        # a long list of visually identical rows.
        self._hover_surface(row, lambda: BG2, lambda: HOVER_BG)

        # Bind mousewheel on every widget in this row so scrolling works
        # regardless of which child the cursor is over
        if hasattr(self, "_bind_hist_mw"):
            self._bind_hist_mw(row)

        # Feature 4: register row for search filtering
        if hasattr(self, "_hist_rows"):
            self._hist_rows.append((row, e))
            self._trim_history_rows()

    def _load_thumb(self, canvas, url):
        # Fetch on a worker thread — never block the Tk main loop on network I/O.
        surface = BG2   # the history row behind the thumbnail
        def _fetch():
            return _round_image(_fetch_art(url, THUMB_PX), THUMB_RADIUS, surface)
        def _apply(result, cv=canvas):
            if result is None: return
            try:
                photo = ImageTk.PhotoImage(result)
                # Park the reference on the canvas itself rather than in a
                # module-level list. Tk needs *a* live reference or it GCs the
                # image; attaching it to the widget means the reference dies
                # with the widget instead of leaking for the whole session
                # (self._hist_imgs only ever grew, never shrank).
                cv._statusify_photo = photo
                cv.delete("all")
                cv.create_image(0,0, anchor="nw", image=photo)
            except Exception:
                pass
        fut = image_executor.submit(_fetch)
        def _done(f):
            try: res = f.result()
            except Exception: res = None
            # PhotoImage MUST be created on the Tk thread.
            self.win.after(0, lambda: _apply(res))
        fut.add_done_callback(_done)

    def _show_lyrics(self, e):
        if not e: return

        # Close any existing lyrics panel first
        self._close_lyrics_panel()

        # Build an overlay Frame that sits on top of the main window content
        panel = tk.Frame(self.win, bg=BG, bd=1, relief="flat",
                         highlightbackground=BORDER, highlightthickness=1)
        panel.place(relx=0.5, rely=0.5, anchor="center", width=460, height=560)
        panel.lift()
        self._lyrics_panel = panel

        # Title bar
        pbar = tk.Frame(panel, bg=BG2, height=40)
        pbar.pack(fill="x"); pbar.pack_propagate(False)

        title_str = f"{e['title']} — {e['artist']}"
        tk.Label(pbar, text=title_str[:55] + ("…" if len(title_str)>55 else ""),
                 fg=TEXT, bg=BG2, font=self._f(9, True), anchor="w").pack(
                 side="left", padx=14, fill="x", expand=True)

        cb = tk.Label(pbar, text="✕", fg=MUTED, bg=BG2, font=self._f(10),
                      cursor="hand2", padx=10)
        cb.pack(side="right")
        cb.bind("<Button-1>", lambda ev: self._close_lyrics_panel())
        self._hoverable(cb, fg=lambda: MUTED, hover_fg=lambda: DANGER, duration_ms=90)

        # Lyrics search bar
        sbar = tk.Frame(panel, bg=BG)
        sbar.pack(fill="x", padx=14, pady=(10, 0))
        tk.Label(sbar, text="🔍", fg=MUTED, bg=BG, font=self._f(9)).pack(side="left")
        search_var = tk.StringVar()
        ent_search = tk.Entry(sbar, textvariable=search_var, bg=BG2, fg=TEXT, insertbackground=TEXT,
                              relief="flat", font=self._f(9))
        self._focus_ring(ent_search)
        ent_search.pack(side="left", fill="x", expand=True, padx=(SP_SM,0), ipady=3)

        # Lyrics content
        frm = tk.Frame(panel, bg=BG)
        frm.pack(fill="both", expand=True, padx=14, pady=10)
        vsb = tk.Scrollbar(frm, command=lambda *a: txt.yview(*a),
                           bg=BG2, troughcolor=BG, relief="flat", width=5, bd=0)
        vsb.pack(side="right", fill="y")
        txt = tk.Text(frm, bg=BG, fg=TEXT2, font=self._f(10), relief="flat",
                      wrap="word", padx=10, pady=8, yscrollcommand=vsb.set)
        txt.pack(side="left", fill="both", expand=True)
        txt.bind("<MouseWheel>", lambda ev: txt.yview_scroll(int(-1*(ev.delta/120)), "units"))
        txt.tag_config("line", foreground=TEXT2, spacing1=3, spacing3=3)
        txt.tag_config("ts",   foreground=MUTED)
        txt.tag_config("highlight", background=ACCENT, foreground=ACCENT_FG)
        # Active line = the lyric currently being sung. Coloured rather than
        # background-filled so it reads differently from a search hit.
        txt.tag_config("active", foreground=ACCENT)

        # Remember what this panel is showing so _highlight_active_lyric can
        # follow along while the song plays.
        self._lyrics_txt   = txt
        self._lyrics_entry = e

        if e["mode"] == "synced" and e["synced"]:
            for ln in e["synced"]:
                ms = ln["startMs"]; mins, secs = divmod(ms//1000, 60)
                txt.insert("end", f"{mins}:{secs:02d}  ", "ts")
                txt.insert("end", ln["words"]+"\n", "line")
        elif e["plain"]:
            for ln in e["plain"]:
                txt.insert("end", ln+"\n", "line")
        else:
            txt.insert("end", "No lyrics available for this song.", "ts")

        txt.config(state="disabled")

        # Handle highlighting on search
        def _on_search(*args):
            q = search_var.get().lower()
            txt.tag_remove("highlight", "1.0", "end")
            if not q: return
            
            idx = "1.0"
            first_match = None
            while True:
                idx = txt.search(q, idx, nocase=True, stopindex="end")
                if not idx: break
                
                if not first_match: first_match = idx
                length = len(q)
                end_idx = f"{idx}+{length}c"
                txt.tag_add("highlight", idx, end_idx)
                idx = end_idx
                
            if first_match:
                txt.see(first_match)

        search_var.trace_add("write", _on_search)

        # ── Footer: export / copy ─────────────────────────────────
        foot = tk.Frame(panel, bg=BG)
        foot.pack(fill="x", padx=SP_LG, pady=(0, SP_MD))

        def _mkbtn(parent, label, cmd, accent=False):
            b = tk.Label(parent, text=label,
                         fg=ACCENT_FG if accent else TEXT2,
                         bg=ACCENT if accent else BG3,
                         font=self._f(FS_MICRO, True), cursor="hand2",
                         padx=SP_MD, pady=SP_XS + 1)
            b.pack(side="left", padx=(0, SP_SM))
            b.bind("<Button-1>", lambda ev: cmd())
            if not accent:
                self._hoverable(b, fg=lambda: TEXT2, hover_fg=lambda: ACCENT,
                                bg=lambda: BG3, hover_bg=lambda: ACCENT_SOFT)
            return b

        has_synced = bool(e.get("synced"))
        if has_synced:
            _mkbtn(foot, "EXPORT .LRC", lambda: self._do_export(e, "lrc"), accent=True)
        _mkbtn(foot, "EXPORT .TXT", lambda: self._do_export(e, "txt"))
        _mkbtn(foot, "COPY ALL",    lambda: self._copy_all_lyrics(e))
        if e.get("track_uri"):
            _mkbtn(foot, "OPEN IN SPOTIFY", lambda: self._open_in_spotify(e))

        self._highlight_active_lyric()

    def _do_export(self, entry, fmt):
        path = _export_lyrics(entry, fmt)
        if path:
            self._set_error("")
            log(f"Saved to exports/{os.path.basename(path)}")

    def _copy_all_lyrics(self, entry):
        synced = entry.get("synced") or []
        plain  = entry.get("plain") or []
        body = "\n".join(ln.get("words", "") for ln in synced) if synced else "\n".join(plain)
        if body:
            self._to_clipboard(body, "Copied lyrics")
        else:
            log("Nothing to copy — no lyrics for this track")

    def _open_in_spotify(self, entry):
        """Open the track in the Spotify desktop client via its spotify: URI."""
        uri = entry.get("track_uri") or ""
        if not uri.startswith("spotify:"):
            log("No Spotify URI stored for this entry")
            return
        try:
            os.startfile(uri)          # noqa: S606 — a spotify: URI, not a shell string
            log(f"Opening in Spotify  ·  {entry.get('title', '')}")
        except OSError as e:
            log(f"Could not open Spotify: {e}")

    def _highlight_active_lyric(self):
        """Mark and scroll to the line currently being sung.

        Only applies when the open panel is showing the track that is actually
        playing — scrolling someone's browsing of an old song would be wrong."""
        txt = getattr(self, "_lyrics_txt", None)
        e   = getattr(self, "_lyrics_entry", None)
        if txt is None or not e:
            return
        try:
            if not txt.winfo_exists():
                return
        except tk.TclError:
            return
        if e.get("track_uri") != getattr(state, "track_uri", None):
            return
        synced = e.get("synced") or []
        if not synced:
            return
        pos = self._estimate_pos_ms() + _track_offset_ms()
        line_no = 0
        for i, ln in enumerate(synced):
            if ln.get("startMs", 0) <= pos:
                line_no = i
            else:
                break
        try:
            txt.tag_remove("active", "1.0", "end")
            start = f"{line_no + 1}.0"
            txt.tag_add("active", start, f"{line_no + 1}.end")
            txt.see(start)
        except tk.TclError:
            pass

    def _close_lyrics_panel(self):
        panel = getattr(self, "_lyrics_panel", None)
        if panel:
            try: panel.destroy()
            except Exception: pass
            self._lyrics_panel = None
        # Drop the panel refs so _highlight_active_lyric stops doing work.
        self._lyrics_txt   = None
        self._lyrics_entry = None

    # ── SETTINGS ──────────────────────────────────────────────────
    def _build_settings(self):
        p = tk.Frame(self._container, bg=BG); self._pages["SETTINGS"] = p

        container = tk.Frame(p, bg=BG); container.pack(fill="both", expand=True, padx=14, pady=(10,14))
        self._set_vsb = tk.Scrollbar(container, bg=BG3, troughcolor=BG, relief="flat", width=5, bd=0)
        self.set_cv = tk.Canvas(container, bg=BG, highlightthickness=0, yscrollcommand=self._set_vsb.set)
        self.set_cv.pack(side="left", fill="both", expand=True)
        self._set_vsb.config(command=self.set_cv.yview)

        outer = tk.Frame(self.set_cv, bg=BG)
        self._set_hw = self.set_cv.create_window((0,0), window=outer, anchor="nw")

        def _update_set_scroll(e=None):
            self.set_cv.configure(scrollregion=self.set_cv.bbox("all"))
            if outer.winfo_reqheight() > self.set_cv.winfo_height():
                self._set_vsb.pack(side="right", fill="y")
                self._set_scroll_enabled = True
            else:
                self._set_vsb.pack_forget()
                self.set_cv.yview_moveto(0)
                self._set_scroll_enabled = False

        outer.bind("<Configure>", _update_set_scroll)
        self.set_cv.bind("<Configure>",
            lambda e: (self.set_cv.itemconfig(self._set_hw, width=e.width), _update_set_scroll()))

        def _on_mousewheel(e):
            if getattr(self, "_set_scroll_enabled", False):
                self.set_cv.yview_scroll(int(-1*(e.delta/120)), "units")

        self.set_cv.bind("<MouseWheel>", _on_mousewheel)
        def _bind_mw(widget):
            widget.bind("<MouseWheel>", _on_mousewheel)
            for child in widget.winfo_children():
                _bind_mw(child)
        self._bind_set_mw = _bind_mw

        # ── Section: Session Stats ─────────────────────────────────
        tk.Label(outer, text="SESSION STATS", fg=MUTED, bg=BG,
                 font=self._f(7,True)).pack(anchor="w", pady=(0,4))
        stats_card = tk.Frame(outer, bg=BG2); stats_card.pack(fill="x", pady=(0,10))
        inner_s = tk.Frame(stats_card, bg=BG2); inner_s.pack(fill="x", padx=14, pady=10)
        self.lbl_stats_songs = tk.Label(inner_s, text="Songs played:  0",
                                        fg=TEXT2, bg=BG2, font=self._f(9), anchor="w")
        self.lbl_stats_songs.pack(fill="x")
        self.lbl_stats_time = tk.Label(inner_s, text="Listening time:  0m 0s",
                                       fg=TEXT2, bg=BG2, font=self._f(9), anchor="w")
        self.lbl_stats_time.pack(fill="x", pady=(4,0))
        self._refresh_stats()

        # ── Section: Behaviour (tray + blacklist + per-track offset) ─
        tk.Label(outer, text="BEHAVIOUR", fg=MUTED, bg=BG,
                 font=self._f(7,True)).pack(anchor="w", pady=(4,4))
        beh_card = tk.Frame(outer, bg=BG2); beh_card.pack(fill="x", pady=(0,10))
        inner_b  = tk.Frame(beh_card, bg=BG2); inner_b.pack(fill="x", padx=14, pady=10)

        # Close-to-tray toggle (#12)
        row_ct = tk.Frame(inner_b, bg=BG2); row_ct.pack(fill="x", pady=(0,6))
        self._ct_btn = tk.Label(row_ct, text="", fg=ACCENT_FG, bg=ACCENT,
                                font=self._f(FS_MICRO, True), cursor="hand2",
                                padx=SP_MD, pady=SP_XS)
        self._ct_btn.pack(side="right")
        tk.Label(row_ct, text="Close hides to tray", fg=TEXT2, bg=BG2,
                 font=self._f(FS_BODY), anchor="w").pack(side="left", fill="x", expand=True)

        def _paint_ct():
            on = CLOSE_TO_TRAY
            self._ct_btn.config(text="ON" if on else "OFF",
                                bg=ACCENT if on else BG3,
                                fg=ACCENT_FG if on else MUTED)
        def _toggle_ct(_e=None):
            global CLOSE_TO_TRAY
            CLOSE_TO_TRAY = not CLOSE_TO_TRAY
            _cfg_set("preferences", "close_to_tray", str(CLOSE_TO_TRAY).lower())
            _paint_ct()
            if CLOSE_TO_TRAY and not getattr(self, "_tray", None):
                log("Note: pystray not installed — close will minimise instead")
        self._ct_btn.bind("<Button-1>", _toggle_ct)
        _paint_ct()

        # Per-track lyric offset (#13)
        row_to = tk.Frame(inner_b, bg=BG2); row_to.pack(fill="x", pady=(0,6))
        self.lbl_track_off = tk.Label(row_to, text="global", fg=MUTED, bg=BG2,
                                      font=self._f(FS_SMALL))

        def _nudge_track_offset(delta):
            uri = getattr(state, "track_uri", "")
            if not uri:
                log("No track playing — per-track offset not saved")
                return
            _set_track_offset_ms(uri, _track_offset_ms(uri) + delta)
            self._refresh_track_offset()
        def _clear_track_offset(_e=None):
            uri = getattr(state, "track_uri", "")
            if uri:
                _set_track_offset_ms(uri, None)
                self._refresh_track_offset()

        # side="right" stacks right-to-left, so iterate in reverse to get
        # "RESET  −250  value  +250" reading order on screen.
        b_clr = tk.Label(row_to, text="RESET", fg=MUTED, bg=BG3,
                         font=self._f(FS_MICRO, True), cursor="hand2",
                         padx=SP_SM, pady=SP_XS)
        b_clr.pack(side="right", padx=(SP_XS, 0))
        b_clr.bind("<Button-1>", _clear_track_offset)
        for label, delta in (("+250", 250), ("−250", -250)):
            b = tk.Label(row_to, text=label, fg=TEXT, bg=BG3,
                         font=self._f(FS_MICRO, True), cursor="hand2",
                         padx=SP_SM, pady=SP_XS)
            b.pack(side="right", padx=(SP_XS, 0))
            b.bind("<Button-1>", lambda e, d=delta: _nudge_track_offset(d))
        self.lbl_track_off.pack(side="right", padx=(SP_SM, SP_XS))
        tk.Label(row_to, text="Offset for this track", fg=TEXT2, bg=BG2,
                 font=self._f(FS_BODY), anchor="w").pack(side="left", fill="x", expand=True)
        self._refresh_track_offset()

        # Always on top
        row_top = tk.Frame(inner_b, bg=BG2); row_top.pack(fill="x", pady=(0, SP_XS + 2))
        self._top_set_btn = tk.Label(row_top, text="", fg=MUTED, bg=BG3,
                                     font=self._f(FS_MICRO, True), cursor="hand2",
                                     padx=SP_MD, pady=SP_XS)
        self._top_set_btn.pack(side="right")
        tk.Label(row_top, text="Always on top  ·  Ctrl+T", fg=TEXT2, bg=BG2,
                 font=self._f(FS_BODY), anchor="w").pack(side="left", fill="x", expand=True)

        def _paint_top_set():
            self._top_set_btn.config(text="ON" if ALWAYS_ON_TOP else "OFF",
                                     bg=ACCENT if ALWAYS_ON_TOP else BG3,
                                     fg=ACCENT_FG if ALWAYS_ON_TOP else MUTED)
        self._top_set_btn.bind("<Button-1>",
                               lambda e: (self._toggle_topmost(), _paint_top_set()))
        _paint_top_set()

        # Start minimised to tray
        row_sm = tk.Frame(inner_b, bg=BG2); row_sm.pack(fill="x", pady=(0, SP_XS + 2))
        sm_btn = tk.Label(row_sm, text="", fg=MUTED, bg=BG3,
                          font=self._f(FS_MICRO, True), cursor="hand2",
                          padx=SP_MD, pady=SP_XS)
        sm_btn.pack(side="right")
        tk.Label(row_sm, text="Start minimised to tray", fg=TEXT2, bg=BG2,
                 font=self._f(FS_BODY), anchor="w").pack(side="left", fill="x", expand=True)

        def _paint_sm():
            sm_btn.config(text="ON" if START_MINIMIZED else "OFF",
                          bg=ACCENT if START_MINIMIZED else BG3,
                          fg=ACCENT_FG if START_MINIMIZED else MUTED)
        def _toggle_sm(_e=None):
            global START_MINIMIZED
            START_MINIMIZED = not START_MINIMIZED
            _cfg_set("preferences", "start_minimized", str(START_MINIMIZED).lower())
            _paint_sm()
        sm_btn.bind("<Button-1>", _toggle_sm)
        _paint_sm()

        # Lyric font size
        row_lf = tk.Frame(inner_b, bg=BG2); row_lf.pack(fill="x", pady=(0, SP_XS + 2))
        self.lbl_lyric_size = tk.Label(row_lf, text="", fg=MUTED, bg=BG2,
                                       font=self._f(FS_SMALL))

        def _paint_lf():
            self.lbl_lyric_size.config(
                text=("default" if LYRIC_FONT_BOOST == 0 else f"{LYRIC_FONT_BOOST:+d}"),
                fg=ACCENT if LYRIC_FONT_BOOST else MUTED)
        def _nudge_lf(delta):
            global LYRIC_FONT_BOOST
            LYRIC_FONT_BOOST = max(-2, min(10, LYRIC_FONT_BOOST + delta))
            _cfg_set("preferences", "lyric_font_boost", str(LYRIC_FONT_BOOST))
            try:
                self.lbl_lyric.config(font=self._f(FS_TITLE + LYRIC_FONT_BOOST, True))
            except (AttributeError, tk.TclError):
                pass
            _paint_lf()
        for lbl, d in (("A+", 1), ("A−", -1)):
            b = tk.Label(row_lf, text=lbl, fg=TEXT, bg=BG3, font=self._f(FS_MICRO, True),
                         cursor="hand2", padx=SP_SM + 2, pady=SP_XS)
            b.pack(side="right", padx=(SP_XS, 0))
            b.bind("<Button-1>", lambda e, dd=d: _nudge_lf(dd))
        self.lbl_lyric_size.pack(side="right", padx=(SP_SM, SP_XS))
        tk.Label(row_lf, text="Lyric text size", fg=TEXT2, bg=BG2,
                 font=self._f(FS_BODY), anchor="w").pack(side="left", fill="x", expand=True)
        _paint_lf()

        # Discord diagnostics
        row_dx = tk.Frame(inner_b, bg=BG2); row_dx.pack(fill="x", pady=(SP_XS, SP_XS + 2))
        for lbl, cmd in (("RECONNECT", self._reconnect_rpc), ("TEST", self._test_presence)):
            b = tk.Label(row_dx, text=lbl, fg=TEXT2, bg=BG3, font=self._f(FS_MICRO, True),
                         cursor="hand2", padx=SP_MD, pady=SP_XS)
            b.pack(side="right", padx=(SP_SM, 0))
            b.bind("<Button-1>", lambda e, c=cmd: c())
            self._hoverable(b, fg=lambda: TEXT2, hover_fg=lambda: ACCENT, bg=lambda: BG3, hover_bg=lambda: ACCENT_SOFT)
        tk.Label(row_dx, text="Discord", fg=TEXT2, bg=BG2,
                 font=self._f(FS_BODY), anchor="w").pack(side="left", fill="x", expand=True)

        # Blacklist (#16)
        tk.Label(inner_b, text="Blacklist — one term per line; matches artist or title",
                 fg=MUTED, bg=BG2, font=self._f(7), anchor="w").pack(fill="x", pady=(4,2))
        # width=1 is deliberate. A tk.Text defaults to 80 columns, and pack()
        # will not shrink a widget below its requested size — so the default
        # forced the entire settings frame far wider than the 520 px window and
        # pushed every right-aligned control off the visible area. width=1 lets
        # fill="x" decide the real width.
        self._bl_txt = tk.Text(inner_b, bg=BG3, fg=TEXT, font=self._f(FS_SMALL),
                               height=4, width=1,
                               relief="flat", wrap="word", padx=6, pady=4,
                               insertbackground=TEXT)
        self._focus_ring(self._bl_txt)
        self._bl_txt.pack(fill="x")
        self._bl_txt.insert("1.0", "\n".join(_BLACKLIST))
        bl_btn = tk.Label(inner_b, text="SAVE BLACKLIST", fg=ACCENT_FG, bg=ACCENT,
                          font=self._f(7,True), cursor="hand2", padx=10, pady=4)
        bl_btn.pack(anchor="e", pady=(4,0))

        def _save_blacklist(_e=None):
            global _BLACKLIST
            raw = self._bl_txt.get("1.0", "end").strip()
            # configparser can't hold raw newlines in a value, so store them
            # escaped and unescape on load.
            _cfg_set("preferences", "blacklist", raw.replace("\n", "\\n"))
            _BLACKLIST = _load_blacklist()
            state.blacklisted = _is_blacklisted(
                getattr(state, "artist", ""), getattr(state, "title", ""))
            log(f"Blacklist saved  ·  {len(_BLACKLIST)} term(s)")
        bl_btn.bind("<Button-1>", _save_blacklist)

        # ── Section: Appearance ────────────────────────────────────
        tk.Label(outer, text="APPEARANCE", fg=MUTED, bg=BG,
                 font=self._f(7,True)).pack(anchor="w", pady=(4,4))
        appear_card = tk.Frame(outer, bg=BG2); appear_card.pack(fill="x", pady=(0,10))
        inner_a = tk.Frame(appear_card, bg=BG2); inner_a.pack(fill="x", padx=14, pady=10)

        # Dark/Light toggle — custom pill buttons (no ugly Tk radio circles)
        row_dm = tk.Frame(inner_a, bg=BG2); row_dm.pack(fill="x", pady=(0,6))
        tk.Label(row_dm, text="Theme", fg=TEXT2, bg=BG2, font=self._f(9), anchor="w").pack(side="left")

        self._theme_btns = {}  # "dark"/"light" → Label widget
        pill_frame = tk.Frame(row_dm, bg=BG3); pill_frame.pack(side="right")

        for label_text, key in [("Dark", "dark"), ("Light", "light")]:
            is_active = (key == "dark") == _DARK_MODE
            b = tk.Label(pill_frame, text=label_text,
                         fg=TEXT if is_active else MUTED,
                         bg=BG3 if not is_active else BG2,
                         font=self._f(9, True),
                         cursor="hand2", padx=10, pady=4)
            b.pack(side="left")
            b.bind("<Button-1>", lambda e, k=key: self._set_theme(k))
            # Like the tabs, these are stateful: on <Leave> fade back to the
            # colour the pill's own selected/unselected state calls for, not
            # to a fixed resting colour.
            b.bind("<Enter>", lambda e, w=b: self._fade_colors(
                f"hover:{w}", w, 110, fg=ACCENT))
            b.bind("<Leave>", lambda e, w=b, k=key: self._fade_colors(
                f"hover:{w}", w, 110,
                fg=TEXT if (k == "dark") == _DARK_MODE else MUTED))
            self._theme_btns[key] = b

        # Accent color picker
        row_ac = tk.Frame(inner_a, bg=BG2); row_ac.pack(fill="x")
        tk.Label(row_ac, text="Accent color", fg=TEXT2, bg=BG2, font=self._f(9), anchor="w").pack(side="left")
        self._accent_swatch = tk.Label(row_ac, bg=ACCENT, width=5, height=1,
                                       cursor="hand2", relief="groove", bd=2)
        self._accent_swatch.pack(side="right")
        self._accent_swatch.bind("<Button-1>", self._pick_accent)
        self._accent_swatch.bind("<Enter>", lambda e: self._accent_swatch.config(relief="solid"))
        self._accent_swatch.bind("<Leave>", lambda e: self._accent_swatch.config(relief="groove"))

        # Motion toggle. Animation is an accessibility question before it is a
        # taste one, and it doubles as the escape hatch on hardware where the
        # 30 fps progress bar is not free. Turning it off degrades every
        # transition to the instant snap this UI used to do — nothing becomes
        # unreachable or invisible.
        row_an = tk.Frame(inner_a, bg=BG2); row_an.pack(fill="x", pady=(SP_SM, 0))
        self._anim_btn = tk.Label(row_an, text="", fg=MUTED, bg=BG3,
                                  font=self._f(FS_MICRO, True), cursor="hand2",
                                  padx=SP_MD, pady=SP_XS)
        self._anim_btn.pack(side="right")
        tk.Label(row_an, text="Smooth animations", fg=TEXT2, bg=BG2,
                 font=self._f(FS_BODY), anchor="w").pack(side="left", fill="x", expand=True)

        def _paint_anim():
            on = ANIMATIONS_ENABLED
            self._anim_btn.config(text="ON" if on else "OFF",
                                  bg=ACCENT if on else BG3,
                                  fg=ACCENT_FG if on else MUTED)
        def _toggle_anim(_e=None):
            global ANIMATIONS_ENABLED
            ANIMATIONS_ENABLED = not ANIMATIONS_ENABLED
            _cfg_set("preferences", "animations", str(ANIMATIONS_ENABLED).lower())
            _paint_anim()
            # The progress tick reads the flag when it re-arms, so switching
            # off takes effect within one frame rather than one track.
            log(f"Smooth animations {'enabled' if ANIMATIONS_ENABLED else 'disabled'}")
        self._anim_btn.bind("<Button-1>", _toggle_anim)
        _paint_anim()

        # ── Section: Hotkeys ───────────────────────────────────────
        tk.Label(outer, text="GLOBAL HOTKEYS", fg=MUTED, bg=BG,
                 font=self._f(7,True)).pack(anchor="w", pady=(4,4))
        hotkey_card = tk.Frame(outer, bg=BG2); hotkey_card.pack(fill="x", pady=(0,10))
        inner_h = tk.Frame(hotkey_card, bg=BG2); inner_h.pack(fill="x", padx=14, pady=10)

        if not KEYBOARD_AVAILABLE:
            tk.Label(inner_h, text="Install 'keyboard' package to enable hotkeys:\npip install keyboard",
                     fg=MUTED, bg=BG2, font=self._f(8), justify="left").pack(anchor="w")
        else:
            # Skip track
            row_sk = tk.Frame(inner_h, bg=BG2); row_sk.pack(fill="x", pady=(0,4))
            tk.Label(row_sk, text="Skip track", fg=TEXT2, bg=BG2, font=self._f(9), width=14, anchor="w").pack(side="left")
            self._skip_var = tk.StringVar(value=_hotkey_skip_combo)
            ent_sk = tk.Entry(row_sk, textvariable=self._skip_var, bg=BG3, fg=TEXT,
                              insertbackground=TEXT, relief="flat", font=self._f(9), width=18)
            self._focus_ring(ent_sk); ent_sk.pack(side="left", padx=(SP_XS,0))

            # Skip instrumental
            row_si = tk.Frame(inner_h, bg=BG2); row_si.pack(fill="x", pady=(0,4))
            tk.Label(row_si, text="Skip instrumental", fg=TEXT2, bg=BG2, font=self._f(9), width=14, anchor="w").pack(side="left")
            self._skip_instr_var = tk.StringVar(value=_hotkey_skip_instr_combo)
            ent_si = tk.Entry(row_si, textvariable=self._skip_instr_var, bg=BG3, fg=TEXT,
                              insertbackground=TEXT, relief="flat", font=self._f(9), width=18)
            self._focus_ring(ent_si); ent_si.pack(side="left", padx=(SP_XS,0))

            # Toggle RPC
            row_tg = tk.Frame(inner_h, bg=BG2); row_tg.pack(fill="x", pady=(0,4))
            tk.Label(row_tg, text="Toggle RPC", fg=TEXT2, bg=BG2, font=self._f(9), width=14, anchor="w").pack(side="left")
            self._toggle_var = tk.StringVar(value=_hotkey_toggle_combo)
            ent_tg = tk.Entry(row_tg, textvariable=self._toggle_var, bg=BG3, fg=TEXT,
                              insertbackground=TEXT, relief="flat", font=self._f(9), width=18)
            self._focus_ring(ent_tg); ent_tg.pack(side="left", padx=(SP_XS,0))

            def _save_hotkeys():
                global _hotkey_skip_combo, _hotkey_toggle_combo, _hotkey_skip_instr_combo, _hotkey_registered
                # Unregister old
                try: _keyboard.unhook_all_hotkeys()
                except Exception as e: log(f"Hotkey unhook failed: {e}")
                _hotkey_registered = False
                _hotkey_skip_combo       = self._skip_var.get().strip()
                _hotkey_toggle_combo     = self._toggle_var.get().strip()
                _hotkey_skip_instr_combo = self._skip_instr_var.get().strip()
                _cfg_set("preferences", "hotkey_skip",       _hotkey_skip_combo)
                _cfg_set("preferences", "hotkey_toggle",     _hotkey_toggle_combo)
                _cfg_set("preferences", "hotkey_skip_instr", _hotkey_skip_instr_combo)
                _register_hotkeys(self)
                log("Hotkeys saved & re-registered")

            sv_btn = tk.Label(inner_h, text="SAVE HOTKEYS", fg=MUTED, bg=BG2,
                              font=self._f(7,True), cursor="hand2")
            sv_btn.pack(anchor="e", pady=(4,0))
            sv_btn.bind("<Button-1>", lambda e: _save_hotkeys())
            self._hoverable(sv_btn, fg=lambda: MUTED, hover_fg=lambda: ACCENT)

        # ── Section: Startup ───────────────────────────────────────
        tk.Label(outer, text="SYSTEM", fg=MUTED, bg=BG,
                 font=self._f(7,True)).pack(anchor="w", pady=(4,4))
        sys_card = tk.Frame(outer, bg=BG2); sys_card.pack(fill="x", pady=(0,10))
        inner_sy = tk.Frame(sys_card, bg=BG2); inner_sy.pack(fill="x", padx=14, pady=10)

        row_su = tk.Frame(inner_sy, bg=BG2); row_su.pack(fill="x")
        tk.Label(row_su, text="Launch at Windows startup", fg=TEXT2, bg=BG2,
                 font=self._f(9), anchor="w").pack(side="left")
        self._startup_var = tk.BooleanVar(value=_get_startup_enabled())
        def _toggle_startup():
            _set_startup_enabled(self._startup_var.get())
        tk.Checkbutton(row_su, variable=self._startup_var, bg=BG2, activebackground=BG2,
                       selectcolor=BG3, command=_toggle_startup).pack(side="right")

        row_sh = tk.Frame(inner_sy, bg=BG2); row_sh.pack(fill="x", pady=(6,0))
        tk.Label(row_sh, text="Remember session history", fg=TEXT2, bg=BG2,
                 font=self._f(9), anchor="w").pack(side="left")
        self._save_hist_var = tk.BooleanVar(value=SAVE_HISTORY)
        def _toggle_save_hist():
            global SAVE_HISTORY
            SAVE_HISTORY = self._save_hist_var.get()
            _cfg_set("preferences", "save_history", str(SAVE_HISTORY).lower())
            log(f'Session history {"enabled" if SAVE_HISTORY else "disabled"}')
        tk.Checkbutton(row_sh, variable=self._save_hist_var, bg=BG2, activebackground=BG2,
                       selectcolor=BG3, command=_toggle_save_hist).pack(side="right")

        # Window position reset
        row_wp = tk.Frame(inner_sy, bg=BG2); row_wp.pack(fill="x", pady=(6,0))
        tk.Label(row_wp, text="Reset window position", fg=TEXT2, bg=BG2,
                 font=self._f(9), anchor="w").pack(side="left")
        rst_pos = tk.Label(row_wp, text="CENTER", fg=MUTED, bg=BG2,
                           font=self._f(7,True), cursor="hand2")
        rst_pos.pack(side="right")
        rst_pos.bind("<Button-1>", lambda e: self._center(force=True))
        self._hoverable(rst_pos, fg=lambda: MUTED, hover_fg=lambda: ACCENT)

        # ── Section: Discord RPC Behaviour ────────────────────────
        tk.Label(outer, text="DISCORD RPC BEHAVIOUR", fg=MUTED, bg=BG,
                 font=self._f(7,True)).pack(anchor="w", pady=(4,4))
        rpc_card = tk.Frame(outer, bg=BG2); rpc_card.pack(fill="x", pady=(0,10))
        inner_rpc = tk.Frame(rpc_card, bg=BG2); inner_rpc.pack(fill="x", padx=14, pady=10)

        # Feature 7 — paused state toggle
        row_ps = tk.Frame(inner_rpc, bg=BG2); row_ps.pack(fill="x", pady=(0,6))
        tk.Label(row_ps, text='Show "Paused" on Discord', fg=TEXT2, bg=BG2,
                 font=self._f(9), anchor="w").pack(side="left")
        self._paused_var = tk.BooleanVar(value=SHOW_PAUSED_RPC)
        def _toggle_paused_rpc():
            global SHOW_PAUSED_RPC
            SHOW_PAUSED_RPC = self._paused_var.get()
            _cfg_set("preferences", "show_paused_rpc", str(SHOW_PAUSED_RPC).lower())
            log(f'Paused RPC {"enabled" if SHOW_PAUSED_RPC else "disabled"}')
        tk.Checkbutton(row_ps, variable=self._paused_var, bg=BG2, activebackground=BG2,
                       selectcolor=BG3, command=_toggle_paused_rpc).pack(side="right")

        # Feature 5 — custom instrumental text
        row_it = tk.Frame(inner_rpc, bg=BG2); row_it.pack(fill="x")
        tk.Label(row_it, text="Instrumental text", fg=TEXT2, bg=BG2,
                 font=self._f(9), anchor="w").pack(anchor="w")
        row_it2 = tk.Frame(inner_rpc, bg=BG2); row_it2.pack(fill="x", pady=(2,0))
        self._instr_var = tk.StringVar(value=INSTRUMENTAL_TEXT)
        ent_it = tk.Entry(row_it2, textvariable=self._instr_var, bg=BG3, fg=TEXT,
                          insertbackground=TEXT, relief="flat", font=self._f(9))
        self._focus_ring(ent_it)
        ent_it.pack(side="left", fill="x", expand=True, padx=(0, SP_SM))
        def _save_instr():
            global INSTRUMENTAL_TEXT
            INSTRUMENTAL_TEXT = self._instr_var.get() or "🎵 ─ ─ ─ ─ ─ ─ ─ ─ ─ 🎵"
            _cfg_set("preferences", "instrumental_text", INSTRUMENTAL_TEXT)
            log(f"Instrumental text set to: {INSTRUMENTAL_TEXT}")
        sv_it = tk.Label(row_it2, text="SAVE", fg=MUTED, bg=BG2,
                         font=self._f(7,True), cursor="hand2")
        sv_it.pack(side="left")
        sv_it.bind("<Button-1>", lambda e: _save_instr())
        self._hoverable(sv_it, fg=lambda: MUTED, hover_fg=lambda: ACCENT)
        # ── Section: Discord Profiles ──────────────────────────────
        tk.Label(outer, text="DISCORD PROFILES", fg=MUTED, bg=BG,
                 font=self._f(7,True)).pack(anchor="w", pady=(4,4))
        prof_card = tk.Frame(outer, bg=BG2); prof_card.pack(fill="x", pady=(0,10))
        inner_pr = tk.Frame(prof_card, bg=BG2); inner_pr.pack(fill="x", padx=14, pady=10)

        tk.Label(inner_pr, text="Save multiple App IDs and switch between them.",
                 fg=MUTED, bg=BG2, font=self._f(8), anchor="w").pack(anchor="w", pady=(0,6))

        # Profile listbox
        lb_frame = tk.Frame(inner_pr, bg=BG2); lb_frame.pack(fill="x")
        self._prof_lb = tk.Listbox(lb_frame, bg=BG3, fg=TEXT2,
                                   selectbackground=ACCENT, selectforeground=ACCENT_FG,
                                   relief="flat", font=self._f(9), height=4,
                                   activestyle="none", bd=0)
        self._prof_lb.pack(fill="x")

        def _load_profiles():
            self._prof_lb.delete(0, "end")
            cfg = _load_config()
            if not cfg.has_section("profiles"):
                cfg.add_section("profiles")
            for name, app_id in cfg.items("profiles"):
                marker = " ✓" if app_id == DISCORD_APP_ID else ""
                self._prof_lb.insert("end", f"{name}{marker}  —  {app_id}")
        _load_profiles()

        btn_row = tk.Frame(inner_pr, bg=BG2); btn_row.pack(fill="x", pady=(6,0))

        def _mk_btn(parent, txt, cmd):
            b = tk.Label(parent, text=txt, fg=MUTED, bg=BG2,
                         font=self._f(7,True), cursor="hand2", padx=8)
            b.pack(side="left", padx=(0,6))
            b.bind("<Button-1>", lambda e: cmd())
            self._hoverable(b, fg=lambda: MUTED, hover_fg=lambda: ACCENT)
            return b

        def _add_profile():
            dlg = tk.Toplevel(self.win); dlg.title("Add Profile")
            dlg.configure(bg=BG); dlg.resizable(False, False)
            dlg.geometry("340x200")
            dlg.transient(self.win); dlg.grab_set()

            tk.Label(dlg, text="Profile name:", fg=TEXT2, bg=BG, font=self._f(9)).pack(pady=(12,2))
            nv = tk.StringVar()
            ent_n = tk.Entry(dlg, textvariable=nv, bg=BG2, fg=TEXT, insertbackground=TEXT,
                             relief="flat", font=self._f(9))
            self._focus_ring(ent_n)
            ent_n.pack(fill="x", padx=20)
            ent_n.focus_set()

            tk.Label(dlg, text="App ID:", fg=TEXT2, bg=BG, font=self._f(9)).pack(pady=(8,2))
            av = tk.StringVar()
            self._focus_ring(
                tk.Entry(dlg, textvariable=av, bg=BG2, fg=TEXT, insertbackground=TEXT,
                         relief="flat", font=self._f(9))).pack(fill="x", padx=20)

            err = tk.Label(dlg, text="", fg=DANGER, bg=BG, font=self._f(FS_MICRO),
                           wraplength=300, justify="center")
            err.pack(pady=(4,0))

            def _ok():
                n = nv.get().strip(); a = av.get().strip()
                if not n or not a:
                    err.config(text="Both a name and an App ID are required")
                    return
                # Profile names become configparser option keys, so anything
                # the INI grammar treats as a delimiter has to go.
                if any(ch in n for ch in "=:[]\n"):
                    err.config(text="Name cannot contain  =  :  [  ]")
                    return
                if not a.isdigit() or len(a) < 16:
                    err.config(text="App ID must be a long numeric ID")
                    return
                _cfg_set("profiles", n, a)
                _load_profiles()
                log(f"Profile saved  ·  {n}")
                dlg.destroy()

            # Keep a real reference to the button. This used to be an
            # unassigned tk.Label reached back through dlg.children["!label4"],
            # which is not even the SAVE label's auto-generated name — the
            # lookup raised KeyError every time the dialog opened and the
            # button was simply dead. Only the Return key ever worked.
            save_btn = tk.Label(dlg, text="SAVE", fg=ACCENT_FG, bg=ACCENT,
                                font=self._f(FS_MICRO, True), cursor="hand2",
                                padx=SP_MD, pady=SP_XS + 1)
            save_btn.pack(pady=(8,0))
            save_btn.bind("<Button-1>", lambda e: _ok())
            dlg.bind("<Return>", lambda e: _ok())
            dlg.bind("<Escape>", lambda e: dlg.destroy())

        def _del_profile():
            sel = self._prof_lb.curselection()
            if not sel: return
            text = self._prof_lb.get(sel[0])
            name = text.split(" ✓")[0].split("  —  ")[0].strip()
            cfg = _load_config()
            if cfg.has_option("profiles", name):
                cfg.remove_option("profiles", name)
                _save_config(cfg)
            _load_profiles()

        def _switch_profile():
            global DISCORD_APP_ID
            sel = self._prof_lb.curselection()
            if not sel: return
            text = self._prof_lb.get(sel[0])
            # Parse: "name [✓]  —  app_id"
            parts = text.split("  —  ")
            if len(parts) < 2: return
            new_id = parts[-1].strip()
            DISCORD_APP_ID = new_id
            _cfg_set("preferences", "discord_app_id_active", new_id)
            # Update .env
            try:
                lines = open(_ENV_PATH, encoding="utf-8").readlines()
                with open(_ENV_PATH, "w", encoding="utf-8") as f:
                    written = False
                    for ln in lines:
                        if ln.startswith("DISCORD_APP_ID="):
                            f.write(f"DISCORD_APP_ID={new_id}\n"); written = True
                        else:
                            f.write(ln)
                    if not written:
                        f.write(f"DISCORD_APP_ID={new_id}\n")
            except Exception: pass
            _load_profiles()
            log(f"Switched Discord profile to: {new_id}")

        _mk_btn(btn_row, "ADD",    _add_profile)
        _mk_btn(btn_row, "DELETE", _del_profile)
        _mk_btn(btn_row, "SWITCH", _switch_profile)

        # Desktop shortcut. This used to open a SECOND settings card, also
        # headed "SYSTEM" — two identically-titled sections on one page, with
        # the startup/history toggles in one and this in the other. It belongs
        # in the existing SYSTEM card (inner_sy), so it is packed there.
        _NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        def _do_shortcut():
            app_dir  = _APP_DIR

            if _FROZEN:
                # A release build needs no launcher shim: the exe the user
                # downloaded is already the thing a shortcut should point at,
                # and it embeds its own icon. Running build_launcher.ps1 here
                # would try to compile a Python launcher for a machine that
                # need not have Python at all.
                exe_path = sys.executable
                ico_path = sys.executable
            else:
                exe_path = os.path.join(app_dir, "Statusify.exe")
                ico_path = os.path.join(_RES_DIR, "statusify.ico")

                if not os.path.exists(exe_path):
                    self._log("Building Statusify.exe…")
                    try:
                        subprocess.run(
                            ["powershell.exe", "-NoProfile", "-NonInteractive",
                             "-ExecutionPolicy", "Bypass", "-File", "build_launcher.ps1"],
                            cwd=app_dir, check=True, capture_output=True,
                            timeout=120, creationflags=_NO_WINDOW)
                    except Exception as e:
                        self._log(f"Build failed: {e}")
                        self._set_error(f"Could not build Statusify.exe: {e}")
                        return

                if not os.path.exists(exe_path):
                    self._log("Build reported success but Statusify.exe is missing")
                    return

            desk = os.path.join(os.environ.get("USERPROFILE", ""), "Desktop")
            lnk  = os.path.join(desk, "Statusify.lnk")
            ps = (f"$s=(New-Object -COM WScript.Shell).CreateShortcut('{lnk}');"
                  f"$s.TargetPath='{exe_path}';$s.WorkingDirectory='{app_dir}';"
                  f"$s.IconLocation='{ico_path}';$s.Save()")
            try:
                subprocess.run(
                    ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
                    check=True, capture_output=True, timeout=30,
                    creationflags=_NO_WINDOW)
                self._log("Shortcut created on Desktop")
            except Exception as e:
                self._log(f"Shortcut failed: {e}")
                self._set_error(f"Could not create shortcut: {e}")

        row_sc = tk.Frame(inner_sy, bg=BG2); row_sc.pack(fill="x", pady=(6,0))
        tk.Label(row_sc, text="Desktop Access", fg=TEXT2, bg=BG2, font=self._f(9), anchor="w").pack(side="left")
        btn_sc = tk.Label(row_sc, text="CREATE SHORTCUT", fg=ACCENT_FG, bg=ACCENT, 
                          font=self._f(7,True), cursor="hand2", padx=10, pady=4)
        btn_sc.pack(side="right")
        btn_sc.bind("<Button-1>", lambda e: _do_shortcut())

        # Apply mousewheel binding to all elements in settings
        self._bind_set_mw(outer)

    def _refresh_stats(self, reschedule=True):
        """Update session stats labels.

        FREEZE BUG (fixed): this method re-armed itself with after(5000, ...)
        on EVERY call, but it is also called directly — once from
        _build_settings() and again for every ("stats",) event drained in
        _poll() (emitted on every track start / pause / resume). Each of those
        direct calls spawned an ADDITIONAL self-perpetuating 5 s timer chain
        that was never cancelled, so the number of concurrent chains grew
        monotonically for the whole session. After a few hours there were
        thousands of chains firing, each one appending a row to health.csv from
        the Tk thread — the main loop ended up doing nothing but disk I/O, the
        window stopped repainting/responding, and the event queue backed up
        (observed: 8000+ pending events, a 17 GB health.csv), while the asyncio
        backend thread happily kept the Discord RPC alive. Hence "GUI frozen,
        RPC still working".

        Fix: cancel any pending timer before arming a new one, so there is at
        most ONE chain, and let event-driven refreshes pass reschedule=False.
        """
        _health_snapshot()
        total_secs = int(_get_listen_time())
        mins, secs = divmod(total_secs, 60)
        hrs, mins  = divmod(mins, 60)
        if hrs:
            tstr = f"{hrs}h {mins}m {secs}s"
        else:
            tstr = f"{mins}m {secs}s"
        if hasattr(self, "lbl_stats_songs"):
            self.lbl_stats_songs.config(text=f"Songs played:  {_session_songs}")
            self.lbl_stats_time.config(text=f"Listening time:  {tstr}")
        if not reschedule:
            return
        # Named slot guarantees exactly one live chain no matter how many
        # callers invoke this method. See _schedule() for the full story.
        self._schedule("stats", 5000, self._refresh_stats)

    def _set_theme(self, key):
        """Set dark or light theme from the pill-button key."""
        dark = (key == "dark")
        _apply_palette(dark, ACCENT)
        _cfg_set("preferences", "dark_mode", str(dark).lower())
        self._rebuild_all()
        # Re-highlight the active theme pill + active tab
        self._highlight_theme_btn()
        if self._cur_page:
            for n, b in self._tab_btns.items():
                b.config(fg=ACCENT if n == self._cur_page else MUTED)

    def _highlight_theme_btn(self):
        """Update the Dark/Light pill visuals to reflect the current theme."""
        if not hasattr(self, "_theme_btns"):
            return
        dark = _DARK_MODE
        for key, b in self._theme_btns.items():
            is_active = (key == "dark") == dark
            b.config(fg=TEXT if is_active else MUTED,
                     bg=BG2 if is_active else BG3)

    def _pick_accent(self, _event=None):
        color = tkcolor.askcolor(color=ACCENT, title="Choose accent color")[1]
        if color:
            _apply_palette(_DARK_MODE, color)
            _cfg_set("preferences", "accent_color", color)
            self._rebuild_all()
            if self._cur_page:
                for n, b in self._tab_btns.items():
                    b.config(fg=ACCENT if n == self._cur_page else MUTED)

    def _rebuild_all(self):
        """Recolour every widget in-place without destroying state."""
        # Build an exact before→after mapping from the palette snapshot taken
        # just before _apply_palette overwrote the globals.  Using the previous
        # *actual* values avoids any hash collision (e.g. dark BG2 == light TEXT
        # == "#111111" was previously ambiguous in a merged static dict).
        _remap = {
            _PREV_BG:     BG,
            _PREV_BG2:    BG2,
            _PREV_BG3:    BG3,
            _PREV_BG4:    BG4,
            _PREV_MUTED:  MUTED,
            _PREV_TEXT:   TEXT,
            _PREV_TEXT2:  TEXT2,
            _PREV_BORDER: BORDER,
            _PREV_ACCENT: ACCENT,
            _PREV_ACCENT_SOFT: ACCENT_SOFT,
            _PREV_ACCENT_FG:   ACCENT_FG,
            _PREV_HOVER_BG:    HOVER_BG,
            _PREV_SHADOW:      SHADOW,
            _PREV_DANGER:      DANGER,
            _PREV_WARN:        WARN,
        }
        # A derived token can coincide with a base one (ACCENT_FG is often
        # exactly TEXT's white). Base colours are authoritative — re-assert
        # them last so a derived key can never shadow them.
        for _old, _new in ((_PREV_BG, BG), (_PREV_BG2, BG2), (_PREV_BG3, BG3),
                           (_PREV_MUTED, MUTED), (_PREV_TEXT, TEXT),
                           (_PREV_TEXT2, TEXT2), (_PREV_ACCENT, ACCENT)):
            _remap[_old] = _new

        def _recolour(w):
            try:
                cur_bg = w.cget("bg")
                new_bg = _remap.get(cur_bg)
                if new_bg:
                    w.config(bg=new_bg)
            except tk.TclError:
                pass
            try:
                cur_fg = w.cget("fg")
                new_fg = _remap.get(cur_fg)
                if new_fg:
                    w.config(fg=new_fg)
            except tk.TclError:
                pass
            # Entry/Text focus rings (see _focus_ring). These are ordinary
            # palette colours living on different option names, so without
            # this every input kept its old border after a theme switch —
            # dark BORDER hairlines around white fields in light mode.
            for opt in ("highlightbackground", "highlightcolor",
                        "insertbackground"):
                try:
                    new = _remap.get(w.cget(opt))
                    if new:
                        w.config(**{opt: new})
                except tk.TclError:
                    pass
            for child in w.winfo_children():
                _recolour(child)

        _recolour(self.win)

        # Accent-coloured widgets need explicit update
        self._tab_line.config(bg=ACCENT)
        try: self._accent_swatch.config(bg=ACCENT)
        except (AttributeError, tk.TclError): pass

        # Log widget text tags
        try:
            self.log_txt.tag_config("g",  foreground=ACCENT)
            self.log_txt.tag_config("m",  foreground=MUTED)
            self.log_txt.tag_config("ts", foreground=BORDER)
        except (AttributeError, tk.TclError): pass

        # The lyric label fg might be ACCENT (active lyric) or MUTED — don't touch it.
        # Update canvas placeholder art to new colours
        try:
            if self._img is None:
                self._default_art()
        except (AttributeError, tk.TclError): pass

        # Repaint the Now-Playing progress bar so its fill uses the new ACCENT.
        # Colours live on persistent canvas items now, so they have to be
        # pushed explicitly — a redraw alone only moves them.
        try:
            self._repaint_progress_colors()
            self._redraw_progress()
        except Exception:
            pass

    # ── Page switcher ─────────────────────────────────────────────
    def _show(self, name):
        if self._cur_page == name:
            return
        if self._cur_page: self._pages[self._cur_page].pack_forget()
        self._pages[name].pack(fill="both", expand=True)
        self._cur_page = name
        # Cross-fade the labels over the same 220 ms the underline takes to
        # travel, so the colour change reads as one movement with the slide
        # rather than as a separate flash.
        for n, b in self._tab_btns.items():
            self._fade_colors(f"tabfg:{n}", b, 220, fg=ACCENT if n == name else MUTED)
        self._move_tab_line()

    def _move_tab_line(self, animate=True):
        """Slide the accent underline to the active tab.

        Position is measured from the real widgets, never hardcoded. An
        earlier version used a fixed lookup — x/width of 0/120, 120/88,
        208/90 — taken once at 96 DPI. _f() enforces a 10 pt font floor and
        __init__ multiplies Tk's scaling by dpi/72, so on any HiDPI display
        the tabs grew while the underline did not move: it marked the wrong
        tab entirely.

        The move itself is tweened. Snapping the underline between tabs gave
        no sense of direction — the eye had to re-find it after every click,
        because nothing connected where it was to where it went."""
        btn = self._tab_btns.get(self._cur_page)
        if btn is None:
            return
        try:
            bar = btn.master
            target_w = btn.winfo_width()
            if target_w <= 1:
                # Not laid out yet (first call happens during _build) — the
                # named slot means repeated retries can't stack up.
                self._schedule("tabline", 30, self._move_tab_line)
                return
            target_x = btn.winfo_x() + bar.winfo_x()
            y = bar.winfo_y() + bar.winfo_height() - 2

            cur_x = self._tab_line.winfo_x()
            cur_w = self._tab_line.winfo_width()
            # First placement, or the underline is somewhere nonsensical:
            # put it straight down rather than sliding in from the corner.
            if not animate or cur_w <= 1 or not self._tab_line.winfo_ismapped():
                self._tab_line.place(x=target_x, y=y, width=target_w)
            else:
                def _apply(t):
                    self._tab_line.place(
                        x=int(round(cur_x + (target_x - cur_x) * t)),
                        y=y,
                        width=int(round(cur_w + (target_w - cur_w) * t)),
                    )
                self._animate("tabline", 220, _apply)
            self._tab_line.lift()
        except tk.TclError:
            pass

    # ── Art helpers ───────────────────────────────────────────────
    @staticmethod
    def _rounded_rect(cv, x1, y1, x2, y2, r, **kw):
        """Draw a rounded rectangle on a Tk canvas.

        Tk has no such primitive. The trick is a polygon whose corner points
        are doubled up and drawn with smooth=True, which runs a spline
        through them and rounds exactly the corners."""
        pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
               x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
               x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
        return cv.create_polygon(pts, smooth=True, **kw)

    def _default_art(self):
        """Placeholder shown until artwork arrives (or when there is none).

        Matches the rounded corner of the real artwork so the swap-in doesn't
        change the silhouette."""
        self.canvas.delete("all")
        self._rounded_rect(self.canvas, 0, 0, HERO_ART_PX - 1, HERO_ART_PX - 1,
                           HERO_ART_RADIUS, fill=BG3, outline=BORDER)
        self.canvas.create_text(HERO_ART_PX // 2, HERO_ART_PX // 2,
                                text="♫", fill=MUTED, font=self._f(20))

    # ── Progress bar ──────────────────────────────────────────────
    PROG_H = 6          # bar thickness in px; radius is half of this

    def _prog_items(self):
        """Create (once) and return the bar's persistent canvas items.

        The old implementation called delete('all') and re-created both
        rectangles on every single frame. Rebuilding the display list that
        often is what forced the tick rate down to 4 fps to stay affordable,
        and 4 fps is exactly slow enough to read as stepping rather than
        moving. Creating the items once and only moving them with coords()
        is roughly an order of magnitude cheaper per frame, which is what
        buys the 30 fps below."""
        items = getattr(self, "_prog_shapes", None)
        if items:
            return items
        cv = self._prog_cv
        r = self.PROG_H / 2.0
        # Rounded ends: a rectangle spanning the middle plus a circle at each
        # end. A square-ended 6 px bar looks like a progress *meter*; the
        # rounded cap is what makes it read as a track being filled.
        items = {
            "track":   cv.create_rectangle(0, 0, 0, 0, fill=BG3, outline=""),
            "track_l": cv.create_oval(0, 0, 0, 0, fill=BG3, outline=""),
            "track_r": cv.create_oval(0, 0, 0, 0, fill=BG3, outline=""),
            "fill":    cv.create_rectangle(0, 0, 0, 0, fill=ACCENT, outline=""),
            "fill_l":  cv.create_oval(0, 0, 0, 0, fill=ACCENT, outline=""),
            "fill_r":  cv.create_oval(0, 0, 0, 0, fill=ACCENT, outline=""),
        }
        self._prog_shapes = items
        self._prog_radius = r
        return items

    def _redraw_progress(self):
        """Move the Now-Playing progress bar to match current state.

        Called every tick AND from _rebuild_all (so an accent/palette change
        repaints the fill with the new ACCENT colour). Safe to call before the
        canvas is realised: winfo_width() returns 1 until mapped, in which
        case we lay it out at a default width and let the next tick correct it.

        Note there is deliberately no <Configure> binding on this canvas. An
        early version had one, and delete('all')+create_rectangle() retriggers
        Configure — a self-perpetuating C-level storm that pegged a core and
        froze the window. coords() does not resize the canvas and so cannot
        retrigger it, but the binding stays absent regardless: the tick keeps
        the bar correct during a drag on its own."""
        if not hasattr(self, "_prog_cv"):
            return
        try:
            w = self._prog_cv.winfo_width()
            items = self._prog_items()
        except (tk.TclError, AttributeError):
            return
        if w is None or w <= 1:
            w = 460  # optimistic default until the canvas is mapped
        dur = state.duration_ms
        # Estimate live position: advance from the last WS-reported position
        # while playing (mirrors how Discord RPC derives its timer).
        pos = self._estimate_pos_ms()
        frac = (pos / dur) if dur and dur > 0 else 0.0
        frac = max(0.0, min(1.0, frac))

        cv = self._prog_cv
        h = self.PROG_H
        r = self._prog_radius
        usable = max(0.0, w - h)          # centres of the two end caps
        fill_x = r + usable * frac        # centre of the fill's right cap

        try:
            cv.coords(items["track"],   r, 0, r + usable, h)
            cv.coords(items["track_l"], 0, 0, h, h)
            cv.coords(items["track_r"], w - h, 0, w, h)
            cv.coords(items["fill"],    r, 0, fill_x, h)
            cv.coords(items["fill_l"],  0, 0, h, h)
            cv.coords(items["fill_r"],  fill_x - r, 0, fill_x + r, h)
            # Hide the fill entirely at zero rather than leaving a stray dot
            # of accent sitting at the start of an unplayed track.
            vis = "normal" if frac > 0.0005 else "hidden"
            for k in ("fill", "fill_l", "fill_r"):
                cv.itemconfigure(items[k], state=vis)
        except tk.TclError:
            return

        # Only touch the time labels when the displayed text actually changes.
        # At 30 fps this is 30 config() calls a second on a Label that changes
        # once a second; each one triggers a relayout of the row.
        try:
            el = self._fmt_time(pos)
            if el != getattr(self, "_prog_last_elapsed", None):
                self._prog_last_elapsed = el
                self._prog_elapsed.config(text=el)
            tot = self._fmt_time(dur) if dur and dur > 0 else "--:--"
            if tot != getattr(self, "_prog_last_total", None):
                self._prog_last_total = tot
                self._prog_total.config(text=tot)
        except (tk.TclError, AttributeError):
            pass

    def _repaint_progress_colors(self):
        """Re-apply palette colours to the bar's persistent items.

        _rebuild_all can no longer rely on the bar being redrawn from scratch,
        so the accent/track colours have to be pushed onto the existing items
        explicitly after a theme or accent change."""
        items = getattr(self, "_prog_shapes", None)
        if not items:
            return
        try:
            for k in ("track", "track_l", "track_r"):
                self._prog_cv.itemconfigure(items[k], fill=BG3)
            for k in ("fill", "fill_l", "fill_r"):
                self._prog_cv.itemconfigure(items[k], fill=ACCENT)
            self._prog_cv.config(bg=BG)
        except tk.TclError:
            pass

    def _estimate_pos_ms(self):
        """Best estimate of the current playback position in ms.

        `state.position_ms` is refreshed only on each WS 'position' ping, so
        between pings we add the wall-clock delta while playing. When a fresh
        ping arrives (position changed), we re-anchor."""
        import time as _time
        pos = getattr(state, "position_ms", 0) or 0
        dur = getattr(state, "duration_ms", 0) or 0
        playing = getattr(state, "is_playing", False)
        # Re-anchor when the backend reports a new position (seek / ping).
        if pos != self._last_pos_ms:
            self._last_pos_ms = pos
            self._last_pos_mono = _time.monotonic()
        if dur:
            self._last_dur_ms = dur
        if playing and self._last_pos_mono is not None and self._last_dur_ms:
            advanced = (_time.monotonic() - self._last_pos_mono) * 1000.0
            pos = min(self._last_dur_ms, self._last_pos_ms + advanced)
        return pos

    @staticmethod
    def _fmt_time(ms):
        try:
            s = int(ms) // 1000
        except Exception:
            return "0:00"
        return f"{s // 60}:{s % 60:02d}"

    def _tick_progress(self):
        """Self-rescheduling timer that advances the Now-Playing progress bar.

        The interval is adaptive. A fixed 250 ms tick meant the bar advanced
        in four visible steps per second — the single most obviously "cheap"
        thing in the window. Now that a frame is just six coords() calls we
        can afford 30 fps while a track is actually playing, and back off
        hard when there is nothing to animate: paused playback and an idle
        app (no track loaded) cost a fifth and a twentieth of the old tick
        respectively, so the smoother bar is also cheaper at rest.

        Also skips the redraw entirely when the Now-Playing page isn't the
        visible one — animating a bar nobody is looking at is pure waste."""
        interval = 1000
        try:
            dur = getattr(state, "duration_ms", 0) or 0
            playing = bool(getattr(state, "is_playing", False))
            if dur > 0:
                visible = (self._cur_page == "NOW PLAYING") and not self._hidden
                if visible:
                    self._redraw_progress()
                    interval = 33 if (playing and ANIMATIONS_ENABLED) else 250
                else:
                    interval = 500
        except Exception:
            pass
        # Named slot: re-arming is idempotent and stops automatically once
        # _cancel_all_timers() has run at shutdown.
        self._schedule("progress", interval, self._tick_progress)

    def _start_rl_countdown(self, wait_secs):
        import time
        end_time = time.monotonic() + wait_secs
        def _tick():
            remaining = end_time - time.monotonic()
            if remaining > 0:
                self.lbl_rl.config(text=f"⏸ {remaining:.0f}s", fg=WARN)
                self._schedule("rlcountdown", 500, _tick)
            else:
                self.lbl_rl.config(text="")
                self._cancel("rlcountdown")
        # Arming the named slot cancels any countdown already running, so
        # back-to-back rate-limit events can't stack two tickers.
        self._cancel("rlcountdown")
        _tick()

    def _set_art(self, url):
        if not PIL_AVAILABLE or not url: self._default_art(); return
        # Fetch on a worker thread — urllib.urlopen blocks for up to `timeout`
        # seconds and must NEVER run on the Tk main loop (it freezes the UI).
        # Round on the worker thread too — the 4× mask is the most expensive
        # part of this path and has no business running on the Tk loop.
        surface = BG2
        def _fetch():
            return _round_image(_fetch_art(url, HERO_ART_PX), HERO_ART_RADIUS, surface)
        def _apply(result):
            if result is None:
                self._default_art(); return
            try:
                self._img = ImageTk.PhotoImage(result)
                self.canvas.delete("all"); self.canvas.create_image(0,0, anchor="nw", image=self._img)
            except Exception:
                self._default_art()
        fut = image_executor.submit(_fetch)
        def _done(f):
            try: res = f.result()
            except Exception: res = None
            # PhotoImage MUST be created on the Tk thread.
            self.win.after(0, lambda: _apply(res))
        fut.add_done_callback(_done)

    # ── Log ───────────────────────────────────────────────────────
    def _log(self, msg):
        """Buffer one log line for the next _flush_log().

        Lines are accumulated in _log_buf and flushed in bulk by _flush_log()
        (called once per _poll cycle). Writing to a Tk Text widget is O(n) per
        insert because it reflows, so doing one insert per log line — which the
        backend emits on every lyric change — makes logging the dominant cost
        during playback and progressively freezes the UI. Buffering collapses a
        whole burst into a single widget mutation.

        (There was a second copy of this method, _log_buf_append, identical
        line for line, used by _poll while GUI code used this one.)"""
        if not hasattr(self, "_log_buf"):
            self._log_buf = []
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        low = msg.lower()
        tag = ("g" if "RPC" in msg and not any(x in low for x in ("error", "rate"))
               else "y" if any(x in low for x in ("rate", "drop", "error", "warn"))
               else "m")
        self._log_buf.append((ts, msg, tag))

    def _flush_log(self):
        """Flush buffered log lines to the Text widget in ONE mutation.

        Called at most once per _poll cycle (50ms). This is the key fix for the
        'gets slow over time' symptom: during lyric streaming the backend logs
        on every line, and the old code did a full insert+see+(maybe)delete+
        config cycle for EACH line on the GUI thread — an O(n) reflow every
        time, hundreds of times per song. Batching turns that into a single
        reflow per poll."""
        buf = getattr(self, "_log_buf", None)
        if not buf:
            return
        self._log_buf = []
        # Build the whole block once, insert once, scroll once.
        parts = []
        for ts, msg, tag in buf:
            parts.append((ts + "  ", "ts"))
            parts.append((msg + "\n", tag))
        self.log_txt.config(state="normal")
        for text, tag in parts:
            self.log_txt.insert("end", text, tag)
        self.log_txt.see("end")
        # Trim in chunks (not line-by-line) so the Text widget stays small.
        # Trim to 200 lines whenever it exceeds 300 — a single bulk delete.
        try:
            line_count = int(self.log_txt.index("end-1c").split(".")[0])
            if line_count > 300:
                self.log_txt.delete("1.0", f"{line_count - 200}.0")
        except Exception:
            pass
        self.log_txt.config(state="disabled")

    def _refresh_track_offset(self):
        """Update the 'offset for current track' readout."""
        try:
            uri = getattr(state, "track_uri", "")
            raw = _cfg_get("offsets", offset_key(uri), "") if uri else ""
            if raw == "":
                self.lbl_track_off.config(text=f"global ({LYRIC_DELAY_MS:+d} ms)", fg=MUTED)
            else:
                self.lbl_track_off.config(text=f"{int(raw):+d} ms", fg=ACCENT)
        except (AttributeError, tk.TclError, ValueError):
            pass

    def _set_error(self, msg):
        """Show (or clear) the last-error line under the status dots."""
        try:
            if msg:
                stamp = datetime.datetime.now().strftime("%H:%M")
                self.lbl_err.config(text=f"⚠ {stamp}  {msg}"[:180])
            else:
                self.lbl_err.config(text="")
        except (AttributeError, tk.TclError):
            pass

    # ── Event poll ────────────────────────────────────────────────
    # Max items drained per cycle. Capping this guarantees _poll returns
    # control to Tk within a bounded time even if thousands of events are
    # queued (e.g. a long burst of lyric/log lines after a stall). Anything
    # not processed this cycle stays in the queue for the next tick. Without
    # this cap, a single burst could block the GUI thread for seconds — the
    # classic "becomes unresponsive / glitchy after a while" symptom.
    _POLL_MAX_DRAIN = 40

    def _poll(self):
        # Drain a BOUNDED number of log messages; batch them into one flush.
        drained = 0
        try:
            while drained < self._POLL_MAX_DRAIN:
                self._log(log_queue.get_nowait())
                drained += 1
        except queue.Empty: pass
        try:
            self._flush_log()
        except Exception:
            pass
        # Drain a BOUNDED number of events per cycle.
        drained = 0
        try:
            while drained < self._POLL_MAX_DRAIN:
                ev = event_queue.get_nowait(); k = ev[0]
                if   k == "rpc_ok":
                    self.dot_dc.config(fg=ACCENT)
                    self._set_error("")          # connected — clear stale error
                elif k == "bind_conflict":
                    # A previous Statusify instance is holding our WS port.
                    # Backend is attempting to kill it and retry; tell the user.
                    log(f"⚠ Another Statusify instance detected — closing it…")
                elif k == "bind_error":
                    # Backend could not bind the WS port and could not recover.
                    # This is the "silent freeze" root cause — surface it
                    # loudly so the user knows the app isn't just hanging.
                    msg = ev[1] if len(ev) > 1 else "unknown error"
                    log(f"❌ Cannot start WebSocket: {msg}")
                    log("❌ Close any other Statusify window and restart.")
                    self.lbl_lyric.config(text="Port conflict — restart app", fg="#ff6b6b")
                    self._set_error(f"WebSocket bind failed: {msg}")
                elif k == "sp":          self.dot_sp.config(fg=ACCENT if ev[1] else MUTED)
                elif k == "track":
                    _, ar, ti, art = ev
                    self.lbl_title.config(text=ti); self.lbl_artist.config(text=ar)
                    self.lbl_lyric.config(text="—", fg=MUTED); self.lbl_info.config(text="")
                    # Named slot: rapid skipping coalesces to the latest track
                    # instead of firing one art fetch per skipped song.
                    self._schedule("setart", 30, lambda u=art: self._set_art(u))
                    self._refresh_track_offset()
                elif k == "lyrics":
                    _, src, mode, n = ev
                    lbl = src if mode in ("synced","plain") else "No lyrics"
                    if mode == "plain": lbl += " (plain)"
                    self.lbl_info.config(text=f"{lbl}  ·  {n} lines")
                elif k == "line":
                    t = ev[1]
                    self.lbl_lyric.config(text=t or "—", fg=ACCENT if t and t not in ("—","— ") else MUTED)
                    self._refresh_mini()
                    self._highlight_active_lyric()
                elif k == "paused":      self.lbl_lyric.config(text="Paused", fg=MUTED)
                elif k == "rl":
                    w = ev[1]; self._start_rl_countdown(w)
                elif k == "history_add": self._add_history_row(ev[1])
                elif k == "dropped":
                    n = ev[1]
                    self.lbl_dropped.config(
                        text=(f"{n} line{'s' if n != 1 else ''} dropped" if n else ""),
                        fg=WARN if n else MUTED)
                elif k == "error":
                    self._set_error(ev[1])
                # Event-driven refresh: update the labels only. Must NOT arm a
                # timer — the 5 s chain is owned solely by the timer itself.
                elif k == "stats":       self._refresh_stats(reschedule=False)
                elif k == "hotkey_skip":
                    log("Hotkey: skip track")
                    threading.Thread(target=_send_skip, daemon=True).start()
                elif k == "rpc_err":
                    self.dot_dc.config(fg=MUTED)
                    if len(ev) > 1 and ev[1]:
                        self._set_error(str(ev[1]))
                elif k == "hotkey_skip_instr":
                    pass  # handled directly in _hotkey_skip_instrumental
                elif k == "hotkey_toggle":
                    enabled = ev[1]
                    status  = "enabled" if enabled else "disabled"
                    log(f"Hotkey: RPC {status}")
                    self.dot_dc.config(fg=ACCENT if enabled else MUTED)
                elif k == "update_available":
                    _, tag, url, changelog = ev
                    self._show_update_dialog(tag, url, changelog)
                drained += 1
        except queue.Empty: pass
        self._schedule("poll", 50, self._poll)

    def _show_update_dialog(self, tag, url, changelog):
        """Show a modal dialog asking the user to update, with changelog."""
        import webbrowser
        if getattr(self, "_update_banner_shown", False): return
        self._update_banner_shown = True

        dlg = tk.Toplevel(self.win)
        dlg.title("Update Available")
        dlg.configure(bg=BG)
        dlg.geometry("450x380")
        dlg.resizable(False, False)
        dlg.grab_set()

        tk.Label(dlg, text=f"Statusify v{tag} is available!", fg=TEXT, bg=BG,
                 font=self._f(12, True)).pack(pady=(16, 4))
        tk.Label(dlg, text="New changes since your version:", fg=TEXT2, bg=BG,
                 font=self._f(9)).pack()

        # Changelog frame
        cf = tk.Frame(dlg, bg=BG2)
        cf.pack(fill="both", expand=True, padx=20, pady=16)

        scrollbar = tk.Scrollbar(cf, bg=BG3, troughcolor=BG2, relief="flat", width=12, bd=0)
        scrollbar.pack(side="right", fill="y")
        txt = tk.Text(cf, bg=BG2, fg=TEXT2, font=self._f(8), relief="flat",
                      wrap="word", yscrollcommand=scrollbar.set, padx=10, pady=10)
        txt.pack(side="left", fill="both", expand=True)
        scrollbar.config(command=txt.yview)

        txt.insert("end", changelog)
        txt.config(state="disabled")

        bf = tk.Frame(dlg, bg=BG)
        bf.pack(fill="x", pady=(0, 20))
        btn_no = tk.Label(bf, text="LATER", fg=MUTED, bg=BG, font=self._f(8, True), cursor="hand2")
        btn_no.pack(side="left", padx=30)
        btn_no.bind("<Button-1>", lambda e: dlg.destroy())

        btn_yes = tk.Label(bf, text="DOWNLOAD", fg=ACCENT_FG, bg=ACCENT, font=self._f(8, True), cursor="hand2", padx=16, pady=6)
        btn_yes.pack(side="right", padx=30)
        def _yes(): webbrowser.open(url); dlg.destroy()
        btn_yes.bind("<Button-1>", lambda e: _yes())


# ── Backend ───────────────────────────────────────────────────────
def run_backend(loop):
    asyncio.set_event_loop(loop); loop.run_until_complete(_backend())

async def _backend():
    if not DISCORD_APP_ID: log("ERROR: Missing DISCORD_APP_ID in .env"); return

    # Start the WebSocket server first so Spicetify can connect regardless of
    # whether Discord RPC is available yet (e.g. Discord not open yet, or a
    # game is currently holding the pipe).
    #
    # CRITICAL: bind failures (port already in use) MUST be caught here. An
    # uncaught OSError used to silently kill the daemon backend thread —
    # the Tk window kept drawing but no track updates arrived, looking like
    # a freeze, and re-launches hit the same failure ("can't open again").
    _ws_server = None
    global _WS_SERVER
    for attempt in (1, 2):
        try:
            _ws_server = await websockets.serve(ws_handler, WS_HOST, WS_PORT)
            _WS_SERVER = _ws_server  # expose for graceful shutdown in _quit()
            break
        except OSError as e:
            # WinError 10048 = address in use. Most commonly an orphaned
            # previous Statusify instance that didn't release the port.
            log(f"WebSocket bind failed (attempt {attempt}): {e}")
            if attempt == 1 and _is_port_in_use(WS_PORT):
                log("Port 8765 is held — attempting to kill orphaned instance…")
                event_queue.put(("bind_conflict", str(e)))
                # Run the blocking orphan-kill on a worker, then retry once.
                killed = await asyncio.get_event_loop().run_in_executor(
                    None, _kill_orphan_instance)
                if killed:
                    await asyncio.sleep(0.5)
                    continue
            # Could not recover — surface a clear error and stop.
            event_queue.put(("bind_error", str(e)))
            log("FATAL: cannot bind WebSocket. Another instance running?")
            return

    if _ws_server is None:
        # Belt-and-braces: never proceed without a server.
        event_queue.put(("bind_error", "WebSocket server did not start"))
        return

    log(f"WebSocket ready  ·  ws://{WS_HOST}:{WS_PORT}")
    log("Open Spotify to begin")

    # Keep trying to connect to Discord RPC.  If the connection drops (e.g.
    # Discord restarts, or a game briefly takes the pipe) we wait 15 s and
    # reconnect automatically — lyrics keep streaming to Discord as soon as
    # the pipe becomes available again.
    while True:
        rpc = DiscordRPC(DISCORD_APP_ID)
        _ACTIVE_RPC["rpc"] = rpc   # expose to the GUI for reconnect / test
        try:
            await rpc.connect()
        except RuntimeError as e:
            log(f"RPC unavailable: {e}  — retrying in 15s")
            event_queue.put(("rpc_err", f"Discord unreachable: {e} (retrying in 15s)"))
            await asyncio.sleep(15)
            continue
        # Connected — run the RPC update loop until the pipe breaks
        await rpc_loop(rpc)
        # rpc_loop returned (pipe died) — wait briefly then reconnect
        log("RPC disconnected — reconnecting in 5s")
        event_queue.put(("rpc_err", "Discord pipe closed (reconnecting in 5s)"))
        await asyncio.sleep(5)

_backend_loop = None  # set in __main__, used by _send_skip
_WS_SERVER   = None   # set in _backend(), closed in _quit() to release port

# ── Feature 1: First-run setup wizard ────────────────────────────
def _run_setup_wizard():
    """If DISCORD_APP_ID is missing/empty, show a blocking modal dialog."""
    global DISCORD_APP_ID
    if DISCORD_APP_ID:
        return  # Already configured — nothing to do

    import webbrowser
    root = tk.Tk()
    try:
        import ctypes
        dpi = ctypes.windll.user32.GetDpiForSystem()
        root.tk.call('tk', 'scaling', dpi / 72.0)
    except Exception:
        pass
    root.withdraw()  # hide the blank root; we only want the toplevel

    dlg = tk.Toplevel(root)
    dlg.title("Statusify — First-run Setup")
    dlg.resizable(False, False)
    dlg.configure(bg="#0a0a0a")
    dlg.grab_set()
    dlg.focus_force()

    # Center the dialog
    dlg.update_idletasks()
    w, h = 420, 250
    sw = dlg.winfo_screenwidth(); sh = dlg.winfo_screenheight()
    dlg.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")

    tk.Label(dlg, text="Welcome to Statusify", fg="#ffffff", bg="#0a0a0a",
             font=("Segoe UI", 14, "bold")).pack(pady=(24, 4))
    tk.Label(dlg, text="Paste your Discord Application ID below.\n"
             "Create one free at discord.com/developers/applications",
             fg="#b3b3b3", bg="#0a0a0a", font=("Segoe UI", 9),
             justify="center", wraplength=380).pack(pady=(0, 6))

    link = tk.Label(dlg, text="Open Discord Developer Portal ↗",
                    fg="#1db954", bg="#0a0a0a",
                    font=("Segoe UI", 9, "underline"), cursor="hand2")
    link.pack()
    link.bind("<Button-1>", lambda e: webbrowser.open(
        "https://discord.com/developers/applications"))

    tk.Frame(dlg, bg="#2a2a2a", height=1).pack(fill="x", padx=24, pady=12)

    entry_var = tk.StringVar()
    ent = tk.Entry(dlg, textvariable=entry_var, bg="#181818", fg="#ffffff",
                   insertbackground="#ffffff", relief="flat",
                   font=("Segoe UI", 11), justify="center")
    ent.pack(fill="x", padx=24, ipady=6)
    ent.focus_set()

    err_lbl = tk.Label(dlg, text="", fg="#e05555", bg="#0a0a0a",
                       font=("Segoe UI", 8))
    err_lbl.pack(pady=(4, 0))

    def _save():
        global DISCORD_APP_ID
        val = entry_var.get().strip()
        if not val.isdigit() or len(val) < 16:
            err_lbl.config(text="App ID must be a long numeric ID (e.g. 1480612100416999474)")
            return
        DISCORD_APP_ID = val
        # Write to .env
        try:
            with open(_ENV_PATH, "a", encoding="utf-8") as f:
                f.write(f"\nDISCORD_APP_ID={val}\n")
        except Exception:
            pass
        dlg.destroy()

    btn = tk.Label(dlg, text="SAVE & CONTINUE", fg="#0a0a0a", bg="#1db954",
                   font=("Segoe UI", 9, "bold"), cursor="hand2",
                   padx=16, pady=6)
    btn.pack(pady=(8, 0))
    btn.bind("<Button-1>", lambda e: _save())
    ent.bind("<Return>", lambda e: _save())

    root.wait_window(dlg)
    root.destroy()

# ── Feature 2: Auto-update checker ───────────────────────────────
def _check_for_updates():
    """Background thread: fetches all releases and compiles changelog."""
    if _GITHUB_REPO == "owner/repo": return
    try:
        import urllib.request as _req
        url = f"https://api.github.com/repos/{_GITHUB_REPO}/releases"
        req = _req.Request(url, headers={"User-Agent": "Statusify/UpdateChecker"})
        with _req.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())

        cur = _VERSION.lstrip("v")
        def _sv(v):
            try: return tuple(int(x) for x in v.split("."))
            except (TypeError, ValueError): return (0, 0, 0)

        latest_tag = None
        latest_url = ""
        changelog_lines = []

        for release in data:
            tag = release.get("tag_name", "").lstrip("v")
            if not tag: continue

            if _sv(tag) > _sv(cur):
                if not latest_tag:
                    latest_tag = tag
                    latest_url = release.get("html_url", "")

                body = release.get("body", "").strip()
                changelog_lines.append(f"• v{tag}")
                if body:
                    for ln in body.splitlines():
                        if ln.strip():
                            changelog_lines.append(f"  {ln}")
                changelog_lines.append("")

        if latest_tag:
            event_queue.put(("update_available", latest_tag, latest_url, "\n".join(changelog_lines).strip()))
            log(f"Update available: v{latest_tag} (with {len(changelog_lines)} lines of notes)")
    except Exception as e:
        log(f"Update check failed: {e}")

def _ensure_dependencies():
    """Install any missing optional libraries, then refresh the Spicetify bridge.

    Only the *optional* packages are handled here — websockets and
    python-dotenv are imported at module scope and are bootstrapped by
    _pip_install() up there, long before this function can run."""
    import importlib
    deps    = ["pypresence", "pillow", "keyboard", "pystray"]
    mapping = {"pillow": "PIL"}
    missing = []

    if _FROZEN:
        # Everything is compiled in. Anything genuinely absent from the bundle
        # is a build-time packaging mistake that pip cannot fix at runtime, so
        # skip straight to refreshing the bridge rather than printing install
        # advice the user has no way to act on.
        _install_bridge()
        return

    for d in deps:
        try:
            importlib.import_module(mapping.get(d, d))
        except ImportError:
            missing.append(d)

    if missing:
        log(f"Installing missing libraries: {', '.join(missing)}")
        print(f"Statusify v{_VERSION}")
        print(f"Missing libraries: {', '.join(missing)}")
        print("Installing dependencies, please wait...")
        if _pip_install(missing):
            print("Dependencies installed successfully!\n")
            log("Dependencies installed — restart Statusify to enable them")
        else:
            msg = "pip install " + " ".join(missing)
            print(f"Error installing dependencies. Please run: {msg}")
            log(f"Dependency install failed — run: {msg}")

    _install_bridge()


def _injected_bridge_path():
    """Where Spicetify actually loads the bridge from, or None.

    %APPDATA%\\spicetify\\Extensions is only the SOURCE folder. `spicetify
    apply` injects a copy into Spotify's unpacked xpui bundle, and that copy
    is the one that runs. Restarting Spotify re-runs the injected copy — it
    does not re-read the source. This distinction is the whole bug below.
    """
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    return os.path.join(appdata, "Spotify", "Apps", "xpui",
                        "extensions", "lyrics-bridge.js")


def _bridge_needs_apply():
    """True when the bridge Spotify is running differs from the one we ship.

    Returns False when we can't tell (no injected copy found, unreadable),
    because a spurious "run spicetify apply" nag is worse than silence.
    """
    inj = _injected_bridge_path()
    if not inj or not os.path.exists(inj):
        return False
    src = os.path.join(_RES_DIR, "lyrics-bridge.js")
    if not os.path.exists(src):
        return False
    try:
        with open(src, "rb") as a, open(inj, "rb") as b:
            return a.read() != b.read()
    except OSError:
        return False


def _install_bridge():
    """Copy lyrics-bridge.js into the Spicetify Extensions folder if stale.

    Then check whether that copy has actually reached Spotify. It usually has
    NOT: writing the source file is only half the job, and for a long time
    this function did only that half while telling the user to "restart
    Spotify" — advice that cannot work, because Spotify re-runs the injected
    copy in its xpui bundle and never looks at the source folder. The result
    was an eleven-day-old bridge running against a Spicy Lyrics API version it
    had long since stopped accepting, so every track came back with no lyrics
    while the official Spicy Lyrics panel showed them perfectly.
    """
    import shutil
    # _RES_DIR, not _APP_DIR: this is a file we ship, and in a frozen build it
    # is unpacked into the PyInstaller bundle rather than sitting beside the exe.
    js_src = os.path.join(_RES_DIR, "lyrics-bridge.js")
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return
    if not os.path.exists(js_src):
        log("lyrics-bridge.js is missing from the app folder — lyrics will not work")
        return
    ext_dir = os.path.join(appdata, "spicetify", "Extensions")
    if not os.path.isdir(ext_dir):
        print("[!] Spicetify extensions folder not found.")
        print("  Please install Spicetify first for lyrics to work!")
        log("Spicetify Extensions folder not found — install Spicetify for lyrics")
        return
    js_dest = os.path.join(ext_dir, "lyrics-bridge.js")
    try:
        # Compare content, not size. A same-size edit (e.g. bumping
        # SPICY_VERSION from "5.19.12" to "6.11.2") left the stale copy in
        # place, which is exactly the failure mode the version pin caused
        # before: the bridge silently kept using the old API version.
        need_copy = True
        if os.path.exists(js_dest):
            with open(js_src, "rb") as a, open(js_dest, "rb") as b:
                need_copy = a.read() != b.read()
        if need_copy:
            shutil.copy2(js_src, js_dest)
            print(f"[Bridge] Installed to: {js_dest}")
        else:
            print("[Bridge] Already up to date.")
    except OSError as e:
        print(f"Could not install bridge: {e}")
        log(f"Could not install Spicetify bridge: {e}")
        return

    # The copy above is necessary but NOT sufficient. Ask the only question
    # that actually predicts whether lyrics will work: is the bridge running
    # inside Spotify the same one we just wrote?
    global _BRIDGE_UPDATED
    _BRIDGE_UPDATED = _bridge_needs_apply()
    if _BRIDGE_UPDATED:
        log("Spicetify bridge is OUT OF DATE inside Spotify — "
            "run `spicetify apply` (restarting Spotify is not enough)")
    else:
        log("Spicetify bridge is current")

if __name__ == "__main__":
    # Install crash handlers FIRST, before anything else can fail. Under
    # pythonw.exe there is no console, so without these an exception during
    # startup produces absolutely no output anywhere.
    _install_crash_handlers()

    # Single-instance guard (#6). A held mutex normally means a live copy is
    # already running, so we bow out. But if the mutex is held while the WS
    # port is FREE, the holder is a dead or dying process that hasn't been
    # reaped yet — in that case carry on and let the existing orphan cleanup
    # deal with it, rather than locking the user out of their own app.
    if not _acquire_single_instance():
        if _is_port_in_use(WS_PORT):
            # A copy is already running. Launching the app again is the user
            # asking to SEE it, not an error — so hand the request to the
            # running instance and exit quietly. Popping up "already running"
            # and doing nothing was useless: if that instance was hidden in
            # the tray, the app became effectively unopenable.
            _request_show()
            sys.exit(0)
        else:
            log("Stale single-instance mutex detected — continuing")

    _ensure_dependencies()
    # DPI awareness — prevents blurry scaling on HiDPI screens
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # Per-monitor DPI aware
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()  # fallback
        except Exception:
            pass
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Statusify.App.1")
    except Exception:
        pass
    # Feature 1: show setup wizard if no App ID is configured
    _run_setup_wizard()
    _migrate_offset_keys()
    _load_history()
    # Off the Tk thread: scandir + unlink over a few hundred files should never
    # be able to stall the first paint.
    image_executor.submit(_prune_art_cache)
    app = App()
    _backend_loop = asyncio.new_event_loop()
    threading.Thread(target=run_backend, args=(_backend_loop,), daemon=True).start()
    # Feature 2: check for updates in background after app starts (5s delay to
    # let UI settle). daemon=True because threading.Timer is non-daemon by
    # default, and a pending one would hold the process open for its full
    # delay — the same class of shutdown hang as the thread pools.
    _upd = threading.Timer(5.0, _check_for_updates)
    _upd.daemon = True
    _upd.start()
    app.mainloop()

    # Backstop. The normal exit path is _quit() -> _teardown_and_exit(), which
    # never returns. Reaching here means the root window went away by some
    # other route, and simply falling off the end of __main__ would drop us
    # into the interpreter's thread-joining shutdown — the exact hang that
    # left pythonw.exe running with no window, holding the single-instance
    # mutex and port 8765 so the app could not be started again.
    _teardown_and_exit()
