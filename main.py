"""
Spotify -> Discord Rich Presence (via Spicetify lyrics-bridge extension)
"""

import asyncio, os, json, time, threading, queue, datetime, base64
import socket as _socket
import subprocess
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk
from concurrent.futures import ThreadPoolExecutor
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
# STATUSIFY_DATA_DIR overrides it — the test suite points it at a temp folder
# so a test run never writes into the real statusify.log / history / config.
_APP_DIR = (os.environ.get("STATUSIFY_DATA_DIR")
            or (os.path.dirname(os.path.abspath(sys.executable)) if _FROZEN
                else os.path.dirname(os.path.abspath(__file__))))
_RES_DIR = (getattr(sys, "_MEIPASS", _APP_DIR) if _FROZEN
            else os.path.dirname(os.path.abspath(__file__)))

# Pure lyric helpers live in their own module so they can be unit-tested
# without importing tkinter/winreg (see tests/test_lyrics.py). Imported up
# here rather than halfway down the file because _track_offset_ms — defined
# long before the old import site — now depends on resolve_offset_ms.
from statusify_lyrics import (join_lines, select_line, resolve_offset_ms,
                              offset_key, _calc_instrumental_gaps,
                              clean_title, pick_lrclib)

# Global hotkeys use RegisterHotKey (statusify_hotkeys), not the `keyboard`
# library: its low-level hook went deaf whenever a game had focus and fired on
# every auto-repeat of a held key. See tests/test_hotkeys.py.
try:
    from statusify_hotkeys import HotkeyManager as _HotkeyManager
    KEYBOARD_AVAILABLE = True
except Exception:
    KEYBOARD_AVAILABLE = False
_HOTKEYS = None   # HotkeyManager, created on first registration

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
    from PIL import Image, ImageTk
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
    def _cleanup(p=tf.name):
        # Windows refuses the delete while Tk still has the icon open, which
        # is normal at exit; the temp folder cleans it up later.
        try:
            os.unlink(p)
        except OSError:
            pass
    _atexit.register(_cleanup)
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
import statusify_maintenance as _maint
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
# Seconds of song time one presence update has to buy to be sustainable.
# Discord's SET_ACTIVITY limit is a server-side quota — it cannot be raised or
# opted out of — so the only lever is spending each slot on more song.
RPC_BUDGET_S      = RATE_LIMIT_WINDOW / RATE_LIMIT_CALLS   # 4.0 s per call
# Headroom on top of the budget. The loop ticks at 20 Hz and the Spicetify
# bridge pings position only every few seconds, so a group aimed exactly at
# 4.0 s lands under it about half the time.
GROUP_MARGIN_MS   = 750
MAX_STATE         = 128
LYRIC_DELAY_MS    = 0       # user-adjustable lyric timing offset (ms)
_ENV_PATH         = os.path.join(_APP_DIR, ".env")

# ── Persistent config ─────────────────────────────────────────────
_CONFIG_PATH = os.path.join(_APP_DIR, "statusify.cfg")
_HIST_FILE   = os.path.join(_APP_DIR, "history.json")   # legacy; migrated once
_HIST_DB     = os.path.join(_APP_DIR, "history.db")

import statusify_config as _cfg_mod
_cfg_mod.init(_CONFIG_PATH, log)
from statusify_config import (_load_config, _save_config, _cfg_get, _cfg_set,
                              _cfg_flush, _cfg_set_soon, _CFG_LOCK)

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
# Lyric sheet quality: "auto" measures frame cost and steps down on slow
# PCs; "high" always animates everything; "low" keeps the background still.
RENDER_QUALITY = _cfg_get("preferences", "render_quality", "auto").lower()
if RENDER_QUALITY not in ("auto", "high", "low"):
    RENDER_QUALITY = "auto"
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

import statusify_art as _art_mod
_art_mod.init(os.path.join(_APP_DIR, ".artcache"), log)
from statusify_art import _prune_art_cache, _round_image, _fetch_art, dominant_tint as _dominant_tint

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
    """(Re)register all three hotkeys. Safe to call repeatedly: each call
    replaces the previous set, so a settings change needs no separate unhook."""
    global _hotkey_registered, _HOTKEYS
    if not KEYBOARD_AVAILABLE:
        return
    try:
        if _HOTKEYS is None:
            _HOTKEYS = _HotkeyManager()
        skip_combo       = _hotkey_skip_combo.strip()
        toggle_combo     = _hotkey_toggle_combo.strip()
        skip_instr_combo = _hotkey_skip_instr_combo.strip()
        failed = _HOTKEYS.set({
            "skip":       (skip_combo,       lambda: _hotkey_skip(app_ref)),
            "toggle":     (toggle_combo,     lambda: _hotkey_toggle(app_ref)),
            "skip_instr": (skip_instr_combo, _hotkey_skip_instrumental),
        })
        _hotkey_registered = True
        log(f"Hotkeys registered  ·  skip={skip_combo or 'none'}  toggle={toggle_combo or 'none'}  skip_instr={skip_instr_combo or 'none'}")
        # A combo another program already owns used to fail silently; say so.
        for name, why in failed.items():
            log(f"Hotkey '{name}' not active: {why}")
            event_queue.put(("error", f"Hotkey not active: {why}"))
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

import statusify_startup as _startup_mod
_startup_mod.init(_APP_DIR, _RES_DIR, _FROZEN, log)
from statusify_startup import _get_startup_enabled, _set_startup_enabled

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

    # 3b. Force any debounced config write to disk (os._exit skips its timer).
    try:
        _cfg_flush()
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
    duration_ms = 0
    is_playing  = False
    lyrics_mode        = "none"
    instrumental_gaps  = []  # pre-calculated list of {startMs, endMs, gap_ms, key}
    synced = []; plain = []
    blacklisted        = False  # current track matches the user's blacklist
    _position_ms = 0
    _pos_mono    = None        # time.monotonic() when _position_ms was last set

    @property
    def position_ms(self):
        """Live playback position. The Spicetify bridge only pings position
        every few seconds — and sometimes as sparsely as once a minute — so
        every lyric helper reading this used to sit on one line until the next
        ping. Interpolate with the wall clock while playing so lyric selection
        advances continuously; a fresh ping re-anchors via the setter.
        ponytail: wall-clock interpolation, re-anchored on every position ping."""
        if self.is_playing and self._pos_mono is not None:
            live = self._position_ms + (time.monotonic() - self._pos_mono) * 1000.0
            return int(min(self.duration_ms, live) if self.duration_ms else live)
        return self._position_ms

    @position_ms.setter
    def position_ms(self, value):
        self._position_ms = int(value)
        self._pos_mono = time.monotonic()

state = State()

# History entries shown in the UI, oldest first:
# [{id, track_uri, artist, title, album_art, played_at, time, mode, synced, plain}]
# The durable copy is _HISTORY_STORE (SQLite, statusify_history), written as
# each play happens — not on quit, which a kill/logoff/update never reached.
history = []
_HISTORY_STORE = None
# The play in progress: its row id, the session-listen clock when it started,
# and its in-memory entry once lyrics have arrived.
_current_play = {"id": None, "listen_start": 0.0, "entry": None}

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
def _line_index(w):
    """Index of `w` in state.synced, disambiguating repeats by playback position.

    get_line_dur and get_nth each re-derive this with their own full scan, so
    pick_group used to walk the whole lyric sheet three times to build one
    group. Resolve it once and walk forward from there."""
    pos = state.position_ms + _track_offset_ms()
    best = None
    for i, e in enumerate(state.synced):
        if e["words"] == w:
            if best is None or abs(e["startMs"] - pos) < abs(best[0] - pos):
                best = (e["startMs"], i)
    return best[1] if best else None

def pick_group(line1):
    """Pick the lyric lines to publish in one presence update.

    Discord allows RATE_LIMIT_CALLS SET_ACTIVITY frames per RATE_LIMIT_WINDOW
    seconds, so a sustainable update buys at least RPC_BUDGET_S of song time.
    The old version chose the group from the FIRST line's duration alone, with
    a single-line cutoff at 3500 ms — which meant every line lasting 3.5-4.0 s
    was published on its own and scheduled the next call under the 4.0 s
    budget. The live log showed the consequence precisely: single-line calls
    sat at a median spacing of exactly 4.0 s, zero headroom, and the limiter
    absorbed the overrun as lyric lag.

    So group by what actually matters — cumulative coverage — rather than by
    the head line's duration, packing lines until the group spans a budget
    period (plus a margin for tick and position-ping jitter) or runs into
    MAX_STATE. Fast passages group more, slow passages stay on one line
    exactly as before."""
    if state.lyrics_mode != "synced" or not state.synced: return [line1], 0
    i = _line_index(line1)
    if i is None: return [line1], 0

    def dur(j):
        nxt = state.synced[j+1]["startMs"] if j+1 < len(state.synced) else state.duration_ms
        return max(0, nxt - state.synced[j]["startMs"])

    target = RPC_BUDGET_S * 1000 + GROUP_MARGIN_MS
    group   = [line1]
    covered = dur(i)
    j = i + 1
    while covered < target and j < len(state.synced):
        cand = group + [state.synced[j]["words"]]
        if len(join_lines(cand)) > MAX_STATE: break
        group = cand; covered += dur(j); j += 1
    return group, len(group) - 1

# Handle on the live DiscordRPC instance so the GUI can act on it (force a
# reconnect, send a test presence). A dict rather than a bare global so the
# backend can swap the instance on every reconnect without the GUI holding a
# stale reference.
_ACTIVE_RPC = {"rpc": None}

# ── Discord RPC ───────────────────────────────────────────────────
# The pipe client lives in statusify_rpc; it gets our logger, event queue
# and pipe-read executor injected rather than importing main.
import statusify_rpc as _rpc_mod
from statusify_rpc import DiscordRPC
_rpc_mod.configure(log, event_queue.put, _recv_executor, executor, MAX_STATE,
                   current_uri=lambda: state.track_uri)
_rpc_mod.status_display_type = (
    _rpc_mod.STATUS_DISPLAY_DETAILS
    if _cfg_get("preferences", "status_shows_song", "true").lower() == "true"
    else _rpc_mod.STATUS_DISPLAY_NAME)
_rpc_mod.link_track = _cfg_get("preferences", "link_track", "true").lower() == "true"

# ── WebSocket ─────────────────────────────────────────────────────
def _apply_lyrics(mode, synced, plain, src):
    """Make these the current track's lyrics (bridge, cache or LRCLIB)."""
    state.lyrics_mode = mode; state.synced = synced; state.plain = plain
    state.instrumental_gaps = (_calc_instrumental_gaps(synced, state.duration_ms)
                               if mode == "synced" else [])
    n = len(synced) or len(plain)
    log(f"Lyrics ({src})  ·  {mode}  ·  {n} lines")
    event_queue.put(("lyrics", src, mode, n))
    _save_history(mode, synced, plain, src)

# ── LRCLIB fallback ───────────────────────────────────────────────
# Third source, tried only after the bridge reports no lyrics from Spicy or
# Spotify. One lookup per track per session, off the event loop.
LRCLIB_ENABLED = _cfg_get("preferences", "lrclib_fallback", "true").lower() == "true"
_LRCLIB_URL    = "https://lrclib.net/api/search"
_LRCLIB_TRIED  = set()   # track URIs already looked up this session
_LRCLIB_TASKS  = set()   # strong refs, so pending tasks are not GC'd

def _lrclib_search(artist, title):
    import urllib.request, urllib.parse
    q = urllib.parse.urlencode({"track_name": clean_title(title), "artist_name": artist})
    req = urllib.request.Request(
        f"{_LRCLIB_URL}?{q}",
        # LRCLIB asks clients to identify themselves.
        headers={"User-Agent": f"Statusify/{_VERSION} (https://github.com/{_GITHUB_REPO})"})
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read().decode("utf-8"))

def _maybe_fetch_lrclib(uri):
    if not LRCLIB_ENABLED or not uri or uri in _LRCLIB_TRIED:
        return
    _LRCLIB_TRIED.add(uri)
    task = asyncio.get_running_loop().create_task(
        _lrclib_task(uri, state.artist, state.title, state.duration_ms))
    _LRCLIB_TASKS.add(task)
    task.add_done_callback(_LRCLIB_TASKS.discard)

async def _lrclib_task(uri, artist, title, duration_ms):
    loop = asyncio.get_running_loop()
    results = None
    for attempt in (1, 2):
        try:
            results = await loop.run_in_executor(None, _lrclib_search, artist, title)
            break
        except Exception as e:
            # LRCLIB answers 503 under load now and then; one retry covers it.
            if attempt == 2 or getattr(e, "code", 500) < 500:
                log(f"LRCLIB lookup failed: {type(e).__name__}: {e}")
                return
            await asyncio.sleep(3)
    picked = pick_lrclib(results, duration_ms)
    if not picked:
        log(f"LRCLIB: no match for {artist} — {title}")
        return
    # The user may have skipped, or lyrics may have arrived meanwhile.
    if state.track_uri != uri or state.lyrics_mode != "none":
        return
    _apply_lyrics(*picked, "LRCLIB")

def _handle_pause():
    state.is_playing = False
    event_queue.put(("paused",))
    _on_track_pause()
    _finish_play()

def _send_bridge(obj):
    """Send one command to the Spicetify bridge from any thread. Returns
    False when Spotify isn't connected."""
    ws = _spicetify_ws
    if ws is None or _backend_loop is None:
        return False
    try:
        asyncio.run_coroutine_threadsafe(ws.send(json.dumps(obj)), _backend_loop)
        return True
    except Exception:
        return False

def player_command(action):
    """prev / next / toggle / play / pause, sent to Spotify via the bridge."""
    return _send_bridge({"type": "player", "action": action})

def seek_to(ms):
    """Seek Spotify to `ms`. The UI moves at once; the bridge confirms."""
    ms = max(0, int(ms))
    if not _send_bridge({"type": "seek", "position_ms": ms}):
        return False
    state.position_ms = ms
    return True

async def ws_handler(ws):
    global _spicetify_ws, _dropped_lines, _BRIDGE_UPDATED
    _spicetify_ws = ws
    log("Spicetify connected"); event_queue.put(("sp", True))
    # A repair restarts Spotify, so the bridge reconnecting is the moment to
    # re-check; otherwise the launch-time warning outlives the fix.
    if _BRIDGE_UPDATED and not _bridge_needs_apply():
        _BRIDGE_UPDATED = False
        log("Spicetify bridge is current")
        event_queue.put(("bridge_ok",))
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
                # Older bridges repeat "paused" every 500 ms. Act on the
                # transition only: each one wrote SQLite and re-ran the stats
                # queries on the Tk thread, twice a second, for as long as
                # playback stayed paused.
                if state.is_playing:
                    _handle_pause()
            elif t == "track_change":
                state.artist    = data.get("artist",""); state.title = data.get("title","")
                state.album_art = data.get("album_art","")
                state.duration_ms = int(data.get("duration_ms",0))
                state.track_uri = data.get("track_uri","")
                state.lyrics_mode = "none"; state.synced = []; state.plain = []
                # Gaps belong to the old lyric sheet. Left in place, rpc_loop
                # (which checks gaps before lyrics) published a phantom
                # "instrumental" for the next track's first seconds.
                state.instrumental_gaps = []
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
                _start_play()
                # A track heard before has its lyrics on disk: use them now
                # rather than waiting on the network. The bridge still fetches;
                # real lyrics from it replace these, a miss does not.
                cached = _cached_lyrics(state.track_uri)
                if cached:
                    c_mode, c_synced, c_plain, _ = cached
                    _apply_lyrics(c_mode, c_synced, c_plain, "cache")
            elif t == "lyrics":
                mode   = data.get("mode", data.get("lyrics_mode","none"))
                synced = data.get("synced",[]); plain = data.get("plain",[])
                uri    = data.get("track_uri","")
                src    = data.get("source", "Spicy" if mode=="synced" and synced else "fallback")
                # Accept only this track's lyrics. The old fallback clause
                # (`or state.lyrics_mode == "none"`) was always true right
                # after a track_change, so lyrics still in flight for the
                # PREVIOUS track were adopted by the new one. A missing uri
                # is still accepted for bridges that predate the field.
                if (mode == "none" and state.lyrics_mode != "none"
                        and (uri == state.track_uri or not uri)):
                    # Already showing cached lyrics; a failed fetch keeps them.
                    log(f"Lyrics ({src})  ·  none  ·  keeping cached lyrics")
                elif uri == state.track_uri or (not uri and state.lyrics_mode == "none"):
                    _apply_lyrics(mode, synced, plain, src)
                    if mode == "none":
                        _maybe_fetch_lrclib(state.track_uri)
            elif t == "position":
                was = state.is_playing
                state.position_ms = int(data.get("position_ms",0))
                state.duration_ms = int(data.get("duration_ms", state.duration_ms))
                playing = bool(data.get("is_playing", True))
                if was and not playing:
                    _handle_pause()
                state.is_playing = playing
                if not was and playing:
                    _on_track_resume()
                    event_queue.put(("resumed",))
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

def _store():
    """The history store, or None when history is off or failed to open."""
    return _HISTORY_STORE if SAVE_HISTORY else None

def _finish_play():
    """Write the listening time of the play in progress."""
    st = _store()
    if st and _current_play["id"]:
        try:
            st.set_listened(_current_play["id"],
                            (_get_listen_time() - _current_play["listen_start"]) * 1000)
        except Exception as e:
            log(f"Could not save listening time: {e}")

def _start_play():
    """Record a new play for the track in `state`. Called on track_change."""
    _finish_play()
    _current_play["id"] = None
    _current_play["entry"] = None
    _current_play["listen_start"] = _get_listen_time()
    st = _store()
    if st and state.track_uri:
        try:
            _current_play["id"] = st.record_play(state.track_uri, state.artist,
                                                 state.title, state.album_art)
        except Exception as e:
            log(f"Could not record play: {e}")

def _save_history(mode, synced, plain, source=""):
    """Attach lyrics to the play in progress and show it in the History tab.

    Lyrics can arrive more than once for one play (cache, then bridge), so
    the entry is updated in place after the first time. A replay of the same
    song is a new play and gets its own row."""
    st = _store()
    if st:
        try:
            st.save_lyrics(state.track_uri, mode, synced, plain, source)
        except Exception as e:
            log(f"Could not cache lyrics: {e}")
    entry = _current_play["entry"]
    if entry is not None and entry["track_uri"] == state.track_uri:
        entry["synced"] = synced; entry["plain"] = plain; entry["mode"] = mode
        return
    now = datetime.datetime.now().replace(microsecond=0)
    entry = {
        "id": _current_play["id"], "track_uri": state.track_uri,
        "artist": state.artist, "title": state.title,
        "album_art": state.album_art, "mode": mode, "synced": synced, "plain": plain,
        "played_at": now.isoformat(), "time": now.strftime("%H:%M"),
    }
    _current_play["entry"] = entry
    history.append(entry)
    # Bounds the in-memory list (each entry holds a lyric sheet). The full
    # history stays in the database, where search still reaches it.
    while len(history) > MAX_HISTORY_ROWS:
        history.pop(0)
    # The event carries the entry itself rather than its index. Indices shift
    # the moment the front is trimmed, which would repoint every already-
    # rendered row's LYRICS button at the wrong song.
    event_queue.put(("history_add", entry))

def _cached_lyrics(uri):
    """(mode, synced, plain, source) from the local lyric cache, or None."""
    st = _HISTORY_STORE
    if not st:
        return None
    try:
        return st.get_lyrics(uri)
    except Exception as e:
        log(f"Lyric cache read failed: {e}")
        return None

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
    """Open the history database (migrating history.json once) and load the
    newest entries for the History tab."""
    global history, _HISTORY_STORE
    from statusify_history import HistoryStore
    try:
        _HISTORY_STORE = HistoryStore(_HIST_DB)
    except Exception as e:
        log(f"Could not open history database: {e}")
        _HISTORY_STORE = None
        return
    if os.path.exists(_HIST_FILE):
        try:
            n = _HISTORY_STORE.import_json(_HIST_FILE)
            log(f"Migrated {n} entries from history.json")
        except Exception as e:
            log(f"Could not migrate history.json: {e}")
    if not SAVE_HISTORY:
        return
    try:
        history = _HISTORY_STORE.recent(MAX_HISTORY_ROWS)
        log(f"Loaded {len(history)} history entries  ·  {_HISTORY_STORE.count()} plays on record")
    except Exception as e:
        log(f"Could not load history: {e}")

def _persist_history():
    """On quit: store the current play's listening time, or — with history
    turned off — wipe what is on record, as the setting promises."""
    st = _HISTORY_STORE
    if not st:
        return
    if SAVE_HISTORY:
        _finish_play()
    else:
        try:
            st.clear(); log("History deleted (history disabled)")
        except Exception as e:
            log(f"Could not delete history: {e}")
    st.close()

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

    # Does Discord currently hold a presence from us? clear_activity() is a
    # SET_ACTIVITY frame like any other and spends a rate-limit slot, so
    # clearing when nothing is published buys nothing and costs a slot. Rapid
    # pause/resume used to emit one such frame per flick.
    have_presence = False

    def avail():
        now = time.monotonic(); rl["t"] = [x for x in rl["t"] if now-x < RATE_LIMIT_WINDOW]
        return len(rl["t"]) < RATE_LIMIT_CALLS

    def connected():
        return rpc._connected
    def rec(): rl["t"].append(time.monotonic())

    async def clear():
        """Take our presence down, and account for it.

        Every call site used to be `await rpc.clear_activity(); rl["t"].clear()`
        - wiping the local ledger on the assumption that a clear frees the
        budget. It does the opposite: the clear is itself a SET_ACTIVITY frame
        that consumes a slot, so after a pause or a blacklisted track
        Statusify believed it had five fresh slots while Discord was still
        counting the previous ones. That is the one path here to a real
        server-side 429 rather than a self-imposed wait.

        The clear is deliberately NOT gated on avail(): dropping it would
        leave a stale lyric pinned to the user's profile, which is the worse
        failure and the one the surrounding comments were written about. It is
        recorded instead, so the publishes that follow respect the true cost."""
        nonlocal have_presence
        if not have_presence:
            return False
        await rpc.clear_activity(); rec()
        have_presence = False
        return True
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
                await clear()
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
                cleared = False
                if SHOW_PAUSED_RPC:
                    # Feature 7: show paused indicator instead of clearing
                    if avail():
                        await rpc.set_activity(state.title, state.artist, ["\u23f8 Paused"], state.album_art)
                        rec(); have_presence = True; log("RPC paused indicator")
                else:
                    cleared = await clear()
                reset_track_state()
                event_queue.put(("line",""))
                if cleared:
                    log("RPC cleared")
            continue
        was_playing = True
        if not state.title: continue
        # Blacklist gate (#16): clear any presence we already published for
        # this track, then stay silent for as long as it's playing.
        if getattr(state, "blacklisted", False):
            if last_uri != state.track_uri:
                await clear()
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
                    have_presence = True
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
                title_sent = True; rec(); have_presence = True
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

        last_line = line1; skip = group[1:]; rec(); have_presence = True
        await rpc.set_activity(state.title, state.artist, group, state.album_art, state.position_ms, state.duration_ms)
        display = join_lines(group)
        log(f"RPC ({len(group)}L)  ·  {display[:55]}"); event_queue.put(("line", display))

from statusify_colors import _hex_to_rgb, _blend, _readable_on
from statusify_colors import tinted_palette as _tinted_palette

# ── GUI colors ────────────────────────────────────────────────────
def _apply_palette(dark: bool, accent: str, tint=None):
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
    if tint:
        # Lyric-sheet mode: surfaces, text and accent all come from the album
        # cover's hue at fixed lightness (statusify_colors.tinted_palette).
        _t = _tinted_palette(tint, dark)
        BG, BG2, BG3, BG4 = _t["BG"], _t["BG2"], _t["BG3"], _t["BG4"]
        MUTED, TEXT, TEXT2 = _t["MUTED"], _t["TEXT"], _t["TEXT2"]
        BORDER, SHADOW, ACCENT = _t["BORDER"], _t["SHADOW"], _t["ACCENT"]
    ACCENT_SOFT = _blend(BG3, ACCENT, 0.22 if dark else 0.16)
    ACCENT_FG   = _readable_on(ACCENT)
    HOVER_BG    = _blend(BG2, TEXT, 0.06 if dark else 0.05)

# USER_ACCENT is the colour picked in Settings; ACCENT is what is on screen,
# which in album-tint mode comes from the cover instead.
USER_ACCENT  = ACCENT
ALBUM_TINT   = (_cfg_get("preferences", "album_tint", "true").lower() == "true")
_CUR_TINT    = None    # tint of the current cover, or None

def _repalette():
    """Rebuild the palette from theme + accent + (if enabled) album tint."""
    _apply_palette(_DARK_MODE, USER_ACCENT, _CUR_TINT if ALBUM_TINT else None)

_apply_palette(_DARK_MODE, ACCENT)
# After _apply_palette the names BG, BG2 … ACCENT … are module-level strings.

# Hero album-art size — single source of truth shared by the Now-Playing
# canvas, the placeholder art (_default_art) and the live art loader (_set_art).
HERO_ART_PX = 64
# Corner radii. Album art was the one large square in a UI made entirely of
# squares, so it read as an unstyled <img> dropped into the layout.
HERO_ART_RADIUS = 8
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

# App's pages and tray live in mixin modules. They reach main's globals
# through their M attribute, bound here before App is ever instantiated.
# Some names imported above (pystray, _fetch_art, _round_image,
# _cfg_set_soon, the startup helpers) are used only by those mixins, as
# M.<name>, so pyflakes reports them unused here. They are not.
import statusify_ui_mini
import statusify_ui_now_playing
import statusify_ui_history
import statusify_ui_settings
import statusify_ui_stats
for _ui_mod in (statusify_ui_mini, statusify_ui_now_playing, statusify_ui_history, statusify_ui_settings,
                statusify_ui_stats):
    _ui_mod.M = sys.modules[__name__]
from statusify_ui_mini import MiniTrayMixin
from statusify_ui_now_playing import NowPlayingPage
from statusify_ui_history import HistoryPage
from statusify_ui_settings import SettingsPage
from statusify_ui_stats import StatsPage

class App(MiniTrayMixin, NowPlayingPage, HistoryPage, StatsPage, SettingsPage):
    """
    Single Tk() window in a native Windows frame.

    It used to strip the frame (overrideredirect) and draw its own title bar,
    which cost everything Windows gives a real program: Snap Layouts, the
    drop shadow, Aero Snap and shake, native minimise/restore animations,
    resizing from any edge, and a dependable taskbar button — each of which
    was then partly re-faked with Win32 hacks and hand-placed resize grips.
    The frame is now the OS's own, tinted to the theme through DWM
    (_apply_titlebar_theme).
    """
    # DWM window attributes (dwmapi.h)
    DWMWA_USE_IMMERSIVE_DARK_MODE        = 20   # Windows 10 20H1+ / 11
    DWMWA_USE_IMMERSIVE_DARK_MODE_LEGACY = 19   # Windows 10 1809–1909
    DWMWA_CAPTION_COLOR                  = 35   # Windows 11 only
    DWMWA_TEXT_COLOR                     = 36   # Windows 11 only

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
        self._root.protocol("WM_DELETE_WINDOW", self._on_close_button)
        # Tk delivers Windows' WM_QUERYENDSESSION (logoff/shutdown/restart) as
        # WM_SAVE_YOURSELF. Plays are already committed as they happen; this
        # saves the current play's listening time and any debounced config.
        self._root.protocol("WM_SAVE_YOURSELF", self._on_session_end)
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
        self._style_scrollbars()
        self._build()
        self._tray_start()
        self._poll()
        # Drive the Now-Playing progress bar (~4 fps is smooth enough and cheap).
        self._schedule("progress", 250, self._tick_progress)

        self._schedule("hotkeys", 200, lambda: _register_hotkeys(self))
        # The frame HWND exists once the window is mapped; theme it then (and
        # again on every map, since Windows can reset it on restore).
        self._root.bind("<Map>", lambda e: e.widget is self._root and self._apply_titlebar_theme(), add="+")
        self._root.after_idle(self._apply_titlebar_theme)
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
        # Ctrl+T and the lyric page's "On top" also move the Settings switch.
        self._sync_settings_switches()

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
            "<Control-Key-4>":    lambda e: self._show("STATS"),
            "<Control-c>":        lambda e: self._copy_current_lyric(),
            "<Control-m>":        lambda e: self._toggle_mini(),
            "<Control-t>":        lambda e: self._toggle_topmost(),
            # Playback. Plain keys only while not typing into a field.
            "<space>":            lambda e: self._key_player(e, "toggle"),
            "<Control-Left>":     lambda e: self._key_player(e, "prev"),
            "<Control-Right>":    lambda e: self._key_player(e, "next"),
            "<Left>":             lambda e: self._key_seek(e, -5000),
            "<Right>":            lambda e: self._key_seek(e, 5000),
        }
        for seq, fn in binds.items():
            try:
                W.bind(seq, fn)
            except tk.TclError:
                pass

    @staticmethod
    def _typing(e):
        return isinstance(e.widget, (tk.Entry, tk.Text, ttk.Entry))

    def _key_player(self, e, action):
        if not self._typing(e):
            self._np_transport(action)
            return "break"

    def _key_seek(self, e, delta):
        if self._typing(e) or self._cur_page != "NOW PLAYING":
            return
        dur = getattr(state, "duration_ms", 0) or 0
        if dur:
            self._np_seek(max(0, min(dur - 1000, self._estimate_pos_ms() + delta)))
        return "break"

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
            files = _maint.repair_files(_RES_DIR, _APP_DIR)
            msg = ("Lyrics bridge out of date in Spotify — click here to repair" if files
                   else "Bridge out of date in Spotify — run: spicetify apply")
            log(f"⚠ {msg}")
            self._set_error(msg)
            if files:
                self.lbl_err.config(cursor="hand2")
                self.lbl_err.bind("<Button-1>", lambda e: self._repair_bridge())

    def _repair_bridge(self):
        """Run the Spicetify setup script in its own window, then clear the
        warning once the bridge inside Spotify matches ours."""
        files = _maint.repair_files(_RES_DIR, _APP_DIR)
        if not files:
            return
        try:
            _maint.launch_repair(*files)
        except Exception as e:
            self._set_error(f"Could not start repair: {e}")
            return
        log("Bridge repair started")
        self._set_error("Repairing — follow the window that just opened (Spotify will restart)")
        self.lbl_err.unbind("<Button-1>"); self.lbl_err.config(cursor="")
        deadline = time.monotonic() + 300

        def check():
            global _BRIDGE_UPDATED
            if not _bridge_needs_apply():
                _BRIDGE_UPDATED = False
                log("Bridge repaired — Spotify is running the current bridge")
                self._set_error("")
            elif time.monotonic() < deadline:
                self._schedule("bridge_repair", 3000, check)
            else:
                self._check_bridge_version()   # restore the clickable warning
        self._schedule("bridge_repair", 3000, check)

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
            btn.config(text="●  On Discord" if _rpc_enabled else "○  Not sharing",
                       bg=ACCENT_SOFT if _rpc_enabled else BG3,
                       fg=ACCENT if _rpc_enabled else MUTED)
        except tk.TclError:
            pass

    def _on_session_end(self):
        log("Windows session ending  ·  saving state")
        for fn in (_cfg_flush, _finish_play, self._save_geometry):
            try:
                fn()
            except Exception:
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
        # Force any debounced config write to disk before we tear down.
        try:
            _cfg_flush()
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
        self._root.iconify()

    SCROLLBAR_STYLE = "Statusify.Vertical.TScrollbar"

    def _style_scrollbars(self):
        """Theme the ttk scrollbar style from the current palette.

        Tk's classic Scrollbar is drawn by Windows and ignores bg/trough
        colours, so on the dark theme every list had a bright white bar down
        its side. The 'clam' ttk theme honours colours; its layout is cut down
        to trough + thumb (no arrow buttons), like a modern overlay bar.
        Called again on theme change."""
        try:
            st = ttk.Style(self._root)
            if st.theme_use() != "clam":
                st.theme_use("clam")
            st.layout(self.SCROLLBAR_STYLE, [
                ("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [
                    ("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})])
            st.configure(self.SCROLLBAR_STYLE, troughcolor=BG, background=BG4,
                         bordercolor=BG, lightcolor=BG4, darkcolor=BG4,
                         arrowsize=8, gripcount=0, relief="flat", borderwidth=0)
            st.map(self.SCROLLBAR_STYLE,
                   background=[("pressed", MUTED), ("active", MUTED)],
                   lightcolor=[("pressed", MUTED), ("active", MUTED)],
                   darkcolor=[("pressed", MUTED), ("active", MUTED)])
        except tk.TclError as e:
            log(f"Scrollbar styling skipped: {e}")

    def _scrollbar(self, parent, **kw):
        return ttk.Scrollbar(parent, orient="vertical", style=self.SCROLLBAR_STYLE, **kw)

    def _apply_titlebar_theme(self):
        """Tint the native title bar to match the current theme.

        Dark mode (Windows 10 20H1+ and 11) switches the caption to Windows'
        dark style; on Windows 11 the caption and its text also take the
        app's exact background and text colours, so the frame and the
        content read as one surface. Older Windows ignore the attributes."""
        try:
            dwm  = ctypes.windll.dwmapi
            hwnd = int(self._root.wm_frame(), 16)

            def _set(attr, value):
                v = ctypes.c_int(value)
                return dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v), ctypes.sizeof(v))

            dark = 1 if _DARK_MODE else 0
            if _set(self.DWMWA_USE_IMMERSIVE_DARK_MODE, dark) != 0:
                _set(self.DWMWA_USE_IMMERSIVE_DARK_MODE_LEGACY, dark)

            def _colorref(hex_c):
                r, g, b = _hex_to_rgb(hex_c)
                return r | (g << 8) | (b << 16)
            _set(self.DWMWA_CAPTION_COLOR, _colorref(BG))
            _set(self.DWMWA_TEXT_COLOR, _colorref(TEXT2))
        except Exception as e:
            log(f"Title bar theming skipped: {e}")

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
        # ── Page switcher ─────────────────────────────────────────
        # A segmented control pinned to the bottom, like a media app, so the
        # lyric sheet gets the whole top of the window. Packed before the page
        # container so it keeps its height when the window shrinks.
        nav_row = tk.Frame(W, bg=BG); nav_row.pack(side="bottom", fill="x", pady=(SP_XS, SP_MD))
        nav = tk.Frame(nav_row, bg=BG2, padx=3, pady=3); nav.pack()
        self._nav = nav
        self._tab_btns = {}
        for name, label in (("NOW PLAYING", "Lyrics"), ("HISTORY", "History"),
                            ("STATS", "Stats"), ("SETTINGS", "Settings")):
            b = tk.Label(nav, text=label, fg=MUTED, bg=BG2,
                         font=self._f(FS_SMALL, True), cursor="hand2",
                         padx=SP_LG, pady=SP_XS + 1)
            b.pack(side="left", padx=1)
            b.bind("<Button-1>", lambda e, n=name: self._show(n))
            b.bind("<Enter>", lambda e, w=b, n=name:
                   None if self._cur_page == n else w.config(fg=TEXT2))
            b.bind("<Leave>", lambda e, w=b, n=name:
                   None if self._cur_page == n else w.config(fg=MUTED))
            self._tab_btns[name] = b

        # ── Page container ────────────────────────────────────────
        self._container = tk.Frame(W, bg=BG); self._container.pack(fill="both", expand=True)
        self._build_now_playing()
        self._show("NOW PLAYING")
        # Build the heavier History and Settings pages just after the window
        # first paints. Startup shows Now Playing immediately instead of
        # blocking on the whole widget tree; _show() below also builds a page
        # on demand if its tab is clicked before this idle callback runs.
        self._root.after_idle(self._build_deferred_pages)

    def _build_deferred_pages(self):
        """Build the non-default pages once the window is up (see __init__)."""
        if "HISTORY" not in self._pages:
            self._build_history()
        if "STATS" not in self._pages:
            self._build_stats()
        if "SETTINGS" not in self._pages:
            self._build_settings()

    # ── Page switcher ─────────────────────────────────────────────
    def _show(self, name):
        if self._cur_page == name:
            return
        # Deferred pages (History/Settings) are built lazily — construct on the
        # first switch if the idle builder hasn't run yet.
        if name not in self._pages:
            builder = {"HISTORY": getattr(self, "_build_history", None),
                       "STATS": getattr(self, "_build_stats", None),
                       "SETTINGS": getattr(self, "_build_settings", None)}.get(name)
            if builder:
                builder()
            if name not in self._pages:
                return
        # Remember the Settings scroll position as we leave it.
        if self._cur_page == "SETTINGS" and hasattr(self, "set_cv"):
            try: self._set_scroll_pos = self.set_cv.yview()[0]
            except Exception: pass
        # Pages are stacked in one spot and raised, never re-packed. Packing a
        # page made Tk lay out its whole widget tree again, so every tab click
        # cost ~60 ms (several frames) — the app felt a beat behind the mouse.
        page = self._pages[name]
        if not page.winfo_manager():
            page.place(x=0, y=0, relwidth=1, relheight=1)
        page.tkraise()
        self._cur_page = name
        if name == "STATS":
            self._refresh_long_stats()      # queries only while shown; fresh on show
        # The lyric sheet stops drawing while hidden; wake it at once.
        if name == "NOW PLAYING":
            self._schedule("progress", 0, self._tick_progress)
        # Restore where the user last was on the Settings page.
        if name == "SETTINGS" and hasattr(self, "set_cv"):
            pos = getattr(self, "_set_scroll_pos", 0.0)
            self._root.after_idle(lambda: self._safe_yview(self.set_cv, pos))
        self._paint_nav(animate=True)

    def _paint_nav(self, animate=False):
        """Raise the selected segment of the page switcher; mute the rest.

        On a click the old segment sinks and the new one rises over 140 ms.
        The page itself switches at once; only the switcher eases, so it
        reads as feedback rather than lag. Palette changes repaint instantly."""
        nav = getattr(self, "_nav", None)
        if nav is None:
            return
        try:
            nav.config(bg=BG2)
            for n, b in self._tab_btns.items():
                sel = (n == self._cur_page)
                bg, fg = (BG4 if sel else BG2), (TEXT if sel else MUTED)
                if animate:
                    self._fade_colors(f"hover:{b}", b, 140, bg=bg, fg=fg)
                else:
                    b.config(bg=bg, fg=fg)
        except tk.TclError:
            pass

    @staticmethod
    def _safe_yview(canvas, fraction):
        """Scroll a canvas to a fraction, swallowing teardown races."""
        try:
            canvas.yview_moveto(max(0.0, min(1.0, fraction)))
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
        if not hasattr(self, "log_txt"):
            # The log view lives on the Settings page, built just after the
            # first paint. Keep the newest lines until it exists.
            del buf[:-300]
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
                    self.lbl_prev.config(text=""); self.lbl_next.config(text="")
                    self._sheet_idx = None
                    # Named slot: rapid skipping coalesces to the latest track
                    # instead of firing one art fetch per skipped song.
                    self._schedule("setart", 30, lambda u=art: self._set_art(u))
                    self._refresh_track_offset()
                elif k == "lyrics":
                    _, src, mode, n = ev
                    lbl = src if mode in ("synced","plain") else "No lyrics"
                    if mode == "plain": lbl += " (plain)"
                    self.lbl_info.config(text=f"·  {lbl}")
                    self._sheet_idx = None
                elif k == "line":
                    t = ev[1]
                    # Synced tracks: the sheet shows the lines around the
                    # playhead (_update_sheet). Plain / lyric-less tracks show
                    # what the RPC loop publishes.
                    if not (state.lyrics_mode == "synced" and state.synced):
                        self.lbl_lyric.config(text=t or "—", fg=TEXT if t and t not in ("—", "— ") else MUTED)
                    self._refresh_mini()
                    self._highlight_active_lyric()
                elif k == "paused":
                    self.lbl_lyric.config(fg=MUTED)
                    if not (state.lyrics_mode == "synced" and state.synced):
                        self.lbl_lyric.config(text="Paused")
                elif k == "rl":
                    w = ev[1]; self._start_rl_countdown(w)
                elif k == "history_add":
                    # While a search is showing, new plays wait for it to clear.
                    if not (getattr(self, "_hist_search", None) and self._hist_search.get().strip()):
                        self._add_history_row(ev[1])
                    self._stats_on_play()
                elif k == "dropped":
                    n = ev[1]
                    self.lbl_dropped.config(
                        text=(f"{n} line{'s' if n != 1 else ''} dropped" if n else ""),
                        fg=WARN if n else MUTED)
                elif k == "error":
                    self._set_error(ev[1])
                elif k == "bridge_ok":
                    self._set_error("")
                    self.lbl_err.unbind("<Button-1>"); self.lbl_err.config(cursor="")
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
                    _, tag, url, changelog, setup = ev
                    self._show_update_dialog(tag, url, changelog, setup)
                drained += 1
        except queue.Empty: pass
        self._schedule("poll", 50, self._poll)

    def _show_update_dialog(self, tag, url, changelog, setup=None):
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

        scrollbar = self._scrollbar(cf)
        scrollbar.pack(side="right", fill="y")
        txt = tk.Text(cf, bg=BG2, fg=TEXT2, font=self._f(8), relief="flat",
                      wrap="word", yscrollcommand=scrollbar.set, padx=10, pady=10)
        txt.pack(side="left", fill="both", expand=True)
        scrollbar.config(command=txt.yview)

        txt.insert("end", changelog)
        txt.config(state="disabled")

        bf = tk.Frame(dlg, bg=BG)
        bf.pack(fill="x", pady=(0, 20))
        btn_no = tk.Label(bf, text="Later", fg=MUTED, bg=BG, font=self._f(8, True), cursor="hand2")
        btn_no.pack(side="left", padx=30)
        btn_no.bind("<Button-1>", lambda e: dlg.destroy())

        # Setup.exe installs update in place: download, verify the published
        # SHA-256, run the installer silently and let it relaunch us. Portable
        # exes and source checkouts can't be updated that way, so they keep
        # the browser link.
        auto = bool(setup) and _maint.is_installed(_APP_DIR, _FROZEN)
        btn_yes = tk.Label(bf, text="Install update" if auto else "DOWNLOAD",
                           fg=ACCENT_FG, bg=ACCENT, font=self._f(8, True), cursor="hand2", padx=16, pady=6)
        btn_yes.pack(side="right", padx=30)

        def _install():
            btn_yes.config(text="Downloading…", cursor="watch"); btn_yes.unbind("<Button-1>")
            dest = os.path.join(tempfile.gettempdir(), "statusify-update")
            def work():
                try:
                    path = _maint.download_verified(setup[0], setup[1], dest)
                except Exception as e:
                    log(f"Update download failed: {e}")
                    err = f"Update failed: {e} — opening the download page"
                    self._root.after(0, lambda: (self._set_error(err), webbrowser.open(url), dlg.destroy()))
                    return
                log(f"Update v{tag} downloaded and verified — installing")
                _maint.launch_silent_update(str(path))
                self._root.after(0, self._quit)
            threading.Thread(target=work, daemon=True, name="updater").start()

        def _yes():
            if auto: _install()
            else: webbrowser.open(url); dlg.destroy()
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

    btn = tk.Label(dlg, text="Save & continue", fg="#0a0a0a", bg="#1db954",
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
        latest_setup = None   # (setup_url, sha256_url) when the release ships an installer
        changelog_lines = []

        for release in data:
            tag = release.get("tag_name", "").lstrip("v")
            if not tag: continue

            if _sv(tag) > _sv(cur):
                if not latest_tag:
                    latest_tag = tag
                    latest_url = release.get("html_url", "")
                    latest_setup = _maint.setup_asset(release)

                body = release.get("body", "").strip()
                changelog_lines.append(f"• v{tag}")
                if body:
                    for ln in body.splitlines():
                        if ln.strip():
                            changelog_lines.append(f"  {ln}")
                changelog_lines.append("")

        if latest_tag:
            event_queue.put(("update_available", latest_tag, latest_url, "\n".join(changelog_lines).strip(), latest_setup))
            log(f"Update available: v{latest_tag} (with {len(changelog_lines)} lines of notes)")
    except Exception as e:
        log(f"Update check failed: {e}")

def _ensure_dependencies():
    """Install any missing optional libraries, then refresh the Spicetify bridge.

    Only the *optional* packages are handled here — websockets and
    python-dotenv are imported at module scope and are bootstrapped by
    _pip_install() up there, long before this function can run."""
    import importlib
    deps    = ["pypresence", "pillow", "pystray"]
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
