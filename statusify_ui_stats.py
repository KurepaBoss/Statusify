"""The Stats page: listening overview, activity heatmap, a monthly Wrapped
card (exportable as an image), top songs and recently played.

Drawn the same way as the Settings page (statusify_ui_settings): the whole
page is ONE canvas of text items and small anti-aliased PIL images, rebuilt
by _stats_render() only when the data, the width or the palette changes.
Nothing on it is a native child window, so it scrolls without tearing.

All database work runs on M.image_executor; results come back to the Tk
thread through after(0). The page queries only while it is on screen (or
when it is first shown after a new play), never on a timer in the
background.

Names that belong to main are reached through M, the live main module:
palette colours and settings are rebound at runtime, so they must be read
from main on every use, never copied.
"""
import calendar
import colorsys
import datetime
import io
import sys
import time
import tkinter as tk
from tkinter import filedialog

try:
    from PIL import Image, ImageDraw, ImageFilter
except ImportError:
    Image = ImageDraw = ImageFilter = None

from statusify_ui_settings import _rgb

M = None   # the main module

RECENT_PAGE = 30          # "Recently played" rows per page ("Show more" adds this many)
HEAT_MAX_WEEKS = 53
VISIBLE_REFRESH_S = 15    # while the page is open, re-query at most this often


# ── Pure helpers (tested directly) ───────────────────────────────
def rel_time(played_at, now=None):
    """'Just now', '3 min ago', 'Today 14:05', 'Yesterday 21:14', 'Mon 21:14',
    '14 Sep', '14 Sep 2025'."""
    try:
        t = datetime.datetime.fromisoformat(played_at)
    except (TypeError, ValueError):
        return played_at or ""
    now = now or datetime.datetime.now()
    secs = (now - t).total_seconds()
    if 0 <= secs < 60:
        return "Just now"
    if 0 <= secs < 3600:
        return f"{int(secs // 60)} min ago"
    days = (now.date() - t.date()).days
    hm = t.strftime("%H:%M")
    if days == 0:
        return f"Today {hm}"
    if days == 1:
        return f"Yesterday {hm}"
    if 1 < days < 7:
        return f"{t.strftime('%a')} {hm}"
    if t.year == now.year:
        return f"{t.day} {t.strftime('%b')}"
    return f"{t.day} {t.strftime('%b')} {t.year}"


def fmt_duration(ms):
    """3:07 for a play's listened time; '' when nothing was recorded."""
    s = int((ms or 0) // 1000)
    if s <= 0:
        return ""
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_hm(ms):
    h, m = divmod(int((ms or 0) // 60000), 60)
    return f"{h}h {m}m" if h else f"{m}m"


def heat_level(n, most):
    """0 for no plays, else 1..4 by share of the busiest day."""
    if n <= 0 or most <= 0:
        return 0
    return max(1, min(4, -(-4 * n // most)))


def heat_tip(day, n):
    """'12 plays · Mon 14 Sep'."""
    what = "No plays" if n == 0 else f"{n} play{'s' if n != 1 else ''}"
    return f"{what} · {day.strftime('%a')} {day.day} {day.strftime('%b')}"


def month_name(y, m):
    return f"{calendar.month_name[m]} {y}"


def copy_image_to_clipboard(img):
    """Put a PIL image on the Windows clipboard as CF_DIB. True on success.

    A BMP file is a 14-byte file header followed by exactly a packed DIB, so
    the clipboard payload is the saved BMP minus its first 14 bytes."""
    if sys.platform != "win32" or img is None:
        return False
    import ctypes
    from ctypes import wintypes
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "BMP")
    data = buf.getvalue()[14:]
    # Private WinDLL instances: setting argtypes here must not change the
    # signatures other code in the process relies on.
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    k32.GlobalAlloc.restype = wintypes.HGLOBAL
    k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    k32.GlobalLock.restype = ctypes.c_void_p
    k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    k32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    u32.OpenClipboard.argtypes = [wintypes.HWND]
    u32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    u32.SetClipboardData.restype = wintypes.HANDLE
    h = k32.GlobalAlloc(0x0002, len(data))          # GMEM_MOVEABLE
    if not h:
        return False
    p = k32.GlobalLock(h)
    if not p:
        k32.GlobalFree(h)
        return False
    ctypes.memmove(p, data, len(data))
    k32.GlobalUnlock(h)
    for _ in range(10):                              # another app may hold it
        if u32.OpenClipboard(None):
            break
        time.sleep(0.03)
    else:
        k32.GlobalFree(h)
        return False
    try:
        u32.EmptyClipboard()
        if not u32.SetClipboardData(8, h):          # CF_DIB; owns h on success
            k32.GlobalFree(h)
            return False
    finally:
        u32.CloseClipboard()
    return True


def render_wrapped(summary, accent, text_renderer, art=None, size=(1080, 1350)):
    """The shareable Wrapped image for one month_summary(), as a PIL RGB image.

    Background: soft blurred colour fields derived from the accent (the same
    feel as the fluid behind the lyric sheet), darkened so white type always
    reads. `art` is the top song's cover (PIL) or None."""
    W, H = size
    TR = text_renderer
    ar, ag, ab = _rgb(accent)
    hh, ll, ss = colorsys.rgb_to_hls(ar / 255, ag / 255, ab / 255)
    ss = max(ss, 0.45)

    def hls(dh, l, s=ss):
        r, g, b = colorsys.hls_to_rgb((hh + dh) % 1.0, l, s)
        return int(r * 255), int(g * 255), int(b * 255)

    # Colour fields at 1/10 size, blurred, then upscaled: smooth and cheap.
    sw, sh = W // 10, H // 10
    bg = Image.new("RGB", (sw, sh), hls(0.0, 0.07, ss * 0.7))
    d = ImageDraw.Draw(bg)
    for (cx, cy, r, col) in ((0.15, 0.10, 0.55, hls(0.00, 0.33)),
                             (0.95, 0.30, 0.50, hls(0.09, 0.26)),
                             (0.20, 0.75, 0.60, hls(-0.08, 0.20)),
                             (0.85, 0.95, 0.45, hls(0.04, 0.28))):
        R = r * sw
        d.ellipse((cx * sw - R, cy * sh - R, cx * sw + R, cy * sh + R), fill=col)
    bg = bg.filter(ImageFilter.GaussianBlur(sw * 0.16)).resize((W, H), Image.BICUBIC)
    # Darken toward the bottom, where the numbers sit.
    shade = Image.linear_gradient("L").resize((W, H)).point(lambda v: int(v * 0.55))
    img = Image.composite(Image.new("RGB", (W, H), (8, 8, 12)), bg, shade)
    # Type and shapes go on a transparent layer composited at the end, so
    # the translucent whites really are translucent over the gradient.
    layer = Image.new("RGBA", (W, H), (255, 255, 255, 0))
    dr = ImageDraw.Draw(layer)

    white = (255, 255, 255, 255)
    soft = (255, 255, 255, 178)
    faint = (255, 255, 255, 120)
    M_ = 84                                  # margin
    y = 84
    TR.draw(dr, (M_, y), "STATUSIFY  ·  WRAPPED", "semibold", 28, soft)
    y += 50
    TR.draw(dr, (M_, y), month_name(summary["year"], summary["month"]), "bold", 88, white)
    y += TR.line_height("bold", 88) + 36

    # Top song, with its cover.
    art_px = 300
    box = (M_, y, M_ + art_px, y + art_px)
    if art is not None:
        a = art.convert("RGB").resize((art_px, art_px), Image.LANCZOS)
        mask = Image.new("L", (art_px * 4, art_px * 4), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, art_px * 4 - 1, art_px * 4 - 1),
                                               radius=28 * 4, fill=255)
        mask = mask.resize((art_px, art_px), Image.LANCZOS)
        # Soft shadow under the cover.
        sh_ = Image.new("L", (W, H), 0)
        ImageDraw.Draw(sh_).rounded_rectangle((box[0] + 6, box[1] + 14, box[2] + 6, box[3] + 14),
                                              radius=28, fill=150)
        sh_ = sh_.filter(ImageFilter.GaussianBlur(18))
        img.paste((0, 0, 0), (0, 0), sh_)
        img.paste(a, (M_, y), mask)
    else:
        dr.rounded_rectangle(box, radius=28, fill=hls(0.0, 0.5) + (255,))
        note = "♫"
        TR.draw(dr, (M_ + (art_px - TR.measure(note, "bold", 140)) / 2,
                     y + (art_px - TR.line_height("bold", 140)) / 2), note, "bold", 140,
                (255, 255, 255, 220))
    tx = M_ + art_px + 48
    tw = W - M_ - tx
    ty = y + 18
    TR.draw(dr, (tx, ty), "TOP SONG", "semibold", 26, faint)
    ty += 48
    song = summary.get("top_song")
    title = song["title"] if song else "—"
    lines = TR.wrap(title, "bold", 54, tw)[:3]
    if len(TR.wrap(title, "bold", 54, tw)) > 3:
        lines[-1] = TR.ellipsize(lines[-1] + "…", "bold", 54, tw)
    for ln in lines:
        TR.draw(dr, (tx, ty), ln, "bold", 54, white)
        ty += TR.line_height("bold", 54) - 4
    if song:
        ty += 8
        TR.draw(dr, (tx, ty), TR.ellipsize(song["artist"], "regular", 34, tw), "regular", 34, soft)
        ty += 48
        TR.draw(dr, (tx, ty), f"{song['plays']} play{'s' if song['plays'] != 1 else ''}",
                "semibold", 28, faint)
    y += art_px + 64

    # 2 x 3 grid of numbers.
    hour = summary.get("busiest_hour")
    art_name = summary.get("top_artist")
    streak = summary.get("longest_streak", 0)
    cells = [
        ("TOP ARTIST", art_name[0] if art_name else "—",
         f"{art_name[1]} plays" if art_name else ""),
        ("LISTENING TIME", fmt_hm(summary.get("listened_ms", 0)), ""),
        ("PLAYS", f"{summary.get('plays', 0):,}",
         f"{summary.get('artists', 0)} artist{'s' if summary.get('artists', 0) != 1 else ''}"),
        ("BUSIEST HOUR", f"{hour[0]:02d}:00" if hour else "—",
         f"{hour[1]} plays" if hour else ""),
        ("LONGEST STREAK", f"{streak} day{'s' if streak != 1 else ''}", "in a row"),
        ("ACTIVE DAYS", f"{summary.get('active_days', 0)}",
         f"of {calendar.monthrange(summary['year'], summary['month'])[1]}"),
    ]
    col_w = (W - 2 * M_) // 2
    row_h = 192
    for i, (label, value, sub) in enumerate(cells):
        cx = M_ + (i % 2) * col_w
        cy = y + (i // 2) * row_h
        TR.draw(dr, (cx, cy), label, "semibold", 26, faint)
        TR.draw(dr, (cx, cy + 38), TR.ellipsize(value, "bold", 62, col_w - 30), "bold", 62, white)
        if sub:
            TR.draw(dr, (cx, cy + 118), sub, "regular", 28, soft)
    foot = "Made with Statusify"
    TR.draw(dr, (W - M_ - TR.measure(foot, "semibold", 26), H - 84), foot, "semibold", 26, faint)
    dr.rounded_rectangle((M_, H - 76, M_ + 96, H - 68), radius=4, fill=hls(0.0, 0.6) + (255,))
    return Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")


class _StatText:
    """Text on the stats canvas that main updates with .config(text=…)."""

    def __init__(self, page, text="", fg=None):
        self._page = page
        self._o = {"text": text, "fg": fg}
        self.item = None

    def config(self, **kw):
        if not any(self._o.get(k) != v for k, v in kw.items()):
            return
        self._o.update(kw)
        self._page._stats_text_changed(self)
    configure = config

    def cget(self, key):
        return M.BG2 if key == "bg" else self._o.get(key, "")


class StatsPage:

    # ── Build ────────────────────────────────────────────────────
    def _build_stats(self):
        p = tk.Frame(self._container, bg=M.BG); self._pages["STATS"] = p
        S = self._ss
        area = tk.Frame(p, bg=M.BG)
        area.pack(fill="both", expand=True, padx=(S(20), S(6)), pady=(0, S(4)))
        self._st_sb = tk.Canvas(area, width=S(12), bg=M.BG, highlightthickness=0, bd=0)
        self._st_sb.pack(side="right", fill="y", padx=(S(6), 0))
        self.st_cv = tk.Canvas(area, bg=M.BG, highlightthickness=0, bd=0,
                               yscrollincrement=1, confine=True)
        self.st_cv.pack(side="left", fill="both", expand=True)
        cv = self.st_cv
        self._st_total = 1
        self._st_target = 0.0
        self._st_data = None          # last query bundle, or {"off": True}
        self._st_dirty = True
        self._st_recent_n = RECENT_PAGE
        self._st_top_mode = "all"
        today = datetime.date.today()
        self._st_month = (today.year, today.month)
        self._st_art = {}             # url -> PIL image (raw square)
        self._st_art_pending = set()
        self._st_thumb_items = {}     # url -> [(item, size, bg)]
        self._st_tags = []
        self._st_gen = 0
        self._st_flash = ""
        self._st_heat = None

        self.lbl_stats_songs = _StatText(self, "0")
        self.lbl_stats_time = _StatText(self, "0m 0s")
        self.lbl_stats_week = _StatText(self, "")
        self.lbl_stats_all = _StatText(self, "")
        # Stack the page under the current one straight away: its canvas gets
        # a real size and draws once now, so the first visit shows at once.
        p.place(x=0, y=0, relwidth=1, relheight=1)
        p.lower()

        last_w = {"w": 0}
        def _cfg(e):
            if e.width != last_w["w"]:
                last_w["w"] = e.width
                self._stats_render()
            else:
                self._st_scroll_to(cv.canvasy(0), animate=False)
        cv.bind("<Configure>", _cfg)
        cv.bind("<Motion>", self._stats_heat_motion)
        cv.bind("<Leave>", lambda e: self._stats_hide_tip())

        def _wheel(e):
            if self._cur_page != "STATS":
                return
            self._st_scroll_by(-(e.delta / 120.0) * S(84))
        cv.bind_all("<MouseWheel>", _wheel, add="+")

        sb = self._st_sb
        def _sb_hot(v):
            self._st_sb_hot = v
            self._st_draw_thumb()
        sb.bind("<Enter>", lambda e: _sb_hot(True))
        sb.bind("<Leave>", lambda e: _sb_hot(False))
        sb.bind("<Configure>", lambda e: self._st_draw_thumb())
        def _sb_drag(e):
            view = cv.winfo_height()
            frac = max(0.0, min(1.0, e.y / max(1, sb.winfo_height())))
            self._st_scroll_to(frac * max(0, self._st_total - view), animate=False)
        sb.bind("<Button-1>", _sb_drag)
        sb.bind("<B1-Motion>", _sb_drag)

        self._refresh_stats(reschedule=False)
        self._stats_render()

    # ── Session stats (the 5 s timer and the "stats" event) ──────
    def _refresh_stats(self, reschedule=True):
        """Update the session tiles.

        Named timer slot: this is called both by its own 5 s timer and on
        every ("stats",) event. Raw after() here once spawned a new chain per
        event, thousands after a few hours (the 1.1.5 freeze). Event-driven
        callers pass reschedule=False."""
        M._health_snapshot()
        total_secs = int(M._get_listen_time())
        mins, secs = divmod(total_secs, 60)
        hrs, mins = divmod(mins, 60)
        tstr = f"{hrs}h {mins}m" if hrs else f"{mins}m {secs}s"
        if hasattr(self, "lbl_stats_songs"):
            self.lbl_stats_songs.config(text=str(M._session_songs))
            self.lbl_stats_time.config(text=tstr)
            self._refresh_long_stats()
        if not reschedule:
            return
        self._schedule("stats", 5000, self._refresh_stats)

    STATS_EVERY_S = VISIBLE_REFRESH_S

    def _stats_on_play(self):
        """A play was recorded ("history_add"): refresh if the page is open,
        otherwise just remember that the next visit needs fresh data."""
        self._st_dirty = True
        if self._cur_page == "STATS" and not self._hidden:
            self._schedule("stats_reload", 800, lambda: self._refresh_long_stats(force=True))

    def _refresh_long_stats(self, sync=False, force=False):
        """Everything the page shows that comes from the history database.

        Queries only while the page is on screen (at most every
        VISIBLE_REFRESH_S unless a new play made the data stale). Off screen
        it does nothing: _show() calls it when the page is opened."""
        if not hasattr(self, "lbl_stats_week"):
            return
        st = M._store()
        if not st:
            self._st_data = {"off": True}
            self.lbl_stats_week._o["text"] = \
                "History is off. Turn on \"Remember history\" for long-term stats."
            self.lbl_stats_all._o["text"] = ""
            self._stats_render_soon()
            return
        now = time.monotonic()
        if not sync:
            visible = self._cur_page == "STATS" and not self._hidden
            if not visible:
                return
            fresh = now - getattr(self, "_stats_at", -1e9) < self.STATS_EVERY_S
            if fresh and not self._st_dirty and not force:
                return
            if getattr(self, "_stats_busy", False):
                return
        self._stats_at = now
        n = self._st_recent_n
        ym = self._st_month

        def query():
            dnow = datetime.datetime.now()
            today = dnow.date()
            return {
                "week": st.stats(since=dnow - datetime.timedelta(days=7), top=5),
                "all": st.stats(top=5),
                "recent": st.recent_plays(limit=n),
                "recent_n": n,
                "heat": st.plays_per_day(today - datetime.timedelta(days=7 * (HEAT_MAX_WEEKS + 1))),
                "month": st.month_summary(*ym),
                "first": st.first_played(),
                "top": {"all": st.top_tracks(limit=8),
                        "30": st.top_tracks(since=dnow - datetime.timedelta(days=30), limit=8)},
                "today": today,
                "current_id": (getattr(M, "_current_play", None) or {}).get("id"),
            }

        def apply(res):
            self._stats_busy = False
            if res is None:
                return
            def _fmt(label, d):
                txt = f"{label}  ·  {d['plays']} plays  ·  {fmt_hm(d['listened_ms'])}"
                if d["top_artists"]:
                    txt += chr(10) + "Top: " + ", ".join(f"{a} ({c})" for a, c in d["top_artists"])
                return txt
            same = res == self._st_data
            self._st_data = res
            self._stats_data = {"week": res["week"], "all": res["all"]}
            self._st_dirty = False
            self.lbl_stats_week._o["text"] = _fmt("Last 7 days", res["week"])
            self.lbl_stats_all._o["text"] = _fmt("All time", res["all"])
            # The periodic refresh usually finds nothing new; redraw then only
            # once a minute, so "3 min ago" keeps up without needless redraws.
            if not same or time.monotonic() - getattr(self, "_st_drawn_at", 0) > 60:
                self._stats_render_soon()

        self._stats_busy = True
        self._st_run(query, apply, sync)

    def _st_run(self, fn, apply, sync=False):
        """fn() on the image executor, apply(result or None) on the Tk thread.

        Finished futures are collected by a short Tk-side poll rather than
        after() from the worker: calling into Tk from another thread only
        works while mainloop() is running, and fails silently otherwise."""
        if not sync:
            try:
                fut = M.image_executor.submit(fn)
            except RuntimeError:            # executor shut down (quitting)
                fut = None
            if fut is not None:
                self.__dict__.setdefault("_st_jobs", []).append((fut, apply))
                self._schedule("stats_jobs", 16, self._st_poll_jobs)
                return
        try:
            res = fn()
        except Exception as e:
            M.log(f"Stats query failed: {e}")
            res = None
        apply(res)

    def _st_poll_jobs(self):
        jobs = self.__dict__.get("_st_jobs", [])
        done = [j for j in jobs if j[0].done()]
        self._st_jobs = [j for j in jobs if not j[0].done()]
        for fut, apply in done:
            try:
                res = fut.result()
            except Exception as e:
                M.log(f"Stats query failed: {e}")
                res = None
            try:
                apply(res)
            except tk.TclError:
                pass
        if self._st_jobs:
            self._schedule("stats_jobs", 30, self._st_poll_jobs)

    # ── Text slots ───────────────────────────────────────────────
    def _stats_text_changed(self, slot):
        cv = getattr(self, "st_cv", None)
        if cv is None or slot.item is None:
            return
        try:
            before = cv.bbox(slot.item)
            cv.itemconfigure(slot.item, text=slot.cget("text"))
            after = cv.bbox(slot.item)
        except tk.TclError:
            return
        if not before or not after or (after[2] - after[0]) > (before[2] - before[0]) + 4:
            self._stats_render_soon()

    def _stats_render_soon(self):
        self._schedule("statsrender", 30, self._stats_render)

    # ── Small drawing helpers ────────────────────────────────────
    def _st_bind(self, tag, cmd, enter=None, leave=None):
        cv = self.st_cv
        self._st_tags.append(tag)
        cv.tag_bind(tag, "<Button-1>", lambda e: cmd())
        cv.tag_bind(tag, "<Enter>", lambda e: (cv.config(cursor="hand2"), enter and enter()))
        cv.tag_bind(tag, "<Leave>", lambda e: (cv.config(cursor=""), leave and leave()))

    def _st_button(self, cv, x, y, text, cmd, kind="secondary", enabled=True, anchor="nw"):
        """Rounded canvas button with a hover fade. Returns its width."""
        S = self._ss
        font = self._f(M.FS_SMALL, True)
        w, h = font.measure(text) + S(24), S(28)
        if anchor == "ne":
            x -= w
        def colours(hover):
            if not enabled:
                return M.BG2, M.BG4
            if kind == "primary":
                return (M._blend(M.ACCENT, M.TEXT, 0.15) if hover else M.ACCENT), M.ACCENT_FG
            if kind == "ghost":
                return (M.BG3 if hover else M.BG2), (M.TEXT if hover else M.TEXT2)
            return (M.BG4 if hover else M.BG3), M.TEXT
        fill, fg = colours(False)
        tag = f"stb{self._st_gen}_{len(self._st_tags)}"
        img = cv.create_image(x, y, anchor="nw", tags=(tag,),
                              image=self._pill_photo(w, h, fill, M.BG2, radius=S(7)))
        txt = cv.create_text(x + w // 2, y + h // 2, text=text, fill=fg, font=font, tags=(tag,))
        if not enabled:
            return w
        st = {"t": 0.0}
        def fade(to):
            a = st["t"]
            def apply(e):
                st["t"] = a + (to - a) * e
                f0, g0 = colours(False)
                f1, g1 = colours(True)
                f = M._blend(f0, f1, round(st["t"] * 6) / 6)
                cv.itemconfigure(img, image=self._pill_photo(w, h, f, M.BG2, radius=S(7)))
                cv.itemconfigure(txt, fill=M._blend(g0, g1, st["t"]))
            self._animate(f"hover:{tag}", 120, apply)
        self._st_bind(tag, cmd, lambda: fade(1.0), lambda: fade(0.0))
        return w

    def _st_fit(self, text, font, w):
        """`text` cut with an ellipsis to fit `w` pixels in a Tk font."""
        if font.measure(text) <= w:
            return text
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if font.measure(text[:mid].rstrip() + "…") <= w:
                lo = mid
            else:
                hi = mid - 1
        return text[:lo].rstrip() + "…" if lo else "…"

    def _st_card(self, cv, x0, x1, y, draw):
        """A Settings-style card around whatever draw(cv, ix0, ix1, y) draws;
        draw returns the content height."""
        pad = self._ss(16)
        h = draw(cv, x0 + pad, x1 - pad, y)
        self._set_card_bg(cv, x0, y, x1, y + h, f"stcard{self._st_gen}_{y}")
        return y + h

    # ── Artwork thumbnails ───────────────────────────────────────
    def _st_thumb(self, cv, x, y, url, size, bg, tags=()):
        """Rounded cover at (x, y), composited onto `bg`; a note glyph until
        the art arrives (it is fetched off-thread and swapped in place)."""
        S = self._ss
        ph = self._st_thumb_photo(url, size, bg) if url else None
        if ph is not None:
            item = cv.create_image(x, y, anchor="nw", image=ph, tags=tags)
        else:
            item = cv.create_image(x, y, anchor="nw", tags=tags,
                                   image=self._pill_photo(size, size, M.BG3, bg, radius=S(6)))
            cv.create_text(x + size // 2, y + size // 2, text="♫", fill=M.MUTED,
                           font=self._f(M.FS_SMALL), tags=tuple(tags) + ("stnote",))
            if url and M.PIL_AVAILABLE:
                self._st_fetch_art(url)
        if url:
            self._st_thumb_items.setdefault(url, []).append((item, size, bg))
        return item

    def _st_thumb_photo(self, url, size, bg):
        raw = self._st_art.get(url)
        if raw is None:
            return None
        cache = self.__dict__.setdefault("_st_thumb_cache", {})
        key = (url, size, bg)
        ph = cache.get(key)
        if ph is None:
            img = M._round_image(raw.resize((size, size), Image.LANCZOS), self._ss(6), bg)
            ph = M.ImageTk.PhotoImage(img)
            if len(cache) > 400:
                cache.clear()
            cache[key] = ph
        return ph

    def _st_fetch_art(self, url):
        if url in self._st_art_pending:
            return
        self._st_art_pending.add(url)

        def apply(img):
            self._st_art_pending.discard(url)
            if img is None:
                return
            self._st_art[url] = img
            if len(self._st_art) > 300:
                for k in list(self._st_art)[:100]:
                    self._st_art.pop(k, None)
            for item, size, bg in self._st_thumb_items.get(url, []):
                try:
                    self.st_cv.itemconfigure(item, image=self._st_thumb_photo(url, size, bg))
                except tk.TclError:
                    pass
            # The placeholder's note glyph sits over the old image; a render
            # (coalesced across a burst of arrivals) clears it.
            self._schedule("statsart", 120, self._stats_render)
        self._st_run(lambda: M._fetch_art(url, M.THUMB_PX), apply)

    # ── Rendering ────────────────────────────────────────────────
    def _stats_render(self):
        cv = getattr(self, "st_cv", None)
        if cv is None:
            return
        W = cv.winfo_width()
        if W < 120:
            return
        top = cv.canvasy(0)
        cv.delete("all")
        for t in self._st_tags:
            for seq in ("<Button-1>", "<Button-3>", "<Enter>", "<Leave>"):
                try:
                    cv.tag_unbind(t, seq)
                except tk.TclError:
                    pass
        self._st_tags = []
        self._st_gen += 1
        self._st_thumb_items = {}
        self._st_heat = None
        self._st_tip = None
        S = self._ss
        x0, x1 = S(2), W - S(2)
        y = S(18)
        y = self._set_draw_title(cv, x0, x1, y, "Stats", "Your listening, at a glance.")
        data = self._st_data or {}

        y = self._set_draw_section(cv, x0, x1, y, "Overview")
        y = self._st_card(cv, x0, x1, y, self._st_draw_overview)

        if data.get("off"):
            y = self._set_draw_section(cv, x0, x1, y, "Your history")
            y = self._st_card(cv, x0, x1, y, lambda *a: self._st_draw_empty(
                *a, "History is off",
                "Turn on \"Remember history\" in Settings and your listening "
                "stats will build up here.", ("Open Settings", lambda: self._show("SETTINGS"))))
        elif data and not data["all"]["plays"]:
            y = self._set_draw_section(cv, x0, x1, y, "Your history")
            y = self._st_card(cv, x0, x1, y, lambda *a: self._st_draw_empty(
                *a, "Nothing here yet",
                "Play something and your stats will appear here: an activity map, "
                "your top songs and a monthly Wrapped.", None))
        elif data:
            y = self._set_draw_section(cv, x0, x1, y, "Activity",
                                       "Plays per day. Hover a day for details.")
            y = self._st_card(cv, x0, x1, y, self._st_draw_heat)
            y = self._set_draw_section(cv, x0, x1, y, "Wrapped",
                                       "Your month in numbers. Save it as an image to share.")
            y = self._st_card(cv, x0, x1, y, self._st_draw_wrapped)
            y = self._set_draw_section(cv, x0, x1, y, "Top songs")
            y = self._st_card(cv, x0, x1, y, self._st_draw_top)
            y = self._set_draw_section(cv, x0, x1, y, "Recently played",
                                       "Click a song to find it in History. Right-click to copy.")
            y = self._st_card(cv, x0, x1, y, self._st_draw_recent)

        total = y + S(28)
        self._st_total = total
        self._st_drawn_at = time.monotonic()
        cv.config(scrollregion=(0, 0, W, total))
        if getattr(self, "_st_gliding", False):
            # A redraw mid-glide keeps the glide going to where it was headed.
            self._st_target = min(self._st_target, max(0, total - cv.winfo_height()))
            cv.yview_moveto(top / total)
        else:
            self._st_scroll_to(top, animate=False)

    def _st_draw_empty(self, cv, x0, x1, y, title, body, button):
        S = self._ss
        yy = y + S(22)
        t = cv.create_text((x0 + x1) // 2, yy, anchor="n", text="♫", fill=M.ACCENT,
                           font=self._f(M.FS_HERO + 8, True))
        yy = cv.bbox(t)[3] + S(4)
        t = cv.create_text((x0 + x1) // 2, yy, anchor="n", text=title, fill=M.TEXT,
                           font=self._f(M.FS_LARGE + 1, True))
        yy = cv.bbox(t)[3] + S(4)
        t = cv.create_text((x0 + x1) // 2, yy, anchor="n", text=body, fill=M.MUTED,
                           font=self._f(M.FS_SMALL), width=min(x1 - x0, S(360)), justify="center")
        yy = cv.bbox(t)[3] + S(14)
        if button:
            font = self._f(M.FS_SMALL, True)
            w = font.measure(button[0]) + S(24)
            self._st_button(cv, (x0 + x1 - w) // 2, yy, button[0], button[1], "primary")
            yy += S(28) + S(14)
        return yy + S(8) - y

    def _st_draw_overview(self, cv, x0, x1, y):
        """2x2 grid of big numbers, then top artists for both periods as
        ranked lists with a bar each (moved here from Settings)."""
        S = self._ss
        col_w = (x1 - x0) // 2
        big, cap = self._f(M.FS_HERO + 3, True), self._f(M.FS_SMALL)
        data = self._st_data or {}

        def tile(x, yy, value, caption, slot=None):
            v = cv.create_text(x, yy, anchor="nw", text=value, fill=M.TEXT, font=big)
            if slot is not None:
                slot.item = v
            c = cv.create_text(x, cv.bbox(v)[3], anchor="nw", text=caption, fill=M.MUTED,
                               font=cap, width=col_w - S(12))
            return cv.bbox(c)[3]

        yy = y + S(14)
        b1 = tile(x0, yy, self.lbl_stats_songs.cget("text"), "songs this session",
                  self.lbl_stats_songs)
        b2 = tile(x0 + col_w, yy, self.lbl_stats_time.cget("text"), "listened this session",
                  self.lbl_stats_time)
        yy = max(b1, b2) + S(14)
        if "week" not in data:
            text = self.lbl_stats_week.cget("text")
            if not text:
                return yy - y
            cv.create_line(x0, yy, x1 + S(16), yy, fill=M.BORDER)
            self.lbl_stats_week.item = cv.create_text(
                x0, yy + S(12), anchor="nw", text=text,
                fill=M.TEXT2, font=self._f(M.FS_BODY), width=x1 - x0)
            self.lbl_stats_all.item = None
            return cv.bbox(self.lbl_stats_week.item)[3] + S(16) - y
        wk, al = data["week"], data["all"]
        cv.create_line(x0, yy, x1 + S(16), yy, fill=M.BORDER)
        yy += S(14)
        b1 = tile(x0, yy, f"{wk['plays']:,}", f"plays in the last 7 days · {fmt_hm(wk['listened_ms'])}")
        b2 = tile(x0 + col_w, yy, f"{al['plays']:,}", f"plays all time · {fmt_hm(al['listened_ms'])}")
        self.lbl_stats_week.item = self.lbl_stats_all.item = None
        yy = max(b1, b2) + S(14)

        if not (wk["top_artists"] or al["top_artists"]):
            return yy - y
        cv.create_line(x0, yy, x1 + S(16), yy, fill=M.BORDER)
        yy += S(14)
        body = self._f(M.FS_BODY)
        small_b = self._f(M.FS_SMALL, True)
        bottom = yy
        for k, (title, d) in enumerate((("Top artists · 7 days", wk), ("Top artists · all time", al))):
            x = x0 + k * col_w
            w = col_w - S(18)
            t = cv.create_text(x, yy, anchor="nw", text=title, fill=M.MUTED, font=small_b)
            ry = cv.bbox(t)[3] + S(8)
            tops = d["top_artists"]
            if not tops:
                cv.create_text(x, ry, anchor="nw", text="Nothing yet", fill=M.MUTED, font=body)
                bottom = max(bottom, ry + S(20))
                continue
            most = max(n for _, n in tops) or 1
            for rank, (name, n) in enumerate(tops, 1):
                cnt = f"{n}"
                cw = small_b.measure(cnt)
                label = self._st_fit(f"{rank}. {name}", body, w - cw - S(10))
                cv.create_text(x, ry, anchor="nw", text=label,
                               fill=M.TEXT if rank == 1 else M.TEXT2, font=body)
                cv.create_text(x + w, ry, anchor="ne", text=cnt, fill=M.MUTED, font=small_b)
                by = ry + body.metrics("linespace") + S(3)
                cv.create_rectangle(x, by, x + w, by + S(3), fill=M.BG3, outline="")
                cv.create_rectangle(x, by, x + max(S(3), int(w * n / most)), by + S(3),
                                    fill=M.ACCENT, outline="")
                ry = by + S(10)
            bottom = max(bottom, ry)
        return bottom + S(6) - y

    # ── Heatmap ──────────────────────────────────────────────────
    def _st_heat_colours(self):
        return [M.BG3] + [M._blend(M.BG3, M.ACCENT, f) for f in (0.35, 0.58, 0.8, 1.0)]

    def _st_draw_heat(self, cv, x0, x1, y):
        S = self._ss
        data = self._st_data
        heat = data.get("heat") or {}
        today = data.get("today") or datetime.date.today()
        small = self._f(M.FS_SMALL)
        small_b = self._f(M.FS_SMALL, True)
        cell, gap = S(12), S(3)
        step = cell + gap
        lw = small.measure("Wed") + S(8)
        weeks = max(4, min(HEAT_MAX_WEEKS, (x1 - x0 - lw + gap) // step))
        start = today - datetime.timedelta(days=today.weekday() + 7 * (weeks - 1))
        days_in = {start + datetime.timedelta(days=i) for i in range(7 * weeks)}
        counts = {d: heat.get(d.isoformat(), 0) for d in days_in if d <= today}
        total = sum(counts.values())
        active = sum(1 for n in counts.values() if n)
        most = max(counts.values() or [0])

        yy = y + S(14)
        t = cv.create_text(x0, yy, anchor="nw", fill=M.TEXT, font=self._f(M.FS_BODY, True),
                           text=f"{total:,} play{'s' if total != 1 else ''} in the last {weeks} weeks")
        cv.create_text(cv.bbox(t)[2] + S(8), yy + S(1),
                       anchor="nw", fill=M.MUTED, font=small,
                       text=f"· {active} active day{'s' if active != 1 else ''}")
        # Legend, right-aligned.
        cols = self._st_heat_colours()
        lx = x1
        more = cv.create_text(lx, yy + S(1), anchor="ne", text="More", fill=M.MUTED, font=small)
        lx = cv.bbox(more)[0] - S(6)
        for c in reversed(cols):
            cv.create_image(lx - cell, yy + S(3), anchor="nw",
                            image=self._pill_photo(cell, cell, c, M.BG2, radius=S(3)))
            lx -= step
        cv.create_text(lx - S(3), yy + S(1), anchor="ne", text="Less", fill=M.MUTED, font=small)
        yy = cv.bbox(t)[3] + S(12)

        gx = x0 + lw
        # Month labels over the first week (column) that starts in each
        # month; the leading partial month only when it has room.
        free_x = gx
        for c in range(weeks):
            d = start + datetime.timedelta(days=7 * c)
            prev = d - datetime.timedelta(days=7)
            if c and d.month == prev.month:
                continue
            x = gx + c * step
            label = calendar.month_abbr[d.month]
            if x < free_x or x + small.measure(label) > x1:
                continue
            if c == 0 and any((d + datetime.timedelta(days=7 * k)).month != d.month
                              for k in (1, 2)):
                continue
            cv.create_text(x, yy, anchor="nw", fill=M.MUTED, font=small, text=label)
            free_x = x + small.measure(label) + S(6)
        gy = yy + small.metrics("linespace") + S(4)
        for r, name in ((0, "Mon"), (2, "Wed"), (4, "Fri")):
            cv.create_text(x0, gy + r * step + cell // 2, anchor="w", text=name,
                           fill=M.MUTED, font=small)
        photos = [self._pill_photo(cell, cell, c, M.BG2, radius=S(3)) for c in cols]
        for c in range(weeks):
            for r in range(7):
                d = start + datetime.timedelta(days=7 * c + r)
                if d > today:
                    continue
                lvl = heat_level(counts.get(d, 0), most)
                cv.create_image(gx + c * step, gy + r * step, anchor="nw", image=photos[lvl])
        self._st_heat = {"x": gx, "y": gy, "step": step, "cell": cell, "weeks": weeks,
                         "start": start, "today": today, "counts": counts}
        return gy + 7 * step - gap + S(16) - y

    def _stats_heat_at(self, x, y):
        """(date, plays) under canvas point (x, y), or None."""
        h = self._st_heat
        if not h:
            return None
        c = int((x - h["x"]) // h["step"])
        r = int((y - h["y"]) // h["step"])
        if not (0 <= c < h["weeks"] and 0 <= r < 7):
            return None
        if (x - h["x"]) - c * h["step"] > h["cell"] or (y - h["y"]) - r * h["step"] > h["cell"]:
            return None          # in the gap between cells
        d = h["start"] + datetime.timedelta(days=7 * c + r)
        if d > h["today"]:
            return None
        return d, h["counts"].get(d, 0)

    def _stats_heat_motion(self, e):
        cv = self.st_cv
        hit = self._stats_heat_at(cv.canvasx(e.x), cv.canvasy(e.y))
        if hit is None:
            self._stats_hide_tip()
            return
        self._stats_show_tip(*hit)

    def _stats_show_tip(self, day, n):
        h = self._st_heat
        if not h:
            return
        if getattr(self, "_st_tip", None) and self._st_tip[0] == day:
            return
        self._stats_hide_tip()
        cv, S = self.st_cv, self._ss
        text = heat_tip(day, n)
        font = self._f(M.FS_SMALL, True)
        w, ht = font.measure(text) + S(18), S(26)
        idx = (day - h["start"]).days
        cx = h["x"] + (idx // 7) * h["step"] + h["cell"] // 2
        cy = h["y"] + (idx % 7) * h["step"]
        x = max(S(2), min(cv.winfo_width() - w - S(2), cx - w // 2))
        y = cy - ht - S(6)
        img = cv.create_image(x, y, anchor="nw", tags=("sttip",),
                              image=self._pill_photo(w, ht, M.BG4, M.BG2, radius=S(7),
                                                     outline=M.BORDER))
        cv.create_text(x + w // 2, y + ht // 2, text=text, fill=M.TEXT, font=font, tags=("sttip",))
        cv.tag_raise("sttip")
        self._st_tip = (day, img, text)

    def _stats_hide_tip(self):
        try:
            self.st_cv.delete("sttip")
        except (AttributeError, tk.TclError):
            pass
        self._st_tip = None

    # ── Wrapped ──────────────────────────────────────────────────
    def _st_month_bounds(self):
        data = self._st_data or {}
        today = data.get("today") or datetime.date.today()
        first = data.get("first")
        try:
            f = datetime.date.fromisoformat(first[:10]) if first else today
        except ValueError:
            f = today
        return (f.year, f.month), (today.year, today.month)

    def _stats_month_step(self, delta, sync=False):
        lo, hi = self._st_month_bounds()
        y, m = self._st_month
        m += delta
        y += (m - 1) // 12
        m = (m - 1) % 12 + 1
        if (y, m) < lo or (y, m) > hi:
            return
        self._st_month = (y, m)
        st = M._store()
        if not st:
            return

        def apply(res):
            if res is None or not self._st_data or self._st_month != (y, m):
                return
            self._st_data["month"] = res
            self._stats_render()
        self._st_run(lambda: st.month_summary(y, m), apply, sync)

    def _st_draw_wrapped(self, cv, x0, x1, y):
        S = self._ss
        summ = self._st_data.get("month") or {}
        lo, hi = self._st_month_bounds()
        ym = self._st_month
        yy = y + S(14)
        # Header: ‹ Month › on the left, actions on the right.
        bw = self._st_button(cv, x0, yy, "‹", lambda: self._stats_month_step(-1),
                             "ghost", enabled=ym > lo)
        t = cv.create_text(x0 + bw + S(8), yy + S(14), anchor="w", fill=M.TEXT,
                           font=self._f(M.FS_LARGE + 1, True), text=month_name(*ym))
        self._st_button(cv, cv.bbox(t)[2] + S(8), yy, "›", lambda: self._stats_month_step(1),
                        "ghost", enabled=ym < hi)
        has = bool(summ.get("plays"))
        narrow = (x1 - x0) < S(470)
        if has:
            rx = x1
            w = self._st_button(cv, rx, yy, "Save as image", self._stats_save_wrapped,
                                "primary", anchor="ne")
            rx -= w + S(6)
            if sys.platform == "win32" and not narrow:
                w = self._st_button(cv, rx, yy, "Copy image", self._stats_copy_wrapped,
                                    "secondary", anchor="ne")
                rx -= w + S(6)
            if self._st_flash:
                cv.create_text(rx - S(4), yy + S(14), anchor="e", text=self._st_flash,
                               fill=M.ACCENT, font=self._f(M.FS_SMALL, True))
        yy += S(28) + S(16)
        if not has:
            t = cv.create_text(x0, yy, anchor="nw", fill=M.MUTED, font=self._f(M.FS_BODY),
                               text=f"Nothing played in {month_name(*ym)}.")
            return cv.bbox(t)[3] + S(18) - y

        song, art_name, hour = summ.get("top_song"), summ.get("top_artist"), summ.get("busiest_hour")
        streak = summ.get("longest_streak", 0)
        cells = [
            ("Top song", song["title"] if song else "—",
             f"{song['artist']} · {song['plays']} plays" if song else ""),
            ("Top artist", art_name[0] if art_name else "—",
             f"{art_name[1]} plays" if art_name else ""),
            ("Listening time", fmt_hm(summ.get("listened_ms", 0)),
             f"across {summ.get('active_days', 0)} days"),
            ("Plays", f"{summ.get('plays', 0):,}",
             f"{summ.get('artists', 0)} different artists"),
            ("Busiest hour", f"{hour[0]:02d}:00–{(hour[0] + 1) % 24:02d}:00" if hour else "—",
             f"{hour[1]} plays in that hour" if hour else ""),
            ("Longest streak", f"{streak} day{'s' if streak != 1 else ''}",
             "in a row with music"),
        ]
        ncol = 2 if narrow else 3
        col_w = (x1 - x0) // ncol
        cap_b, val, sub = self._f(M.FS_SMALL, True), self._f(M.FS_LARGE + 3, True), self._f(M.FS_SMALL)
        # Top song gets its cover beside the grid's first cell.
        rows_bottom = yy
        for i, (label, value, caption) in enumerate(cells):
            cx = x0 + (i % ncol) * col_w
            if i % ncol == 0 and i:
                yy = rows_bottom + S(14)
            w = col_w - S(14)
            tx = cx
            if i == 0 and song:
                size = S(44)
                self._st_thumb(cv, cx, yy + S(2), song.get("album_art"), size, M.BG2)
                tx = cx + size + S(10)
                w -= size + S(10)
            a = cv.create_text(tx, yy, anchor="nw", text=label.upper(), fill=M.MUTED, font=cap_b)
            b = cv.create_text(tx, cv.bbox(a)[3] + S(1), anchor="nw", fill=M.TEXT, font=val,
                               text=self._st_fit(value, val, w))
            c = cv.create_text(tx, cv.bbox(b)[3], anchor="nw", fill=M.TEXT2, font=sub,
                               text=self._st_fit(caption, sub, w))
            rows_bottom = max(rows_bottom, cv.bbox(c)[3])
        return rows_bottom + S(18) - y

    def _stats_wrapped_image(self, summary=None, art=None):
        summary = summary or (self._st_data or {}).get("month")
        if not summary:
            return None
        return render_wrapped(summary, M.ACCENT, self._np_text, art=art)

    def _st_wrapped_job(self, then, sync=False):
        """Render the Wrapped image (fetching the top song's cover) off the Tk
        thread, then call then(img) on it."""
        summary = (self._st_data or {}).get("month")
        if not summary or not summary.get("plays"):
            return
        url = (summary.get("top_song") or {}).get("album_art")
        accent, tr = M.ACCENT, self._np_text

        def job():
            art = M._fetch_art(url, 480) if url and M.PIL_AVAILABLE else None
            return render_wrapped(summary, accent, tr, art=art)
        self._st_run(job, then, sync)

    def _stats_flash(self, text):
        self._st_flash = text
        self._stats_render()
        def clear():
            self._st_flash = ""
            self._stats_render()
        self._schedule("statsflash", 2600, clear)

    def _stats_save_wrapped(self, sync=False):
        summary = (self._st_data or {}).get("month")
        if not summary:
            return None
        path = filedialog.asksaveasfilename(
            parent=self.win, title="Save Wrapped image", defaultextension=".png",
            filetypes=[("PNG image", "*.png")],
            initialfile=f"Statusify Wrapped {month_name(summary['year'], summary['month'])}.png")
        if not path:
            return None

        def then(img):
            if img is None:
                self._stats_flash("Couldn't render the image")
                return
            try:
                img.save(path, "PNG")
            except OSError as e:
                M.log(f"Could not save Wrapped image: {e}")
                self._stats_flash("Couldn't save the image")
                return
            M.log(f"Wrapped image saved  ·  {path}")
            self._stats_flash("Saved")
        self._st_wrapped_job(then, sync)
        return path

    def _stats_copy_wrapped(self, sync=False):
        def then(img):
            ok = False
            try:
                ok = copy_image_to_clipboard(img)
            except Exception as e:
                M.log(f"Could not copy Wrapped image: {e}")
            self._stats_flash("Copied" if ok else "Couldn't copy the image")
        self._st_wrapped_job(then, sync)

    # ── Top songs ────────────────────────────────────────────────
    def _stats_set_top_mode(self, mode):
        if mode != self._st_top_mode:
            self._st_top_mode = mode
            self._stats_render()

    def _st_draw_top(self, cv, x0, x1, y):
        S = self._ss
        yy = y + S(14)
        small_b = self._f(M.FS_SMALL, True)
        # Two-way toggle: a track with the chosen option raised.
        opts = (("All time", "all"), ("30 days", "30"))
        seg = max(small_b.measure(t) for t, _ in opts) + S(24)
        tw, th = seg * 2 + S(6), S(28)
        tx = x1 - tw
        cv.create_image(tx, yy, anchor="nw", image=self._pill_photo(tw, th, M.BG3, M.BG2, radius=S(8)))
        for i, (label, key) in enumerate(opts):
            sx = tx + S(3) + i * seg
            sel = key == self._st_top_mode
            tag = f"sttop{self._st_gen}_{key}"
            if sel:
                cv.create_image(sx, yy + S(3), anchor="nw", tags=(tag,),
                                image=self._pill_photo(seg, th - S(6),
                                                       M.BG4 if M._DARK_MODE else M.BG2, M.BG3,
                                                       radius=S(6)))
            cv.create_text(sx + seg // 2, yy + th // 2, text=label, font=small_b, tags=(tag,),
                           fill=M.TEXT if sel else M.MUTED)
            if not sel:
                cv.create_rectangle(sx, yy + S(3), sx + seg, yy + th - S(3), fill="",
                                    outline="", tags=(tag,))
                self._st_bind(tag, lambda k=key: self._stats_set_top_mode(k))
        cv.create_text(x0, yy + th // 2, anchor="w", text="Most played", fill=M.MUTED, font=small_b)
        yy += th + S(12)
        tops = (self._st_data.get("top") or {}).get(self._st_top_mode) or []
        body, body_b = self._f(M.FS_BODY), self._f(M.FS_BODY, True)
        if not tops:
            t = cv.create_text(x0, yy, anchor="nw", fill=M.MUTED, font=body,
                               text="No plays in the last 30 days.")
            return cv.bbox(t)[3] + S(16) - y
        most = max(s["plays"] for s in tops) or 1
        size = S(34)
        for rank, s in enumerate(tops, 1):
            cnt = f"{s['plays']} play{'s' if s['plays'] != 1 else ''}"
            cw = small_b.measure(cnt)
            cv.create_text(x0 + S(14), yy + size // 2, anchor="e", text=str(rank),
                           fill=M.ACCENT if rank == 1 else M.MUTED, font=small_b)
            ax = x0 + S(22)
            self._st_thumb(cv, ax, yy, s.get("album_art"), size, M.BG2)
            tx = ax + size + S(10)
            room = x1 - tx - cw - S(12)
            cv.create_text(tx, yy - S(1), anchor="nw", fill=M.TEXT, font=body_b,
                           text=self._st_fit(s["title"], body_b, room))
            cv.create_text(tx, yy + body_b.metrics("linespace") - S(2), anchor="nw", fill=M.MUTED,
                           font=self._f(M.FS_SMALL), text=self._st_fit(s["artist"], self._f(M.FS_SMALL), room))
            cv.create_text(x1, yy + S(2), anchor="ne", text=cnt, fill=M.TEXT2, font=small_b)
            bx0 = x1 - max(cw, S(60))
            by = yy + size - S(8)
            cv.create_rectangle(bx0, by, x1, by + S(3), fill=M.BG3, outline="")
            cv.create_rectangle(bx0, by, bx0 + max(S(3), int((x1 - bx0) * s["plays"] / most)),
                                by + S(3), fill=M.ACCENT, outline="")
            yy += size + S(10)
        return yy + S(6) - y

    # ── Recently played ──────────────────────────────────────────
    def _stats_show_more(self, sync=False):
        st = M._store()
        if not st:
            return
        self._st_recent_n += RECENT_PAGE
        n = self._st_recent_n

        def apply(res):
            if res is None or not self._st_data:
                return
            self._st_data["recent"] = res
            self._st_data["recent_n"] = n
            self._stats_render()
        self._st_run(lambda: st.recent_plays(limit=n), apply, sync)

    def _stats_open_in_history(self, play):
        """Show the play on the History page, found through its search box."""
        self._show("HISTORY")
        var = getattr(self, "_hist_search", None)
        if var is not None:
            var.set(play.get("title") or play.get("artist") or "")

    def _stats_copy_song(self, play):
        text = f"{play.get('artist', '')} — {play.get('title', '')}".strip(" —")
        try:
            self._root.clipboard_clear()
            self._root.clipboard_append(text)
        except tk.TclError:
            return
        M.log(f"Copied  ·  {text}")

    def _st_draw_recent(self, cv, x0, x1, y):
        S = self._ss
        data = self._st_data
        plays = data.get("recent") or []
        now = datetime.datetime.now()
        cur_id = data.get("current_id")
        yy = y + S(8)
        size = S(40)
        row_h = size + S(14)
        body_b, small, small_b = self._f(M.FS_BODY, True), self._f(M.FS_SMALL), self._f(M.FS_SMALL, True)
        pad = S(8)
        for i, p in enumerate(plays):
            tag = f"strow{self._st_gen}_{i}"
            rx0, rx1 = x0 - pad, x1 + pad
            rw = rx1 - rx0
            bg_item = cv.create_image(rx0, yy, anchor="nw", tags=(tag,),
                                      image=self._pill_photo(rw, row_h, M.BG2, M.BG2, radius=S(8)))
            ty = yy + S(7)
            thumb = self._st_thumb(cv, x0, ty, p.get("album_art"), size, M.BG2, tags=(tag,))
            when = rel_time(p.get("played_at"), now)
            dur = "Now playing" if cur_id and p.get("id") == cur_id else fmt_duration(p.get("listened_ms"))
            rw_ = max(small.measure(when), small.measure(dur) if dur else 0)
            tx = x0 + size + S(12)
            room = x1 - tx - rw_ - S(14)
            cv.create_text(tx, ty + S(1), anchor="nw", fill=M.TEXT, font=body_b, tags=(tag,),
                           text=self._st_fit(p.get("title") or "Unknown", body_b, room))
            cv.create_text(tx, ty + body_b.metrics("linespace") + S(1), anchor="nw", fill=M.MUTED,
                           font=small, tags=(tag,),
                           text=self._st_fit(p.get("artist") or "", small, room))
            cv.create_text(x1, ty + S(2), anchor="ne", text=when, fill=M.TEXT2, font=small, tags=(tag,))
            if dur:
                cv.create_text(x1, ty + body_b.metrics("linespace") + S(1), anchor="ne", text=dur,
                               fill=M.ACCENT if dur == "Now playing" else M.MUTED,
                               font=small_b if dur == "Now playing" else small, tags=(tag,))
            self._st_row_hover(tag, p, bg_item, thumb, rw, row_h, size)
            cv.tag_bind(tag, "<Button-3>", lambda e, pl=p: self._stats_copy_song(pl))
            yy += row_h
        yy += S(8)
        if len(plays) >= data.get("recent_n", RECENT_PAGE):
            font = self._f(M.FS_SMALL, True)
            w = font.measure("Show more") + S(24)
            self._st_button(cv, (x0 + x1 - w) // 2, yy, "Show more", self._stats_show_more, "secondary")
            yy += S(28) + S(14)
        return yy - y

    def _st_row_hover(self, tag, play, bg_item, thumb, w, h, size):
        """Whole-row hover fade (the cover is re-rounded onto the hover
        colour so its corners never show) and click-through to History."""
        cv, S = self.st_cv, self._ss
        url = play.get("album_art")
        st = {"t": 0.0}

        def paint(q):
            f = M._blend(M.BG2, M.HOVER_BG, q)
            cv.itemconfigure(bg_item, image=self._pill_photo(w, h, f, M.BG2, radius=S(8)))
            ph = self._st_thumb_photo(url, size, f) if url else None
            if ph is not None:
                cv.itemconfigure(thumb, image=ph)
            elif not url or url not in self._st_art:
                cv.itemconfigure(thumb, image=self._pill_photo(size, size, M.BG3, f, radius=S(6)))

        def fade(to):
            a = st["t"]
            def apply(e):
                st["t"] = a + (to - a) * e
                paint(round(st["t"] * 6) / 6)
            self._animate(f"hover:{tag}", 120, apply)

        self._st_bind(tag, lambda: self._stats_open_in_history(play),
                      lambda: fade(1.0), lambda: fade(0.0))

    # ── Scrolling (same glide as Settings) ───────────────────────
    def _st_scroll_to(self, y, animate=True):
        cv = self.st_cv
        total = max(1, self._st_total)
        view = cv.winfo_height()
        maxy = max(0, total - view)
        # Whole pixels (yscrollincrement=1): see _set_scroll_to in Settings.
        y = float(int(round(max(0.0, min(float(maxy), float(y))))))
        self._st_target = y
        if not animate or not M.ANIMATIONS_ENABLED:
            cv.yview_moveto(y / total)
            self._st_draw_thumb()
            return
        if getattr(self, "_st_gliding", False):
            return
        self._st_gliding = True
        self._stats_hide_tip()

        def step():
            # Fractional yview_moveto steps rounded back to the same pixel near
            # the end, so the glide never finished and re-armed every 15 ms.
            cur = cv.canvasy(0)
            diff = self._st_target - cur
            if abs(diff) >= 1.0:
                move = int(round(diff * 0.25)) or (1 if diff > 0 else -1)
                cv.yview_scroll(move, "units")
                self._st_draw_thumb()
                if cv.canvasy(0) != cur:
                    self._schedule("statsscroll", 15, step)
                    return
            self._st_gliding = False
            self._st_target = cv.canvasy(0)
            self._st_draw_thumb()
        step()

    def _st_scroll_by(self, dy):
        base = self._st_target if getattr(self, "_st_gliding", False) else self.st_cv.canvasy(0)
        self._st_scroll_to(base + dy)

    def _st_draw_thumb(self):
        sb = getattr(self, "_st_sb", None)
        if sb is None:
            return
        try:
            total = max(1, self._st_total)
            view = max(1, self.st_cv.winfo_height())
            h = sb.winfo_height()
            top = self.st_cv.canvasy(0)
        except tk.TclError:
            return
        sb.delete("all")
        if total <= view:
            return
        hot = getattr(self, "_st_sb_hot", False)
        th = max(self._ss(28), h * view / total)
        ty = (h - th) * (top / max(1, total - view))
        w = self._ss(6) if hot else self._ss(4)
        x = (sb.winfo_width() - w) // 2
        self._rounded_rect(sb, x, ty, x + w, ty + th, w // 2,
                           fill=M.MUTED if hot else M.BG4, outline="")
