"""The Now Playing page: hero card, lyric offset, progress bar and album art.

Moved verbatim out of main.App (which inherits NowPlayingPage). Names that
belong to main are reached through M, the live main module, bound by
main at import time: palette colours and settings are rebound at
runtime, so they must be read from main on every use, never copied.
"""
import tkinter as tk
import tkinter.font as tkfont

M = None   # the main module


class NowPlayingPage:

    # ── LYRICS (the lyric sheet) ──────────────────────────────────
    # Layout, top to bottom:
    #   [art] title / artist · source          [● On Discord]
    #
    #   previous line                        (muted)
    #   CURRENT LINE, LARGE                  (display face)
    #   next line                            (secondary)
    #
    #   ━━━━━━━━━━━━━━━░░░░░░░   1:38 / 6:26
    #   Delay [− 0.0s +]              Mini   On top   Copy
    #   ● Spicetify  ● Discord                     (errors)
    # The window's colours come from the cover (_apply_album_tint), so the
    # page itself stays typographic: no cards, no boxes, just the words.
    SHEET_LYRIC_PT = 21      # current line, before the user's size boost
    SHEET_SIDE_PT  = 11      # previous / next lines

    def _display_family(self):
        """Windows 11's display cut of Segoe when present, else Segoe UI."""
        fam = getattr(self, "_disp_family", None)
        if fam is None:
            try:
                have = set(tkfont.families(self._root))
            except tk.TclError:
                have = set()
            fam = next((f for f in ("Segoe UI Variable Display", "Segoe UI Semibold")
                        if f in have), "Segoe UI")
            self._disp_family = fam
        return fam

    def _lyric_font(self):
        size = max(12, self.SHEET_LYRIC_PT + M.LYRIC_FONT_BOOST)
        key = ("lyric", size)
        cache = self.__dict__.setdefault("_font_cache", {})
        f = cache.get(key)
        if f is None:
            f = tkfont.Font(family=self._display_family(), size=size, weight="bold")
            cache[key] = f
        return f

    def _build_now_playing(self):
        p = tk.Frame(self._container, bg=M.BG); self._pages["NOW PLAYING"] = p
        PAD = M.SP_XL

        # ── Track row ─────────────────────────────────────────────
        top = tk.Frame(p, bg=M.BG); top.pack(fill="x", padx=PAD, pady=(M.SP_LG, 0))
        self.canvas = tk.Canvas(top, width=M.HERO_ART_PX, height=M.HERO_ART_PX,
                                bg=M.BG, highlightthickness=0)
        self.canvas.pack(side="left"); self._default_art()

        self._rpc_btn = tk.Label(top, text="", font=self._f(M.FS_SMALL, True),
                                 cursor="hand2", padx=M.SP_MD, pady=M.SP_XS)
        self._rpc_btn.pack(side="right")
        self._rpc_btn.bind("<Button-1>", lambda e: self._toggle_rpc_btn())
        self._paint_rpc_btn()

        inf = tk.Frame(top, bg=M.BG); inf.pack(side="left", fill="x", expand=True, padx=(M.SP_MD, M.SP_MD))
        self.lbl_title = tk.Label(inf, text="Waiting for Spotify…", fg=M.TEXT, bg=M.BG,
                                  font=self._f(M.FS_LARGE + 1, True), anchor="w")
        self.lbl_title.pack(fill="x")
        sub = tk.Frame(inf, bg=M.BG); sub.pack(fill="x")
        self.lbl_artist = tk.Label(sub, text="", fg=M.TEXT2, bg=M.BG,
                                   font=self._f(M.FS_BODY), anchor="w")
        self.lbl_artist.pack(side="left")
        self.lbl_info = tk.Label(sub, text="", fg=M.MUTED, bg=M.BG,
                                 font=self._f(M.FS_SMALL), anchor="w")
        self.lbl_info.pack(side="left", padx=(M.SP_SM, 0))

        # ── Bottom block, packed before the sheet so the sheet takes
        # whatever height is left ─────────────────────────────────
        bottom = tk.Frame(p, bg=M.BG); bottom.pack(side="bottom", fill="x", padx=PAD, pady=(0, M.SP_XS))

        prog_outer = tk.Frame(bottom, bg=M.BG); prog_outer.pack(fill="x")
        self._prog_cv = tk.Canvas(prog_outer, height=self.PROG_H, bg=M.BG,
                                  highlightthickness=0)
        self._prog_cv.pack(fill="x")
        times = tk.Frame(prog_outer, bg=M.BG); times.pack(fill="x", pady=(M.SP_XS, 0))
        self._prog_elapsed = tk.Label(times, text="0:00", fg=M.MUTED, bg=M.BG,
                                      font=self._f(M.FS_MICRO), anchor="w")
        self._prog_elapsed.pack(side="left")
        self._prog_total = tk.Label(times, text="--:--", fg=M.MUTED, bg=M.BG,
                                    font=self._f(M.FS_MICRO), anchor="e")
        self._prog_total.pack(side="right")
        self._redraw_progress()

        # Delay + quiet actions
        ctl = tk.Frame(bottom, bg=M.BG); ctl.pack(fill="x", pady=(M.SP_MD, 0))
        tk.Label(ctl, text="Delay", fg=M.MUTED, bg=M.BG,
                 font=self._f(M.FS_SMALL)).pack(side="left", padx=(0, M.SP_SM))
        chip = tk.Frame(ctl, bg=M.BG3); chip.pack(side="left")

        def _step(label, cmd):
            b = tk.Label(chip, text=label, fg=M.TEXT2, bg=M.BG3, font=self._f(M.FS_LARGE, True),
                         cursor="hand2", padx=M.SP_SM, pady=0)
            b.pack(side="left")
            b.bind("<Button-1>", lambda e: cmd())
            self._hoverable(b, fg=lambda: M.TEXT2, hover_fg=lambda: M.ACCENT,
                            bg=lambda: M.BG3, hover_bg=lambda: M.BG4)
            return b

        def _dec_delay():
            M.LYRIC_DELAY_MS = max(-5000, M.LYRIC_DELAY_MS - 100); self._update_delay_label()
        def _inc_delay():
            M.LYRIC_DELAY_MS = min(5000, M.LYRIC_DELAY_MS + 100); self._update_delay_label()
        def _reset_delay(_e=None):
            M.LYRIC_DELAY_MS = 0; self._update_delay_label()

        _step("−", _dec_delay)
        s0 = M.LYRIC_DELAY_MS / 1000
        self.lbl_delay = tk.Label(chip, text=f"{'+' if s0 > 0 else ''}{s0:.1f}s",
                                  fg=M.ACCENT if M.LYRIC_DELAY_MS else M.TEXT, bg=M.BG3,
                                  font=self._f(M.FS_SMALL, True), width=5, anchor="center",
                                  cursor="hand2")
        self.lbl_delay.pack(side="left")
        # Double-click the value to reset — the old separate Reset link.
        self.lbl_delay.bind("<Double-Button-1>", _reset_delay)
        _step("+", _inc_delay)

        def _quiet(label, cmd):
            b = tk.Label(ctl, text=label, fg=M.MUTED, bg=M.BG, font=self._f(M.FS_SMALL, True),
                         cursor="hand2", padx=M.SP_SM)
            b.pack(side="right")
            b.bind("<Button-1>", lambda e: cmd())
            return b
        cp = _quiet("Copy", self._copy_current_lyric)
        self._hoverable(cp, fg=lambda: M.MUTED, hover_fg=lambda: M.TEXT)
        self._top_btn = _quiet("On top", self._toggle_topmost)
        self._top_btn.bind("<Enter>", lambda e: self._top_btn.config(fg=M.TEXT))
        self._top_btn.bind("<Leave>", lambda e: self._paint_topmost_btn())
        self._paint_topmost_btn()
        mn = _quiet("Mini", self._toggle_mini)
        self._hoverable(mn, fg=lambda: M.MUTED, hover_fg=lambda: M.TEXT)

        # Connection status + reasons
        sb = tk.Frame(bottom, bg=M.BG); sb.pack(fill="x", pady=(M.SP_MD, 0))
        self.dot_sp = tk.Label(sb, text="●", fg=M.MUTED, bg=M.BG, font=self._f(M.FS_MICRO)); self.dot_sp.pack(side="left")
        tk.Label(sb, text=" Spotify", fg=M.MUTED, bg=M.BG, font=self._f(M.FS_SMALL)).pack(side="left")
        self.dot_dc = tk.Label(sb, text="●", fg=M.MUTED, bg=M.BG, font=self._f(M.FS_MICRO)); self.dot_dc.pack(side="left", padx=(M.SP_MD, 0))
        tk.Label(sb, text=" Discord", fg=M.MUTED, bg=M.BG, font=self._f(M.FS_SMALL)).pack(side="left")
        self.lbl_rl = tk.Label(sb, text="", fg=M.MUTED, bg=M.BG, font=self._f(M.FS_SMALL)); self.lbl_rl.pack(side="right")
        self.lbl_dropped = tk.Label(sb, text="", fg=M.MUTED, bg=M.BG, font=self._f(M.FS_SMALL))
        self.lbl_dropped.pack(side="right", padx=(0, M.SP_SM))
        self.lbl_err = tk.Label(bottom, text="", fg=M.DANGER, bg=M.BG, font=self._f(M.FS_SMALL),
                                anchor="w", wraplength=440, justify="left")
        self.lbl_err.pack(fill="x")

        # ── The sheet ─────────────────────────────────────────────
        sheet = tk.Frame(p, bg=M.BG); sheet.pack(fill="both", expand=True, padx=PAD)
        # Grid, not pack, so the block can sit a little ABOVE centre (2:3
        # spacer weights) — optically centred text reads as sagging.
        sheet.grid_columnconfigure(0, weight=1)
        sheet.grid_rowconfigure(0, weight=2)
        sheet.grid_rowconfigure(4, weight=3)
        tk.Frame(sheet, bg=M.BG).grid(row=0, column=0, sticky="nsew")
        self.lbl_prev = tk.Label(sheet, text="", fg=M.MUTED, bg=M.BG,
                                 font=self._f(self.SHEET_SIDE_PT), anchor="w",
                                 justify="left", wraplength=440)
        self.lbl_prev.grid(row=1, column=0, sticky="ew")
        self.lbl_lyric = tk.Label(sheet, text="—", fg=M.MUTED, bg=M.BG,
                                  font=self._lyric_font(), anchor="w",
                                  justify="left", wraplength=440)
        self.lbl_lyric.grid(row=2, column=0, sticky="ew", pady=M.SP_MD)
        self.lbl_next = tk.Label(sheet, text="", fg=M.TEXT2, bg=M.BG,
                                 font=self._f(self.SHEET_SIDE_PT), anchor="w",
                                 justify="left", wraplength=440)
        self.lbl_next.grid(row=3, column=0, sticky="ew")
        tk.Frame(sheet, bg=M.BG).grid(row=4, column=0, sticky="nsew")

        def _rewrap(e):
            w = max(200, e.width - 4)
            if abs(w - getattr(self, "_sheet_wrap", 0)) > 6:
                self._sheet_wrap = w
                for lbl in (self.lbl_prev, self.lbl_lyric, self.lbl_next, self.lbl_err):
                    lbl.config(wraplength=w)
        sheet.bind("<Configure>", _rewrap)
        self._sheet_idx = None

    def _update_sheet(self, force=False):
        """Show the previous / current / next synced lines around the playhead.

        Driven from the progress tick and only touches the labels when the
        line index changes. Plain-text and lyric-less tracks fall back to the
        text the RPC loop publishes (the "line" event)."""
        st = M.state
        if st.lyrics_mode != "synced" or not st.synced:
            if self._sheet_idx is not None:
                self._sheet_idx = None
                self.lbl_prev.config(text=""); self.lbl_next.config(text="")
            return
        pos = self._estimate_pos_ms() + M._track_offset_ms()
        idx = -1
        for i, e in enumerate(st.synced):
            if e["startMs"] <= pos:
                idx = i
            else:
                break
        lines = st.synced
        cur = lines[idx]["words"] if idx >= 0 else ""
        # Paused, or before the first line / in an instrumental gap: dim.
        fg = M.TEXT if (cur and st.is_playing) else M.MUTED
        key = (idx, fg)
        if key == self._sheet_idx and not force:
            return
        self._sheet_idx = key
        prev = lines[idx - 1]["words"] if idx >= 1 else ""
        nxt = lines[idx + 1]["words"] if idx + 1 < len(lines) else ""
        if not cur:
            cur = "♪"
        try:
            self.lbl_prev.config(text=prev)
            self.lbl_lyric.config(text=cur, fg=fg)
            self.lbl_next.config(text=nxt)
        except tk.TclError:
            pass

    def _update_delay_label(self):
        s = M.LYRIC_DELAY_MS / 1000
        sign = "+" if s > 0 else ""
        self.lbl_delay.config(
            text=f"{sign}{s:.1f}s",
            fg=M.ACCENT if M.LYRIC_DELAY_MS != 0 else M.TEXT
        )
        M._cfg_set("preferences", "lyric_delay_ms", str(M.LYRIC_DELAY_MS))
        # Tracks with no per-track override resolve to the global delay and
        # cache that value, so changing the global has to drop the cache.
        M._invalidate_offset_cache()
        self._refresh_track_offset()
        self._update_sheet(force=True)
        M.log(f"Lyric delay set to {sign}{s:.1f}s")

    def _default_art(self):
        """Placeholder shown until artwork arrives (or when there is none)."""
        self._hero_src = None
        self._img = None
        self.canvas.delete("all")
        self.canvas.config(bg=M.BG)
        self._rounded_rect(self.canvas, 0, 0, M.HERO_ART_PX - 1, M.HERO_ART_PX - 1,
                           M.HERO_ART_RADIUS, fill=M.BG3, outline="")
        self.canvas.create_text(M.HERO_ART_PX // 2, M.HERO_ART_PX // 2,
                                text="♫", fill=M.MUTED, font=self._f(14))

    def _show_hero_image(self, img):
        """Round `img` onto the current page colour and show it. Cheap at this
        size (64 px), so it runs on the Tk thread — which also guarantees the
        corners match the palette that is on screen right now."""
        try:
            rounded = M._round_image(img, M.HERO_ART_RADIUS, M.BG)
            self._img = M.ImageTk.PhotoImage(rounded)
            self._hero_src = img
            self.canvas.config(bg=M.BG)
            self.canvas.delete("all")
            self.canvas.create_image(0, 0, anchor="nw", image=self._img)
        except Exception:
            self._default_art()

    # ── Progress bar ──────────────────────────────────────────────
    PROG_H = 4          # bar thickness in px; radius is half of this

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
            "track":   cv.create_rectangle(0, 0, 0, 0, fill=M.BG3, outline=""),
            "track_l": cv.create_oval(0, 0, 0, 0, fill=M.BG3, outline=""),
            "track_r": cv.create_oval(0, 0, 0, 0, fill=M.BG3, outline=""),
            "fill":    cv.create_rectangle(0, 0, 0, 0, fill=M.ACCENT, outline=""),
            "fill_l":  cv.create_oval(0, 0, 0, 0, fill=M.ACCENT, outline=""),
            "fill_r":  cv.create_oval(0, 0, 0, 0, fill=M.ACCENT, outline=""),
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
        dur = M.state.duration_ms
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
                self._prog_cv.itemconfigure(items[k], fill=M.BG3)
            for k in ("fill", "fill_l", "fill_r"):
                self._prog_cv.itemconfigure(items[k], fill=M.ACCENT)
            self._prog_cv.config(bg=M.BG)
        except tk.TclError:
            pass

    def _estimate_pos_ms(self):
        """Best estimate of the current playback position in ms.

        `state.position_ms` is refreshed only on each WS 'position' ping, so
        between pings we add the wall-clock delta while playing. When a fresh
        ping arrives (position changed), we re-anchor."""
        import time as _time
        pos = getattr(M.state, "position_ms", 0) or 0
        dur = getattr(M.state, "duration_ms", 0) or 0
        playing = getattr(M.state, "is_playing", False)
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
            dur = getattr(M.state, "duration_ms", 0) or 0
            playing = bool(getattr(M.state, "is_playing", False))
            if dur > 0:
                visible = (self._cur_page == "NOW PLAYING") and not self._hidden
                if visible:
                    self._redraw_progress()
                    self._update_sheet()
                    interval = 33 if (playing and M.ANIMATIONS_ENABLED) else 250
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
                self.lbl_rl.config(text=f"⏸ {remaining:.0f}s", fg=M.WARN)
                self._schedule("rlcountdown", 500, _tick)
            else:
                self.lbl_rl.config(text="")
                self._cancel("rlcountdown")
        # Arming the named slot cancels any countdown already running, so
        # back-to-back rate-limit events can't stack two tickers.
        self._cancel("rlcountdown")
        _tick()

    def _set_art(self, url):
        """Load the cover, recolour the window from it, then show it.

        The fetch and the tint extraction run on a worker thread. Back on the
        Tk thread the palette is applied FIRST and the art rounded after, so
        its corners are composited onto the colour actually behind them."""
        self._art_token = url
        if not M.PIL_AVAILABLE or not url:
            self._default_art(); self._apply_album_tint(None); return

        def _fetch():
            img = M._fetch_art(url, M.HERO_ART_PX)
            return img, M._dominant_tint(img)

        def _apply(res):
            if getattr(self, "_art_token", None) != url:
                return      # skipped on since; a newer cover owns the window
            img, tint = res if res else (None, None)
            if img is None:
                self._default_art(); self._apply_album_tint(None); return
            self._apply_album_tint(tint)
            self._show_hero_image(img)

        fut = M.image_executor.submit(_fetch)
        def _done(f):
            try: res = f.result()
            except Exception: res = None
            # PhotoImage MUST be created on the Tk thread.
            self.win.after(0, lambda: _apply(res))
        fut.add_done_callback(_done)
