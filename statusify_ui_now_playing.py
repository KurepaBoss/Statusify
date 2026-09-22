"""The Now Playing page: a lyric sheet over the album's colours.

The page is ONE canvas showing ONE image, rebuilt every frame with PIL:

    fluid background (statusify_fluid)    album colours drifting like thick water
    header layer                          art, title, artist, Discord pill
    lyric lines                           scroll up, blur and fade by distance
    footer layer + progress bar           delay, actions, connection status

Tk widgets can't be translucent, can't blur and can't sit on a moving
background, which is everything this look needs, so nothing here is a
widget. Controls are hit-tested regions of the image instead.

main.py and the other mixins still talk to the page through the names the
old Label-based page had (lbl_title.config(text=…), dot_dc.config(fg=…),
lbl_lyric.cget("text")…). Those are now _Slot objects: they keep the options
and ask for a redraw. Names that belong to main are reached through M, the
live main module; palette colours are rebound at runtime, so they're read
from M on every use, never copied.
"""
import time
import tkinter as tk

import statusify_fluid as fluid
from statusify_textrender import TextRenderer

try:
    from PIL import Image, ImageDraw, ImageFilter
except ImportError:          # main refuses to start without Pillow anyway
    Image = ImageDraw = ImageFilter = None

M = None   # the main module


class _Slot:
    """Keeps a former Label's options; any change asks the page to redraw."""

    def __init__(self, page, name, **opts):
        self._page, self._name = page, name
        self._o = {"text": "", "fg": "", "cursor": ""}
        self._o.update(opts)
        self._binds = {}
        self.on = False            # dots and pills: lit or not

    def config(self, **kw):
        if "fg" in kw and M is not None:
            self.on = kw["fg"] == M.ACCENT
        changed = any(self._o.get(k) != v for k, v in kw.items())
        self._o.update(kw)
        if changed:
            self._page._np_slot_changed(self._name, kw)
    configure = config

    def cget(self, key):
        if key == "bg":
            return M.BG
        return self._o.get(key, "")

    def bind(self, seq, fn, add=None):
        self._binds[seq] = fn

    def unbind(self, seq, funcid=None):
        self._binds.pop(seq, None)

    def fire(self, seq, event=None):
        fn = self._binds.get(seq)
        if fn:
            fn(event)
            return True
        return False


def _ease_out(t):
    """Quartic ease-out: quick departure, long soft landing."""
    return 1.0 - (1.0 - t) ** 4


def _clamp(v, lo=0.0, hi=1.0):
    return lo if v < lo else hi if v > hi else v


class NowPlayingPage:

    SHEET_LYRIC_PT = 20      # lyric lines, before the user's size boost
    LINE_MOVE_S = 0.72       # how long a line change glides
    LINE_STAGGER_S = 0.045   # each following line starts this much later
    HEADER_FADE_S = 0.45     # track change crossfade of the header
    FRAME_MS_IDLE = 40       # 25 fps: the water moves slowly
    FRAME_MS_BUSY = 16       # while something is gliding

    # ── Construction ─────────────────────────────────────────────
    def _build_now_playing(self):
        p = tk.Frame(self._container, bg=M.BG); self._pages["NOW PLAYING"] = p
        self.np_cv = tk.Canvas(p, bg=M.BG, highlightthickness=0, bd=0)
        self.np_cv.pack(fill="both", expand=True)
        self.canvas = self.np_cv          # old name, kept for callers
        self._np_item = self.np_cv.create_image(0, 0, anchor="nw")
        self._np_photo = None
        self._np_size = (0, 0)
        self._np_text = TextRenderer()
        self._fluid = fluid.FluidField()
        self._np_palette = []
        try:
            ppt = float(self._root.winfo_fpixels("1p"))
        except tk.TclError:
            ppt = 96 / 72
        self._np_ppt = ppt                 # pixels per point
        self._np_s = ppt / (96 / 72)       # UI scale, 1.0 at 96 dpi

        # Former labels (see _Slot).
        S = lambda n, **o: _Slot(self, n, **o)
        self.lbl_title = S("title", text="Waiting for Spotify…")
        self.lbl_artist = S("artist")
        self.lbl_info = S("info")
        self.lbl_prev, self.lbl_lyric, self.lbl_next = S("prev"), S("lyric", text="—"), S("next")
        self.lbl_delay = S("delay")
        self.lbl_err, self.lbl_rl, self.lbl_dropped = S("err"), S("rl"), S("dropped")
        self.dot_sp, self.dot_dc = S("dot_sp"), S("dot_dc")
        self._rpc_btn, self._top_btn = S("rpc"), S("top")

        self._img = None                   # current cover (PIL) or None
        self._hero_src = None
        self._np_hdr = None                # (layer, hits)
        self._np_hdr_prev = None           # layer fading out
        self._np_hdr_t0 = 0.0
        self._np_ftr = None
        self._np_ftr_key = None
        self._np_hits = []
        self._np_hover = None
        self._np_static = []               # plain/lyric-less lines, newest last
        self._ly = None                    # lyric model (see _np_lyric_model)
        self._ly_masks = {}
        self._sheet_idx = None
        self._np_prog_masks = {}
        self._np_mouse = (0, 0)
        self._np_drag = None               # seek-bar drag fraction
        self._np_pressed = (None, 0.0)     # (key, time) for press feedback
        self._np_play_override = None
        self._ly_user = 0.0                # manual lyric scroll, shown
        self._ly_user_target = 0.0         # … and where it's heading
        self._ly_user_t = 0.0
        self._ly_hover = None
        self._np_fluid_cache = None
        self._np_cost = None               # frame cost EMA, ms
        self._np_frames = 0
        self._np_auto_tier = 0
        self._np_last_sig = None
        self._np_last_busy = True
        self._np_want_frame = True

        cv = self.np_cv
        cv.bind("<Configure>", self._np_on_configure)
        cv.bind("<Motion>", self._np_on_motion)
        cv.bind("<Leave>", lambda e: self._np_set_hover(None))
        cv.bind("<Button-1>", self._np_on_click)
        cv.bind("<Double-Button-1>", self._np_on_double)
        cv.bind("<B1-Motion>", self._np_on_drag)
        cv.bind("<ButtonRelease-1>", self._np_on_release)
        cv.bind("<MouseWheel>", self._np_on_wheel)

        self._paint_rpc_btn()
        self._paint_topmost_btn()
        self._np_update_fluid_colors()

    # Kept for callers that size the old label font (Settings' A+/A−).
    def _lyric_font(self):
        self._np_relayout()
        return None

    def _S(self, v):
        return int(round(v * self._np_s))

    def _px(self, pt):
        return max(8, int(round(pt * self._np_ppt)))

    # ── Colours on the fluid ─────────────────────────────────────
    def _np_fg(self):
        """Text colour over the water: white on dark, ink on light."""
        return (255, 255, 255) if M._DARK_MODE else (18, 20, 26)

    def _np_rgba(self, alpha):
        r, g, b = self._np_fg()
        return (r, g, b, int(255 * alpha))

    @staticmethod
    def _hex(c):
        c = c.lstrip("#")
        return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))

    def _np_accent(self):
        """Accent that stays visible on the water: the palette ACCENT, pulled
        towards the text colour a little."""
        a = self._hex(M.ACCENT)
        f = self._np_fg()
        return tuple(int(a[i] * 0.8 + f[i] * 0.2) for i in range(3))

    def _np_update_fluid_colors(self):
        dark = M._DARK_MODE
        if M.ALBUM_TINT and self._np_palette:
            pal = self._np_palette
        else:
            # Neutral water: the theme's own surfaces with a trace of accent.
            pal = [self._hex(c) for c in (M.BG, M.BG4, M.BG3, M.ACCENT_SOFT, M.BG4)]
        self._fluid.set_colors(fluid.normalise(pal, dark, M.BG3), time.monotonic())
        self._np_invalidate()

    # ── Slots and invalidation ───────────────────────────────────
    def _np_invalidate(self, header=True, footer=True):
        if header:
            self._np_hdr = None
        if footer:
            self._np_ftr = None
        self._np_want_frame = True

    def _np_slot_changed(self, name, kw):
        if name == "title":
            # A new track: crossfade the header and start the lyrics fresh.
            if self._np_hdr is not None:
                self._np_hdr_prev = self._np_hdr[0]
                self._np_hdr_t0 = time.monotonic()
            self._np_static = []
            self._ly = None
            self._np_invalidate(footer=False)
        elif name in ("artist", "info", "rpc"):
            self._np_invalidate(footer=False)
        elif name == "lyric":
            if "text" in kw and not self._np_synced():
                t = (kw["text"] or "").strip()
                if not self._np_static or self._np_static[-1] != t:
                    self._np_static.append(t)
                    del self._np_static[:-6]
            self._np_want_frame = True
        elif name in ("prev", "next"):
            pass
        else:
            self._np_invalidate(header=False)

    # ── Tk events ────────────────────────────────────────────────
    def _np_on_configure(self, e):
        size = (max(1, e.width), max(1, e.height))
        if size != self._np_size:
            self._np_size = size
            self._np_relayout()
            self._np_render()

    def _np_relayout(self):
        self._ly = None
        self._ly_masks.clear()
        self._np_prog_masks.clear()
        self._np_invalidate()

    def _np_hit(self, x, y):
        for x1, y1, x2, y2, key in self._np_hits:
            if x1 <= x <= x2 and y1 <= y <= y2:
                return key
        return None

    def _np_set_hover(self, key):
        if key == self._np_hover:
            return
        old = self._np_hover
        self._np_hover = key
        try:
            self.np_cv.config(cursor="hand2" if key else "")
        except tk.TclError:
            pass
        # Hovering a lyric line or the seek bar only changes the frame, not
        # the cached header/footer layers.
        cheap = lambda k: k is None or k == "seek" or str(k).startswith("line:")
        if not (cheap(old) and cheap(key)):
            self._np_invalidate()
        self._np_render()

    def _np_on_motion(self, e):
        self._np_mouse = (e.x, e.y)
        self._np_set_hover(self._np_hit(e.x, e.y))
        if self._np_hover == "seek":
            self._np_render()          # the hover time follows the pointer

    def _np_seek_frac(self, x):
        W = self._np_size[0]
        pad = self._S(28)
        return _clamp((x - pad) / max(1, W - 2 * pad))

    def _np_on_click(self, e):
        key = self._np_hit(e.x, e.y)
        if key == "seek":
            self._np_drag = self._np_seek_frac(e.x)
            self._np_render()
            return
        if key and key.startswith("line:"):
            self._np_seek_line(int(key[5:]))
            return
        act = {
            "rpc": self._toggle_rpc_btn,
            "dec": lambda: self._np_nudge_delay(-100),
            "inc": lambda: self._np_nudge_delay(100),
            "mini": self._toggle_mini,
            "top": self._toggle_topmost,
            "copy": self._copy_current_lyric,
            "prev": lambda: self._np_transport("prev"),
            "play": lambda: self._np_transport("toggle"),
            "next": lambda: self._np_transport("next"),
            "err": lambda: self.lbl_err.fire("<Button-1>", e),
        }.get(key)
        if act:
            self._np_pressed = (key, time.monotonic())
            act()
            self._np_invalidate()
            self._np_render()

    def _np_on_drag(self, e):
        if self._np_drag is not None:
            self._np_drag = self._np_seek_frac(e.x)
            self._np_mouse = (e.x, e.y)
            self._np_render()

    def _np_on_release(self, e):
        if self._np_drag is None:
            return
        frac = self._np_seek_frac(e.x)
        self._np_drag = None
        dur = getattr(M.state, "duration_ms", 0) or 0
        if dur:
            self._np_seek(frac * dur)
        self._np_render()

    def _np_on_wheel(self, e):
        """Browse the lyrics. The sheet stops following the song while you
        read, then drifts back to the current line a few seconds later."""
        if self._ly is None:
            return
        items = self._ly["items"]
        step = -(e.delta / 120.0) * self._S(64)
        lo = items[0]["y"] - items[self._ly["idx"]]["y"]
        hi = items[-1]["y"] - items[self._ly["idx"]]["y"]
        self._ly_user_target = max(lo, min(hi, self._ly_user_target + step))
        self._ly_user_t = time.monotonic()
        self._np_render()

    def _np_seek(self, ms):
        """Seek Spotify, and move the page there at once."""
        ms = int(max(0, ms))
        if not M.seek_to(ms):
            self._set_error("Spotify isn't connected, so it can't be controlled from here")
            return False
        self._last_pos_ms = ms
        self._last_pos_mono = time.monotonic()
        self._update_sheet(force=True)
        return True

    def _np_seek_line(self, i):
        """Jump to lyric line i of the sheet (0 is the intro before line 1)."""
        if not self._np_synced():
            return
        st = M.state
        start = 0 if i <= 0 else st.synced[min(i, len(st.synced)) - 1]["startMs"]
        ly = self._ly
        if ly is not None:
            # Glide from where the reader is looking, not from the old line.
            now = time.monotonic()
            cur, focus = self._np_scroll_at(ly, ly["idx"], now, base=True)
            ly["from"] = ly["to"] = cur + self._ly_user
            ly["ffrom"] = ly["fto"] = focus
            ly["t0"] = now
        self._ly_user = self._ly_user_target = 0.0
        # The sheet shows position + offset, so land on the line's start.
        self._np_seek(max(0, start - M._track_offset_ms() + 40))

    def _np_playing(self):
        """Play state as shown: an optimistic flip after a click, until the
        bridge confirms (or 1.5 s pass)."""
        ov = self._np_play_override
        if ov and time.monotonic() < ov[1]:
            return ov[0]
        return bool(getattr(M.state, "is_playing", False))

    def _np_transport(self, action):
        if not M.player_command(action):
            self._set_error("Spotify isn't connected, so it can't be controlled from here")
            return
        if action == "toggle":
            now_playing = self._np_playing()
            if now_playing:
                # Freeze the estimate where it is, or it keeps running.
                self._last_pos_ms = int(self._estimate_pos_ms())
            self._np_play_override = (not now_playing, time.monotonic() + 1.5)

    def _np_on_double(self, e):
        if self._np_hit(e.x, e.y) == "delay":
            M.LYRIC_DELAY_MS = 0
            self._update_delay_label()

    def _np_nudge_delay(self, d):
        M.LYRIC_DELAY_MS = max(-5000, min(5000, M.LYRIC_DELAY_MS + d))
        self._update_delay_label()

    def _update_delay_label(self):
        s = M.LYRIC_DELAY_MS / 1000
        sign = "+" if s > 0 else ""
        self.lbl_delay.config(text=f"{sign}{s:.1f}s")
        M._cfg_set("preferences", "lyric_delay_ms", str(M.LYRIC_DELAY_MS))
        # Tracks with no per-track override resolve to the global delay and
        # cache that value, so changing the global has to drop the cache.
        M._invalidate_offset_cache()
        self._refresh_track_offset()
        self._update_sheet(force=True)
        M.log(f"Lyric delay set to {sign}{s:.1f}s")

    # ── Artwork ──────────────────────────────────────────────────
    def _default_art(self):
        """Placeholder shown until artwork arrives (or when there is none)."""
        self._hero_src = None
        self._img = None
        self._np_invalidate(footer=False)

    def _show_hero_image(self, img):
        self._img = img
        self._hero_src = img
        self._np_invalidate(footer=False)

    def _set_art(self, url):
        """Load the cover, recolour the window and the water from it.

        Fetch, tint and palette run on a worker thread; the palette is
        applied on the Tk thread, where the page lives."""
        self._art_token = url
        if not M.PIL_AVAILABLE or not url:
            self._np_palette = []
            self._default_art(); self._apply_album_tint(None)
            self._np_update_fluid_colors(); return

        def _fetch():
            img = M._fetch_art(url, M.HERO_ART_PX * 2)
            return img, M._dominant_tint(img), fluid.palette_from_image(img)

        def _apply(res):
            if getattr(self, "_art_token", None) != url:
                return      # skipped on since; a newer cover owns the window
            img, tint, pal = res if res else (None, None, [])
            self._np_palette = pal or []
            if img is None:
                self._default_art(); self._apply_album_tint(None)
            else:
                self._apply_album_tint(tint)
                self._show_hero_image(img)
            self._np_update_fluid_colors()

        fut = M.image_executor.submit(_fetch)
        def _done(f):
            try: res = f.result()
            except Exception: res = None
            self.win.after(0, lambda: _apply(res))
        fut.add_done_callback(_done)

    # ── Old progress-bar entry points (called from _rebuild_all) ──
    def _redraw_progress(self):
        self._np_want_frame = True

    def _repaint_progress_colors(self):
        """Theme / accent / tint changed: rebuild everything that's cached."""
        if not hasattr(self, "np_cv"):
            return
        try:
            self.np_cv.config(bg=M.BG)
        except tk.TclError:
            pass
        self._ly_masks.clear()
        self._np_update_fluid_colors()
        self._np_render()

    # ── Position ─────────────────────────────────────────────────
    def _estimate_pos_ms(self):
        """Best estimate of the current playback position in ms.

        `state.position_ms` is refreshed only on each WS 'position' ping, so
        between pings we add the wall-clock delta while playing. When a fresh
        ping arrives (position changed), we re-anchor."""
        pos = getattr(M.state, "position_ms", 0) or 0
        dur = getattr(M.state, "duration_ms", 0) or 0
        playing = getattr(M.state, "is_playing", False)
        if pos != self._last_pos_ms:
            self._last_pos_ms = pos
            self._last_pos_mono = time.monotonic()
        if dur:
            self._last_dur_ms = dur
        if playing and self._last_pos_mono is not None and self._last_dur_ms:
            advanced = (time.monotonic() - self._last_pos_mono) * 1000.0
            pos = min(self._last_dur_ms, self._last_pos_ms + advanced)
        return pos

    @staticmethod
    def _fmt_time(ms):
        try:
            s = int(ms) // 1000
        except Exception:
            return "0:00"
        return f"{s // 60}:{s % 60:02d}"

    # ── Lyric model ──────────────────────────────────────────────
    def _np_synced(self):
        st = M.state
        return st.lyrics_mode == "synced" and bool(st.synced)

    def _update_sheet(self, force=False):
        """Work out which line is current and start a glide when it changes.

        The lines around the playhead are also written to lbl_prev /
        lbl_lyric / lbl_next, which the copy action and mini mode read."""
        st = M.state
        if not self._np_synced():
            if self._sheet_idx is not None:
                self._sheet_idx = None
                self.lbl_prev._o["text"] = self.lbl_next._o["text"] = ""
            return
        pos = self._estimate_pos_ms() + M._track_offset_ms()
        idx = -1
        for i, e in enumerate(st.synced):
            if e["startMs"] <= pos:
                idx = i
            else:
                break
        key = (idx, bool(st.is_playing))
        if key == self._sheet_idx and not force:
            return
        self._sheet_idx = key
        lines = st.synced
        cur = lines[idx]["words"] if idx >= 0 else ""
        self.lbl_prev._o["text"] = lines[idx - 1]["words"] if idx >= 1 else ""
        self.lbl_lyric._o["text"] = cur or "♪"
        self.lbl_next._o["text"] = lines[idx + 1]["words"] if idx + 1 < len(lines) else ""
        self._np_want_frame = True

    def _np_lyric_source(self):
        """(key, texts, current index) for whatever the sheet should show."""
        if self._np_synced():
            st = M.state
            texts = ["• • •"] + [(e.get("words") or "").strip() or "• • •" for e in st.synced]
            idx = (self._sheet_idx[0] if self._sheet_idx else -1) + 1
            return ("synced", id(st.synced), len(st.synced)), texts, idx
        texts = [t if t and t not in ("—", "♪") else "• • •" for t in self._np_static] or ["• • •"]
        return ("static", tuple(texts)), texts, len(texts) - 1

    def _np_lyric_px(self):
        return self._px(max(12, self.SHEET_LYRIC_PT + M.LYRIC_FONT_BOOST))

    def _np_lyric_model(self, W):
        key, texts, idx = self._np_lyric_source()
        px = self._np_lyric_px()
        full_key = (key, W, px)
        ly = self._ly
        now = time.monotonic()
        if ly is None or ly["key"] != full_key:
            same_static = (ly is not None and key[0] == "static" and ly["key"][0][0] == "static"
                           and ly["key"][1:] == full_key[1:])
            prev_items = ly["items"] if ly is not None else []
            items = self._np_layout_lines(texts, W, px)
            ly = {"key": full_key, "items": items, "px": px,
                  "idx": idx, "from": items[idx]["y"], "to": items[idx]["y"],
                  "ffrom": idx, "fto": idx, "t0": now - 10, "born": now}
            if same_static and prev_items:
                # A new plain line: glide in from the previous one.
                ly["from"] = items[max(0, idx - 1)]["y"]
                ly["ffrom"] = idx - 1
                ly["t0"] = now
                ly["born"] = now - 10
            self._ly = ly
            self._ly_masks.clear()
            self._ly_user = self._ly_user_target = 0.0
        elif idx != ly["idx"]:
            # Continue from wherever the glide is right now.
            cur_scroll, cur_focus = self._np_scroll_at(ly, idx, now, base=True)
            jump = abs(idx - ly["idx"]) > 6
            ly["from"] = ly["items"][idx]["y"] if jump else cur_scroll
            ly["ffrom"] = idx if jump else cur_focus
            ly["to"] = ly["items"][idx]["y"]
            ly["fto"] = idx
            ly["idx"] = idx
            ly["t0"] = now
        return ly

    def _np_layout_lines(self, texts, W, px):
        """Wrap and measure every line; the line images themselves are drawn
        lazily, the first time a line comes on screen (_np_item_mask). A
        127-line song used to render all 127 on each track change, a visible
        hitch on a slow CPU."""
        TR = self._np_text
        pad = self._S(28)
        maxw = max(120, W - 2 * pad)
        lh = int(TR.line_height("bold", px) * 1.02)
        gap = int(px * 0.62)
        margin = self._S(10)      # room for the blur to spread
        items, y = [], 0
        for t in texts:
            lines = TR.wrap(t, "bold", px, maxw)
            h = lh * len(lines)
            w = int(max(TR.measure(ln, "bold", px) for ln in lines)) if lines else 0
            items.append({"text": t, "lines": lines, "mask": None, "blur": {}, "h": h,
                          "w": w, "y": y, "m": margin, "lh": lh, "maxw": maxw, "px": px})
            y += h + gap
        return items

    def _np_item_mask(self, it):
        if it["mask"] is None:
            m = it["m"]
            mask = Image.new("L", (it["maxw"] + 2 * m, it["h"] + 2 * m), 0)
            d = ImageDraw.Draw(mask)
            for k, ln in enumerate(it["lines"]):
                self._np_text.draw(d, (m, m + k * it["lh"]), ln, "bold", it["px"], 255)
            it["mask"] = mask
        return it["mask"]

    def _np_scroll_at(self, ly, i, now, base=False):
        """Scroll offset and focus position for line i at `now`. Lines below
        the current one start their glide a little later (a gentle wave)."""
        rel = i - ly["fto"]
        delay = 0.0 if base else _clamp(rel, 0, 5) * self.LINE_STAGGER_S
        t = _clamp((now - ly["t0"] - delay) / self.LINE_MOVE_S) if M.ANIMATIONS_ENABLED else 1.0
        e = _ease_out(t)
        return (ly["from"] + (ly["to"] - ly["from"]) * e,
                ly["ffrom"] + (ly["fto"] - ly["ffrom"]) * e)

    def _np_line_mask(self, ly, i, blur, alpha):
        """The line's mask at a blur amount (0–3) and opacity, cached."""
        bq = round(blur * 4) / 4
        aq = int(round(alpha * 48))
        key = (i, bq, aq)
        m = self._ly_masks.get(key)
        if m is not None:
            return m
        it = ly["items"][i]

        def level(n):
            if n == 0:
                return self._np_item_mask(it)
            b = it["blur"].get(n)
            if b is None:
                b = self._np_item_mask(it).filter(ImageFilter.GaussianBlur(self._S(1.6) * n))
                it["blur"][n] = b
            return b
        lo = int(bq)
        frac = bq - lo
        m = level(lo) if frac == 0 or lo >= 3 else Image.blend(level(lo), level(lo + 1), frac)
        if aq < 48:
            k = aq / 48.0
            m = m.point([int(v * k) for v in range(256)])
        if len(self._ly_masks) > 240:
            self._ly_masks.clear()
        self._ly_masks[key] = m
        return m

    USER_SCROLL_HOLD_S = 3.5   # manual browsing lasts this long after the last wheel

    def _np_draw_lyrics(self, frame, top, bottom, now):
        W = frame.size[0]
        ly = self._np_lyric_model(W)
        items = ly["items"]
        pad = self._S(28)
        anchor = top + int((bottom - top) * 0.30)
        fade = self._S(56)
        synced = self._np_synced()
        playing = self._np_playing() or not synced
        born = _clamp((now - ly["born"]) / 0.45) if M.ANIMATIONS_ENABLED else 1.0
        born_e = _ease_out(born)
        rise = int((1 - born_e) * self._S(14))
        fg = self._np_fg()
        busy = born < 1.0

        # Manual browsing: ease towards the wheel's target; after a pause
        # with the pointer elsewhere, drift back to the sung line.
        hovering_line = str(self._np_hover).startswith("line:")
        if (self._ly_user_target and now - self._ly_user_t > self.USER_SCROLL_HOLD_S
                and not hovering_line):
            self._ly_user_target = 0.0
        diff = self._ly_user_target - self._ly_user
        if abs(diff) > 0.5:
            self._ly_user += diff * (0.22 if M.ANIMATIONS_ENABLED else 1.0)
            busy = True
        else:
            self._ly_user = self._ly_user_target
        # 0 → following the song; 1 → browsing (every line sharp and readable)
        browse = _clamp(abs(self._ly_user) / self._S(40))

        rects = []
        for i, it in enumerate(items):
            scroll, focus = self._np_scroll_at(ly, i, now)
            if i >= ly["fto"] and (now - ly["t0"]) < self.LINE_MOVE_S + 0.3:
                busy = True
            y = anchor + it["y"] - int(round(scroll + self._ly_user)) + rise
            if y + it["h"] < top - fade or y > bottom + fade:
                continue
            d = i - focus
            ad = abs(d)
            if ad < 1:
                dim = 0.42 if d > 0 else 0.34
                alpha = 1.0 + (dim - 1.0) * ad
            elif d > 0:
                alpha = max(0.16, 0.42 - 0.07 * (ad - 1))
            else:
                alpha = max(0.08, 0.34 - 0.10 * (ad - 1))
            if not playing and ad < 1:
                alpha *= 0.62
            blur = min(3.0, ad * 1.15)
            if browse:
                alpha = alpha + (max(alpha, 0.5) - alpha) * browse
                blur *= 1.0 - browse
            hot = synced and self._np_hover == f"line:{i}"
            if hot:
                # Hovered: as bright and sharp as the line being sung.
                alpha, blur = max(alpha, 0.92), 0.0
            # Soft edges: lines melt away near the header and the footer.
            cy = y + it["h"] / 2
            alpha *= _clamp((cy - top) / fade) * _clamp((bottom - cy) / fade)
            alpha *= born_e
            if synced and top <= cy <= bottom:
                rects.append((pad - self._S(8), y - self._S(4),
                              pad + it["w"] + self._S(8), y + it["h"] + self._S(4), f"line:{i}"))
            if alpha <= 0.02:
                continue
            if hot:
                self._np_pill_on(frame, (pad - self._S(10), y - self._S(6),
                                         pad + it["w"] + self._S(10), y + it["h"] + self._S(6)), 0.09)
            mask = self._np_line_mask(ly, i, blur, alpha)
            x0, y0 = pad - it["m"], y - it["m"]
            # Clip to the lyric band.
            cut_t = max(0, top - y0)
            cut_b = max(0, (y0 + mask.size[1]) - bottom)
            if cut_t or cut_b:
                if cut_t + cut_b >= mask.size[1]:
                    continue
                mask = mask.crop((0, cut_t, mask.size[0], mask.size[1] - cut_b))
                y0 += cut_t
            frame.paste(fg, (x0, y0), mask)
        self._ly_rects = rects
        return busy

    def _np_pill_on(self, frame, box, alpha):
        """Translucent rounded highlight straight onto an RGB frame."""
        x1, y1, x2, y2 = [int(v) for v in box]
        w, h = max(1, x2 - x1), max(1, y2 - y1)
        key = ("hl", w, h)
        m = self._np_prog_masks.get(key)
        if m is None:
            m = self._np_rounded_mask((w, h), self._S(10))
            self._np_prog_masks[key] = m
        frame.paste(self._np_fg(), (x1, y1), m.point([int(v * alpha) for v in range(256)]))

    # ── Header ───────────────────────────────────────────────────
    def _np_rounded_mask(self, size, r):
        sc = 4
        m = Image.new("L", (size[0] * sc, size[1] * sc), 0)
        ImageDraw.Draw(m).rounded_rectangle((0, 0, size[0] * sc - 1, size[1] * sc - 1),
                                            radius=r * sc, fill=255)
        return m.resize(size, Image.LANCZOS)

    def _np_pill(self, layer, box, alpha):
        """A translucent rounded pill, anti-aliased (drawn at 4x)."""
        x1, y1, x2, y2 = [int(v) for v in box]
        w, h = max(1, x2 - x1), max(1, y2 - y1)
        mask = self._np_rounded_mask((w, h), h // 2 if h < 40 else self._S(10))
        if alpha < 1:
            mask = mask.point([int(v * alpha) for v in range(256)])
        fg = self._np_fg()
        tile = Image.new("RGBA", (w, h), fg + (0,))
        tile.putalpha(mask)
        layer.alpha_composite(tile, (x1, y1))

    def _np_build_header(self, W):
        TR, S = self._np_text, self._S
        pad = S(28)
        top = S(20)
        A = S(56)
        Hh = top + A + S(10)
        layer = Image.new("RGBA", (W, Hh), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        hits = []

        # Artwork, rounded, with its corners over the water itself.
        mask = self._np_rounded_mask((A, A), S(8))
        if self._img is not None:
            art = self._img.convert("RGB").resize((A, A), Image.LANCZOS).convert("RGBA")
            art.putalpha(mask)
            layer.alpha_composite(art, (pad, top))
        else:
            self._np_pill(layer, (pad, top, pad + A, top + A), 0.10)
            TR.draw(d, (pad + A // 2 - self._px(9) // 2, top + A // 2 - self._px(9) * 0.7),
                    "♫", "regular", self._px(12), self._np_rgba(0.45))

        # Discord pill, right-aligned.
        on = self._rpc_btn.on
        label = "On Discord" if on else "Not sharing"
        fpx = self._px(9.5)
        tw = TR.measure(label, "semibold", fpx)
        ph = S(28)
        pw = int(tw + S(34))
        px2 = W - pad
        px1 = px2 - pw
        py1 = top + (A - ph) // 2
        hov = self._np_hover == "rpc"
        self._np_pill(layer, (px1, py1, px2, py1 + ph), 0.20 if hov else 0.12)
        dot = S(7)
        dx, dy = px1 + S(12), py1 + (ph - dot) // 2
        d.ellipse((dx, dy, dx + dot, dy + dot),
                  fill=(self._np_accent() + (255,)) if on else self._np_rgba(0.35))
        TR.draw(d, (dx + dot + S(7), py1 + (ph - TR.line_height("semibold", fpx)) // 2),
                label, "semibold", fpx, self._np_rgba(0.95 if on else 0.6))
        hits.append((px1, py1, px2, py1 + ph, "rpc"))

        # Title, artist, lyric source.
        tx = pad + A + S(14)
        maxw = px1 - S(14) - tx
        tpx, apx, ipx = self._px(13), self._px(10.5), self._px(9)
        title = TR.ellipsize(self.lbl_title.cget("text") or "", "semibold", tpx, maxw)
        artist = TR.ellipsize(self.lbl_artist.cget("text") or "", "regular", apx, maxw)
        info = (self.lbl_info.cget("text") or "").lstrip("· ").strip()
        info = TR.ellipsize(info, "regular", ipx, maxw)
        lh_t = TR.line_height("semibold", tpx)
        lh_a = TR.line_height("regular", apx)
        lh_i = TR.line_height("regular", ipx) if info else 0
        block = lh_t + lh_a + lh_i
        y = top + (A - block) // 2
        TR.draw(d, (tx, y), title, "semibold", tpx, self._np_rgba(1.0))
        TR.draw(d, (tx, y + lh_t), artist, "regular", apx, self._np_rgba(0.72))
        if info:
            TR.draw(d, (tx, y + lh_t + lh_a), info, "regular", ipx, self._np_rgba(0.45))
        return layer, hits

    # ── Footer ───────────────────────────────────────────────────
    def _np_footer_key(self, W, pos, dur):
        pk, pt = self._np_pressed
        pressed = pk if time.monotonic() - pt < 0.16 else None
        return (W, self._fmt_time(pos), self._fmt_time(dur) if dur else "--:--",
                M.LYRIC_DELAY_MS, M.ALWAYS_ON_TOP, self.dot_sp.on, self.dot_dc.on,
                self.lbl_rl.cget("text"), self.lbl_dropped.cget("text"),
                self.lbl_err.cget("text"), bool(self.lbl_err._binds),
                self._np_hover if not str(self._np_hover).startswith("line:") else None,
                M._DARK_MODE, M.ACCENT, self._np_playing(), pressed)

    def _np_icon(self, kind, size, rgba):
        """Anti-aliased transport glyph (prev / next / play / pause), cached."""
        key = ("icon", kind, size, rgba)
        ic = self._np_prog_masks.get(key)
        if ic is not None:
            return ic
        sc = 4
        n = size * sc
        m = Image.new("L", (n, n), 0)
        d = ImageDraw.Draw(m)
        if kind == "play":
            d.polygon([(n * 0.28, n * 0.18), (n * 0.28, n * 0.82), (n * 0.84, n * 0.5)], fill=255)
        elif kind == "pause":
            d.rounded_rectangle((n * 0.24, n * 0.18, n * 0.42, n * 0.82), radius=n * 0.04, fill=255)
            d.rounded_rectangle((n * 0.58, n * 0.18, n * 0.76, n * 0.82), radius=n * 0.04, fill=255)
        else:
            # Skip: a triangle and a bar; prev is the mirror image.
            d.polygon([(n * 0.20, n * 0.20), (n * 0.20, n * 0.80), (n * 0.68, n * 0.5)], fill=255)
            d.rounded_rectangle((n * 0.68, n * 0.20, n * 0.80, n * 0.80), radius=n * 0.03, fill=255)
            if kind == "prev":
                m = m.transpose(Image.FLIP_LEFT_RIGHT)
        m = m.resize((size, size), Image.LANCZOS)
        ic = Image.new("RGBA", (size, size), rgba[:3] + (0,))
        ic.putalpha(m.point([int(v * rgba[3] / 255) for v in range(256)]))
        self._np_prog_masks[key] = ic
        return ic

    def _np_build_footer(self, W, pos, dur):
        TR, S = self._np_text, self._S
        pad = S(28)
        spx = self._px(9.5)
        mpx = self._px(8.5)
        lh_s = TR.line_height("regular", spx)
        err = self.lbl_err.cget("text") or ""
        err_lines = TR.wrap(err, "regular", spx, W - 2 * pad)[:2] if err else []

        # Top-down: seek bar, times, transport, controls, (error), status.
        bar_y = S(10)
        times_y = bar_y + S(10)
        tr_y = times_y + S(2)
        tr_h = S(44)
        ctl_y = tr_y + tr_h + S(10)
        ctl_h = S(32)
        err_y = ctl_y + ctl_h + S(10)
        stat_y = err_y + len(err_lines) * lh_s + (S(4) if err_lines else 0)
        Hf = stat_y + lh_s + S(14)
        layer = Image.new("RGBA", (W, Hf), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        hits = [(pad - S(6), bar_y - S(9), W - pad + S(6), bar_y + S(12), "seek")]
        hov = self._np_hover
        pk, pt = self._np_pressed
        pressed = pk if time.monotonic() - pt < 0.16 else None

        # Times under the bar.
        el = self._fmt_time(pos)
        tot = self._fmt_time(dur) if dur else "--:--"
        TR.draw(d, (pad, times_y), el, "regular", mpx, self._np_rgba(0.55))
        TR.draw(d, (W - pad - TR.measure(tot, "regular", mpx), times_y), tot,
                "regular", mpx, self._np_rgba(0.55))

        # Transport, centred: previous, play/pause (a filled disc), next.
        live = self.dot_sp.on
        cx = W // 2
        cy = tr_y + tr_h // 2
        big = S(44)
        small = S(22)
        gap = S(34)
        disc_r = big // 2 - (S(2) if pressed == "play" else 0)
        fg = self._np_fg()
        disc_a = (1.0 if hov == "play" else 0.92) * (1.0 if live else 0.4)
        d.ellipse((cx - disc_r, cy - disc_r, cx + disc_r, cy + disc_r),
                  fill=fg + (int(255 * disc_a),))
        ink = (0, 0, 0, 220) if M._DARK_MODE else (255, 255, 255, 235)
        glyph = "pause" if self._np_playing() else "play"
        gs = S(20) - (S(2) if pressed == "play" else 0)
        layer.alpha_composite(self._np_icon(glyph, gs, ink), (cx - gs // 2, cy - gs // 2))
        hits.append((cx - big // 2, cy - big // 2, cx + big // 2, cy + big // 2, "play"))
        for key, x in (("prev", cx - big // 2 - gap), ("next", cx + big // 2 + gap)):
            box = (x - S(18), cy - S(18), x + S(18), cy + S(18))
            if hov == key:
                self._np_pill(layer, box, 0.12)
            sz = small - (S(2) if pressed == key else 0)
            a = (1.0 if hov == key else 0.8) * (1.0 if live else 0.4)
            layer.alpha_composite(self._np_icon(key, sz, fg + (int(255 * a),)),
                                  (x - sz // 2, cy - sz // 2))
            hits.append(box + (key,))

        # Delay stepper: "Delay", then a chip with minus, value, plus.
        x = pad
        cy = ctl_y + ctl_h // 2
        TR.draw(d, (x, cy - lh_s // 2), "Delay", "regular", spx, self._np_rgba(0.55))
        x += int(TR.measure("Delay", "regular", spx)) + S(10)
        seg = S(32)
        val_w = S(60)
        chip_w = seg * 2 + val_w
        self._np_pill(layer, (x, ctl_y, x + chip_w, ctl_y + ctl_h), 0.10)
        for key, x1, x2 in (("dec", x, x + seg), ("inc", x + seg + val_w, x + chip_w)):
            if hov == key:
                self._np_pill(layer, (x1 + S(2), ctl_y + S(2), x2 - S(2), ctl_y + ctl_h - S(2)), 0.14)
            glyph = "−" if key == "dec" else "+"
            gpx = self._px(11)
            gw = TR.measure(glyph, "semibold", gpx)
            TR.draw(d, (x1 + (x2 - x1 - gw) / 2, cy - TR.line_height("semibold", gpx) / 2 - S(1)),
                    glyph, "semibold", gpx, self._np_rgba(0.9 if hov == key else 0.7))
            hits.append((x1, ctl_y, x2, ctl_y + ctl_h, key))
        sv = M.LYRIC_DELAY_MS / 1000
        vtxt = f"{'+' if sv > 0 else ''}{sv:.1f}s"
        vw = TR.measure(vtxt, "semibold", spx)
        vcol = (self._np_accent() + (255,)) if M.LYRIC_DELAY_MS else self._np_rgba(0.95)
        TR.draw(d, (x + seg + (val_w - vw) / 2, cy - lh_s / 2), vtxt, "semibold", spx, vcol)
        hits.append((x + seg, ctl_y, x + seg + val_w, ctl_y + ctl_h, "delay"))

        # Quiet actions, right-aligned: Mini, On top, Copy.
        rx = W - pad
        for key, label, lit in (("copy", "Copy", False), ("top", "On top", M.ALWAYS_ON_TOP),
                                ("mini", "Mini", False)):
            w = int(TR.measure(label, "semibold", spx)) + S(22)
            x1 = rx - w
            if hov == key or lit:
                self._np_pill(layer, (x1, ctl_y, rx, ctl_y + ctl_h), 0.16 if hov == key else 0.10)
            col = (self._np_accent() + (255,)) if lit else self._np_rgba(0.95 if hov == key else 0.62)
            TR.draw(d, (x1 + S(11), cy - lh_s / 2), label, "semibold", spx, col)
            hits.append((x1, ctl_y, rx, ctl_y + ctl_h, key))
            rx = x1 - S(4)

        # Error line (clickable when main bound a repair action to it).
        if err_lines:
            danger = self._hex(M.DANGER) + (255,)
            for k, ln in enumerate(err_lines):
                TR.draw(d, (pad, err_y + k * lh_s), ln, "regular", spx, danger)
            if self.lbl_err._binds:
                hits.append((pad, err_y, W - pad, err_y + len(err_lines) * lh_s, "err"))
                if hov == "err":
                    d.line((pad, err_y + len(err_lines) * lh_s, W - pad, err_y + len(err_lines) * lh_s),
                           fill=danger, width=1)

        # Connection status.
        x = pad
        dot = S(6)
        for label, slot in (("Spotify", self.dot_sp), ("Discord", self.dot_dc)):
            dy = stat_y + (lh_s - dot) // 2
            d.ellipse((x, dy, x + dot, dy + dot),
                      fill=(self._np_accent() + (255,)) if slot.on else self._np_rgba(0.3))
            x += dot + S(6)
            x += int(TR.draw(d, (x, stat_y), label, "regular", spx, self._np_rgba(0.55))) + S(16)
        right = " · ".join(t for t in (self.lbl_dropped.cget("text"), self.lbl_rl.cget("text")) if t)
        if right:
            rw = TR.measure(right, "regular", spx)
            TR.draw(d, (W - pad - rw, stat_y), right, "regular", spx,
                    self._hex(M.WARN) + (255,))
        return layer, hits, bar_y

    def _np_progress(self, frame, y, frac):
        """Anti-aliased rounded seek bar straight onto the frame. Thicker with
        a knob and a time readout while hovered or dragged."""
        W = frame.size[0]
        pad = self._S(28)
        active = self._np_drag is not None or self._np_hover == "seek"
        h = max(3, self._S(6 if active else 4))
        w = W - 2 * pad
        if w < 10:
            return
        y -= (h - self._S(4)) // 2
        if self._np_drag is not None:
            frac = self._np_drag
        key = (w, h)
        track = self._np_prog_masks.get(key)
        if track is None:
            track = self._np_rounded_mask((w, h), h // 2)
            self._np_prog_masks[key] = track
        fg = self._np_fg()
        frame.paste(fg, (pad, y), track.point([int(v * 0.2) for v in range(256)]))
        fw = int(round(w * frac))
        if fw >= 2:
            fill = self._np_rounded_mask((max(h, fw), h), h // 2)
            frame.paste(fg, (pad, y), fill.point([int(v * 0.92) for v in range(256)]))
        if active:
            kr = self._S(7)
            kx = pad + fw
            ky = y + h // 2
            k = self._np_rounded_mask((2 * kr, 2 * kr), kr)
            frame.paste(fg, (kx - kr, ky - kr), k)
            # Where a click would land (or where the drag is).
            dur = getattr(M.state, "duration_ms", 0) or 0
            if dur:
                mx = self._np_mouse[0] if self._np_drag is None else pad + fw
                t = self._fmt_time(self._np_seek_frac(mx) * dur)
                TR = self._np_text
                px = self._px(8.5)
                tw = TR.measure(t, "semibold", px)
                bx = int(max(pad, min(W - pad - tw - self._S(16), mx - tw / 2 - self._S(8))))
                by = y - self._S(30)
                self._np_pill_on(frame, (bx, by, bx + tw + self._S(16), by + self._S(22)), 0.9)
                d = ImageDraw.Draw(frame)
                ink = (0, 0, 0) if M._DARK_MODE else (255, 255, 255)
                TR.draw(d, (bx + self._S(8), by + (self._S(22) - TR.line_height("semibold", px)) / 2),
                        t, "semibold", px, ink)

    # ── Frame ────────────────────────────────────────────────────
    def _np_visible(self):
        try:
            return (self._cur_page == "NOW PLAYING" and not self._hidden
                    and self._root.state() != "iconic")
        except tk.TclError:
            return False

    def _np_frame_sig(self, W, H, now, tier, still):
        """Everything a frame depends on besides a running glide. The clock
        skips a frame whose signature matches the last one: most idle ticks
        change nothing visible (the bar moves a pixel every second or so)."""
        cache = self._np_fluid_cache
        max_age = 1e9 if still else (0.08, 0.15, 1e9)[tier]
        fluid_fresh = bool(cache) and now - cache[2] < max_age and not self._fluid.crossfading()
        dur = getattr(M.state, "duration_ms", 0) or 0
        pos = self._estimate_pos_ms()
        w = max(1, W - 2 * self._S(28))
        bar_px = int(w * _clamp(pos / dur)) if dur else 0
        return (W, H, cache[2] if fluid_fresh else now, bar_px, self._fmt_time(pos),
                self._np_hover, self._np_drag, self._np_mouse if self._np_hover == "seek" else None,
                id(self._np_hdr), id(self._np_ftr), self._sheet_idx, self._np_playing(),
                round(self._ly_user), self._ly_user_target, M._DARK_MODE)

    def _np_render(self, force=True):
        """Compose and show one frame. Returns True while something glides.

        force=False (the frame clock) skips the frame when nothing on it
        would differ from the last one."""
        if Image is None or not hasattr(self, "np_cv"):
            return False
        W, H = self._np_size
        if W < 60 or H < 60:
            return False
        now = time.monotonic()
        if not force and not self._np_want_frame and not self._np_last_busy:
            tier0 = self._np_tier()
            sig = self._np_frame_sig(W, H, now, tier0, (not M.ANIMATIONS_ENABLED) or tier0 >= 2)
            if sig == self._np_last_sig:
                return False
        else:
            sig = None
        t_start = time.perf_counter()
        anim = M.ANIMATIONS_ENABLED
        tier = self._np_tier()
        bg = self._hex(M.BG)
        still = (not anim) or tier >= 2
        # A still background is the same image every frame until the colours
        # or the size change, so it's rendered once and copied.
        # The moving background is also reused between refreshes: it drifts
        # a few pixels a second under a heavy blur, so ~12 updates a second
        # look the same as 60, and a lyric glide doesn't pay for it each frame.
        cache = self._np_fluid_cache
        ckey = ((W, H), bg, self._fluid._to, M._DARK_MODE, still)
        max_age = 1e9 if still else (0.08, 0.15, 1e9)[tier]
        if (cache and cache[0] == ckey and now - cache[2] < max_age
                and not (still and self._fluid.crossfading())):
            frame = cache[1].copy()
        else:
            frame = self._fluid.render((W, H), now, bg, self._S(18), self._S(26),
                                       motion_t=40.0 if still else None, dither=tier == 0)
            self._np_fluid_cache = (ckey, frame.copy(), now)
        busy = self._fluid.crossfading()

        # Header, crossfading on a track change.
        if self._np_hdr is None:
            self._np_hdr = self._np_build_header(W)
        hdr, hhits = self._np_hdr
        prev = self._np_hdr_prev
        t = _clamp((now - self._np_hdr_t0) / self.HEADER_FADE_S) if anim else 1.0
        if prev is not None and t < 1.0 and prev.size == hdr.size:
            e = _ease_out(t)
            a = hdr.getchannel("A").point([int(v * e) for v in range(256)])
            b = prev.getchannel("A").point([int(v * (1 - e)) for v in range(256)])
            p2 = prev.copy(); p2.putalpha(b)
            frame.paste(p2, (0, -int(e * self._S(6))), p2)
            h2 = hdr.copy(); h2.putalpha(a)
            frame.paste(h2, (0, int((1 - e) * self._S(6))), h2)
            busy = True
        else:
            self._np_hdr_prev = None
            frame.paste(hdr, (0, 0), hdr)

        # Footer.
        dur = getattr(M.state, "duration_ms", 0) or 0
        pos = self._estimate_pos_ms()
        fkey = self._np_footer_key(W, pos, dur)
        if self._np_ftr is None or fkey != self._np_ftr_key:
            self._np_ftr = self._np_build_footer(W, pos, dur)
            self._np_ftr_key = fkey
        ftr, fhits, bar_y = self._np_ftr
        fy = H - ftr.size[1]
        frame.paste(ftr, (0, fy), ftr)
        frac = _clamp(pos / dur) if dur > 0 else 0.0
        self._np_progress(frame, fy + bar_y, frac)

        # Lyrics between the two.
        busy = self._np_draw_lyrics(frame, hdr.size[1] + self._S(4), fy - self._S(4), now) or busy

        self._np_hits = (list(hhits) + [(x1, y1 + fy, x2, y2 + fy, k) for x1, y1, x2, y2, k in fhits]
                         + getattr(self, "_ly_rects", []))

        try:
            if self._np_photo is None or (self._np_photo.width(), self._np_photo.height()) != (W, H):
                self._np_photo = M.ImageTk.PhotoImage(frame)
                self.np_cv.itemconfigure(self._np_item, image=self._np_photo)
            else:
                self._np_photo.paste(frame)
        except tk.TclError:
            return False
        self._np_want_frame = False
        self._np_last_busy = busy
        self._np_last_sig = self._np_frame_sig(W, H, now, tier, still)
        self._np_measure((time.perf_counter() - t_start) * 1000.0)
        return busy

    # ── Adaptive quality ─────────────────────────────────────────
    # Tier 0 draws everything at 25/60 fps. Tier 1 (a frame costs over
    # ~14 ms) drops the dither and runs at 15/30 fps. Tier 2 (over ~24 ms, or
    # "Fastest" in Settings) keeps the background still and draws only when
    # something changes. The drop is automatic and sticky for the session;
    # a 2 GHz dual-core with no GPU lands in tier 1 or 2.
    TIER_MS = (14.0, 24.0)

    def _np_tier(self):
        mode = getattr(M, "RENDER_QUALITY", "auto")
        if mode == "high":
            return 0
        if mode == "low":
            return 2
        return self._np_auto_tier

    def _np_measure(self, ms):
        ema = self._np_cost = ms if self._np_cost is None else self._np_cost * 0.92 + ms * 0.08
        self._np_frames += 1
        if self._np_frames < 40 or getattr(M, "RENDER_QUALITY", "auto") != "auto":
            return
        want = 2 if ema > self.TIER_MS[1] else 1 if ema > self.TIER_MS[0] else 0
        if want > self._np_auto_tier:
            self._np_auto_tier = want
            self._np_frames = 0
            self._np_cost = None
            M.log(f"Lyric sheet: frames cost {ema:.0f} ms here, switching to "
                  f"{'still background' if want == 2 else 'lighter animation'}")

    def _tick_progress(self):
        """The page's frame clock.

        25 fps while the page is on screen (the water is slow, so that's
        plenty), 60 fps while a line or the header is gliding, and nothing
        at all while the page is hidden. With animations off, frames are
        drawn only when something changed or the clock ticks over a second."""
        interval = 500
        try:
            if self._np_visible():
                self._update_sheet()
                tier = self._np_tier() if M.ANIMATIONS_ENABLED else 2
                busy = self._np_render(force=False)
                if busy:
                    interval = (self.FRAME_MS_BUSY, 33, 33)[tier]
                elif self._np_hover == "seek" or self._np_drag is not None:
                    interval = 33
                else:
                    interval = (self.FRAME_MS_IDLE, 66, 250)[tier]
                # Wake exactly when the next lyric line starts, so a low frame
                # rate never makes a line late.
                interval = min(interval, self._np_ms_to_next_line())
        except Exception as e:
            M.log(f"Lyric sheet frame failed: {e}")
            interval = 1000
        self._schedule("progress", interval, self._tick_progress)

    def _np_ms_to_next_line(self):
        if not self._np_synced() or not self._np_playing():
            return 10_000
        pos = self._estimate_pos_ms() + M._track_offset_ms()
        for e in M.state.synced:
            if e["startMs"] > pos:
                return int(max(4, e["startMs"] - pos + 2))
        return 10_000

    def _start_rl_countdown(self, wait_secs):
        end_time = time.monotonic() + wait_secs
        def _tick():
            remaining = end_time - time.monotonic()
            if remaining > 0:
                self.lbl_rl.config(text=f"Rate limited · {remaining:.0f}s", fg=M.WARN)
                self._schedule("rlcountdown", 500, _tick)
            else:
                self.lbl_rl.config(text="")
                self._cancel("rlcountdown")
        self._cancel("rlcountdown")
        _tick()
