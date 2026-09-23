"""Mini player and the system-tray icon.

App inherits MiniTrayMixin. Names that belong to main are reached through
M, the live main module, bound by main at import time: palette colours and
settings are rebound at runtime, so they must be read from main on every
use, never copied.

The mini player is a rounded "pill" rendered with PIL into one PhotoImage,
the same way the Now Playing page draws its sheet. Tk can't give a
toplevel per-pixel alpha, so the corners use a colour key
(-transparentcolor): pixels exactly KEY are cut out of the window (and are
click-through), and the anti-aliased rim is pre-blended against a darkened
edge colour instead of the key, so it reads as a soft hairline rather than
a fringe of key colour. -alpha still applies on top for the idle fade.

Nothing here renders on a clock: the pill is recomposed only when what it
shows changes (track, cover, lyric, play state, hover) or while one of its
short animations is running.
"""
import os
import re
import threading
import time
import tkinter as tk

try:
    from PIL import Image, ImageDraw, ImageTk
    _PIL = True
except ImportError:   # pragma: no cover - Pillow ships with the app
    _PIL = False

import statusify_fluid as fluid

M = None   # the main module


# ── Pure helpers (unit-tested without Tk) ─────────────────────────

KEY_RGB = (1, 2, 3)                 # colour key: never produced by the renderer
KEY_HEX = "#010203"

SNAP_THRESHOLD = 24                 # px: magnetic range of an edge
SNAP_MARGIN = 12                    # px: gap kept from a snapped edge
IDLE_ALPHA = 0.35
IDLE_DELAY = 1.5                    # s after the pointer leaves
LYRIC_ANIM_S = 0.28
_PLACEHOLDER_LINES = {"", "—", "— ", "-", "♪", "• • •", "…"}


def ease_out(p):
    p = max(0.0, min(1.0, p))
    return 1 - (1 - p) ** 3


def snap_position(x, y, w, h, area, threshold=SNAP_THRESHOLD, margin=SNAP_MARGIN):
    """Where a w×h window dropped at (x, y) should settle inside `area`
    (left, top, right, bottom — the monitor's work area).

    Each axis snaps on its own to the near edge, the far edge or (x only)
    the centre when within `threshold` px of it, so a drop near a corner
    lands in the corner. Being dragged past an edge counts as near it.
    Whatever happens, the window ends up fully on the monitor."""
    left, top, right, bottom = area

    def axis(p, size, lo, hi, centre):
        near, far = lo + margin, hi - margin - size
        cands = [(near, p < near), (far, p > far)]
        if centre:
            cands.append(((lo + hi - size) // 2, False))
        best, dist = p, None
        for c, past in cands:
            d = abs(p - c)
            if (d <= threshold or past) and (dist is None or d < dist):
                best, dist = c, d
        if hi - lo <= size:
            return lo
        return int(max(lo, min(best, hi - size)))

    return axis(x, w, left, right, True), axis(y, h, top, bottom, False)


def parse_position(geometry):
    """(x, y) from a Tk geometry string ('560x76+10+-5', '+3+4'), or None."""
    m = re.search(r"\+(-?\d+)\+(-?\d+)\s*$", geometry or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


class Tween:
    """A scalar that eases (cubic out) from its current value to a target."""

    def __init__(self, value):
        self._from = self._to = float(value)
        self._t0 = 0.0
        self._dur = 0.0

    def value(self, now):
        if now <= self._t0:
            return self._from
        if self._dur <= 0 or now >= self._t0 + self._dur:
            return self._to
        return self._from + (self._to - self._from) * ease_out((now - self._t0) / self._dur)

    def to(self, target, now, dur, delay=0.0):
        self._from = self.value(now)
        self._to = float(target)
        self._t0 = now + delay
        self._dur = dur

    def done(self, now):
        return now >= self._t0 + self._dur

    @property
    def target(self):
        return self._to


class FadeState:
    """Opacity of the mini player: full while hovered, dims to `idle` after
    the pointer has been away for `delay` seconds, and comes straight back
    when it returns — from wherever a half-finished fade left it."""

    def __init__(self, idle=IDLE_ALPHA, delay=IDLE_DELAY, fade_out=0.45, fade_in=0.15):
        self.idle, self.delay = idle, delay
        self.fade_out, self.fade_in = fade_out, fade_in
        self.hovered = False
        self._t = Tween(1.0)

    def enter(self, now):
        self.hovered = True
        self._t.to(1.0, now, self.fade_in)

    def leave(self, now):
        self.hovered = False
        self._t.to(self.idle, now, self.fade_out, delay=self.delay)

    def alpha(self, now):
        return self._t.value(now)

    def animating(self, now):
        return not self._t.done(now)


def _u16(s):
    return len(s.encode("utf-16-le")) // 2


def _clip16(s, n):
    """`s` cut to at most `n` UTF-16 units (what szTip counts), with '…'."""
    if _u16(s) <= n:
        return s
    out, used = [], 0
    for ch in s:
        u = 2 if ord(ch) > 0xFFFF else 1
        if used + u > n - 1:
            break
        out.append(ch)
        used += u
    return "".join(out).rstrip() + "…"


def tray_tooltip(title, artist, lyric="", playing=True, limit=127):
    """Tray hover text: 'Title — Artist' and, on a second line, the current
    lyric (or 'Paused'). NOTIFYICONDATA.szTip holds 128 WCHARs (127 + NUL),
    counted in UTF-16 units, so the lyric is shortened first and the track
    line only when it alone would not fit."""
    one = lambda s: " ".join(str(s or "").split())
    title, artist, lyric = one(title), one(artist), one(lyric)
    if not title:
        return _clip16("Statusify — waiting for Spotify", limit)
    head = _clip16(f"{title} — {artist}" if artist else title, min(limit, 90))
    if not playing:
        second = "Paused"
    else:
        second = "♪ " + lyric if lyric not in _PLACEHOLDER_LINES else ""
    room = limit - _u16(head) - 1
    if second and room >= 6:
        return head + "\n" + _clip16(second, room)
    return head


def monitor_work_area(x, y):
    """(left, top, right, bottom) of the work area of the monitor nearest
    (x, y), or None off Windows. A private WinDLL, so setting argtypes can't
    disturb other ctypes users of user32."""
    try:
        import ctypes
        from ctypes import wintypes

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

        u32 = ctypes.WinDLL("user32")
        u32.MonitorFromPoint.restype = wintypes.HANDLE
        u32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
        u32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MONITORINFO)]
        hmon = u32.MonitorFromPoint(wintypes.POINT(int(x), int(y)), 2)  # NEAREST
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(mi)
        if not hmon or not u32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            return None
        r = mi.rcWork
        return (r.left, r.top, r.right, r.bottom)
    except Exception:
        return None


# ── Drawing helpers ───────────────────────────────────────────────

def _hex(c):
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def _mix(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _aa_mask(size, radius, ss=4):
    """Anti-aliased rounded-rect mask filling `size`: drawn at ss×, downsampled."""
    w, h = size
    big = Image.new("L", (w * ss, h * ss), 0)
    ImageDraw.Draw(big).rounded_rectangle((0, 0, w * ss - 1, h * ss - 1),
                                          radius=radius * ss, fill=255)
    return big.resize((w, h), Image.LANCZOS)


def _with_alpha(img, a):
    if a >= 0.999:
        return img
    out = img.copy()
    out.putalpha(img.getchannel("A").point(lambda v: int(v * max(0.0, a))))
    return out


def _blit(dst, src, x, y, alpha=1.0):
    """alpha_composite `src` onto `dst` at (x, y), clipped to `dst`."""
    if alpha <= 0.003:
        return
    x, y = int(round(x)), int(round(y))
    sx0, sy0 = max(0, -x), max(0, -y)
    sx1, sy1 = min(src.width, dst.width - x), min(src.height, dst.height - y)
    if sx1 <= sx0 or sy1 <= sy0:
        return
    if (sx0, sy0, sx1, sy1) != (0, 0, src.width, src.height):
        src = src.crop((sx0, sy0, sx1, sy1))
    dst.alpha_composite(_with_alpha(src, alpha), (x + sx0, y + sy0))


def _glyph(kind, d, color, ss=4):
    """A control glyph as a d×d RGBA sprite, supersampled for clean edges."""
    D = d * ss
    im = Image.new("RGBA", (D, D), (0, 0, 0, 0))
    g = ImageDraw.Draw(im)
    c = tuple(color) + (255,)
    u = D / 16.0
    if kind == "play":
        g.polygon([(5.8 * u, 4.2 * u), (5.8 * u, 11.8 * u), (12.2 * u, 8 * u)], fill=c)
    elif kind == "pause":
        g.rounded_rectangle((5.0 * u, 4.4 * u, 7.1 * u, 11.6 * u), radius=0.7 * u, fill=c)
        g.rounded_rectangle((8.9 * u, 4.4 * u, 11.0 * u, 11.6 * u), radius=0.7 * u, fill=c)
    elif kind in ("next", "prev"):
        g.polygon([(3.6 * u, 3.8 * u), (3.6 * u, 12.2 * u), (10.4 * u, 8 * u)], fill=c)
        g.rounded_rectangle((10.6 * u, 3.8 * u, 12.4 * u, 12.2 * u), radius=0.6 * u, fill=c)
        if kind == "prev":
            im = im.transpose(Image.FLIP_LEFT_RIGHT)
    elif kind == "disc":
        g.ellipse((0, 0, D - 1, D - 1), fill=c)
    return im.resize((d, d), Image.LANCZOS)


class MiniTrayMixin:

    # ── Mini mode: lifecycle ──────────────────────────────────────
    def _toggle_mini(self, _e=None):
        if getattr(self, "_mini", None) is not None:
            self._close_mini()
        else:
            self._open_mini()

    def _mini_scale(self):
        return max(1.0, min(3.0, float(getattr(self, "_np_s", 1.0) or 1.0)))

    def _mini_layout(self):
        s = self._mini_scale()
        S = lambda v: int(round(v * s))
        W, H = S(440), S(68)
        pad = S(8)
        return {
            "W": W, "H": H, "pad": pad, "cover": H - 2 * pad,
            "tx": pad + (H - 2 * pad) + S(12),      # text left
            "tx1": W - S(60),                       # text right
            "title_y": S(13), "title_px": S(12),
            "lyric_y": S(30), "lyric_px": S(17), "lyric_h": S(28),
            "play_r": S(16),
            "play_c": (W - S(34), W - S(70)),       # play x: collapsed, hovered
            "next_x": W - S(32), "prev_x": W - S(106), "btn_r": S(13),
            "fade_x0": W - S(162), "fade_x1": W - S(124),
            "slide": S(12),
        }

    def _open_mini(self):
        """A compact always-on-top pill: cover, track, the current lyric and
        a play/pause button (previous/next slide in on hover).

        The main window is 540x720 — far too big to leave floating over a
        game or a video. Mini mode is the form this app wants most of the
        time: one line of text, always visible, out of the way."""
        try:
            L = self._mini_layout()
            m = tk.Toplevel(self._root)
            m.withdraw()
            m.overrideredirect(True)
            m.attributes("-topmost", True)
            m.configure(bg=KEY_HEX)
            try:
                m.attributes("-transparentcolor", KEY_HEX)
            except tk.TclError:
                pass   # not Windows: square corners, still usable
            cv = tk.Canvas(m, width=L["W"], height=L["H"], bg=KEY_HEX,
                           highlightthickness=0, bd=0)
            cv.pack(fill="both", expand=True)
            self._mini_cv = cv
            self._mini_item = cv.create_image(0, 0, anchor="nw")
            self._mini_photo = None
            self._mini_frame = None
            self._mini_parts = {}
            self._mini_shown = None           # content key of the last frame
            self._mini_lyric_cur = None
            self._mini_ly_anim = None         # (old text, t0) while sliding
            self._mini_fade = FadeState()
            self._mini_alpha = 1.0
            self._mini_hover = Tween(0.0)     # 0 collapsed → 1 prev/next shown
            self._mini_hot = None             # control under the pointer
            self._mini_snap = None            # (x0, y0, x1, y1, t0) settle
            self._mini_drag = None
            self._mini_menu = None

            sw, sh = m.winfo_screenwidth(), m.winfo_screenheight()
            pos = parse_position(M._cfg_get("window", "mini_geometry", ""))
            if pos is None:
                pos = ((sw - L["W"]) // 2, 40)
            # Keep a saved spot on screen (its monitor may be gone).
            area = monitor_work_area(pos[0] + L["W"] // 2, pos[1] + L["H"] // 2) or (0, 0, sw, sh)
            x, y = snap_position(pos[0], pos[1], L["W"], L["H"], area, threshold=0, margin=0)
            m.geometry(f"{L['W']}x{L['H']}+{x}+{y}")

            cv.bind("<ButtonPress-1>", self._mini_press)
            cv.bind("<B1-Motion>", self._mini_motion_drag)
            cv.bind("<ButtonRelease-1>", self._mini_release)
            cv.bind("<Double-Button-1>", self._mini_double)
            cv.bind("<Motion>", self._mini_motion)
            cv.bind("<Enter>", self._mini_enter)
            cv.bind("<Leave>", self._mini_leave)
            cv.bind("<Button-3>", self._mini_context)

            self._mini = m
            self._mini_update(force=True)
            m.deiconify()
            m.attributes("-alpha", 1.0)
            # Unless the pointer arrives, dim after the idle delay.
            self._mini_fade.leave(time.monotonic())
            self._mini_kick()
            self._mini_tick_start()
            self._tray_sync_menu()
            M.log("Mini player on  ·  Ctrl+M to close")
        except tk.TclError as e:
            self._mini = None
            M.log(f"Mini mode failed: {e}")

    def _close_mini(self):
        m = getattr(self, "_mini", None)
        if m is None:
            return
        try:
            M._cfg_set("window", "mini_geometry", m.geometry())
        except (tk.TclError, ValueError, OSError):
            pass
        try:
            if getattr(self, "_mini_menu", None) is not None:
                self._mini_menu.destroy()
            m.destroy()
        except (tk.TclError, ValueError):
            pass
        self._mini = None
        self._mini_menu = None
        self._mini_photo = None
        self._mini_frame = None
        self._cancel("mini")
        self._cancel("mini_anim")
        self._tray_sync_menu()
        M.log("Mini player off")

    def _refresh_mini(self):
        """Mirror the current track/lyric into the mini player and the tray
        tooltip. Cheap: redraws only when something shown has changed."""
        self._mini_update()
        self._tray_update_tip()

    # ── Content ──────────────────────────────────────────────────
    def _mini_current_lyric(self):
        st = M.state
        if not getattr(st, "title", ""):
            return "Waiting for Spotify…"
        mode = getattr(st, "lyrics_mode", "none")
        if mode in ("synced", "plain") and (st.synced or st.plain):
            try:
                cur, _ = M.get_current_line()
            except Exception:
                cur = ""
            cur = (cur or "").strip()
        else:
            # No lyrics / instrumental / blacklisted: whatever the RPC loop
            # published to the Now Playing label.
            try:
                cur = (self.lbl_lyric.cget("text") or "").strip()
            except (AttributeError, tk.TclError):
                cur = ""
        return cur if cur not in _PLACEHOLDER_LINES else "♪"

    def _mini_colors(self):
        dark = bool(getattr(M, "_DARK_MODE", True))
        pal = list(getattr(self, "_np_palette", None) or [])
        if not (getattr(M, "ALBUM_TINT", True) and pal):
            pal = [_hex(c) for c in (M.BG, M.BG4, M.BG3, getattr(M, "ACCENT_SOFT", M.BG3))]
        base, blobs = fluid.normalise(pal, dark, M.BG3)
        fg = (255, 255, 255) if dark else (18, 20, 26)
        acc = _mix(_hex(M.ACCENT), fg, 0.2)
        return dark, tuple(base), tuple(tuple(b) for b in blobs), fg, acc

    def _mini_update(self, force=False):
        """Redraw if the shown content changed; start the slide when the
        lyric line changes."""
        if getattr(self, "_mini", None) is None:
            return
        lyric = self._mini_current_lyric()
        now = time.monotonic()
        if lyric != self._mini_lyric_cur:
            if self._mini_lyric_cur is not None and getattr(M, "ANIMATIONS_ENABLED", True):
                self._mini_ly_anim = (self._mini_lyric_cur, now)
                self._mini_kick()
            self._mini_lyric_cur = lyric
        if force:
            self._mini_shown = None
        self._mini_render(now)

    def _mini_content_key(self, now):
        st = M.state
        return (getattr(st, "title", ""), getattr(st, "artist", ""), self._mini_lyric_cur,
                bool(getattr(st, "is_playing", False)), id(getattr(self, "_hero_src", None)),
                self._mini_colors(), self._mini_hot, round(self._mini_hover.value(now), 3),
                self._mini_ly_anim is not None and round(now, 3))

    # ── Rendering ────────────────────────────────────────────────
    def _mini_part(self, key, build):
        """Per-slot cache: key[0] names the slot, the rest must match."""
        hit = self._mini_parts.get(key[0])
        if hit is None or hit[0] != key:
            hit = self._mini_parts[key[0]] = (key, build())
        return hit[1]

    def _mini_text(self):
        tr = getattr(self, "_mini_tr", None)
        if tr is None:
            from statusify_textrender import TextRenderer
            tr = self._mini_tr = TextRenderer()
        return tr

    def _mini_build_base(self, L, colors, title, artist, cover):
        """Background gradient, cover and the track line (RGBA)."""
        dark, base, blobs, fg, acc = colors
        W, H, pad, cs = L["W"], L["H"], L["pad"], L["cover"]
        strip = Image.new("RGB", (4, 1))
        # Blobs pulled halfway to the base: a tint, not a stripe.
        for i, c in enumerate((_mix(blobs[0], base, 0.45), base, _mix(blobs[1], base, 0.35), base)):
            strip.putpixel((i, 0), c)
        img = strip.resize((W, 1), Image.BILINEAR).resize((W, H), Image.NEAREST).convert("RGBA")
        # A soft vertical sheen: lighter top, deeper bottom.
        tone = Image.new("RGBA", (W, H), (0, 0, 0, 255))
        tone.putalpha(Image.linear_gradient("L").resize((W, H))
                      .point(lambda v: int(v * (0.22 if dark else 0.08))))
        img.alpha_composite(tone)

        # Cover, or a note on a blob-coloured tile.
        tr = self._mini_text()
        if cover is not None:
            tile = cover.convert("RGB").resize((cs, cs), Image.LANCZOS)
        else:
            tile = Image.new("RGB", (cs, cs), _mix(blobs[2], fg, 0.08))
            px = int(cs * 0.5)
            tw = tr.measure("♪", "regular", px)
            tr.draw(ImageDraw.Draw(tile), ((cs - tw) / 2, (cs - tr.line_height("regular", px)) / 2),
                    "♪", "regular", px, _mix(fg, blobs[2], 0.45))
        img.paste(tile, (pad, pad), _aa_mask((cs, cs), int(cs * 0.3)))

        # Track line: title (semibold) · artist, muted.
        px, x, x1 = L["title_px"], L["tx"], L["tx1"]
        d = ImageDraw.Draw(img)
        sub = fg + (int(255 * 0.62),)
        if title:
            t = tr.ellipsize(title, "semibold", px, x1 - x)
            adv = tr.draw(d, (x, L["title_y"]), t, "semibold", px, fg + (int(255 * 0.88),))
            if artist and x + adv + px < x1:
                rest = tr.ellipsize("  ·  " + artist, "regular", px, x1 - x - adv)
                tr.draw(d, (x + adv, L["title_y"]), rest, "regular", px, sub)
        else:
            tr.draw(d, (x, L["title_y"]), "Statusify", "semibold", px, sub)
        return img

    def _mini_build_edge(self, L, colors):
        """(mask, edge): the pill's AA mask and what its rim blends into —
        the colour key outside, a darkened base colour along the edge."""
        dark, base = colors[0], colors[1]
        W, H = L["W"], L["H"]
        mask = _aa_mask((W, H), H // 2).point(lambda v: 0 if v <= 10 else v)
        edge = Image.new("RGB", (W, H), _mix(base, (0, 0, 0), 0.55 if dark else 0.35))
        edge.paste(KEY_RGB, (0, 0, W, H), mask.point(lambda v: 255 if v == 0 else 0))
        return mask, edge

    def _mini_build_lyric(self, L, colors, text):
        acc = colors[4]
        tw = max(1, L["tx1"] - L["tx"])
        tr = self._mini_text()
        im = Image.new("RGBA", (tw, L["lyric_h"]), (0, 0, 0, 0))
        t = tr.ellipsize(text, "bold", L["lyric_px"], tw)
        tr.draw(ImageDraw.Draw(im), (0, 0), t, "bold", L["lyric_px"], acc + (255,))
        return im

    def _mini_build_controls(self, L, colors, playing):
        fg, acc = colors[3], colors[4]
        r, br = L["play_r"], L["btn_r"]
        light = (0.299 * acc[0] + 0.587 * acc[1] + 0.114 * acc[2]) > 150
        disc = _glyph("disc", 2 * r, acc)
        disc.alpha_composite(_glyph("pause" if playing else "play", 2 * r,
                                    (18, 20, 26) if light else (255, 255, 255)))
        return {"toggle": disc,
                "prev": _glyph("prev", 2 * br, fg),
                "next": _glyph("next", 2 * br, fg),
                "ring": _with_alpha(_glyph("disc", 2 * br + 6, fg), 0.16),
                "play_ring": _with_alpha(_glyph("disc", 2 * r + 6, fg), 0.18)}

    def _mini_button_centres(self, L, hp):
        c0, c1 = L["play_c"]
        cy = L["H"] // 2
        return {"toggle": (c0 + (c1 - c0) * hp, cy),
                "prev": (L["prev_x"], cy), "next": (L["next_x"], cy)}

    def _mini_hit(self, x, y):
        """Which control, if any, is under window point (x, y)."""
        L = self._mini_layout()
        hp = self._mini_hover.value(time.monotonic())
        for name, (cx, cy) in self._mini_button_centres(L, hp).items():
            if name != "toggle" and hp < 0.5:
                continue
            r = (L["play_r"] if name == "toggle" else L["btn_r"]) + 3
            if (x - cx) ** 2 + (y - cy) ** 2 <= r * r:
                return name
        return None

    def _mini_render(self, now=None):
        if getattr(self, "_mini", None) is None:
            return
        now = time.monotonic() if now is None else now
        key = self._mini_content_key(now)
        if key == self._mini_shown:
            return
        self._mini_shown = key
        if not _PIL:
            return self._mini_render_plain()
        L = self._mini_layout()
        W, H = L["W"], L["H"]
        colors = key[5]
        st = M.state
        title, artist = getattr(st, "title", ""), getattr(st, "artist", "")
        cover = getattr(self, "_hero_src", None)
        playing = bool(getattr(st, "is_playing", False))
        ck = (W, H, colors)
        base = self._mini_part(("base", ck, title, artist, id(cover)),
                               lambda: self._mini_build_base(L, colors, title, artist, cover))
        mask, edge = self._mini_part(("edge", ck), lambda: self._mini_build_edge(L, colors))
        ctl = self._mini_part(("ctl", ck, playing), lambda: self._mini_build_controls(L, colors, playing))

        img = base.copy()
        # Lyric, with the line-change slide + crossfade, clipped to its band.
        area = Image.new("RGBA", (max(1, L["tx1"] - L["tx"]), L["lyric_h"]), (0, 0, 0, 0))
        cur = self._mini_lyric_cur or "—"
        new = self._mini_part(("ly_new", ck, cur), lambda: self._mini_build_lyric(L, colors, cur))
        anim = self._mini_ly_anim
        p = (now - anim[1]) / LYRIC_ANIM_S if anim else 1.0
        if anim and p < 1:
            e = ease_out(p)
            old = self._mini_part(("ly_old", ck, anim[0]),
                                  lambda: self._mini_build_lyric(L, colors, anim[0]))
            _blit(area, old, 0, -L["slide"] * e, 1 - e)
            _blit(area, new, 0, L["slide"] * (1 - e), e)
        else:
            self._mini_ly_anim = None
            _blit(area, new, 0, 0)
        img.alpha_composite(area, (L["tx"], L["lyric_y"]))

        # Hovered: the text's right end fades out under prev/next.
        hp = self._mini_hover.value(now)
        if hp > 0.003:
            fx0, fx1 = L["fade_x0"], L["fade_x1"]
            vmask = Image.new("L", (W - fx0, H), 255)
            vmask.paste(Image.frombytes("L", (256, 1), bytes(range(256))).resize((max(1, fx1 - fx0), H)), (0, 0))
            veil = base.crop((fx0, 0, W, H))
            veil.putalpha(vmask.point(lambda v: int(v * hp)))
            img.alpha_composite(veil, (fx0, 0))
        centres = self._mini_button_centres(L, hp)
        for name in ("prev", "next", "toggle"):
            a = 1.0 if name == "toggle" else hp * 0.92
            if a <= 0.003:
                continue
            cx, cy = centres[name]
            if self._mini_hot == name:
                rg = ctl["play_ring" if name == "toggle" else "ring"]
                _blit(img, rg, cx - rg.width / 2, cy - rg.height / 2, min(1.0, a + 0.08))
            g = ctl[name]
            _blit(img, g, cx - g.width / 2, cy - g.height / 2, a)

        frame = Image.composite(img.convert("RGB"), edge, mask)
        self._mini_frame = frame
        try:
            ph = self._mini_photo
            if ph is not None and (ph.width(), ph.height()) == frame.size:
                ph.paste(frame)
            else:
                self._mini_photo = ImageTk.PhotoImage(frame, master=self._mini)
                self._mini_cv.itemconfigure(self._mini_item, image=self._mini_photo)
        except tk.TclError:
            pass

    def _mini_render_plain(self):
        """No Pillow: a flat strip with Tk text, so mini mode still works."""
        cv, st, L = self._mini_cv, M.state, self._mini_layout()
        cv.delete("plain")
        cv.configure(bg=M.BG2)
        cv.create_text(L["pad"] * 2, L["title_y"], anchor="nw", fill=M.MUTED, tags="plain",
                       text=f"{st.title} — {st.artist}" if st.title else "Statusify")
        cv.create_text(L["pad"] * 2, L["lyric_y"], anchor="nw", fill=M.ACCENT, tags="plain",
                       text=self._mini_lyric_cur or "—", font=self._f(M.FS_TITLE, True))

    # ── Animation driver ─────────────────────────────────────────
    def _mini_kick(self):
        """Make sure the animation loop is running (it stops by itself)."""
        if getattr(self, "_mini", None) is not None and "mini_anim" not in self._timers:
            self._schedule("mini_anim", 16, self._mini_animate)

    def _mini_animate(self):
        self._timers.pop("mini_anim", None)
        m = getattr(self, "_mini", None)
        if m is None:
            return
        now = time.monotonic()
        # Fade (-alpha); the tween holds its value through the idle delay.
        a = self._mini_fade.alpha(now)
        if abs(a - self._mini_alpha) > 0.004:
            try:
                m.attributes("-alpha", a)
            except tk.TclError:
                pass
            self._mini_alpha = a
        busy = self._mini_fade.animating(now)
        # Magnetic settle after a drag.
        sn = self._mini_snap
        if sn is not None and self._mini_drag is None:
            x0, y0, x1, y1, t0 = sn
            p = (now - t0) / 0.2
            e = ease_out(p)
            try:
                m.geometry(f"+{int(round(x0 + (x1 - x0) * e))}+{int(round(y0 + (y1 - y0) * e))}")
            except tk.TclError:
                pass
            if p >= 1:
                self._mini_snap = None
                self._mini_save_pos()
            else:
                busy = True
        busy = busy or not self._mini_hover.done(now) or self._mini_ly_anim is not None
        self._mini_render(now)
        if busy:
            self._schedule("mini_anim", 16, self._mini_animate)

    # ── Pointer ──────────────────────────────────────────────────
    def _mini_enter(self, _e=None):
        now = time.monotonic()
        self._mini_fade.enter(now)
        self._mini_hover.to(1.0, now, 0.16)
        self._mini_kick()

    def _mini_leave(self, e=None):
        m = getattr(self, "_mini", None)
        if m is None or self._mini_drag is not None:
            return
        if e is not None:
            try:   # a spurious Leave while still over the pill: ignore
                px, py = m.winfo_pointerxy()
                x, y = px - m.winfo_rootx(), py - m.winfo_rooty()
                fr = self._mini_frame
                if fr is not None and 0 <= x < fr.width and 0 <= y < fr.height \
                        and fr.getpixel((x, y)) != KEY_RGB:
                    return
            except (tk.TclError, IndexError):
                pass
        now = time.monotonic()
        self._mini_fade.leave(now)
        self._mini_hover.to(0.0, now, 0.2, delay=0.25)
        self._mini_hot = None
        self._mini_kick()

    def _mini_motion(self, e):
        hot = self._mini_hit(e.x, e.y)
        if hot != self._mini_hot:
            self._mini_hot = hot
            try:
                self._mini_cv.configure(cursor="hand2" if hot else "")
            except tk.TclError:
                pass
            self._mini_render()

    def _mini_press(self, e):
        m = self._mini
        self._mini_snap = None
        self._mini_drag = {"x": e.x_root, "y": e.y_root, "wx": m.winfo_x(), "wy": m.winfo_y(),
                           "moved": False, "hit": self._mini_hit(e.x, e.y)}

    def _mini_motion_drag(self, e):
        d = self._mini_drag
        if d is None:
            return
        dx, dy = e.x_root - d["x"], e.y_root - d["y"]
        if not d["moved"] and dx * dx + dy * dy < 16:
            return   # a click with a shaky hand, not a drag
        d["moved"] = True
        try:
            self._mini.geometry(f"+{d['wx'] + dx}+{d['wy'] + dy}")
        except tk.TclError:
            pass

    def _mini_release(self, e):
        d, self._mini_drag = self._mini_drag, None
        m = getattr(self, "_mini", None)
        if d is None or m is None:
            return
        if not d["moved"]:
            if d["hit"] and d["hit"] == self._mini_hit(e.x, e.y):
                self._mini_command(d["hit"])
            return
        self._mini_settle()
        if not (0 <= e.x < m.winfo_width() and 0 <= e.y < m.winfo_height()):
            self._mini_leave()

    def _mini_settle(self):
        """Glide to the magnetic edge/corner of the monitor it's on."""
        m = self._mini
        try:
            x, y, w, h = m.winfo_x(), m.winfo_y(), m.winfo_width(), m.winfo_height()
            area = (monitor_work_area(x + w // 2, y + h // 2)
                    or (0, 0, m.winfo_screenwidth(), m.winfo_screenheight()))
        except tk.TclError:
            return
        tx, ty = snap_position(x, y, w, h, area)
        if (tx, ty) == (x, y):
            self._mini_save_pos()
        elif getattr(M, "ANIMATIONS_ENABLED", True):
            self._mini_snap = (x, y, tx, ty, time.monotonic())
            self._mini_kick()
        else:
            m.geometry(f"+{tx}+{ty}")
            self._mini_save_pos()

    def _mini_save_pos(self):
        m = getattr(self, "_mini", None)
        if m is not None:
            try:
                M._cfg_set("window", "mini_geometry", m.geometry())
            except (tk.TclError, ValueError, OSError):
                pass

    def _mini_double(self, e):
        if e.x < self._mini_layout()["tx"]:     # double-click the cover
            self._tray_show()

    def _mini_command(self, which):
        self._tray_player({"toggle": "toggle", "prev": "prev", "next": "next"}[which])

    def _mini_context(self, e):
        menu = getattr(self, "_mini_menu", None)
        if menu is None:
            menu = self._mini_menu = tk.Menu(self._mini, tearoff=0)
            menu.add_command(label="Play/Pause", command=lambda: self._mini_command("toggle"))
            menu.add_command(label="Previous", command=lambda: self._mini_command("prev"))
            menu.add_command(label="Next", command=lambda: self._mini_command("next"))
            menu.add_separator()
            menu.add_command(label="Open Statusify", command=self._tray_show)
            menu.add_command(label="Close mini player", command=self._close_mini)
        try:
            menu.tk_popup(e.x_root, e.y_root)
        finally:
            try:
                menu.grab_release()
            except tk.TclError:
                pass

    # ── Shared tick: lyric follow + tray tooltip ─────────────────
    def _mini_tick_start(self):
        if "mini_tick" not in self._timers:
            self._schedule("mini_tick", 100, self._mini_tick)

    def _mini_tick(self):
        """Follows the playhead for the mini lyric (the RPC loop's line
        events are paced for Discord; the pill shouldn't be) and keeps the
        tray tooltip current. Slow when nothing is moving."""
        self._timers.pop("mini_tick", None)
        try:
            self._refresh_mini()
        except Exception as e:   # decoration must never break the chain
            M.log(f"Mini refresh failed: {e}")
        mini = getattr(self, "_mini", None) is not None
        if not mini and getattr(self, "_tray", None) is None:
            return
        playing = bool(getattr(M.state, "is_playing", False))
        self._schedule("mini_tick", (120 if playing else 400) if mini else 1000, self._mini_tick)

    # ── System tray (#11, #12) ────────────────────────────────────
    _WM_MBUTTONUP = 0x0208

    def _tray_start(self):
        """Create the tray icon, if pystray is available.

        A tray icon is the idiomatic home for a background presence app:
        closing to it keeps RPC running without a taskbar button."""
        self._tray = None
        if not M.TRAY_AVAILABLE:
            M.log("Tray unavailable (pystray/Pillow not installed) — window-only mode")
            return
        try:
            image = None
            try:
                image = M.Image.open(M._ensure_icon_path())
            except Exception:
                image = M.Image.new("RGB", (64, 64), M.ACCENT)

            def _do(fn):
                # pystray callbacks run on the tray's own thread; every Tk
                # call must be marshalled back to the main loop.
                return lambda *_: self._root.after(0, fn)

            # Middle-click → play/pause wraps pystray's Win32 notify handler.
            # Without that internal, Play/Pause becomes the left-click default.
            middle_ok = self._tray_can_hook_middle()
            P = M.pystray
            menu = P.Menu(
                P.MenuItem("Show Statusify", _do(self._tray_show), default=middle_ok),
                P.MenuItem("Hide to tray",   _do(self._hide_to_tray)),
                P.Menu.SEPARATOR,
                P.MenuItem("Play/Pause", _do(self._tray_play_pause), default=not middle_ok),
                P.MenuItem("Next",       _do(lambda: self._tray_player("next"))),
                P.MenuItem("Previous",   _do(lambda: self._tray_player("prev"))),
                P.Menu.SEPARATOR,
                P.MenuItem(lambda item: ("Hide mini player" if getattr(self, "_mini", None) is not None
                                         else "Show mini player"), _do(self._toggle_mini)),
                P.MenuItem("Always on top",      _do(self._toggle_topmost)),
                P.Menu.SEPARATOR,
                P.MenuItem("Toggle Discord RPC", _do(self._tray_toggle_rpc)),
                P.MenuItem("Reconnect RPC",      _do(self._reconnect_rpc)),
                P.Menu.SEPARATOR,
                P.MenuItem("Quit", _do(self._quit)),
            )
            self._tray = P.Icon("Statusify", image, "Statusify", menu)
            self._tray_tip = ("Statusify", 0.0)
            if middle_ok and not self._tray_hook_middle(self._tray):
                M.log("Tray middle-click unavailable — use the Play/Pause menu item")
            threading.Thread(target=self._tray.run, name="tray", daemon=True).start()
            M.log("Tray icon started")
            self._mini_tick_start()
        except Exception as e:
            self._tray = None
            M.log(f"Tray icon failed: {e}")

    @staticmethod
    def _tray_can_hook_middle():
        if os.name != "nt":
            return False
        try:
            from pystray._util import win32 as _pw
            return hasattr(M.pystray.Icon, "_on_notify") and hasattr(_pw, "WM_NOTIFY")
        except Exception:
            return False

    def _tray_hook_middle(self, icon):
        """Route WM_MBUTTONUP on the icon to play/pause. pystray (legacy
        notify version: lParam is the mouse message) looks its handler up
        in _message_handlers per message, so swapping the entry is enough."""
        try:
            from pystray._util import win32 as _pw
            handlers = icon._message_handlers
            orig = handlers[_pw.WM_NOTIFY]
        except Exception:
            return False
        root = self._root

        def on_notify(wparam, lparam):
            if lparam == self._WM_MBUTTONUP:
                try:
                    root.after(0, self._tray_play_pause)
                except Exception:
                    pass
                return 0
            return orig(wparam, lparam)

        handlers[_pw.WM_NOTIFY] = on_notify
        return True

    def _tray_player(self, action):
        if not M.player_command(action):
            M.log("Playback control needs Spotify connected (Spicetify bridge)")

    def _tray_play_pause(self):
        self._tray_player("toggle")

    def _tray_sync_menu(self):
        """Re-evaluate dynamic menu text (Show/Hide mini player)."""
        tray = getattr(self, "_tray", None)
        if tray is not None:
            try:
                tray.update_menu()
            except Exception:
                pass

    def _tray_update_tip(self, force=False):
        """Tooltip = 'Title — Artist' + current lyric, at most once a second."""
        tray = getattr(self, "_tray", None)
        if tray is None:
            return
        st = M.state
        lyric = self._mini_current_lyric() if getattr(st, "title", "") else ""
        text = tray_tooltip(getattr(st, "title", ""), getattr(st, "artist", ""), lyric,
                            bool(getattr(st, "is_playing", False)))
        last, t = getattr(self, "_tray_tip", ("", 0.0))
        now = time.monotonic()
        if text == last or (not force and now - t < 1.0):
            return
        self._tray_tip = (text, now)
        try:
            tray.title = text
        except Exception:
            pass

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
            self._root.lift()
            self._root.focus_force()
            self._hidden = False
        except Exception as e:
            M.log(f"Tray show failed: {e}")

    def _watch_show_request(self):
        """Restore the window when another launch asks us to.

        Double-clicking Statusify.exe while a copy is already running used to
        hit the single-instance guard and do nothing but show an 'already
        running' box. If that instance was hidden in the tray, the app was
        effectively unopenable — you had to hunt for the tray icon or kill the
        process. The second launch now drops a sentinel file and exits; this
        picks it up and brings the window back."""
        try:
            if os.path.exists(M._SHOW_FLAG):
                try:
                    os.remove(M._SHOW_FLAG)
                except OSError:
                    pass
                M.log("Second launch detected — restoring window")
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
            M.log(f"Hide to tray failed: {e}")
