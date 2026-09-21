"""Global hotkeys via the Win32 RegisterHotKey API.

Replaces the `keyboard` library, which had two problems (see
tests/test_hotkeys.py): it fired on every auto-repeat of a held key, and its
low-level keyboard hook stopped receiving input whenever an elevated window —
most games — had focus, so Statusify had to run as administrator.

RegisterHotKey has neither problem. Windows matches the combo itself and posts
WM_HOTKEY to the registering thread whatever window is in front, and
MOD_NOREPEAT turns a held key into a single press. It installs no hook, so the
exe also stops looking like a keylogger to antivirus heuristics.

Hotkeys belong to the thread that registered them and arrive in that thread's
message queue, so HotkeyManager owns one daemon thread running a GetMessage
loop; every (re)registration is marshalled onto it.
"""
import ctypes
import ctypes.wintypes as wt
import queue
import threading

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
WM_HOTKEY, WM_QUIT, WM_APP = 0x0312, 0x0012, 0x8000
ERROR_HOTKEY_ALREADY_REGISTERED = 1409

_MODS = {"ctrl": MOD_CONTROL, "control": MOD_CONTROL, "alt": MOD_ALT,
         "shift": MOD_SHIFT, "win": MOD_WIN, "windows": MOD_WIN, "super": MOD_WIN}

_KEYS = {
    "space": 0x20, "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B,
    "escape": 0x1B, "backspace": 0x08, "insert": 0x2D, "ins": 0x2D,
    "delete": 0x2E, "del": 0x2E, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pgup": 0x21, "pagedown": 0x22, "pgdn": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "pause": 0x13, "printscreen": 0x2C, "capslock": 0x14,
    "playpause": 0xB3, "nexttrack": 0xB0, "prevtrack": 0xB1,
    "volumeup": 0xAF, "volumedown": 0xAE, "volumemute": 0xAD,
    ";": 0xBA, "=": 0xBB, ",": 0xBC, "-": 0xBD, ".": 0xBE, "/": 0xBF,
    "`": 0xC0, "[": 0xDB, "\\": 0xDC, "]": 0xDD, "'": 0xDE,
}
_KEYS.update({f"f{i}": 0x70 + i - 1 for i in range(1, 25)})
_KEYS.update({f"num{i}": 0x60 + i for i in range(10)})
_KEYS.update({f"numpad{i}": 0x60 + i for i in range(10)})


def parse_combo(combo):
    """'ctrl+alt+n' -> (modifier flags incl. MOD_NOREPEAT, virtual-key code).

    Case- and space-insensitive. Exactly one non-modifier key is required.
    Raises ValueError on anything it can't map."""
    parts = [p.strip().lower().replace(" ", "") for p in (combo or "").split("+")]
    parts = [p for p in parts if p]
    if not parts:
        raise ValueError("empty hotkey")
    mods, keys = MOD_NOREPEAT, []
    for p in parts:
        if p in _MODS:
            mods |= _MODS[p]
        else:
            keys.append(p)
    if len(keys) != 1:
        raise ValueError(f"'{combo}' needs exactly one non-modifier key")
    k = keys[0]
    if len(k) == 1 and (k.isalpha() or k.isdigit()):
        vk = ord(k.upper())
    elif k in _KEYS:
        vk = _KEYS[k]
    else:
        raise ValueError(f"unknown key '{k}' in '{combo}'")
    if mods == MOD_NOREPEAT:
        raise ValueError(f"'{combo}' needs at least one of ctrl/alt/shift/win")
    return mods, vk


class HotkeyManager:
    """Owns a message-loop thread holding the registered hotkeys."""

    def __init__(self):
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._user32.RegisterHotKey.argtypes = [wt.HWND, ctypes.c_int, wt.UINT, wt.UINT]
        self._user32.UnregisterHotKey.argtypes = [wt.HWND, ctypes.c_int]
        self._user32.PostThreadMessageW.argtypes = [wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM]
        self._user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
        self._jobs = queue.Queue()
        self._callbacks = {}      # hotkey id -> callable
        self._ids = []            # ids currently registered
        self._ready = threading.Event()
        self._tid = None
        self._thread = threading.Thread(target=self._run, name="hotkeys", daemon=True)
        self._thread.start()
        self._ready.wait(5)

    # ── public ────────────────────────────────────────────────────
    def set(self, bindings):
        """Replace all hotkeys. bindings: {name: (combo, callback)}.

        Empty combos are skipped (hotkey disabled). Returns {name: reason}
        for every binding that couldn't be registered; never raises."""
        done = threading.Event()
        box = {}
        self._jobs.put((bindings, box, done))
        self._user32.PostThreadMessageW(self._tid, WM_APP, 0, 0)
        if not done.wait(5):
            return {n: "hotkey thread not responding" for n in bindings}
        return box.get("failed", {})

    def stop(self):
        if self._tid and self._thread.is_alive():
            self.set({})
            self._user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
            self._thread.join(2)

    # ── hotkey thread ─────────────────────────────────────────────
    def _apply(self, bindings):
        for i in self._ids:
            self._user32.UnregisterHotKey(None, i)
        self._ids, self._callbacks = [], {}
        failed = {}
        for n, (name, (combo, cb)) in enumerate(bindings.items(), start=1):
            if not (combo or "").strip():
                continue
            try:
                mods, vk = parse_combo(combo)
            except ValueError as e:
                failed[name] = str(e)
                continue
            if self._user32.RegisterHotKey(None, n, mods, vk):
                self._ids.append(n)
                self._callbacks[n] = cb
            else:
                err = ctypes.get_last_error()
                failed[name] = (f"'{combo}' is already in use by another program"
                                if err == ERROR_HOTKEY_ALREADY_REGISTERED
                                else f"'{combo}' could not be registered (error {err})")
        return failed

    def _run(self):
        self._tid = ctypes.windll.kernel32.GetCurrentThreadId()
        msg = wt.MSG()
        # Touch the queue so PostThreadMessage can't race ahead of it.
        self._user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)
        self._ready.set()
        while self._user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                cb = self._callbacks.get(msg.wParam)
                if cb:
                    try:
                        cb()
                    except Exception:
                        pass
            elif msg.message == WM_APP:
                while True:
                    try:
                        bindings, box, done = self._jobs.get_nowait()
                    except queue.Empty:
                        break
                    box["failed"] = self._apply(bindings)
                    done.set()
        for i in self._ids:
            self._user32.UnregisterHotKey(None, i)
