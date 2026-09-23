"""Now Playing, phase B: player controls, Up Next, lyric search, sharing.

A second mixin for NowPlayingPage (statusify_ui_now_playing), in the same
style as statusify_np_fx: everything is drawn with PIL into the page's one
frame and hit-tested; the only real widget is the lyric search box, a Tk
Entry placed on the canvas while the search panel is open.

    transport extras   shuffle / repeat (with the "1" badge) around the
                       transport, a heart by the title, a speaker whose wheel
                       and click change and mute Spotify's volume
    Up Next            a glass card sliding in over the lyric sheet listing
                       state.queue; click a row to jump there
    lyric search       "Wrong lyrics? Search…": LRCLIB results in the same
                       card; the pick is pinned for that track (HistoryStore
                       lyric_pins) so the bridge can't replace it later
    menus              a small drawn context menu (right-click, "⋯")
    share image        a lyric line as a 1080×1350 PNG, saved or copied
    sub-lines          romanisation / translation under each lyric line
                       (statusify_translate), laid out with the line

Performance: nothing here costs anything while it's closed. The glass is a
downscaled blur of the background under the card, cached until the water
itself refreshes; the card's contents are a cached RGBA layer rebuilt only
when what it shows changes.
"""
import json
import threading
import time
import tkinter as tk

import statusify_fluid as fluid
from statusify_textrender import TextRenderer

try:
    from PIL import Image, ImageDraw, ImageFilter
except ImportError:          # main refuses to start without Pillow anyway
    Image = ImageDraw = ImageFilter = None

M = None   # the main module (bound by the page / main)

PANEL_S = 0.22            # Up Next / search card slide
MENU_S = 0.12             # context menu fade
TOAST_S = 1.5             # "Volume 65%" pill
TOAST_FADE_S = 0.35
VOL_STEP = 0.05
QUEUE_ROWS = 8
PIN_SOURCE = "LRCLIB · chosen"
SHARE_SIZE = (1080, 1350)

# Lyrics the bridge sent for a pinned track, kept so "Use Spotify's lyrics
# again" can bring them back without a refetch. {uri: (mode, synced, plain, src)}
_STASH = {}
_STASH_MAX = 20

ICONS = ("shuffle", "repeat", "heart", "heart_o", "vol0", "vol1", "vol2", "queue",
         "more", "moon", "close", "search")


def _ease_out(t):
    return 1.0 - (1.0 - t) ** 4


def _clamp(v, lo=0.0, hi=1.0):
    return lo if v < lo else hi if v > hi else v


# ── Pins (called from main's backend thread and from the page) ──

def _store():
    try:
        return M._store()
    except Exception:
        return None


def pinned_lyrics(uri):
    """(mode, synced, plain, source) the user pinned for `uri`, or None."""
    st = _store()
    if not st or not uri:
        return None
    try:
        return st.get_pin(uri)
    except Exception:
        return None


def is_pin_source(src):
    return src == PIN_SOURCE


def keep_pinned(uri, mode, synced, plain, src):
    """True when `uri` has pinned lyrics, so lyrics arriving from the bridge
    (or the prefetch cache) must not replace them. They're stashed so the
    user can switch back without waiting for a refetch."""
    if mode not in ("synced", "plain") or pinned_lyrics(uri) is None:
        return False
    _STASH[uri] = (mode, synced, plain, src)
    while len(_STASH) > _STASH_MAX:
        _STASH.pop(next(iter(_STASH)))
    try:
        M.log("Lyrics: keeping the lyrics you chose for this song")
    except Exception:
        pass
    return True


# ── LRCLIB search ────────────────────────────────────────────────

def lrclib_query(text, artist="", title=""):
    """LRCLIB results for the search box. The prefilled "artist title" asks
    by field (what the automatic fallback does); anything the user typed is a
    free-text search."""
    text = (text or "").strip()
    default = f"{artist} {title}".strip()
    if text and text == default and title:
        res = M._lrclib_search(artist, title)
        if res:
            return res
    if not text:
        return []
    import urllib.parse
    import urllib.request
    q = urllib.parse.urlencode({"q": text})
    req = urllib.request.Request(
        f"{M._LRCLIB_URL}?{q}",
        headers={"User-Agent": f"Statusify/{M._VERSION} (https://github.com/{M._GITHUB_REPO})"})
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read().decode("utf-8"))


def clean_results(results, duration_ms=0, limit=20):
    """Usable LRCLIB results, the ones matching the song's length first."""
    out = []
    for r in results or []:
        if not isinstance(r, dict) or r.get("instrumental"):
            continue
        synced = bool((r.get("syncedLyrics") or "").strip())
        plain = bool((r.get("plainLyrics") or "").strip())
        if not (synced or plain):
            continue
        try:
            dur = float(r.get("duration") or 0)
        except (TypeError, ValueError):
            dur = 0.0
        out.append({"track": str(r.get("trackName") or r.get("name") or ""),
                    "artist": str(r.get("artistName") or ""),
                    "album": str(r.get("albumName") or ""),
                    "duration": dur, "synced": synced, "raw": r})
    if duration_ms:
        near = lambda e: 0 if e["duration"] and abs(e["duration"] - duration_ms / 1000) <= 3 else 1
        out.sort(key=lambda e: (near(e), 0 if e["synced"] else 1))
    return out[:limit]


# ── Glyphs ───────────────────────────────────────────────────────

def _stroke(d, pts, w):
    d.line(pts, fill=255, width=int(w), joint="curve")
    for x, y in (pts[0], pts[-1]):
        d.ellipse((x - w / 2, y - w / 2, x + w / 2, y + w / 2), fill=255)


def _heart(d, n, cx, cy, s, fill):
    r = s * 0.27
    for ox in (-0.235, 0.235):
        d.ellipse((cx + ox * s - r, cy - 0.12 * s - r, cx + ox * s + r, cy - 0.12 * s + r), fill=fill)
    d.polygon([(cx - 0.495 * s, cy - 0.06 * s), (cx, cy + 0.44 * s), (cx + 0.495 * s, cy - 0.06 * s),
               (cx, cy - 0.10 * s)], fill=fill)


def icon_mask(kind, n):
    """An n×n "L" mask of one of the extra glyphs (the caller draws at 4x
    and reduces, as _np_icon does)."""
    m = Image.new("L", (n, n), 0)
    d = ImageDraw.Draw(m)
    w = max(4, n * 0.085)
    P = lambda *xy: [(x * n, y * n) for x, y in zip(xy[::2], xy[1::2])]
    if kind == "shuffle":
        _stroke(d, P(0.10, 0.30, 0.30, 0.30, 0.62, 0.70, 0.78, 0.70), w)
        _stroke(d, P(0.10, 0.70, 0.30, 0.70, 0.62, 0.30, 0.78, 0.30), w)
        for y in (0.30, 0.70):
            d.polygon(P(0.74, y - 0.13, 0.92, y, 0.74, y + 0.13), fill=255)
    elif kind == "repeat":
        box = (0.12 * n, 0.26 * n, 0.88 * n, 0.74 * n)
        d.rounded_rectangle(box, radius=0.20 * n, outline=255, width=int(w))
        d.rectangle((0.44 * n, 0.26 * n - w, 0.60 * n, 0.26 * n + w), fill=0)
        d.rectangle((0.40 * n, 0.74 * n - w, 0.56 * n, 0.74 * n + w), fill=0)
        d.polygon(P(0.50, 0.26 - 0.13, 0.64, 0.26, 0.50, 0.26 + 0.13), fill=255)
        d.polygon(P(0.50, 0.74 - 0.13, 0.36, 0.74, 0.50, 0.74 + 0.13), fill=255)
    elif kind in ("heart", "heart_o"):
        _heart(d, n, n * 0.5, n * 0.52, n * 0.92, 255)
        if kind == "heart_o":
            _heart(d, n, n * 0.5, n * 0.52 - w * 0.25, n * 0.92 - 2.6 * w, 0)
    elif kind.startswith("vol"):
        d.polygon(P(0.10, 0.38, 0.26, 0.38, 0.46, 0.18, 0.46, 0.82, 0.26, 0.62, 0.10, 0.62), fill=255)
        if kind == "vol0":
            _stroke(d, P(0.62, 0.38, 0.86, 0.62), w)
            _stroke(d, P(0.62, 0.62, 0.86, 0.38), w)
        else:
            radii = (0.17,) if kind == "vol1" else (0.17, 0.33)
            for r in radii:
                d.arc((0.46 * n - r * n, 0.5 * n - r * n, 0.46 * n + r * n, 0.5 * n + r * n),
                      -52, 52, fill=255, width=int(w))
    elif kind == "queue":
        for y, x2 in ((0.26, 0.86), (0.50, 0.86), (0.74, 0.56)):
            _stroke(d, P(0.14, y, x2, y), w)
        d.polygon(P(0.68, 0.62, 0.68, 0.86, 0.90, 0.74), fill=255)
    elif kind == "more":
        r = n * 0.085
        for x in (0.22, 0.5, 0.78):
            d.ellipse((x * n - r, 0.5 * n - r, x * n + r, 0.5 * n + r), fill=255)
    elif kind == "moon":
        d.ellipse((0.14 * n, 0.14 * n, 0.86 * n, 0.86 * n), fill=255)
        d.ellipse((0.36 * n, 0.02 * n, 1.02 * n, 0.68 * n), fill=0)
    elif kind == "close":
        _stroke(d, P(0.24, 0.24, 0.76, 0.76), w)
        _stroke(d, P(0.24, 0.76, 0.76, 0.24), w)
    elif kind == "search":
        d.ellipse((0.12 * n, 0.12 * n, 0.66 * n, 0.66 * n), outline=255, width=int(w))
        _stroke(d, P(0.60, 0.60, 0.86, 0.86), w)
    return m


# ── Share image ──────────────────────────────────────────────────

def _cover_fill(cover, size):
    """`cover` cropped to the aspect of `size` and resized to it."""
    W, H = size
    cw, ch = cover.size
    want = W / H
    if cw / ch > want:
        nw = int(ch * want)
        box = ((cw - nw) // 2, 0, (cw - nw) // 2 + nw, ch)
    else:
        nh = int(cw / want)
        box = (0, (ch - nh) // 2, cw, (ch - nh) // 2 + nh)
    return cover.convert("RGB").crop(box).resize(size, Image.BICUBIC)


def render_share_image(line, next_line="", title="", artist="", cover=None, palette=(),
                       accent=(30, 215, 96), family=None, size=SHARE_SIZE):
    """A lyric line as a picture: the song's colours blurred behind it, the
    line large, the next one quieter, the cover and title at the bottom and a
    small Statusify mark. Safe to call off the Tk thread (own renderers)."""
    W, H = size
    TR = TextRenderer(family)
    UI = TextRenderer()
    small = (max(8, W // 8), max(8, H // 8))
    bg = None
    if palette:
        ff = fluid.FluidField()
        ff.set_colors(fluid.normalise(list(palette), True, "#121218"), 0.0)
        bg = ff.render(small, 0.0, (18, 18, 24), 0, 0, motion_t=40.0, dither=False)
    if cover is not None:
        cv = _cover_fill(cover, small).filter(ImageFilter.GaussianBlur(3))
        bg = cv if bg is None else Image.blend(bg, cv, 0.45)
    if bg is None:
        bg = Image.new("RGB", small, (24, 24, 32))
    bg = bg.filter(ImageFilter.GaussianBlur(2)).resize((W, H), Image.BICUBIC)
    # Darken for legibility, more towards the bottom where the title sits.
    shade = Image.new("L", (1, H))
    shade.putdata([int(255 * (0.40 + 0.35 * max(0.0, (y / H - 0.45) / 0.55) ** 1.4)) for y in range(H)])
    bg.paste((8, 8, 12), (0, 0), shade.resize((W, H)))
    img = bg.convert("RGBA")
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)

    margin = int(W * 0.1)
    maxw = W - 2 * margin
    line = (line or "").strip() or "♪"
    px = int(W * 0.089)
    while True:
        rows = TR.wrap(line, "bold", px, maxw)
        if len(rows) <= 5 or px <= int(W * 0.044):
            break
        px -= 4
    rows = rows[:6]
    lh = int(TR.line_height("bold", px) * 1.04)
    npx = int(px * 0.52)
    nrows = TR.wrap(next_line.strip(), "semibold", npx, maxw)[:3] if next_line else []
    nlh = int(TR.line_height("semibold", npx) * 1.06)
    block = len(rows) * lh + (int(px * 0.6) + len(nrows) * nlh if nrows else 0)
    foot_h = int(H * 0.2)
    top = max(int(H * 0.1), (H - foot_h - block) // 2)
    d.rounded_rectangle((margin, top - int(px * 0.62), margin + int(W * 0.066), top - int(px * 0.62) + 8),
                        radius=4, fill=tuple(accent[:3]) + (255,))
    y = top
    for r in rows:
        TR.draw(d, (margin, y), r, "bold", px, (255, 255, 255, 255))
        y += lh
    if nrows:
        y += int(px * 0.6)
        for r in nrows:
            TR.draw(d, (margin, y), r, "semibold", npx, (255, 255, 255, 140))
            y += nlh

    # Footer: cover, title, artist; the mark on the right.
    A = int(W * 0.111)
    fy = H - margin - A
    tx = margin
    if cover is not None:
        thumb = cover.convert("RGB").resize((A, A), Image.LANCZOS)
        mk = Image.new("L", (A * 4, A * 4), 0)
        ImageDraw.Draw(mk).rounded_rectangle((0, 0, A * 4 - 1, A * 4 - 1), radius=int(A * 0.14) * 4, fill=255)
        img.paste(thumb, (margin, fy), mk.resize((A, A), Image.LANCZOS))
        tx = margin + A + int(W * 0.026)
    mpx = int(W * 0.024)
    mark = "Statusify"
    mw = UI.measure(mark, "semibold", mpx)
    tpx, apx = int(W * 0.035), int(W * 0.03)
    tmax = W - margin - tx - mw - int(W * 0.04)
    t_s = UI.ellipsize(title or "", "semibold", tpx, tmax)
    a_s = UI.ellipsize(artist or "", "regular", apx, tmax)
    lt, la = UI.line_height("semibold", tpx), UI.line_height("regular", apx)
    ty = fy + (A - lt - la) // 2 if cover is not None else fy + A - lt - la
    UI.draw(d, (tx, ty), t_s, "semibold", tpx, (255, 255, 255, 245))
    UI.draw(d, (tx, ty + lt), a_s, "regular", apx, (255, 255, 255, 170))
    ml = UI.line_height("semibold", mpx)
    mx = W - margin - mw
    my = ty + lt + la - ml
    dot = int(mpx * 0.45)
    d.ellipse((mx - dot - int(mpx * 0.45), my + (ml - dot) // 2, mx - int(mpx * 0.45), my + (ml + dot) // 2),
              fill=tuple(accent[:3]) + (200,))
    UI.draw(d, (mx, my), mark, "semibold", mpx, (255, 255, 255, 130))
    img.alpha_composite(layer)
    return img.convert("RGB")


# ── The mixin ────────────────────────────────────────────────────

class NpExtrasMixin:

    def _np_ex_init(self):
        self._np_panel = None              # None | "queue" | "search"
        self._np_panel_t0 = -10.0
        self._np_panel_closing = False
        self._np_panel_layer = None        # (key, RGBA layer, hits)
        self._np_panel_scroll = 0
        self._np_panel_box_last = None
        self._np_menu = None               # {"items", "at", "t0"}
        self._np_menu_layer = None
        self._np_toast = None              # (text, frac or None, t0)
        self._np_toast_spr = None
        self._np_q_art = {}                # (url, A) -> sprite | False while loading
        self._np_q_gen = 0
        self._np_search = {"q": "", "status": "", "results": [], "gen": 0, "busy": False}
        self._np_search_entry = None
        self._np_search_win = None
        self._np_search_shown = None
        self._np_vol_prev = 0.5
        self._np_ex_hits = []
        self._np_glass_masks = {}

    # ── Shared bits ──────────────────────────────────────────────
    @staticmethod
    def _np_to_tk(fn):
        """Run `fn` on the Tk thread (from any thread): main._poll drains
        ("np_call", fn) from the event queue."""
        M.event_queue.put(("np_call", fn))

    def _np_live(self):
        return bool(self.dot_sp.on)

    def _np_is_pinned(self):
        return pinned_lyrics(getattr(M.state, "track_uri", "")) is not None

    def _np_overlay_on(self):
        try:
            return bool(self._ov_prefs()["enabled"])
        except Exception:
            return False

    def _np_sleep_text(self):
        try:
            return self._sleep_timer_label() or ""
        except Exception:
            return ""

    @staticmethod
    def _np_cheap_key(k):
        """Hover keys that change only the frame (or a panel layer), not
        the cached header and footer."""
        return k is None or k == "seek" or str(k).startswith(("line:", "q:", "res:", "menu:", "panel",
                                                              "unpin"))

    def _np_ctl_hover(self):
        h = self._np_hover
        return None if self._np_cheap_key(h) else h

    def _np_on_event(self, kind):
        """Tk events from the bridge / translator (main._poll)."""
        if kind == "queue":
            self._np_panel_layer = None
        elif kind == "player_state":
            self._np_invalidate()
        elif kind == "translation":
            self._ly_masks.clear()
        self._np_want_frame = True

    def _np_ex_sig(self):
        t = self._np_toast
        return (self._np_panel, self._np_panel_closing, id(self._np_panel_layer),
                id(self._np_menu), t[0] if t else None, self._np_sleep_text())

    # ── Transport extras ─────────────────────────────────────────
    def _np_player(self, action):
        if not M.player_command(action):
            self._set_error("Spotify isn't connected, so it can't be controlled from here")
            return False
        return True

    def _np_volume_step(self, n):
        v = float(getattr(M.state, "volume", 1.0) or 0.0)
        v = _clamp(round((v + n * VOL_STEP) * 20) / 20)
        if not M.set_volume(v):
            self._np_toast_show("Spotify isn't connected")
            return False
        if v > 0:
            self._np_vol_prev = v
        self._np_toast_show(f"Volume {int(round(v * 100))}%" if v > 0 else "Muted", v)
        self._np_invalidate(header=False)
        return True

    def _np_toggle_mute(self):
        v = float(getattr(M.state, "volume", 1.0) or 0.0)
        if v > 0.001:
            self._np_vol_prev = v
            target = 0.0
        else:
            target = self._np_vol_prev if self._np_vol_prev > 0.001 else 0.5
        if not M.set_volume(target):
            self._np_toast_show("Spotify isn't connected")
            return
        self._np_toast_show("Muted" if target == 0 else f"Volume {int(round(target * 100))}%", target)
        self._np_invalidate(header=False)

    def _np_key_extra(self, e, what):
        """Ctrl+Up/Down, Ctrl+S, Ctrl+R, Ctrl+L (not while typing)."""
        if self._typing(e):
            return None
        if what == "vol_up":
            self._np_volume_step(1)
        elif what == "vol_down":
            self._np_volume_step(-1)
        elif what in ("shuffle", "repeat", "like"):
            if self._np_player(what):
                self._np_toast_show(self._np_state_text(what))
        self._np_invalidate()
        self._np_render()
        return "break"

    @staticmethod
    def _np_state_text(what):
        st = M.state
        if what == "shuffle":
            return "Shuffle on" if st.shuffle else "Shuffle off"
        if what == "repeat":
            return ("Repeat off", "Repeat all", "Repeat this song")[int(st.repeat) % 3]
        return "Added to Liked Songs" if st.liked else "Removed from Liked Songs"

    # ── Footer pieces ────────────────────────────────────────────
    def _np_footer_extras(self, layer, d, W, cx, cy, big, gap, hov, pressed, live, hits):
        """Shuffle and repeat around the transport; Up Next on the left and
        the speaker on the right of the same row."""
        S, TR = self._S, self._np_text
        st = M.state
        fg, acc = self._np_fg(), self._np_accent()
        off = big // 2 + gap + S(46)
        for key, x, on in (("shuffle", cx - off, bool(st.shuffle)), ("repeat", cx + off, int(st.repeat) > 0),
                           ("queue", S(44), self._np_panel == "queue" and not self._np_panel_closing),
                           ("vol", W - S(44), False)):
            box = (x - S(18), cy - S(18), x + S(18), cy + S(18))
            if hov == key:
                self._np_pill(layer, box, 0.12)
            sz = S(18) - (S(2) if pressed == key else 0)
            if key == "vol":
                v = float(getattr(st, "volume", 1.0) or 0.0)
                glyph = "vol0" if v <= 0.001 else "vol1" if v < 0.5 else "vol2"
                if hov == "vol":
                    layer.alpha_composite(self._np_vol_ring(v), (x - S(18), cy - S(18)))
            else:
                glyph = key
            a = (1.0 if on or hov == key else 0.62) * (1.0 if live or key == "queue" else 0.4)
            layer.alpha_composite(self._np_icon(glyph, sz, (acc if on else fg) + (int(255 * a),)),
                                  (x - sz // 2, cy - sz // 2))
            if on and key in ("shuffle", "repeat"):
                r = max(2, S(2))
                d.ellipse((x - r, cy + S(13) - r, x + r, cy + S(13) + r), fill=acc + (255,))
            if key == "repeat" and int(st.repeat) == 2:
                br = S(6)
                bx, by = x + S(8), cy - S(9)
                d.ellipse((bx - br, by - br, bx + br, by + br), fill=acc + (255,))
                bpx = self._px(6.5)
                ink = (0, 0, 0, 230) if M._DARK_MODE else (255, 255, 255, 240)
                tw = TR.measure("1", "bold", bpx)
                TR.draw(d, (bx - tw / 2, by - TR.line_height("bold", bpx) / 2), "1", "bold", bpx, ink)
            hits.append(box + (key,))

    def _np_vol_ring(self, v):
        """A thin 270° ring around the speaker showing the volume (hover)."""
        S = self._S
        n = S(36)
        key = ("volring", n, int(round(v * 50)), M._DARK_MODE, M.ACCENT)
        spr = self._np_prog_masks.get(key)
        if spr is not None:
            return spr
        sc = 4
        m_bg = Image.new("L", (n * sc, n * sc), 0)
        m_fg = Image.new("L", (n * sc, n * sc), 0)
        w = max(2, S(2)) * sc
        box = (w, w, n * sc - w, n * sc - w)
        ImageDraw.Draw(m_bg).arc(box, 135, 405, fill=255, width=w)
        if v > 0.001:
            ImageDraw.Draw(m_fg).arc(box, 135, 135 + 270 * _clamp(v), fill=255, width=w)
        m_bg, m_fg = m_bg.resize((n, n), Image.LANCZOS), m_fg.resize((n, n), Image.LANCZOS)
        spr = Image.new("RGBA", (n, n), (0, 0, 0, 0))
        spr.paste(self._np_fg() + (255,), (0, 0), m_bg.point([int(x * 0.16) for x in range(256)]))
        spr.paste(self._np_accent() + (255,), (0, 0), m_fg)
        self._np_prog_masks[key] = spr
        return spr

    def _np_fit_actions(self, acts, avail, spx):
        """(shown, overflow) of the footer's quiet actions for `avail` px."""
        TR, S = self._np_text, self._S
        width = lambda lab: int(TR.measure(lab, "semibold", spx)) + S(22) + S(4)
        if sum(width(a[1]) for a in acts) <= avail:
            return list(acts), []
        avail -= S(34) + S(4)
        keep, used = set(), 0
        for k in ("mini", "overlay", "top", "copy"):
            a = next(x for x in acts if x[0] == k)
            if used + width(a[1]) > avail:
                break
            keep.add(k)
            used += width(a[1])
        order = {"mini": 0, "overlay": 1, "top": 2, "copy": 3}
        return ([a for a in acts if a[0] in keep],
                sorted((a for a in acts if a[0] not in keep), key=lambda a: order[a[0]]))

    # ── Toast ────────────────────────────────────────────────────
    def _np_toast_show(self, text, frac=None):
        self._np_toast = (text, frac, time.monotonic())
        self._np_toast_spr = None
        self._np_want_frame = True
        self._np_render()

    def _np_draw_toast(self, frame, bottom, now):
        t = self._np_toast
        if not t:
            return False
        age = now - t[2]
        if age >= TOAST_S:
            self._np_toast = self._np_toast_spr = None
            return False
        a = min(_clamp(age / 0.12), _clamp((TOAST_S - age) / TOAST_FADE_S)) if M.ANIMATIONS_ENABLED else 1.0
        spr = self._np_toast_spr
        if spr is None:
            TR, S = self._np_text, self._S
            px = self._px(9.5)
            tw = int(TR.measure(t[0], "semibold", px))
            lh = TR.line_height("semibold", px)
            bar = t[1] is not None
            w = max(tw + S(28), S(120) if bar else 0)
            h = S(30) + (S(8) if bar else 0)
            spr = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            m = self._np_rounded_mask((w, h), S(12))
            fg = self._np_fg()
            spr.paste(fg + (230,), (0, 0), m)
            d = ImageDraw.Draw(spr)
            ink = (0, 0, 0) if M._DARK_MODE else (255, 255, 255)
            ty = (S(30) - lh) // 2 + (S(1) if bar else 0)
            TR.draw(d, ((w - tw) // 2, ty), t[0], "semibold", px, ink + (255,))
            if bar:
                bx1, bx2, by = S(14), w - S(14), S(30) + S(1)
                bh = max(2, S(3))
                d.rounded_rectangle((bx1, by, bx2, by + bh), radius=bh / 2, fill=ink + (60,))
                fx2 = bx1 + int((bx2 - bx1) * _clamp(t[1]))
                if fx2 > bx1 + 1:
                    d.rounded_rectangle((bx1, by, fx2, by + bh), radius=bh / 2, fill=ink + (230,))
            self._np_toast_spr = spr
        W = frame.size[0]
        x = (W - spr.size[0]) // 2
        y = bottom - spr.size[1] - self._S(10) + int((1 - _ease_out(a)) * self._S(6))
        al = spr.getchannel("A")
        if a < 0.995:
            al = al.point([int(v * a) for v in range(256)])
        frame.paste(spr.convert("RGB"), (x, y), al)
        return True

    # ── Glass ────────────────────────────────────────────────────
    def _np_glass(self, frame, box, alpha, radius):
        """Frosted card: the water under `box` blurred (at 1/6 size) and
        tinted towards the theme's background, with a hairline edge.

        It's made from the cached background (not the lyrics), so while the
        card sits still it's rebuilt only when the water is, ~12 times a
        second, and the lyrics gliding underneath cost it nothing."""
        W, H = frame.size
        x1, y1, x2, y2 = [int(v) for v in box]
        w, h = x2 - x1, y2 - y1
        vx1, vy1, vx2, vy2 = max(0, x1), max(0, y1), min(W, x2), min(H, y2)
        if vx2 - vx1 < 4 or vy2 - vy1 < 4 or alpha <= 0.01:
            return
        fc = self._np_fluid_cache
        src = fc[1] if fc and fc[1].size == frame.size else frame
        ckey = (id(src), fc[2] if fc else None, (x1, y1, x2, y2), round(alpha * 64), radius,
                M.BG, M._DARK_MODE)
        hit = self._np_glass_masks.get(("made", radius))
        if hit is not None and hit[0] == ckey:
            _, glass, m, e = hit
            frame.paste(glass, (vx1, vy1), m)
            frame.paste(self._np_fg(), (vx1, vy1), e)
            return
        region = src.crop((vx1, vy1, vx2, vy2))
        rw, rh = region.size
        k = 6
        small = region.resize((max(2, rw // k), max(2, rh // k)), Image.BILINEAR)
        glass = small.filter(ImageFilter.GaussianBlur(2.5)).resize((rw, rh), Image.BICUBIC)
        tint = self._hex(M.BG)
        glass = Image.blend(glass, Image.new("RGB", (rw, rh), tint), 0.58 if M._DARK_MODE else 0.62)
        key = ("glass", w, h, radius)
        mk = self._np_glass_masks.get(key)
        if mk is None:
            base = self._np_rounded_mask((w, h), radius)
            inner = self._np_rounded_mask((max(2, w - 2), max(2, h - 2)), max(1, radius - 1))
            edge = base.copy()
            hole = Image.new("L", (w, h), 0)
            hole.paste(inner, (1, 1))
            from PIL import ImageChops
            edge = ImageChops.subtract(edge, hole)
            if len(self._np_glass_masks) > 24:
                self._np_glass_masks.clear()
            mk = self._np_glass_masks[key] = (base, edge)
        base, edge = mk
        crop = (vx1 - x1, vy1 - y1, vx2 - x1, vy2 - y1)
        m = base.crop(crop)
        e = edge.crop(crop)
        if alpha < 0.995:
            m = m.point([int(v * alpha) for v in range(256)])
        e = e.point([int(v * 0.14 * alpha) for v in range(256)])
        self._np_glass_masks[("made", radius)] = (ckey, glass, m, e)
        frame.paste(glass, (vx1, vy1), m)
        frame.paste(self._np_fg(), (vx1, vy1), e)

    def _np_paste_layer(self, frame, layer, xy, alpha):
        if alpha >= 0.995:
            # Settled: the RGB/alpha split is kept with the layer.
            split = self._np_glass_masks.get(("split", id(layer)))
            if split is None or split[0] is not layer:
                split = (layer, layer.convert("RGB"), layer.getchannel("A"))
                if len(self._np_glass_masks) > 24:
                    self._np_glass_masks.clear()
                self._np_glass_masks[("split", id(layer))] = split
            frame.paste(split[1], (int(xy[0]), int(xy[1])), split[2])
            return
        al = layer.getchannel("A").point([int(v * alpha) for v in range(256)])
        frame.paste(layer.convert("RGB"), (int(xy[0]), int(xy[1])), al)

    # ── Panels (Up Next, lyric search) ───────────────────────────
    def _np_panel_open(self, kind):
        if self._np_panel == kind and not self._np_panel_closing:
            return
        self._np_menu = None
        self._np_panel = kind
        self._np_panel_closing = False
        self._np_panel_t0 = time.monotonic()
        self._np_panel_layer = None
        self._np_panel_scroll = 0
        if kind == "search":
            st = M.state
            q = f"{getattr(st, 'artist', '')} {getattr(st, 'title', '')}".strip()
            self._np_search_ensure_entry()
            try:
                self._np_search_entry.delete(0, "end")
                self._np_search_entry.insert(0, q)
            except tk.TclError:
                pass
            self._np_search_run(q)
        self._np_invalidate()
        self._np_render()

    def _np_panel_close(self):
        if self._np_panel is None or self._np_panel_closing:
            return False
        self._np_panel_closing = True
        self._np_panel_t0 = time.monotonic()
        self._np_search_place(None)
        try:
            self.np_cv.focus_set()
        except tk.TclError:
            pass
        self._np_invalidate()
        self._np_render()
        return True

    def _np_close_overlays(self):
        """Esc: close a menu, else a panel. True if something closed."""
        if self._np_menu is not None:
            self._np_menu = None
            self._np_want_frame = True
            self._np_render()
            return True
        return self._np_panel_close()

    def _np_panel_geom(self, W, top, bottom):
        S = self._S
        margin = S(12)
        pw = min(W - 2 * margin, S(344))
        return W - margin - pw, top + S(4), W - margin, bottom - S(4)

    def _np_draw_panel(self, frame, top, bottom, now):
        kind = self._np_panel
        if kind is None:
            self._np_search_place(None)
            return False, []
        t = _clamp((now - self._np_panel_t0) / PANEL_S) if M.ANIMATIONS_ENABLED else 1.0
        if self._np_panel_closing and t >= 1.0:
            self._np_panel = None
            self._np_panel_closing = False
            self._np_panel_layer = None
            self._np_invalidate()
            return True, []
        e = _ease_out(1.0 - t if self._np_panel_closing else t)
        x1, y1, x2, y2 = self._np_panel_geom(frame.size[0], top, bottom)
        pw, ph = x2 - x1, y2 - y1
        if pw < 120 or ph < 120:
            return False, []
        dx = int(round((1.0 - e) * (pw * 0.35 + self._S(12))))
        self._np_glass(frame, (x1 + dx, y1, x2 + dx, y2), e, self._S(16))
        layer, hits = self._np_panel_content(kind, pw, ph)
        self._np_paste_layer(frame, layer, (x1 + dx, y1), e)
        settled = t >= 1.0 and not self._np_panel_closing
        self._np_panel_box_last = (x1, y1, x2, y2)
        if kind == "search":
            self._np_search_place((x1, y1, pw) if settled else None)
        out = []
        if settled:
            out = [(x1 + a, y1 + b, x1 + c, y1 + d_, k) for a, b, c, d_, k in hits]
        out.append((x1 + dx, y1, x2 + dx, y2, "panel"))
        return not settled, out

    def _np_panel_content(self, kind, pw, ph):
        st = M.state
        if kind == "queue":
            q = getattr(st, "queue", None) or []
            key = ("queue", pw, ph, id(q), len(q), self._np_q_gen, self._np_hover, self._np_panel_scroll,
                   M._DARK_MODE, M.ACCENT)
        else:
            s = self._np_search
            key = ("search", pw, ph, s["gen"], s["status"], id(s["results"]), self._np_hover,
                   self._np_panel_scroll, self._np_is_pinned(), M._DARK_MODE, M.ACCENT, M.BG3)
        cached = self._np_panel_layer
        if cached is not None and cached[0] == key:
            return cached[1], cached[2]
        build = self._np_build_queue if kind == "queue" else self._np_build_search
        layer, hits = build(pw, ph)
        self._np_panel_layer = (key, layer, hits)
        return layer, hits

    def _np_panel_head(self, d, layer, pw, title, hits):
        TR, S = self._np_text, self._S
        tpx = self._px(11.5)
        TR.draw(d, (S(18), S(16)), title, "semibold", tpx, self._np_rgba(0.96))
        b = (pw - S(14) - S(28), S(12), pw - S(14), S(40))
        if self._np_hover == "panel_close":
            self._np_pill(layer, b, 0.14)
        gs = S(12)
        layer.alpha_composite(self._np_icon("close", gs, self._np_rgba(0.9 if self._np_hover == "panel_close"
                                                                     else 0.6)),
                              (b[0] + (S(28) - gs) // 2, b[1] + (S(28) - gs) // 2))
        hits.append(b + ("panel_close",))

    def _np_small_cover(self, url, A):
        """Rounded cover sprite for a queue row, loaded off the Tk thread."""
        key = (url, A)
        spr = self._np_q_art.get(key)
        if spr or not url:
            return spr or None
        if spr is None:
            self._np_q_art[key] = False
            radius = self._S(6)

            def work():
                return M._fetch_art(url, 96)

            def done(f):
                try:
                    img = f.result()
                except Exception:
                    img = None
                def apply():
                    if img is None:
                        return
                    s = img.convert("RGB").resize((A, A), Image.LANCZOS).convert("RGBA")
                    s.putalpha(self._np_rounded_mask((A, A), radius))
                    if len(self._np_q_art) > 64:
                        self._np_q_art.clear()
                    self._np_q_art[key] = s
                    self._np_q_gen += 1
                    self._np_want_frame = True
                try:
                    self._np_to_tk(apply)
                except Exception:
                    pass
            try:
                M.image_executor.submit(work).add_done_callback(done)
            except Exception:
                pass
        return None

    def _np_build_queue(self, pw, ph):
        TR, S = self._np_text, self._S
        layer = Image.new("RGBA", (pw, ph), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        hits = []
        self._np_panel_head(d, layer, pw, "Up next", hits)
        q = (getattr(M.state, "queue", None) or [])[:QUEUE_ROWS]
        y0, rh, A = S(52), S(54), S(40)
        if not q:
            px = self._px(10)
            msg = "Nothing queued"
            TR.draw(d, ((pw - TR.measure(msg, "semibold", px)) / 2, ph * 0.42), msg, "semibold", px,
                    self._np_rgba(0.62))
            sub = "Songs you queue in Spotify show up here"
            spx = self._px(8.5)
            sub = TR.ellipsize(sub, "regular", spx, pw - S(32))
            TR.draw(d, ((pw - TR.measure(sub, "regular", spx)) / 2, ph * 0.42 + S(24)), sub, "regular", spx,
                    self._np_rgba(0.42))
            return layer, hits
        fits = max(1, (ph - y0 - S(8)) // rh)
        tpx, apx = self._px(10), self._px(8.5)
        lt, la = TR.line_height("semibold", tpx), TR.line_height("regular", apx)
        for n, tr in enumerate(q[:fits]):
            y = y0 + n * rh
            k = f"q:{n}"
            box = (S(8), y, pw - S(8), y + rh - S(4))
            if self._np_hover == k:
                self._np_pill(layer, box, 0.10)
            cy = y + (rh - S(4) - A) // 2
            spr = self._np_small_cover(tr.get("album_art", ""), A)
            if spr is not None:
                layer.alpha_composite(spr, (S(16), cy))
            else:
                self._np_pill(layer, (S(16), cy, S(16) + A, cy + A), 0.10)
            dur = self._fmt_time(tr.get("duration_ms", 0)) if tr.get("duration_ms") else ""
            dw = TR.measure(dur, "regular", apx) if dur else 0
            tx = S(16) + A + S(12)
            maxw = pw - tx - S(18) - (dw + S(10) if dur else 0)
            ty = y + (rh - S(4) - lt - la) // 2
            TR.draw(d, (tx, ty), TR.ellipsize(tr.get("title", "") or "Unknown", "semibold", tpx, maxw),
                    "semibold", tpx, self._np_rgba(0.96))
            TR.draw(d, (tx, ty + lt), TR.ellipsize(tr.get("artist", ""), "regular", apx, maxw),
                    "regular", apx, self._np_rgba(0.6))
            if dur:
                TR.draw(d, (pw - S(18) - dw, ty + (lt - la) // 2 + S(1)), dur, "regular", apx, self._np_rgba(0.5))
            hits.append(box + (k,))
        return layer, hits

    def _np_queue_pick(self, n):
        q = (getattr(M.state, "queue", None) or [])[:QUEUE_ROWS]
        if not (0 <= n < len(q)):
            return
        tr = q[n]
        if not M.skip_to_queue(tr.get("uri", ""), tr.get("uid", "")):
            self._set_error("Spotify isn't connected, so it can't be controlled from here")
            return
        self._np_toast_show(f"Playing {tr.get('title') or 'track'}")
        self._np_panel_close()

    # ── Lyric search ─────────────────────────────────────────────
    def _np_search_ensure_entry(self):
        if self._np_search_entry is not None:
            return
        var = tk.StringVar()
        e = tk.Entry(self.np_cv, textvariable=var, bg=M.BG3, fg=M.TEXT, insertbackground=M.TEXT,
                     relief="flat", bd=0, highlightthickness=0, font=self._f(M.FS_BODY))
        e.bind("<Return>", lambda ev: (self._np_search_run(e.get()), "break")[1])
        e.bind("<Escape>", lambda ev: (self._np_panel_close(), "break")[1])
        self._np_search_entry = e
        self._np_search_win = self.np_cv.create_window(0, 0, window=e, anchor="nw", state="hidden")

    def _np_search_field(self, pw):
        S = self._S
        return S(14), S(50), pw - S(14), S(84)

    def _np_search_place(self, where):
        """Show the Entry over the drawn field (where=(x1, y1, pw)) or hide it."""
        win = self._np_search_win
        if win is None:
            return
        want = None
        if where is not None:
            x1, y1, pw = where
            fx1, fy1, fx2, fy2 = self._np_search_field(pw)
            try:
                eh = self._np_search_entry.winfo_reqheight()
            except tk.TclError:
                eh = self._S(20)
            want = (x1 + fx1 + self._S(34), y1 + fy1 + (fy2 - fy1 - eh) // 2, fx2 - fx1 - self._S(46))
        if want == self._np_search_shown:
            return
        self._np_search_shown = want
        try:
            if want is None:
                self.np_cv.itemconfigure(win, state="hidden")
            else:
                self.np_cv.coords(win, want[0], want[1])
                self.np_cv.itemconfigure(win, state="normal", width=want[2])
                try:
                    self._np_search_entry.config(bg=M.BG3, fg=M.TEXT, insertbackground=M.TEXT)
                    self._np_search_entry.focus_set()
                    self._np_search_entry.icursor("end")
                except tk.TclError:
                    pass
        except tk.TclError:
            pass

    def _np_search_run(self, text):
        s = self._np_search
        s["gen"] += 1
        gen = s["gen"]
        s["q"] = text = (text or "").strip()
        self._np_panel_scroll = 0
        if not text:
            s.update(status="Type an artist and a song title", results=[], busy=False)
            self._np_want_frame = True
            return
        s.update(status="Searching LRCLIB…", results=[], busy=True)
        st = M.state
        artist, title = getattr(st, "artist", ""), getattr(st, "title", "")
        dur = getattr(st, "duration_ms", 0) or 0

        def work():
            try:
                res, err = clean_results(lrclib_query(text, artist, title), dur), None
            except Exception as ex:
                res, err = [], ex
            try:
                self._np_to_tk(lambda: self._np_search_done(gen, res, err))
            except Exception:
                pass
        threading.Thread(target=work, name="lrclib-search", daemon=True).start()
        self._np_want_frame = True

    def _np_search_done(self, gen, results, err=None):
        s = self._np_search
        if gen != s["gen"]:
            return
        s["busy"] = False
        s["results"] = results
        if err is not None:
            s["status"] = "Search failed. Check your connection and try again"
            M.log(f"LRCLIB search failed: {type(err).__name__}: {err}")
        elif not results:
            s["status"] = "No lyrics found. Try different words"
        else:
            s["status"] = f"{len(results)} result{'s' if len(results) != 1 else ''} · pick the right one"
        self._np_panel_layer = None
        self._np_want_frame = True
        self._np_render()

    def _np_build_search(self, pw, ph):
        TR, S = self._np_text, self._S
        layer = Image.new("RGBA", (pw, ph), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        hits = []
        self._np_panel_head(d, layer, pw, "Find lyrics", hits)
        fx1, fy1, fx2, fy2 = self._np_search_field(pw)
        fm = self._np_rounded_mask((fx2 - fx1, fy2 - fy1), S(9))
        layer.paste(self._hex(M.BG3) + (255,), (fx1, fy1), fm)
        gs = S(14)
        layer.alpha_composite(self._np_icon("search", gs, self._hex(M.TEXT) + (150,)),
                              (fx1 + S(12), fy1 + (fy2 - fy1 - gs) // 2))
        s = self._np_search
        spx = self._px(8.5)
        TR.draw(d, (S(18), fy2 + S(8)), TR.ellipsize(s["status"], "regular", spx, pw - S(36)), "regular", spx,
                self._np_rgba(0.55))
        pinned = self._np_is_pinned()
        foot = S(46) if pinned else 0
        y0, rh = fy2 + S(34), S(58)
        res = s["results"]
        fits = max(1, (ph - y0 - foot - S(6)) // rh)
        self._np_panel_scroll = max(0, min(self._np_panel_scroll, max(0, len(res) - fits)))
        tpx, apx = self._px(10), self._px(8.5)
        lt, la = TR.line_height("semibold", tpx), TR.line_height("regular", apx)
        bpx = self._px(7.5)
        acc = self._np_accent()
        for n in range(self._np_panel_scroll, min(len(res), self._np_panel_scroll + fits)):
            r = res[n]
            y = y0 + (n - self._np_panel_scroll) * rh
            k = f"res:{n}"
            box = (S(8), y, pw - S(8), y + rh - S(4))
            if self._np_hover == k:
                self._np_pill(layer, box, 0.10)
            badge = "Synced" if r["synced"] else "Plain"
            bw = int(TR.measure(badge, "semibold", bpx)) + S(14)
            dur = self._fmt_time(r["duration"] * 1000) if r["duration"] else ""
            dw = TR.measure(dur, "regular", apx) if dur else 0
            right = max(bw, dw) + S(12)
            tx = S(18)
            maxw = pw - tx - S(18) - right
            ty = y + (rh - S(4) - lt - la) // 2
            TR.draw(d, (tx, ty), TR.ellipsize(r["track"] or "Untitled", "semibold", tpx, maxw), "semibold", tpx,
                    self._np_rgba(0.96))
            sub = " · ".join(p for p in (r["artist"], r["album"]) if p)
            TR.draw(d, (tx, ty + lt), TR.ellipsize(sub, "regular", apx, maxw), "regular", apx,
                    self._np_rgba(0.6))
            rx = pw - S(18)
            by = ty + S(1)
            bh = TR.line_height("semibold", bpx) + S(4)
            pill = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
            pm = self._np_rounded_mask((bw, bh), bh // 2)
            if r["synced"]:
                pill.paste(acc + (60,), (0, 0), pm.point([int(v * 0.24) for v in range(256)]))
                bcol = acc + (255,)
            else:
                pill.paste(self._np_fg() + (40,), (0, 0), pm.point([int(v * 0.12) for v in range(256)]))
                bcol = self._np_rgba(0.62)
            layer.alpha_composite(pill, (rx - bw, by))
            TR.draw(d, (rx - bw + S(7), by + S(2)), badge, "semibold", bpx, bcol)
            if dur:
                TR.draw(d, (rx - dw, ty + lt + (la - TR.line_height("regular", apx)) // 2), dur, "regular",
                        apx, self._np_rgba(0.5))
            hits.append(box + (k,))
        if pinned:
            lab = "Use Spotify's lyrics again"
            lpx = self._px(9)
            lw = int(TR.measure(lab, "semibold", lpx)) + S(24)
            bx1, by1 = (pw - lw) // 2, ph - foot + S(6)
            b = (bx1, by1, bx1 + lw, by1 + S(30))
            self._np_pill(layer, b, 0.18 if self._np_hover == "unpin" else 0.10)
            TR.draw(d, (bx1 + S(12), by1 + (S(30) - TR.line_height("semibold", lpx)) // 2), lab, "semibold",
                    lpx, self._np_rgba(0.95 if self._np_hover == "unpin" else 0.75))
            hits.append(b + ("unpin",))
        return layer, hits

    def _np_search_pick(self, n):
        res = self._np_search["results"]
        if not (0 <= n < len(res)):
            return
        picked = M.pick_lrclib([res[n]["raw"]], 0)
        if not picked:
            self._np_toast_show("That result has no usable lyrics")
            return
        self._np_pin_lyrics(*picked)
        self._np_panel_close()

    def _np_pin_lyrics(self, mode, synced, plain):
        st = M.state
        uri = getattr(st, "track_uri", "")
        if not uri:
            return False
        store = _store()
        # What was showing before (Spotify's), so unpinning can go back to it.
        if pinned_lyrics(uri) is None and st.lyrics_mode in ("synced", "plain"):
            _STASH[uri] = (st.lyrics_mode, st.synced, st.plain, "Spotify")
        if store:
            try:
                store.pin_lyrics(uri, mode, synced, plain, PIN_SOURCE)
            except Exception as e:
                M.log(f"Could not pin lyrics: {e}")
        M._apply_lyrics(mode, synced, plain, PIN_SOURCE)
        self._np_toast_show("Lyrics saved for this song")
        return True

    def _np_unpin_lyrics(self):
        uri = getattr(M.state, "track_uri", "")
        store = _store()
        had = False
        if store and uri:
            try:
                had = store.unpin_lyrics(uri)
            except Exception as e:
                M.log(f"Could not unpin lyrics: {e}")
        prev = _STASH.pop(uri, None)
        if prev:
            M._apply_lyrics(*prev)
            self._np_toast_show("Back to Spotify's lyrics")
        elif had:
            self._np_toast_show("Spotify's lyrics come back next time this song plays")
        self._np_panel_layer = None
        self._np_close_overlays()
        return had or bool(prev)

    # ── Context menu ─────────────────────────────────────────────
    def _np_menu_open(self, items, at):
        """items: [(id, label, enabled, checked)] or None for a separator;
        at: (x, y, where) with where "pt" (below-right of the pointer),
        "below" (under a button, right-aligned) or "above"."""
        self._np_menu = {"items": items, "at": at, "t0": time.monotonic()}
        self._np_menu_layer = None
        self._np_want_frame = True
        self._np_render()

    def _np_line_texts(self, i):
        ly = self._ly
        if ly is None or not (0 <= i < len(ly["items"])):
            return "", ""
        it = ly["items"][i]
        cur = "" if it.get("dots") else it["text"]
        nxt = ""
        for j in range(i + 1, min(len(ly["items"]), i + 4)):
            if not ly["items"][j].get("dots") and ly["items"][j]["text"]:
                nxt = ly["items"][j]["text"]
                break
        return cur, nxt

    def _np_current_row(self):
        ly = self._ly
        if ly is None:
            return -1
        i = ly["fto"]
        while 0 <= i < len(ly["items"]) and ly["items"][i].get("dots"):
            i -= 1
        return i

    def _np_sheet_menu(self, x, y, line=None):
        live_track = bool(getattr(M.state, "track_uri", ""))
        items = []
        i = line if line is not None else self._np_current_row()
        has_line = i >= 0 and bool(self._np_line_texts(i)[0])
        items += [(f"copy_line:{i}", "Copy line", has_line, False),
                  (f"share:{i}", "Share as image…", has_line, False),
                  (f"copy_img:{i}", "Copy image", has_line, False),
                  None]
        items += self._np_lyrics_menu_items(live_track)
        self._np_menu_open(items, (x, y, "pt"))

    def _np_lyrics_menu_items(self, live_track):
        return [("queue", "Up next", True, self._np_panel == "queue" and not self._np_panel_closing),
                ("search", "Wrong lyrics? Search…", live_track, False),
                ("unpin", "Use Spotify's lyrics again", live_track and self._np_is_pinned(), False)]

    def _np_more_menu(self, box):
        live_track = bool(getattr(M.state, "track_uri", ""))
        i = self._np_current_row()
        has_line = i >= 0 and bool(self._np_line_texts(i)[0])
        items = self._np_lyrics_menu_items(live_track) + [
            None, (f"share:{i}", "Share current line…", has_line, False),
            (f"copy_img:{i}", "Copy line as image", has_line, False)]
        self._np_menu_open(items, (box[2], box[3] + self._S(6), "below"))

    def _np_menu_geom(self, W, H):
        TR, S = self._np_text, self._S
        mn = self._np_menu
        px = self._px(9.5)
        items = mn["items"]
        checks = any(it and it[3] for it in items) or any(it and it[0].startswith("ov:") for it in items)
        lw = max((TR.measure(it[1], "semibold", px) for it in items if it), default=60)
        w = int(lw + S(28) + (S(20) if checks else 0))
        ih, sh = S(30), S(9)
        h = S(12) + sum(ih if it else sh for it in items)
        x, y, where = mn["at"]
        if where == "pt":
            x1, y1 = x + S(2), y + S(2)
            if y1 + h > H - S(6):
                y1 = y - h - S(2)
        elif where == "below":
            x1, y1 = x - w, y
        else:
            x1, y1 = x - w, y - h
        x1 = max(S(6), min(W - S(6) - w, x1))
        y1 = max(S(6), min(H - S(6) - h, y1))
        return int(x1), int(y1), w, h, px, ih, sh, checks

    def _np_draw_menu(self, frame, now):
        mn = self._np_menu
        if mn is None:
            return False, []
        W, H = frame.size
        x1, y1, w, h, px, ih, sh, checks = self._np_menu_geom(W, H)
        TR, S = self._np_text, self._S
        key = (id(mn), w, h, self._np_hover, M._DARK_MODE, M.ACCENT)
        cached = self._np_menu_layer
        if cached is None or cached[0] != key:
            layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            d = ImageDraw.Draw(layer)
            hits = []
            y = S(6)
            lh = TR.line_height("semibold", px)
            for it in mn["items"]:
                if it is None:
                    d.line((S(12), y + sh // 2, w - S(12), y + sh // 2), fill=self._np_rgba(0.14), width=1)
                    y += sh
                    continue
                mid, label, enabled, checked = it
                k = f"menu:{mid}"
                b = (S(5), y, w - S(5), y + ih)
                if enabled and self._np_hover == k:
                    self._np_pill(layer, b, 0.12)
                tx = S(14) + (S(20) if checks else 0)
                if checked:
                    cs = S(10)
                    d.ellipse((S(14) + S(1), y + (ih - cs // 2) // 2 - S(1), S(14) + S(1) + cs // 2,
                               y + (ih + cs // 2) // 2 - S(1)), fill=self._np_accent() + (255,))
                col = (self._np_accent() + (255,)) if checked else self._np_rgba(
                    (0.96 if self._np_hover == k else 0.85) if enabled else 0.32)
                TR.draw(d, (tx, y + (ih - lh) // 2), label, "semibold", px, col)
                if enabled:
                    hits.append(b + (k,))
                y += ih
            self._np_menu_layer = cached = (key, layer, hits)
        _, layer, hits = cached
        t = _clamp((now - mn["t0"]) / MENU_S) if M.ANIMATIONS_ENABLED else 1.0
        e = _ease_out(t)
        dy = int((1 - e) * S(4)) * (-1 if mn["at"][2] == "above" else 1)
        self._np_glass(frame, (x1, y1 + dy, x1 + w, y1 + h + dy), e, S(12))
        self._np_paste_layer(frame, layer, (x1, y1 + dy), e)
        out = [(x1 + a, y1 + b, x1 + c, y1 + d_, k) for a, b, c, d_, k in hits]
        out.append((x1, y1, x1 + w, y1 + h, "menu:"))
        return t < 1.0, out

    def _np_menu_action(self, mid):
        self._np_menu = None
        self._np_want_frame = True
        if mid.startswith(("copy_line:", "share:", "copy_img:")):
            what, _, i = mid.partition(":")
            cur, nxt = self._np_line_texts(int(i))
            if not cur:
                return
            if what == "copy_line":
                self._to_clipboard(cur, "Copied lyric")
                self._np_toast_show("Line copied")
            else:
                self._np_share(cur, nxt, save=(what == "share"))
        elif mid == "queue":
            if self._np_panel == "queue" and not self._np_panel_closing:
                self._np_panel_close()
            else:
                self._np_panel_open("queue")
        elif mid == "search":
            self._np_panel_open("search")
        elif mid == "unpin":
            self._np_unpin_lyrics()
        elif mid.startswith("ov:"):
            self._np_footer_action(mid[3:])
        self._np_invalidate()
        self._np_render()

    def _np_footer_action(self, key):
        fn = {"copy": self._copy_current_lyric,
              "top": self._toggle_topmost,
              "mini": lambda: (self._np_fullscreen_exit(), self._toggle_mini()),
              "overlay": self._np_toggle_overlay}.get(key)
        if fn:
            fn()

    def _np_toggle_overlay(self):
        fn = getattr(self, "_toggle_overlay", None)
        if fn:
            fn()
            self._np_invalidate(header=False)

    # ── Share image ──────────────────────────────────────────────
    def _np_share(self, line, next_line, save):
        st = M.state
        title, artist = getattr(st, "title", ""), getattr(st, "artist", "")
        cover = self._img
        pal = list(self._np_palette or [])
        acc = self._hex(M.ACCENT)
        import statusify_np_fx as fx
        fam = fx.lyric_font()
        family = None if fam == fx.DEFAULT_FAMILY else fam
        path = None
        if save:
            from tkinter import filedialog
            safe = "".join(c for c in f"{artist} - {title}" if c not in '\\/:*?"<>|').strip(" .-")[:100]
            try:
                path = filedialog.asksaveasfilename(
                    parent=self._root, title="Save lyric image", defaultextension=".png",
                    initialfile=(safe or "lyric") + ".png", filetypes=[("PNG image", "*.png")])
            except tk.TclError:
                path = None
            if not path:
                return

        def work():
            return render_share_image(line, next_line, title, artist, cover, pal, acc, family)

        def done(f):
            try:
                img, err = f.result(), None
            except Exception as ex:
                img, err = None, ex

            def finish():
                if img is None:
                    M.log(f"Lyric image failed: {err}")
                    self._np_toast_show("Couldn't make the image")
                    return
                if path:
                    try:
                        img.save(path, "PNG")
                        M.log(f"Saved lyric image: {path}")
                        self._np_toast_show("Image saved")
                    except OSError as ex:
                        M.log(f"Could not save lyric image: {ex}")
                        self._np_toast_show("Couldn't save the image")
                else:
                    from statusify_ui_stats import copy_image_to_clipboard
                    ok = copy_image_to_clipboard(img)
                    self._np_toast_show("Image copied" if ok else "Couldn't copy the image")
            try:
                self._np_to_tk(finish)
            except Exception:
                pass
        M.image_executor.submit(work).add_done_callback(done)

    # ── Sub-lines ────────────────────────────────────────────────
    def _np_sub_key(self):
        import statusify_translate as tr
        mode = tr.subline_mode()
        if mode == "off":
            return None
        t = getattr(M.state, "translation", None) or {}
        return (mode, id(t), len(t))

    def _np_sublines(self, n):
        """Per sheet row: the sub-line text (romanised / translated) or None.
        None for the whole sheet while the setting is off."""
        if not self._np_synced() or self._np_sub_key() is None:
            return None
        import statusify_translate as tr
        plan, _ = self._np_plan()
        out = [tr.subline_for(r["k"]) if r["kind"] == "line" else None for r in plan[:n]]
        return out if any(out) else None

    # ── Input ────────────────────────────────────────────────────
    def _np_ex_click(self, key, e):
        """Clicks for everything this mixin draws. True when handled."""
        if self._np_menu is not None:
            if key and key.startswith("menu:"):
                mid = key[5:]
                if mid:
                    self._np_menu_action(mid)
                return True
            self._np_menu = None
            self._np_want_frame = True
            self._np_render()
            return True                       # a click outside a menu only closes it
        if self._np_panel and not self._np_panel_closing:
            if key == "panel_close":
                self._np_panel_close(); return True
            if key and key.startswith("q:"):
                self._np_queue_pick(int(key[2:])); return True
            if key and key.startswith("res:"):
                self._np_search_pick(int(key[4:])); return True
            if key == "unpin":
                self._np_unpin_lyrics(); return True
            if key == "panel":
                return True
            if key is None or key.startswith("line:"):
                self._np_panel_close(); return True
        act = {
            "shuffle": lambda: self._np_player("shuffle"),
            "repeat": lambda: self._np_player("repeat"),
            "like": lambda: self._np_player("like"),
            "vol": self._np_toggle_mute,
            "queue": lambda: self._np_menu_action("queue"),
            "overlay": self._np_toggle_overlay,
            "more": lambda: self._np_more_menu(self._np_hit_box("more")),
            "ov_more": lambda: self._np_menu_open(
                [(f"ov:{k}", lab, True, lit) for k, lab, lit in getattr(self, "_np_overflow", [])],
                (self._np_hit_box("ov_more")[2], self._np_hit_box("ov_more")[1] - self._S(6), "above")),
        }.get(key)
        if act is None:
            return False
        self._np_pressed = (key, time.monotonic())
        act()
        self._np_invalidate()
        self._np_render()
        return True

    def _np_hit_box(self, key):
        for x1, y1, x2, y2, k in self._np_hits:
            if k == key:
                return (x1, y1, x2, y2)
        W, H = self._np_size
        return (W // 2, H // 2, W // 2, H // 2)

    def _np_ex_right(self, key, e):
        if self._np_menu is not None:
            self._np_menu = None
        if key and key.startswith("line:"):
            self._np_sheet_menu(e.x, e.y, int(key[5:]))
            return True
        if key is None:
            self._np_sheet_menu(e.x, e.y)
            return True
        return False

    def _np_ex_wheel(self, e):
        key = self._np_hit(e.x, e.y)
        if key in ("play", "vol"):
            self._np_volume_step(1 if e.delta > 0 else -1)
            return True
        if key and (key == "panel" or key.startswith(("q:", "res:", "unpin"))):
            if self._np_panel == "search":
                self._np_panel_scroll = max(0, self._np_panel_scroll + (-1 if e.delta > 0 else 1))
                self._np_want_frame = True
                self._np_render()
            return True
        if key and key.startswith("menu:"):
            return True
        return False

    # ── Per-frame entry point ────────────────────────────────────
    def _np_draw_extras(self, frame, top, bottom, now):
        """Panel, menu and toast over the composed frame. Returns busy; the
        hit regions they add (first in line) are kept in _np_ex_hits."""
        if self._np_panel is None and self._np_menu is None and self._np_toast is None:
            self._np_ex_hits = []
            if self._np_search_shown is not None:
                self._np_search_place(None)
            return False
        busy, hits = self._np_draw_panel(frame, top, bottom, now)
        if self._np_draw_toast(frame, bottom, now):
            busy = True
        mbusy, mhits = self._np_draw_menu(frame, now)
        self._np_ex_hits = mhits + hits
        return busy or mbusy
