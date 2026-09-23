"""Desktop lyrics overlay: the current synced line over every other window.

A borderless, always-on-top strip that floats over any app, borderless-
windowed games included. It is a Tk Toplevel, but Tk never paints it: the
Toplevel's wrapper HWND (`wm frame`) is made a *per-pixel-alpha layered
window* and its whole content is handed to Windows as a premultiplied BGRA
bitmap through UpdateLayeredWindow. That is what gives anti-aliased text
with a soft shadow on any background, which a -transparentcolor colour key
cannot do (a colour key is all-or-nothing per pixel, so every soft edge gets
a fringe). The colour key is kept only as a fallback for the day
UpdateLayeredWindow refuses the window.

Why the wrapper and not winfo_id(): winfo_id() is Tk's child window inside
the wrapper. Layering and the extended styles belong to the top-level
window, which for an overrideredirect Toplevel is the WS_POPUP wrapper that
`wm frame` (== GetParent(winfo_id())) returns. Tk creates it on first map and
recreates it only for `wm overrideredirect` / `-toolwindow` changes after
mapping, neither of which happens here. Tk maps an override-redirect window
without activating it (verified: GetActiveWindow is unchanged), and after
that the overlay is shown and hidden with ShowWindow(SW_SHOWNA/SW_HIDE)
directly, never Tk's deiconify, so it cannot steal focus.

Locked (the default) it is WS_EX_TRANSPARENT: every click falls through to
whatever is underneath, and WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW keep it out
of focus, the taskbar and Alt+Tab. Unlocked it takes the mouse: drag to
move, scroll to resize the text, Lock / x buttons on its top bar.

Cost: idle is one timer every ~0.4 s that reads a few attributes of
M.state. Frames are rendered only when the line changes, during the ~300 ms
slide, and at ~30 fps while a line with word timing (`syl`) is being sung,
and then only the band of rows holding the current line is re-rendered and
copied into the reused DIB. Disabled = no window, no timer.
"""
import ctypes
import re
import sys
import time
import tkinter as tk

try:
    from PIL import Image, ImageChops, ImageDraw, ImageFilter
    PIL_AVAILABLE = True
except ImportError:          # pragma: no cover - PIL ships with Statusify
    PIL_AVAILABLE = False

from statusify_textrender import TextRenderer

M = None                     # the live main module, bound by main.py

# Tests turn this off: the whole pipeline runs, but nothing is handed to the
# window manager and the Toplevel is never mapped.
NATIVE = sys.platform == "win32"

DEFAULT_SIZE = 30            # current-line text size in px at 96 DPI
MIN_SIZE, MAX_SIZE = 16, 72
PAUSE_HIDE_S = 5.0           # paused this long -> fade out
SLIDE_S = 0.30               # line change crossfade / slide
FADE_S = 0.35                # whole-window fade in / out
KARAOKE_MS = 33              # word fill frame interval (~30 fps)
IDLE_MS = 400                # state check interval while nothing moves
GAP_MIN_MS = 3000            # a silence at least this long shows the dots
NEXT_ALPHA = 0.70            # opacity of the "next line" preview
DEFAULT_HOTKEY = "ctrl+alt+o"

_BLANK = ("", "♪", "♫", "…", "...", "• • •")


# ── Pure logic (unit-tested in tests/test_overlay.py) ────────────────
def current_index(synced, pos):
    """Index of the line on screen at `pos` (offset already applied), or -1
    before the first line. The same rule as NowPlayingPage._update_sheet —
    the last line whose startMs has been reached — so the overlay and the
    lyric sheet always agree on the current line."""
    idx = -1
    for i, e in enumerate(synced):
        if e["startMs"] <= pos:
            idx = i
        else:
            break
    return idx


def line_syl(line):
    """Word/syllable timing of a line as [(start_ms, end_ms, text), ...] on
    the track's timeline, or None when the line has none (or it is junk).

    The contract gives absolute times; a list that clearly starts before its
    own line is read as relative to the line start instead of shown wrong."""
    syl = line.get("syl") if isinstance(line, dict) else None
    if not syl:
        return None
    out = []
    try:
        for s in syl:
            a, b, t = int(s[0]), int(s[1]), str(s[2])
            out.append((a, max(a, b), t))
    except (TypeError, ValueError, IndexError):
        return None
    if not "".join(t for _a, _b, t in out).strip():
        return None
    st = int(line.get("startMs", 0) or 0)
    if st > 1000 and out[0][0] < st - 1000:
        out = [(a + st, b + st, t) for a, b, t in out]
    return out


def sung_chars(syl, pos):
    """How many characters of the syllables' concatenated text have been
    sung at `pos`, as a float: whole pieces that have ended plus the
    elapsed fraction of the piece being sung."""
    n = 0.0
    for a, b, t in syl:
        if pos >= b:
            n += len(t)
        elif pos > a:
            n += len(t) * (pos - a) / (b - a) if b > a else len(t)
            break
        else:
            break
    return n


def _words(line):
    return (line.get("words") or "").strip()


def pick_view(synced, pos, duration_ms=0):
    """What the overlay should show at `pos` (lyric offset already applied).

    Returns a dict:
      kind "line": idx, cur, next, syl (or None)
      kind "gap":  idx, start, end, next, dots (0-3 lit)  — instrumental
    or None when there are no synced lines."""
    if not synced:
        return None
    idx = current_index(synced, pos)
    n = len(synced)
    nxt_i = idx + 1
    nxt = _words(synced[nxt_i]) if nxt_i < n else ""
    end = synced[nxt_i]["startMs"] if nxt_i < n else (duration_ms or 0)

    gap_start = None
    if idx < 0:
        gap_start = 0
    else:
        line = synced[idx]
        if _words(line) in _BLANK:
            gap_start = line["startMs"]
        else:
            le = line.get("endMs")
            try:
                le = int(le) if le is not None else None
            except (TypeError, ValueError):
                le = None
            if le is not None and pos >= le and end and end - le >= GAP_MIN_MS:
                gap_start = le
    if gap_start is not None:
        span = max(1, (end or pos + 1) - gap_start)
        dots = max(0, min(3, int(3 * (pos - gap_start) / span)))
        return {"kind": "gap", "idx": idx, "start": gap_start, "end": end,
                "next": nxt if nxt not in _BLANK else "", "dots": dots}
    line = synced[idx]
    return {"kind": "line", "idx": idx, "cur": _words(line),
            "next": nxt if nxt not in _BLANK else "", "syl": line_syl(line)}


def ms_to_next_event(synced, pos, view):
    """Milliseconds until the overlay's picture can next change on its own
    (next line start, or the next gap dot lighting up)."""
    best = 10_000
    for e in synced:
        if e["startMs"] > pos:
            best = e["startMs"] - pos
            break
    if view and view["kind"] == "gap" and view["end"] and view["dots"] < 3:
        span = max(1, view["end"] - view["start"])
        nxt = view["start"] + span * (view["dots"] + 1) / 3.0
        best = min(best, nxt - pos)
    if view and view["kind"] == "line" and 0 <= view["idx"] < len(synced):
        # A line with an end time turns into the gap dots when it ends.
        i = view["idx"]
        try:
            le = int(synced[i].get("endMs"))
        except (TypeError, ValueError):
            le = None
        nstart = synced[i + 1]["startMs"] if i + 1 < len(synced) else None
        if le is not None and le > pos and nstart and nstart - le >= GAP_MIN_MS:
            best = min(best, le - pos)
    return int(max(4, best + 2))


_GEO = re.compile(r"^\s*(\d+)x(\d+)\+(-?\d+)\+(-?\d+)\s*$")


def parse_geometry(s):
    """'WxH+X+Y' -> (w, h, x, y), or None. X/Y may be negative written
    Tk's way ('+-1280+0'), which is how monitors left of the primary one
    come out."""
    m = _GEO.match(s or "")
    if not m:
        return None
    w, h, x, y = (int(v) for v in m.groups())
    if w <= 0 or h <= 0:
        return None
    return w, h, x, y


def format_geometry(w, h, x, y):
    # Always '+', never '-': in Tk '-X' means "X from the right edge".
    return f"{int(w)}x{int(h)}+{int(x)}+{int(y)}"


def clamp_rect(x, y, w, h, work):
    """Keep a w x h rect inside the work area (l, t, r, b)."""
    l, t, r, b = work
    x = max(l, min(x, r - w)) if r - l >= w else l + (r - l - w) // 2
    y = max(t, min(y, b - h)) if b - t >= h else t
    return int(x), int(y)


def readable_accent(hexcol):
    """The accent, lifted towards white until it reads on a dark shadow."""
    try:
        c = hexcol.lstrip("#")
        r, g, b = (int(c[i:i + 2], 16) for i in (0, 2, 4))
    except (ValueError, AttributeError):
        return (29, 185, 84)
    for _ in range(10):
        lum = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0
        if lum >= 0.42:
            break
        r, g, b = (int(v + (255 - v) * 0.2) for v in (r, g, b))
    return (r, g, b)


def _hotkey_default():
    try:
        return M._cfg_get("preferences", "hotkey_overlay", DEFAULT_HOTKEY)
    except Exception:
        return DEFAULT_HOTKEY


_HOTKEY = None


def hotkey_combo():
    """The global hotkey that toggles the overlay ("" = none)."""
    global _HOTKEY
    if _HOTKEY is None:
        _HOTKEY = _hotkey_default()
    return (_HOTKEY or "").strip()


def set_hotkey_combo(combo):
    global _HOTKEY
    _HOTKEY = (combo or "").strip()
    M._cfg_set("preferences", "hotkey_overlay", _HOTKEY)


# ── Win32 plumbing ───────────────────────────────────────────────────
GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
WS_EX_APPWINDOW = 0x00040000
SW_HIDE, SW_SHOWNA = 0, 8
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE = 0x1, 0x2, 0x4, 0x10
ULW_ALPHA = 2
MONITOR_DEFAULTTONULL, MONITOR_DEFAULTTOPRIMARY, MONITOR_DEFAULTTONEAREST = 0, 1, 2

_W = None


def _win():
    """Lazily bound user32/gdi32/shcore with 64-bit-safe signatures."""
    global _W
    if _W is not None:
        return _W
    import ctypes.wintypes as wt

    class BLEND(ctypes.Structure):
        _fields_ = [("op", ctypes.c_ubyte), ("flags", ctypes.c_ubyte),
                    ("alpha", ctypes.c_ubyte), ("fmt", ctypes.c_ubyte)]

    class BIH(ctypes.Structure):
        _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                    ("biPlanes", wt.WORD), ("biBitCount", wt.WORD),
                    ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                    ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG),
                    ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT),
                    ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]

    u = ctypes.WinDLL("user32", use_last_error=True)
    g = ctypes.WinDLL("gdi32", use_last_error=True)
    u.GetDC.restype = wt.HDC
    u.GetDC.argtypes = [wt.HWND]
    u.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
    u.GetParent.restype = wt.HWND
    u.GetParent.argtypes = [wt.HWND]
    u.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    u.GetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int]
    u.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    u.SetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_ssize_t]
    u.SetWindowPos.argtypes = [wt.HWND, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                               ctypes.c_int, ctypes.c_int, wt.UINT]
    u.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
    u.IsWindow.argtypes = [wt.HWND]
    u.UpdateLayeredWindow.argtypes = [wt.HWND, wt.HDC, ctypes.POINTER(wt.POINT),
                                      ctypes.POINTER(wt.SIZE), wt.HDC,
                                      ctypes.POINTER(wt.POINT), wt.COLORREF,
                                      ctypes.POINTER(BLEND), wt.DWORD]
    u.MonitorFromPoint.restype = ctypes.c_void_p
    u.MonitorFromPoint.argtypes = [wt.POINT, wt.DWORD]
    u.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(MONITORINFO)]
    g.CreateCompatibleDC.restype = wt.HDC
    g.CreateCompatibleDC.argtypes = [wt.HDC]
    g.CreateDIBSection.restype = wt.HBITMAP
    g.CreateDIBSection.argtypes = [wt.HDC, ctypes.c_void_p, wt.UINT,
                                   ctypes.POINTER(ctypes.c_void_p), wt.HANDLE, wt.DWORD]
    g.SelectObject.restype = wt.HGDIOBJ
    g.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
    g.DeleteObject.argtypes = [wt.HGDIOBJ]
    g.DeleteDC.argtypes = [wt.HDC]
    try:
        sh = ctypes.WinDLL("shcore")
        sh.GetDpiForMonitor.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                        ctypes.POINTER(wt.UINT), ctypes.POINTER(wt.UINT)]
    except OSError:
        sh = None
    _W = {"u": u, "g": g, "sh": sh, "wt": wt, "BLEND": BLEND, "BIH": BIH,
          "MONITORINFO": MONITORINFO}
    return _W


def monitor_at(x, y, primary=False):
    """(work area (l, t, r, b), dpi, found) of the monitor at x, y — or the
    primary one. Physical pixels (Statusify is per-monitor DPI aware)."""
    try:
        w = _win()
        wt = w["wt"]
        flag = MONITOR_DEFAULTTOPRIMARY if primary else MONITOR_DEFAULTTONULL
        hm = w["u"].MonitorFromPoint(wt.POINT(int(x), int(y)), flag)
        found = bool(hm)
        if not hm:
            hm = w["u"].MonitorFromPoint(wt.POINT(int(x), int(y)), MONITOR_DEFAULTTONEAREST)
        mi = w["MONITORINFO"]()
        mi.cbSize = ctypes.sizeof(mi)
        if not w["u"].GetMonitorInfoW(hm, ctypes.byref(mi)):
            raise OSError("GetMonitorInfoW")
        r = mi.rcWork
        dpi = 96
        if w["sh"] is not None:
            dx, dy = wt.UINT(), wt.UINT()
            if w["sh"].GetDpiForMonitor(hm, 0, ctypes.byref(dx), ctypes.byref(dy)) == 0:
                dpi = int(dx.value) or 96
        return (r.left, r.top, r.right, r.bottom), dpi, found
    except Exception:
        return None


class _LayeredWindow:
    """UpdateLayeredWindow on the Toplevel's wrapper HWND, with one reused
    DIB section. Every method is a no-op after release()."""

    def __init__(self, hwnd):
        self.w = _win()
        self.hwnd = hwnd
        self.size = None
        self.pos = (0, 0)
        self.alpha = 0
        self.shown = False
        self.scr = self.w["u"].GetDC(None)
        self.mdc = self.w["g"].CreateCompatibleDC(self.scr)
        self.bmp = self.old = None
        self.bits = ctypes.c_void_p()
        self.ok = True

    def set_style(self, clickthrough):
        u = self.w["u"]
        ex = u.GetWindowLongPtrW(self.hwnd, GWL_EXSTYLE)
        ex = (ex | WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_TOPMOST) \
            & ~WS_EX_APPWINDOW
        ex = ex | WS_EX_TRANSPARENT if clickthrough else ex & ~WS_EX_TRANSPARENT
        u.SetWindowLongPtrW(self.hwnd, GWL_EXSTYLE, ex)

    def _dib(self, W, H):
        if self.size == (W, H) and self.bmp:
            return
        g = self.w["g"]
        if self.bmp:
            g.SelectObject(self.mdc, self.old)
            g.DeleteObject(self.bmp)
        bi = self.w["BIH"](ctypes.sizeof(self.w["BIH"]), W, -H, 1, 32, 0, 0, 0, 0, 0, 0)
        self.bits = ctypes.c_void_p()
        self.bmp = g.CreateDIBSection(self.mdc, ctypes.byref(bi), 0,
                                      ctypes.byref(self.bits), None, 0)
        if not self.bmp or not self.bits:
            raise OSError("CreateDIBSection failed")
        self.old = g.SelectObject(self.mdc, self.bmp)
        self.size = (W, H)

    def _ulw(self, with_content=True):
        wt = self.w["wt"]
        blend = self.w["BLEND"](0, 0, max(0, min(255, int(self.alpha))), 1)
        if with_content:
            W, H = self.size
            ok = self.w["u"].UpdateLayeredWindow(
                self.hwnd, self.scr, ctypes.byref(wt.POINT(*self.pos)),
                ctypes.byref(wt.SIZE(W, H)), self.mdc, ctypes.byref(wt.POINT(0, 0)),
                0, ctypes.byref(blend), ULW_ALPHA)
        else:
            ok = self.w["u"].UpdateLayeredWindow(self.hwnd, None, None, None, None, None,
                                                 0, ctypes.byref(blend), ULW_ALPHA)
        if not ok:
            self.ok = False
        return ok

    def present(self, img, pos, band=None):
        """Show an RGBA image at pos. band=(y0, band_img) updates only those
        rows of the DIB (the size and position must be unchanged)."""
        if self.mdc is None:
            return False
        if band is not None and self.size == img.size and self.pos == tuple(pos):
            y0, bimg = band
            data = bimg.convert("RGBa").tobytes("raw", "BGRa")
            ctypes.memmove(self.bits.value + y0 * self.size[0] * 4, data, len(data))
        else:
            self._dib(*img.size)
            data = img.convert("RGBa").tobytes("raw", "BGRa")
            ctypes.memmove(self.bits, data, len(data))
            self.pos = (int(pos[0]), int(pos[1]))
        return self._ulw()

    def set_alpha(self, a):
        self.alpha = a
        if self.mdc is not None and self.size:
            self._ulw(with_content=False)

    def move(self, x, y):
        self.pos = (int(x), int(y))
        self.w["u"].SetWindowPos(self.hwnd, None, self.pos[0], self.pos[1], 0, 0,
                                 SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)

    def show(self, on):
        if on == self.shown or self.mdc is None:
            return
        self.shown = on
        self.w["u"].ShowWindow(self.hwnd, SW_SHOWNA if on else SW_HIDE)
        if on:
            self.raise_top()

    def raise_top(self):
        self.w["u"].SetWindowPos(self.hwnd, ctypes.c_void_p(-1), 0, 0, 0, 0,
                                 SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)

    def release(self):
        g, u = self.w["g"], self.w["u"]
        try:
            if self.bmp:
                g.SelectObject(self.mdc, self.old)
                g.DeleteObject(self.bmp)
            if self.mdc:
                g.DeleteDC(self.mdc)
            if self.scr:
                u.ReleaseDC(None, self.scr)
        except Exception:
            pass
        self.bmp = self.mdc = self.scr = None


class _ColorKeyWindow:
    """Fallback when UpdateLayeredWindow is refused: Tk's -transparentcolor.
    Soft pixels are flattened over black (the shadow colour), so edges stay
    dark instead of fringing with the key colour."""
    KEY = "#010203"

    def __init__(self, top):
        self.top = top
        self.size = None
        self.pos = (0, 0)
        self.photo = None
        self.lbl = tk.Label(top, bd=0, highlightthickness=0, bg=self.KEY)
        self.lbl.place(x=0, y=0)
        try:
            top.configure(bg=self.KEY)
            top.attributes("-transparentcolor", self.KEY)
        except tk.TclError:
            pass
        self.hwnd = None
        try:
            self.hwnd = int(top.wm_frame(), 16)
        except (tk.TclError, ValueError):
            pass
        self.shown = False
        self.ok = True

    def set_style(self, clickthrough):
        if not self.hwnd or not NATIVE:
            return
        u = _win()["u"]
        ex = u.GetWindowLongPtrW(self.hwnd, GWL_EXSTYLE)
        ex |= WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
        ex = ex | WS_EX_TRANSPARENT if clickthrough else ex & ~WS_EX_TRANSPARENT
        u.SetWindowLongPtrW(self.hwnd, GWL_EXSTYLE, ex)

    def present(self, img, pos, band=None):
        from PIL import ImageTk
        flat = Image.new("RGB", img.size, (1, 2, 3))
        solid = img.getchannel("A").point(lambda v: 255 if v >= 40 else 0)
        dark = Image.new("RGB", img.size, (0, 0, 0))
        dark.paste(img.convert("RGB"), mask=img.getchannel("A"))
        flat.paste(dark, mask=solid)
        self.photo = ImageTk.PhotoImage(flat)
        self.lbl.configure(image=self.photo)
        self.size = img.size
        self.pos = tuple(pos)
        return True

    def set_alpha(self, a):
        try:
            self.top.attributes("-alpha", max(0.0, min(1.0, a / 255.0)))
        except tk.TclError:
            pass

    def move(self, x, y):
        self.pos = (int(x), int(y))
        if self.size:
            self.top.geometry(format_geometry(*self.size, *self.pos))

    def show(self, on):
        if on != self.shown and self.hwnd and NATIVE:
            _win()["u"].ShowWindow(self.hwnd, SW_SHOWNA if on else SW_HIDE)
        self.shown = on

    def raise_top(self):
        pass

    def release(self):
        self.photo = None


class _NullWindow:
    """Stand-in when NATIVE is off (tests): records what would be shown."""

    def __init__(self):
        self.size = None
        self.pos = (0, 0)
        self.alpha = 0
        self.shown = False
        self.presents = 0
        self.last = None
        self.clickthrough = True
        self.ok = True

    def set_style(self, clickthrough):
        self.clickthrough = clickthrough

    def present(self, img, pos, band=None):
        self.presents += 1
        self.size = img.size
        self.pos = tuple(pos)
        self.last = img
        return True

    def set_alpha(self, a):
        self.alpha = a

    def move(self, x, y):
        self.pos = (int(x), int(y))

    def show(self, on):
        self.shown = on

    def raise_top(self):
        pass

    def release(self):
        self.last = None


def _ease(t):
    t = max(0.0, min(1.0, t))
    return 1 - (1 - t) ** 3


def _lut(k, cap=255):
    return [min(cap, int(v * k)) for v in range(256)]


# ── The mixin ────────────────────────────────────────────────────────

def anchor_for(x, y, w, h, work=None):
    """(centre x, edge y, top?) for a strip at x,y,w,h: pinned by its top edge
    in the upper half of the monitor's work area, else by its bottom edge."""
    cx = x + w // 2
    if work:
        top = (y + h / 2) < (work[1] + work[3]) / 2
    else:
        top = False
    return (cx, y, True) if top else (cx, y + h, False)

class OverlayMixin:
    """Desktop lyrics overlay for App. All state lives in _ov_* attributes."""

    # ── Public hooks ─────────────────────────────────────────────────
    def _overlay_init(self):
        """Called once after startup: bring the overlay back if it was on."""
        self._ov_load_prefs()
        if self._ov_enabled:
            self._ov_open()

    def _toggle_overlay(self):
        self._overlay_set_enabled(not self._ov_prefs()["enabled"])

    def _overlay_set_enabled(self, on):
        self._ov_load_prefs()
        on = bool(on)
        self._ov_enabled = on
        M._cfg_set("preferences", "overlay_enabled", str(on).lower())
        if on:
            self._ov_open()
        else:
            self._ov_close()
        M.log(f"Lyrics overlay {'on' if on else 'off'}")
        self._ov_sync_settings()

    def _overlay_toggle_lock(self):
        self._overlay_set_locked(not self._ov_prefs()["locked"])

    def _overlay_set_locked(self, locked):
        self._ov_load_prefs()
        self._ov_locked = bool(locked)
        M._cfg_set("window", "overlay_locked", str(self._ov_locked).lower())
        if not self._ov_locked and not self._ov_enabled:
            self._overlay_set_enabled(True)
        win = getattr(self, "_ov_win", None)
        if win is not None:
            win.set_style(self._ov_locked)
        self._ov_drag = None
        self._ov_kick()
        self._ov_sync_settings()

    def _overlay_set_size(self, size):
        self._ov_load_prefs()
        size = max(MIN_SIZE, min(MAX_SIZE, int(size)))
        if size == self._ov_size:
            return
        self._ov_size = size
        M._cfg_set_soon("preferences", "overlay_size", str(size))
        self._ov_relayout()
        self._ov_sync_settings()

    def _overlay_set_show_next(self, on):
        self._ov_load_prefs()
        self._ov_show_next = bool(on)
        M._cfg_set("preferences", "overlay_next_line", str(self._ov_show_next).lower())
        self._ov_relayout()

    # ── Preferences ──────────────────────────────────────────────────
    def _ov_load_prefs(self):
        if getattr(self, "_ov_loaded", False):
            return
        self._ov_loaded = True
        g = M._cfg_get
        self._ov_enabled = (g("preferences", "overlay_enabled", "false") or "").lower() == "true"
        self._ov_show_next = (g("preferences", "overlay_next_line", "true") or "").lower() == "true"
        self._ov_locked = (g("window", "overlay_locked", "true") or "").lower() != "false"
        try:
            self._ov_size = max(MIN_SIZE, min(MAX_SIZE, int(g("preferences", "overlay_size",
                                                                 str(DEFAULT_SIZE)))))
        except (TypeError, ValueError):
            self._ov_size = DEFAULT_SIZE
        self._ov_anchor = None
        geo = parse_geometry(g("window", "overlay_geometry", ""))
        if geo:
            w, h, x, y = geo
            cx, by = x + w // 2, y + h
            mon = monitor_at(cx, by - 1) if NATIVE else None
            # Only honour it on a monitor that still exists.
            if not NATIVE or (mon and mon[2]):
                self._ov_anchor = anchor_for(x, y, w, h, mon[0] if (NATIVE and mon) else None)

    def _ov_prefs(self):
        self._ov_load_prefs()
        return {"enabled": self._ov_enabled, "locked": self._ov_locked,
                "size": self._ov_size, "next": self._ov_show_next}

    def _ov_sync_settings(self):
        paint = getattr(self, "_ov_paint_settings", None)
        if paint:
            try:
                paint()
            except Exception:
                pass
        try:
            self._sync_settings_switches()
        except Exception:
            pass

    # ── Window lifetime ──────────────────────────────────────────────
    def _ov_open(self):
        if getattr(self, "_ov_top", None) is not None or not PIL_AVAILABLE:
            self._ov_kick()
            return
        self._ov_text = getattr(self, "_ov_text", None) or TextRenderer()
        self._ov_scene = None
        self._ov_scene_key = None
        self._ov_content = None
        self._ov_trans = None
        self._ov_alpha = 0.0
        self._ov_alpha_to = 0.0
        self._ov_paused_at = None
        self._ov_drag = None
        self._ov_fill_key = None
        self._ov_geo = None
        top = tk.Toplevel(self._root)
        top.withdraw()
        top.overrideredirect(True)
        top.configure(bg="#000000", bd=0, highlightthickness=0)
        top.title("Statusify lyrics overlay")
        self._ov_top = top
        win = None
        if NATIVE:
            try:
                # Map it once, off-screen, so Tk creates the wrapper HWND,
                # then make that layered and hide it again ourselves.
                top.geometry("1x1+-32000+-32000")
                top.deiconify()
                top.update_idletasks()
                hwnd = int(top.wm_frame(), 16)
                parent = _win()["u"].GetParent(top.winfo_id())
                if parent and parent != hwnd:
                    hwnd = parent
                win = _LayeredWindow(hwnd)
                # Hide BEFORE adding WS_EX_LAYERED: a window made layered
                # while visible and then hidden refuses its first
                # UpdateLayeredWindow with ERROR_INVALID_PARAMETER.
                win.w["u"].ShowWindow(hwnd, SW_HIDE)
                win.set_style(self._ov_locked)
                top.attributes("-topmost", True)
                # Probe: a 1x1 transparent frame must be accepted.
                win.alpha = 0
                if not win.present(Image.new("RGBA", (1, 1), (0, 0, 0, 0)), (-32000, -32000)):
                    raise OSError(f"UpdateLayeredWindow refused ({ctypes.get_last_error()})")
            except Exception as e:
                M.log(f"Overlay: per-pixel alpha unavailable ({e}) — using colour key")
                if win is not None:
                    win.release()
                win = _ColorKeyWindow(top)
                win.set_style(self._ov_locked)
                try:
                    top.attributes("-topmost", True)
                except tk.TclError:
                    pass
        else:
            win = _NullWindow()
            win.set_style(self._ov_locked)
        self._ov_win = win
        top.bind("<Destroy>", lambda e: e.widget is top and self._ov_released(), add="+")
        top.bind("<ButtonPress-1>", self._ov_on_press)
        top.bind("<B1-Motion>", self._ov_on_motion)
        top.bind("<ButtonRelease-1>", self._ov_on_release)
        top.bind("<Double-Button-1>", lambda e: self._ov_locked or self._overlay_set_locked(True))
        top.bind("<MouseWheel>", self._ov_on_wheel)
        self._ov_relayout()

    def _ov_released(self):
        win = getattr(self, "_ov_win", None)
        self._ov_win = None
        self._ov_top = None
        if win is not None:
            win.release()
        self._ov_fine_timer(False)

    def _ov_close(self):
        try:
            self._cancel("overlay")
        except Exception:
            pass
        top = getattr(self, "_ov_top", None)
        win = getattr(self, "_ov_win", None)
        if win is not None:
            try:
                win.show(False)
            except Exception:
                pass
        if top is not None:
            try:
                top.destroy()
            except tk.TclError:
                pass
        self._ov_released()
        self._ov_scene = self._ov_content = self._ov_trans = None

    def _ov_kick(self, ms=1):
        if getattr(self, "_ov_top", None) is not None:
            self._schedule("overlay", ms, self._ov_tick)

    # ── Geometry ─────────────────────────────────────────────────────
    def _ov_monitor(self, x, y, primary=False):
        mon = monitor_at(x, y, primary) if NATIVE else None
        if mon is None:
            try:
                sw, sh = self._root.winfo_screenwidth(), self._root.winfo_screenheight()
            except tk.TclError:
                sw, sh = 1920, 1080
            return (0, 0, sw, sh - 48), 96
        return mon[0], mon[1]

    def _ov_relayout(self):
        """Size the strip for the monitor it is on and the text size."""
        if getattr(self, "_ov_top", None) is None:
            return
        if self._ov_anchor is None:
            work, dpi = self._ov_monitor(0, 0, primary=True)
            s = dpi / 96.0
            self._ov_anchor = ((work[0] + work[2]) // 2, work[3] - int(36 * s), False)
        cx, ey, top = self._ov_anchor
        work, dpi = self._ov_monitor(cx, ey + (1 if top else -1))
        s = dpi / 96.0
        TR = self._ov_text
        px = max(8, int(round(self._ov_size * s)))
        npx = max(8, int(round(px * 0.62)))
        lh = TR.line_height("bold", px)
        nlh = TR.line_height("semibold", npx)
        pad = max(8, int(round(px * 0.40)))
        bar = int(round(26 * s))
        gap = int(round(px * 0.16))
        ww = work[2] - work[0]
        W = int(min(ww - int(16 * s), max(int(560 * s), px * 30)))
        H = bar + pad + 2 * lh + (gap + nlh if self._ov_show_next else 0) + pad
        # A size change keeps the edge nearest the screen edge where it is (the
        # strip grows away from it). Clamping moves only what is shown: the
        # anchor is left alone, or every resize near an edge crept the strip.
        x, y = clamp_rect(cx - W // 2, ey if top else ey - H, W, H, work)
        self._ov_m = {"s": s, "px": px, "npx": npx, "lh": lh, "nlh": nlh, "pad": pad,
                      "bar": bar, "gap": gap, "W": W, "H": H, "dpi": dpi}
        self._ov_set_geo(W, H, x, y, save=True)
        self._ov_scene_key = None     # rebuild at the new size, no slide
        self._ov_content = None
        self._ov_trans = None
        self._ov_kick()

    def _ov_set_geo(self, W, H, x, y, save=False):
        self._ov_geo = (W, H, x, y)
        top = self._ov_top
        if top is not None and NATIVE:
            try:
                top.geometry(format_geometry(W, H, x, y))
            except tk.TclError:
                pass
        if save:
            M._cfg_set_soon("window", "overlay_geometry", format_geometry(W, H, x, y))

    # ── Mouse (unlocked only; locked is WS_EX_TRANSPARENT) ───────────
    def _ov_hit(self, x, y):
        for name, (x0, y0, x1, y1) in (getattr(self, "_ov_buttons", None) or {}).items():
            if x0 <= x <= x1 and y0 <= y <= y1:
                return name
        return None

    def _ov_on_press(self, e):
        if self._ov_locked or self._ov_geo is None:
            return
        hit = self._ov_hit(e.x, e.y)
        if hit == "close":
            self._overlay_set_enabled(False)
            return "break"
        if hit == "lock":
            self._overlay_set_locked(True)
            return "break"
        W, H, x, y = self._ov_geo
        self._ov_drag = (e.x_root, e.y_root, x, y)
        return "break"

    def _ov_on_motion(self, e):
        d = getattr(self, "_ov_drag", None)
        if d is None or self._ov_geo is None:
            return
        W, H, _x, _y = self._ov_geo
        nx, ny = d[2] + e.x_root - d[0], d[3] + e.y_root - d[1]
        self._ov_geo = (W, H, nx, ny)
        if self._ov_win is not None:
            self._ov_win.move(nx, ny)
        return "break"

    def _ov_on_release(self, e):
        d = getattr(self, "_ov_drag", None)
        self._ov_drag = None
        if d is None or self._ov_geo is None:
            return
        W, H, x, y = self._ov_geo
        if (x, y) == (d[2], d[3]):
            return "break"            # a click, not a drag
        self._ov_anchor = anchor_for(x, y, W, H, self._ov_monitor(x + W // 2, y + H // 2)[0])
        # Re-fit for the monitor it was dropped on (work area and DPI).
        self._ov_relayout()
        return "break"

    def _ov_on_wheel(self, e):
        if self._ov_locked:
            return "break"
        self._overlay_set_size(self._ov_size + (2 if e.delta > 0 else -2))
        return "break"

    # ── Frame clock ──────────────────────────────────────────────────
    def _ov_tick(self):
        if getattr(self, "_ov_top", None) is None:
            return
        t0 = time.perf_counter()
        try:
            delay = self._ov_step(time.monotonic())
        except Exception as e:
            M.log(f"Overlay frame failed: {e}")
            delay = 1000
        self._ov_fine_timer(delay < 30)
        spent = (time.perf_counter() - t0) * 1000.0
        self._schedule("overlay", max(1, int(delay - spent)), self._ov_tick)

    def _ov_fine_timer(self, on):
        """1 ms timer resolution only while sliding (see _np_fine_timer);
        timeBeginPeriod is reference-counted, so this pairs with itself."""
        if on == getattr(self, "_ov_fine", False):
            return
        self._ov_fine = on
        if not NATIVE:
            return
        try:
            (ctypes.windll.winmm.timeBeginPeriod if on else ctypes.windll.winmm.timeEndPeriod)(1)
        except Exception:
            pass

    def _ov_view(self, pos):
        st = M.state
        synced = st.synced if (st.lyrics_mode == "synced" and st.synced) else []
        view = pick_view(synced, pos, getattr(st, "duration_ms", 0) or 0) if synced else None
        return synced, view

    def _ov_step(self, now):
        st = M.state
        playing = bool(getattr(st, "is_playing", False))
        pos = st.position_ms + M._track_offset_ms()
        synced, view = self._ov_view(pos)
        if playing:
            self._ov_paused_at = None
        elif self._ov_paused_at is None:
            self._ov_paused_at = now
        unlocked = not self._ov_locked
        if view is None and unlocked:
            view = {"kind": "hint", "idx": -2, "cur": "Statusify lyrics",
                    "next": "Drag to move · scroll to resize", "syl": None}
        want = unlocked or (view is not None and (
            playing or now - (self._ov_paused_at or now) < PAUSE_HIDE_S))
        anim = bool(getattr(M, "ANIMATIONS_ENABLED", True))
        busy = False

        # 1. Scene: rebuild when what is shown changes.
        if view is not None:
            m = self._ov_m
            key = (view["kind"], view["idx"], view.get("cur"), view.get("next"),
                   view.get("dots"), M.ACCENT, m["W"], m["H"], self._ov_show_next, unlocked,
                   id(synced))
            if key != self._ov_scene_key:
                old = self._ov_content
                old_idx = self._ov_scene["view"]["idx"] if self._ov_scene else None
                self._ov_scene_key = key
                self._ov_scene = self._ov_build_scene(view)
                self._ov_fill_key = None
                new = self._ov_scene_frame(pos)
                slide = (anim and old is not None and old.size == new.size
                         and self._ov_alpha > 0.05 and old_idx != view["idx"])
                if slide:
                    self._ov_trans = {"old": old, "t0": now,
                                      "up": old_idx is None or view["idx"] >= old_idx}
                else:
                    self._ov_trans = None
                    self._ov_present(new)
                self._ov_content = new
                if self._ov_win is not None and self._ov_alpha > 0:
                    self._ov_win.raise_top()

        # 2. Slide between the old line and the new one.
        tr = self._ov_trans
        if tr is not None:
            t = (now - tr["t0"]) / SLIDE_S
            if t >= 1.0:
                self._ov_trans = None
                self._ov_present(self._ov_content)
            else:
                if self._ov_scene and self._ov_scene["karaoke"] and playing:
                    self._ov_content = self._ov_scene_frame(pos)
                self._ov_present(self._ov_slide_frame(tr["old"], self._ov_content, _ease(t),
                                                      tr["up"]))
                busy = True
        # 3. Karaoke fill while a timed line is being sung.
        elif self._ov_scene is not None and self._ov_scene["karaoke"] and playing:
            sc = self._ov_scene
            band = self._ov_karaoke_band(pos)
            if band is not None:
                self._ov_content.paste(band, (0, sc["band"][0]))
                self._ov_present(self._ov_content, band=(sc["band"][0], band))
            if pos < sc["syl_end"]:
                busy = True

        # 4. Whole-window fade.
        self._ov_alpha_to = 1.0 if (want and self._ov_content is not None) else 0.0
        if self._ov_alpha != self._ov_alpha_to:
            dt = now - (getattr(self, "_ov_last_t", None) or now)
            step = max(1.0 / 60, min(0.25, dt)) / FADE_S if anim else 1.0
            if self._ov_alpha < self._ov_alpha_to:
                self._ov_alpha = min(self._ov_alpha_to, self._ov_alpha + step)
            else:
                self._ov_alpha = max(self._ov_alpha_to, self._ov_alpha - step)
            win = self._ov_win
            if win is not None:
                if self._ov_alpha > 0:
                    win.show(True)
                win.set_alpha(int(round(255 * self._ov_alpha)))
                if self._ov_alpha <= 0:
                    win.show(False)
            if self._ov_alpha != self._ov_alpha_to:
                busy = True

        self._ov_last_t = now
        if busy:
            return 16 if (self._ov_trans is not None or self._ov_alpha != self._ov_alpha_to) \
                else KARAOKE_MS
        if self._ov_alpha <= 0 and not want:
            return IDLE_MS
        nxt = ms_to_next_event(synced, pos, view) if (synced and playing) else IDLE_MS
        if not playing and self._ov_paused_at is not None and self._ov_alpha > 0 and not unlocked:
            nxt = min(nxt, int((PAUSE_HIDE_S - (now - self._ov_paused_at)) * 1000) + 5)
        return max(4, min(IDLE_MS, nxt))

    # ── Rendering ────────────────────────────────────────────────────
    def _ov_build_scene(self, view):
        """Masks and the static RGBA layer for one line (or gap / hint)."""
        m = self._ov_m
        TR = self._ov_text
        W, H, pad, px, npx = m["W"], m["H"], m["pad"], m["px"], m["npx"]
        maxw = W - 2 * pad
        white = Image.new("L", (W, H), 0)     # drawn in white
        acc = Image.new("L", (W, H), 0)       # drawn in the accent
        dim = Image.new("L", (W, H), 0)       # next line
        dw, da, dd = ImageDraw.Draw(white), ImageDraw.Draw(acc), ImageDraw.Draw(dim)
        cur_bottom = H - pad - ((m["gap"] + m["nlh"]) if self._ov_show_next else 0)
        rows = []
        karaoke = None
        syl_end = 0
        text_full = ""
        p = px

        if view["kind"] == "gap":
            lh = TR.line_height("bold", px)
            y = cur_bottom - lh
            toks = ["♪"] + ["•"] * 3
            sp = int(px * 0.55)
            widths = [TR.measure(t, "bold", px) for t in toks]
            x = int((W - (sum(widths) + sp * (len(toks) - 1))) / 2)
            for i, (t, w) in enumerate(zip(toks, widths)):
                d = da if (i == 0 or i <= view["dots"]) else dw
                TR.draw(d, (x, y), t, "bold", px, 255 if d is da else 150)
                x += int(w) + sp
        else:
            syl = view.get("syl")
            if syl:
                raw = "".join(t for _a, _b, t in syl)
                lead = len(raw) - len(raw.lstrip())
                text_full = raw.strip()
                karaoke = (syl, lead)
                syl_end = syl[-1][1]
            else:
                text_full = view.get("cur") or ""
            lines = TR.wrap(text_full, "bold", p, maxw) if text_full else []
            while len(lines) > 2 and p > px * 0.62:
                p = int(p * 0.9)
                lines = TR.wrap(text_full, "bold", p, maxw)
            if len(lines) > 2:
                lines = lines[:1] + [TR.ellipsize(" ".join(lines[1:]), "bold", p, maxw)]
            lh = TR.line_height("bold", p)
            cursor = 0
            for k, ln in enumerate(lines):
                j = text_full.find(ln, cursor)
                if j < 0:
                    j = cursor
                w = TR.measure(ln, "bold", p)
                x = int((W - w) / 2)
                y = cur_bottom - (len(lines) - k) * lh
                TR.draw(dw if karaoke else da, (x, y), ln, "bold", p, 255)
                pre = [TR.measure(ln[:i], "bold", p) for i in range(len(ln) + 1)] if karaoke else None
                rows.append({"text": ln, "x": x, "y": y, "cs": j, "ce": j + len(ln), "w": w,
                             "lh": lh, "pre": pre})
                cursor = j + len(ln)
        nxt = view.get("next") or ""
        if self._ov_show_next and nxt:
            t = TR.ellipsize(nxt, "semibold", npx, maxw)
            w = TR.measure(t, "semibold", npx)
            TR.draw(dd, (int((W - w) / 2), H - pad - m["nlh"]), t, "semibold", npx, 255)

        # Shadow: a crisp dark outline plus a soft blur, for bright backgrounds.
        allm = ImageChops.lighter(ImageChops.lighter(white, acc), dim)
        shadow = Image.new("L", (W, H), 0)
        bb = allm.getbbox()
        if bb:
            mg = pad
            box = (max(0, bb[0] - mg), max(0, bb[1] - mg), min(W, bb[2] + mg), min(H, bb[3] + mg))
            crop = allm.crop(box)
            k = 3 if px < 34 else 5
            dil = crop.filter(ImageFilter.MaxFilter(k))
            blur = dil.filter(ImageFilter.GaussianBlur(max(2.0, px * 0.11)))
            sh = ImageChops.lighter(dil.point(_lut(0.55)), blur.point(_lut(1.6, 225)))
            shadow.paste(sh, box[:2])

        accent = readable_accent(M.ACCENT)
        base = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        base.paste((0, 0, 0, 255), mask=shadow)
        base.paste((245, 245, 245, 255), mask=white)
        base.paste(accent + (255,), mask=acc)
        base.paste((235, 235, 235, 255), mask=dim.point(_lut(NEXT_ALPHA)))

        scene = {"view": view, "base": base, "karaoke": karaoke, "rows": rows,
                 "syl_end": syl_end, "accent": accent, "band": None}
        if karaoke and rows:
            y0 = max(0, min(r["y"] for r in rows) - pad)
            y1 = min(H, max(r["y"] + r["lh"] for r in rows) + pad)
            scene["band"] = (y0, y1)
            scene["band_base"] = base.crop((0, y0, W, y1))
            scene["band_mask"] = white.crop((0, y0, W, y1))
            g = max(4, int(p * 0.5))
            grad = Image.linear_gradient("L").rotate(-90, expand=True).resize((g, rows[0]["lh"]))
            scene["grad"] = grad          # 255 on the left fading to 0
        return scene

    def _ov_fill_widths(self, pos):
        sc = self._ov_scene
        syl, lead = sc["karaoke"]
        n = sung_chars(syl, pos) - lead
        out = []
        for r in sc["rows"]:
            k = n - r["cs"]
            if k <= 0:
                out.append(0)
            elif k >= len(r["text"]):
                out.append(int(r["w"]) + sc["grad"].size[0])
            else:
                i = int(k)
                pre = r["pre"]
                out.append(int(pre[i] + (pre[i + 1] - pre[i]) * (k - i)))
        return tuple(out)

    def _ov_karaoke_band(self, pos):
        """The current line's rows with the sung part filled, or None when
        the fill has not moved a whole pixel since the last frame."""
        sc = self._ov_scene
        fills = self._ov_fill_widths(pos)
        if fills == self._ov_fill_key:
            return None
        self._ov_fill_key = fills
        y0, y1 = sc["band"]
        W = sc["band_base"].size[0]
        prog = Image.new("L", (W, y1 - y0), 0)
        d = ImageDraw.Draw(prog)
        grad = sc["grad"]
        g = grad.size[0]
        for r, fx in zip(sc["rows"], fills):
            if fx <= 0:
                continue
            edge = r["x"] + fx - g // 2
            if edge > r["x"]:
                d.rectangle((r["x"] - 4, r["y"] - y0, edge - 1, r["y"] - y0 + r["lh"]), fill=255)
            prog.paste(grad, (edge, r["y"] - y0))
        mask = ImageChops.multiply(sc["band_mask"], prog)
        band = sc["band_base"].copy()
        band.paste(sc["accent"] + (255,), mask=mask)
        return band

    def _ov_scene_frame(self, pos):
        """Full RGBA content of the current scene at pos."""
        sc = self._ov_scene
        img = sc["base"].copy()
        if sc["karaoke"] and sc["band"]:
            self._ov_fill_key = None
            band = self._ov_karaoke_band(pos)
            if band is not None:
                img.paste(band, (0, sc["band"][0]))
        return img

    @staticmethod
    def _ov_faded(img, a):
        if a >= 0.999:
            return img
        out = img.copy()
        out.putalpha(img.getchannel("A").point(_lut(a)))
        return out

    def _ov_slide_frame(self, old, new, e, up=True):
        W, H = new.size
        dist = int(self._ov_m["lh"] * 0.45) * (1 if up else -1)
        frame = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        for img, a, dy in ((old, 1.0 - e, -int(dist * e)), (new, e, int(dist * (1.0 - e)))):
            if a <= 0.01:
                continue
            layer = self._ov_faded(img, a)
            if dy >= 0:
                frame.alpha_composite(layer, dest=(0, dy), source=(0, 0, W, H - dy))
            else:
                frame.alpha_composite(layer, dest=(0, 0), source=(0, -dy, W, H))
        return frame

    def _ov_chrome(self, img):
        """Unlocked: a translucent panel, accent border, grip, Lock and x."""
        m = self._ov_m
        W, H = img.size
        s = m["s"]
        key = (W, H, M.ACCENT)
        cached = getattr(self, "_ov_chrome_cache", None)
        if cached is None or cached[0] != key:
            TR = self._ov_text
            acc = readable_accent(M.ACCENT)
            under = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            d = ImageDraw.Draw(under)
            r = int(12 * s)
            d.rounded_rectangle((0, 0, W - 1, H - 1), radius=r, fill=(12, 12, 16, 150),
                                outline=acc + (230,), width=max(2, int(2 * s)))
            over = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            o = ImageDraw.Draw(over)
            fs = max(10, int(12 * s))
            bh = m["bar"] - int(6 * s)
            ty = int(4 * s)
            # grip + hint
            gx = int(12 * s)
            for i in range(3):
                for j in range(2):
                    cx, cy = gx + j * int(6 * s), ty + int(5 * s) + i * int(6 * s)
                    o.ellipse((cx, cy, cx + int(3 * s), cy + int(3 * s)), fill=(230, 230, 230, 220))
            TR.draw(o, (gx + int(20 * s), ty + int(2 * s)),
                    "Drag to move · scroll to resize · double-click to lock",
                    "semibold", fs, (230, 230, 230, 230))
            # x
            bx1 = W - int(8 * s)
            bx0 = bx1 - bh
            o.rounded_rectangle((bx0, ty, bx1, ty + bh), radius=int(5 * s), fill=(255, 255, 255, 40))
            q = int(bh * 0.3)
            o.line((bx0 + q, ty + q, bx1 - q, ty + bh - q), fill=(240, 240, 240, 255), width=max(2, int(2 * s)))
            o.line((bx0 + q, ty + bh - q, bx1 - q, ty + q), fill=(240, 240, 240, 255), width=max(2, int(2 * s)))
            # Lock
            lw = int(TR.measure("Lock", "semibold", fs)) + int(18 * s)
            lx1 = bx0 - int(6 * s)
            lx0 = lx1 - lw
            o.rounded_rectangle((lx0, ty, lx1, ty + bh), radius=int(5 * s), fill=acc + (255,))
            TR.draw(o, (lx0 + int(9 * s), ty + (bh - TR.line_height("semibold", fs)) // 2),
                    "Lock", "semibold", fs, (10, 10, 10, 255))
            self._ov_buttons = {"close": (bx0, ty, bx1, ty + bh), "lock": (lx0, ty, lx1, ty + bh)}
            cached = (key, under, over)
            self._ov_chrome_cache = cached
        out = cached[1].copy()
        out.alpha_composite(img)
        out.alpha_composite(cached[2])
        return out

    def _ov_present(self, img, band=None):
        win = getattr(self, "_ov_win", None)
        if win is None or img is None or self._ov_geo is None:
            return
        W, H, x, y = self._ov_geo
        if not self._ov_locked:
            img = self._ov_chrome(img)
            band = None
        if not win.present(img, (x, y), band=band):
            M.log("Overlay: UpdateLayeredWindow failed")

    # ── Settings rows (Lyrics section) ───────────────────────────────
    def _overlay_settings_rows(self, T):
        """Rows for the Settings "Lyrics" card: the Desktop overlay group."""
        from statusify_ui_settings import _Buttons
        self._ov_load_prefs()
        self.lbl_ov_size = T()
        self.lbl_ov_lock = T()

        def paint():
            self.lbl_ov_size.config(text=str(self._ov_size),
                                    fg=M.ACCENT if self._ov_size != DEFAULT_SIZE else M.TEXT2)
            self.lbl_ov_lock.config(text="Locked" if self._ov_locked else "Movable",
                                    fg=M.TEXT2 if self._ov_locked else M.ACCENT)
        self._ov_paint_settings = paint
        paint()
        hk = hotkey_combo()
        return [
            {"title": "Desktop overlay",
             "desc": "Shows the current line over other windows and borderless games. "
                     "Clicks pass through it." + (f" Toggle with {hk}." if hk else ""),
             "ctl": self._switch_ctl(lambda: self._ov_enabled,
                                     lambda: self._overlay_set_enabled(not self._ov_enabled))},
            {"title": "Overlay: show the next line",
             "ctl": self._switch_ctl(lambda: self._ov_show_next,
                                     lambda: self._overlay_set_show_next(not self._ov_show_next))},
            {"title": "Overlay text size",
             "ctl": _Buttons(self, ("btn", "A−", lambda: self._overlay_set_size(self._ov_size - 2),
                                    "secondary"),
                             ("value", self.lbl_ov_size, 48),
                             ("btn", "A+", lambda: self._overlay_set_size(self._ov_size + 2),
                                    "secondary"))},
            {"title": "Overlay position",
             "desc": "Unlock, drag it where you want it, then Lock (or double-click it).",
             "ctl": _Buttons(self, ("value", self.lbl_ov_lock, 64),
                             ("btn", "Unlock to move", self._overlay_toggle_lock, "secondary"))},
        ]

    def _overlay_hotkey_row(self, widget_cls, height):
        """Row for the Settings "Global hotkeys" card."""
        var = tk.StringVar(value=hotkey_combo())
        e = self._entry(var, 18)

        def save(_ev=None):
            if var.get().strip() == hotkey_combo():
                return
            set_hotkey_combo(var.get())
            M._register_hotkeys(self)
        e.bind("<Return>", save)
        e.bind("<FocusOut>", save)
        self._ov_hotkey_var = var
        self._ov_hotkey_save = save
        return {"title": "Lyrics overlay on/off", "ctl": widget_cls(e, height=height)}
