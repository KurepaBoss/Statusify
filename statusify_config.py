"""statusify.cfg: one cached ConfigParser, atomic writes, debounced bursts.

Split out of main.py. main calls init() with the config path and its logger
before the first read; everything else is used exactly as before.
"""
import configparser
import os
import threading

_CONFIG_PATH = None
log = lambda *_a, **_k: None

_CFG_CACHE = None
_CFG_LOCK  = threading.RLock()


def init(config_path, log_fn=None):
    global _CONFIG_PATH, log, _CFG_CACHE
    _CONFIG_PATH = config_path
    _CFG_CACHE = None
    if log_fn is not None:
        log = log_fn


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

# ── Debounced config writes ───────────────────────────────────────────
# _cfg_set rewrites the whole INI atomically on every call. That is correct
# but wasteful when the UI fires a burst — nudging a per-track offset or the
# lyric font size hits it once per click, each a full serialise + os.replace on
# the Tk thread. _cfg_set_soon updates the in-memory ConfigParser immediately
# (so a subsequent _cfg_get sees the new value at once) and coalesces the disk
# write to a single flush ~400 ms after the last change in the burst. Any
# pending flush is forced on quit via _cfg_flush(), so nothing is lost.
_CFG_FLUSH_TIMER = None
_CFG_DIRTY       = False

def _cfg_flush():
    """Write pending debounced changes to disk now. Safe to call anytime."""
    global _CFG_FLUSH_TIMER, _CFG_DIRTY
    with _CFG_LOCK:
        if _CFG_FLUSH_TIMER is not None:
            try: _CFG_FLUSH_TIMER.cancel()
            except Exception: pass
            _CFG_FLUSH_TIMER = None
        if not _CFG_DIRTY:
            return
        _CFG_DIRTY = False
        _save_config(_load_config())

def _cfg_set_soon(section, key, value, delay=0.4):
    """Set a value in memory immediately; flush to disk once the burst settles."""
    global _CFG_FLUSH_TIMER, _CFG_DIRTY
    with _CFG_LOCK:
        cfg = _load_config()
        if not cfg.has_section(section):
            cfg.add_section(section)
        cfg.set(section, key, str(value))
        _CFG_DIRTY = True
        if _CFG_FLUSH_TIMER is not None:
            try: _CFG_FLUSH_TIMER.cancel()
            except Exception: pass
        t = threading.Timer(delay, _cfg_flush)
        t.daemon = True
        _CFG_FLUSH_TIMER = t
        t.start()
