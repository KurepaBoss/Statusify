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

    # ── NOW PLAYING ───────────────────────────────────────────────
    def _build_now_playing(self):
        p = tk.Frame(self._container, bg=M.BG); self._pages["NOW PLAYING"] = p

        # ── Hero card: large album art + title/artist/info ───────────────
        # Every pad on this page is one of SP_XS…SP_XL. It previously mixed
        # 14/12/10/6/4/2 more or less at random, which is why nothing on the
        # page shared a left edge or a rhythm.
        card = tk.Frame(p, bg=M.BG2); card.pack(fill="x", padx=M.SP_LG, pady=(M.SP_MD, M.SP_XS))
        self.canvas = tk.Canvas(card, width=M.HERO_ART_PX, height=M.HERO_ART_PX,
                                bg=M.BG2, highlightthickness=0)
        self.canvas.pack(side="left", padx=M.SP_LG, pady=M.SP_LG); self._default_art()

        inf = tk.Frame(card, bg=M.BG2)
        inf.pack(side="left", fill="both", expand=True, padx=(0, M.SP_LG))
        # Centre the text block against the 120 px artwork instead of pushing
        # it down with a magic 18 px top pad — that only lined up at one font
        # size and drifted the moment the title wrapped to two lines.
        inf.pack_propagate(False)
        spacer_top = tk.Frame(inf, bg=M.BG2); spacer_top.pack(fill="both", expand=True)
        self.lbl_title  = tk.Label(inf, text="Waiting for Spotify...", fg=M.TEXT, bg=M.BG2,
                                   font=self._f(M.FS_HERO, True), anchor="w",
                                   wraplength=300, justify="left")
        self.lbl_title.pack(fill="x")
        self.lbl_artist = tk.Label(inf, text="", fg=M.TEXT2, bg=M.BG2,
                                   font=self._f(M.FS_LARGE), anchor="w")
        self.lbl_artist.pack(fill="x", pady=(M.SP_XS // 2, 0))
        self.lbl_info   = tk.Label(inf, text="", fg=M.MUTED, bg=M.BG2,
                                   font=self._f(M.FS_SMALL), anchor="w")
        self.lbl_info.pack(fill="x", pady=(M.SP_XS, 0))
        tk.Frame(inf, bg=M.BG2).pack(fill="both", expand=True)

        # ── Progress bar: track + fill, with elapsed / total times ───────
        # Sits directly under the hero card and shares its horizontal inset,
        # so the bar reads as belonging to the track above it.
        prog_outer = tk.Frame(p, bg=M.BG)
        prog_outer.pack(fill="x", padx=M.SP_LG, pady=(M.SP_SM, 0))
        self._prog_cv = tk.Canvas(prog_outer, height=self.PROG_H, bg=M.BG,
                                  highlightthickness=0)
        self._prog_cv.pack(fill="x")
        # NOTE: still no <Configure> binding here — see _redraw_progress.
        times = tk.Frame(prog_outer, bg=M.BG); times.pack(fill="x", pady=(M.SP_XS, 0))
        self._prog_elapsed = tk.Label(times, text="0:00", fg=M.MUTED, bg=M.BG,
                                      font=self._f(M.FS_MICRO), anchor="w")
        self._prog_elapsed.pack(side="left")
        self._prog_total   = tk.Label(times, text="--:--", fg=M.MUTED, bg=M.BG,
                                      font=self._f(M.FS_MICRO), anchor="e")
        self._prog_total.pack(side="right")
        # Draw the empty bar once; _tick_progress / _redraw_progress keep it current.
        self._redraw_progress()

        lbox = tk.Frame(p, bg=M.BG3); lbox.pack(fill="x", padx=M.SP_LG, pady=(M.SP_SM, M.SP_XS))
        tk.Label(lbox, text="NOW ON DISCORD", fg=M.MUTED, bg=M.BG3,
                 font=self._f(M.FS_MICRO, True)).pack(anchor="w", padx=M.SP_LG,
                                                    pady=(M.SP_MD, 0))
        self.lbl_lyric = tk.Label(lbox, text="—", fg=M.MUTED, bg=M.BG3,
                                  font=self._f(M.FS_TITLE + M.LYRIC_FONT_BOOST, True),
                                  wraplength=430, justify="left", anchor="w",
                                  pady=M.SP_SM + 2)
        self.lbl_lyric.pack(anchor="w", fill="x", padx=M.SP_LG, pady=(0, M.SP_MD))


        # ── Lyric delay control ───────────────────────────────────────
        delay_outer = tk.Frame(p, bg=M.BG2); delay_outer.pack(fill="x", padx=14, pady=(2,2))

        # Header row: label + RESET
        delay_header = tk.Frame(delay_outer, bg=M.BG2)
        delay_header.pack(fill="x", padx=12, pady=(8,4))
        tk.Label(delay_header, text="LYRIC DELAY", fg=M.MUTED, bg=M.BG2,
                 font=self._f(7,True)).pack(side="left")
        rst = tk.Label(delay_header, text="RESET", fg=M.MUTED, bg=M.BG2,
                       font=self._f(7), cursor="hand2")
        rst.pack(side="right")
        rst.bind("<Button-1>", lambda e: _reset_delay())
        self._hoverable(rst, fg=lambda: M.MUTED, hover_fg=lambda: M.ACCENT)

        # Control row: [hear first label] [−] [value] [+] [see first label]
        delay_ctrl = tk.Frame(delay_outer, bg=M.BG2)
        delay_ctrl.pack(fill="x", padx=12, pady=(0,8))

        def _btn(parent, txt, cmd):
            b = tk.Label(parent, text=txt, fg=M.TEXT2, bg=M.BG3,
                         font=self._f(11,True), cursor="hand2",
                         width=3, anchor="center", pady=1)
            b.pack(side="left", padx=(0,2))
            b.bind("<Button-1>", lambda e: cmd())
            self._hoverable(b, fg=lambda: M.TEXT2, hover_fg=lambda: M.ACCENT, bg=lambda: M.BG3, hover_bg=lambda: M.ACCENT_SOFT)
            return b

        def _dec_delay():
            M.LYRIC_DELAY_MS = max(-5000, M.LYRIC_DELAY_MS - 100)
            self._update_delay_label()
        def _inc_delay():
            M.LYRIC_DELAY_MS = min(5000, M.LYRIC_DELAY_MS + 100)
            self._update_delay_label()
        def _reset_delay():
            M.LYRIC_DELAY_MS = 0
            self._update_delay_label()

        # Left side: hear first
        hear_frame = tk.Frame(delay_ctrl, bg=M.BG2)
        hear_frame.pack(side="left", fill="y")
        tk.Label(hear_frame, text="◀ hear first", fg=M.MUTED, bg=M.BG2,
                 font=self._f(7), anchor="e").pack(side="left", padx=(0,6))
        _btn(delay_ctrl, "−", _dec_delay)

        # Center: value display — initialise from persisted value
        _init_delay_s = M.LYRIC_DELAY_MS / 1000
        _init_delay_sign = "+" if _init_delay_s > 0 else ""
        _init_delay_text = f"{_init_delay_sign}{_init_delay_s:.1f}s"
        _init_delay_fg   = M.ACCENT if M.LYRIC_DELAY_MS != 0 else M.TEXT
        self.lbl_delay = tk.Label(delay_ctrl, text=_init_delay_text, fg=_init_delay_fg, bg=M.BG,
                                  font=self._f(11,True), width=6, anchor="center",
                                  relief="flat", padx=4)
        self.lbl_delay.pack(side="left", padx=4)

        # Right side: see first
        _btn(delay_ctrl, "+", _inc_delay)
        tk.Label(delay_ctrl, text="see first ▶", fg=M.MUTED, bg=M.BG2,
                 font=self._f(7), anchor="w").pack(side="left", padx=(6,0))

        # ── Quick actions ─────────────────────────────────────────
        # Everything here was previously either hotkey-only or impossible.
        acts = tk.Frame(p, bg=M.BG); acts.pack(fill="x", padx=M.SP_LG, pady=(M.SP_SM, M.SP_XS))

        def _act(label, cmd, tip=None, accent=False):
            b = tk.Label(acts, text=label,
                         fg=M.ACCENT_FG if accent else M.TEXT2,
                         bg=M.ACCENT if accent else M.BG3,
                         font=self._f(M.FS_MICRO, True), cursor="hand2",
                         padx=M.SP_MD, pady=M.SP_XS + 1)
            b.pack(side="left", padx=(0, M.SP_SM))
            b.bind("<Button-1>", lambda e: cmd())
            if not accent:
                # Tint the chip's surface as well as its label. Recolouring
                # only the text left the button's own shape completely inert
                # under the pointer.
                self._hoverable(b, fg=lambda: M.TEXT2, hover_fg=lambda: M.ACCENT,
                                bg=lambda: M.BG3, hover_bg=lambda: M.ACCENT_SOFT)
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
            rest_fg=lambda: M.ACCENT_FG if M._rpc_enabled else M.MUTED,
            rest_bg=lambda: M.ACCENT if M._rpc_enabled else M.BG3,
            # Lift the accent toward its own foreground when armed, so the
            # primary action finally has some press-me feedback of its own.
            hov_fg=lambda: M.ACCENT_FG if M._rpc_enabled else M.ACCENT,
            hov_bg=lambda: (M._blend(M.ACCENT, M.ACCENT_FG, 0.18) if M._rpc_enabled
                            else M.ACCENT_SOFT),
        )
        self._paint_rpc_btn()
        _act("MINI", self._toggle_mini)
        self._top_btn = _act("ON TOP", self._toggle_topmost)
        _stateful_hover(
            self._top_btn,
            rest_fg=lambda: M.ACCENT if M.ALWAYS_ON_TOP else M.MUTED,
            rest_bg=lambda: M.BG3,
            hov_fg=lambda: M.ACCENT,
            hov_bg=lambda: M.ACCENT_SOFT,
        )
        self._paint_topmost_btn()
        _act("COPY", self._copy_current_lyric)

        sb = tk.Frame(p, bg=M.BG); sb.pack(fill="x", padx=M.SP_LG, pady=(M.SP_SM, M.SP_XS))
        self.dot_sp = tk.Label(sb, text="●", fg=M.MUTED, bg=M.BG, font=self._f(M.FS_MICRO)); self.dot_sp.pack(side="left")
        tk.Label(sb, text=" Spicetify", fg=M.MUTED, bg=M.BG, font=self._f(M.FS_SMALL)).pack(side="left")
        tk.Label(sb, text="   ", bg=M.BG).pack(side="left")
        self.dot_dc = tk.Label(sb, text="●", fg=M.MUTED, bg=M.BG, font=self._f(M.FS_MICRO)); self.dot_dc.pack(side="left")
        tk.Label(sb, text=" Discord RPC", fg=M.MUTED, bg=M.BG, font=self._f(M.FS_SMALL)).pack(side="left")
        self.lbl_rl = tk.Label(sb, text="", fg=M.MUTED, bg=M.BG, font=self._f(M.FS_SMALL)); self.lbl_rl.pack(side="right")

        # Second status row (#14, #15). The two dots above are binary: when
        # RPC drops you get a grey dot and have to open the log to find out
        # why. These two labels put the reason and the per-song dropped-line
        # count where you can actually see them.
        sb2 = tk.Frame(p, bg=M.BG); sb2.pack(fill="x", padx=14, pady=(0,4))
        self.lbl_err = tk.Label(sb2, text="", fg=M.DANGER, bg=M.BG, font=self._f(7),
                                anchor="w", wraplength=330, justify="left")
        self.lbl_err.pack(side="left", fill="x", expand=True)
        self.lbl_dropped = tk.Label(sb2, text="", fg=M.MUTED, bg=M.BG, font=self._f(7))
        self.lbl_dropped.pack(side="right")

        tk.Frame(p, bg=M.BORDER, height=1).pack(fill="x", padx=14, pady=(2,6))
        tk.Label(p, text="LOG", fg=M.MUTED, bg=M.BG, font=self._f(7,True)).pack(anchor="w", padx=14)
        lf = tk.Frame(p, bg=M.BG2); lf.pack(fill="both", expand=True, padx=14, pady=(3,14))
        self.log_txt = tk.Text(lf, bg=M.BG2, fg=M.TEXT2, font=tkfont.Font(family="Consolas", size=8),
                               relief="flat", state="disabled", wrap="word", padx=8, pady=6)
        self.log_txt.pack(fill="both", expand=True)
        for tag, col in [("g",M.ACCENT),("m",M.MUTED),("y",M.WARN),("ts",M.BORDER)]:
            self.log_txt.tag_config(tag, foreground=col)

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
        M.log(f"Lyric delay set to {sign}{s:.1f}s")


    def _default_art(self):
        """Placeholder shown until artwork arrives (or when there is none).

        Matches the rounded corner of the real artwork so the swap-in doesn't
        change the silhouette."""
        self.canvas.delete("all")
        self._rounded_rect(self.canvas, 0, 0, M.HERO_ART_PX - 1, M.HERO_ART_PX - 1,
                           M.HERO_ART_RADIUS, fill=M.BG3, outline=M.BORDER)
        self.canvas.create_text(M.HERO_ART_PX // 2, M.HERO_ART_PX // 2,
                                text="♫", fill=M.MUTED, font=self._f(20))

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
        if not M.PIL_AVAILABLE or not url: self._default_art(); return
        # Fetch on a worker thread — urllib.urlopen blocks for up to `timeout`
        # seconds and must NEVER run on the Tk main loop (it freezes the UI).
        # Round on the worker thread too — the 4× mask is the most expensive
        # part of this path and has no business running on the Tk loop.
        surface = M.BG2
        def _fetch():
            return M._round_image(M._fetch_art(url, M.HERO_ART_PX), M.HERO_ART_RADIUS, surface)
        def _apply(result):
            if result is None:
                self._default_art(); return
            try:
                self._img = M.ImageTk.PhotoImage(result)
                self.canvas.delete("all"); self.canvas.create_image(0,0, anchor="nw", image=self._img)
            except Exception:
                self._default_art()
        fut = M.image_executor.submit(_fetch)
        def _done(f):
            try: res = f.result()
            except Exception: res = None
            # PhotoImage MUST be created on the Tk thread.
            self.win.after(0, lambda: _apply(res))
        fut.add_done_callback(_done)
