"""The Settings page, listening stats and theming.

Moved verbatim out of main.App (which inherits SettingsPage). Names that
belong to main are reached through M, the live main module, bound by
main at import time: palette colours and settings are rebound at
runtime, so they must be read from main on every use, never copied.
"""
import datetime
import os
import subprocess
import sys
import tkinter as tk
import tkinter.colorchooser as tkcolor

M = None   # the main module


class SettingsPage:

    # ── SETTINGS ──────────────────────────────────────────────────
    def _collapsible(self, parent, title, key):
        """Section header that folds its card away on click.

        Returns the card frame to pack the section's content into. Collapsed
        state is keyed by `key` and persisted (debounced) so the page reopens
        the way you left it — the main lever for taming the long settings
        scroll. Header padding is uniform here, which also gives every section
        a consistent rhythm (the old code hand-tuned pady per header)."""
        collapsed = key in getattr(self, "_collapsed_sections", set())
        header = tk.Frame(parent, bg=M.BG); header.pack(fill="x", pady=(6, 4))
        caret = tk.Label(header, text="▸" if collapsed else "▾", fg=M.MUTED, bg=M.BG,
                         font=self._f(7, True), cursor="hand2")
        caret.pack(side="left", padx=(0, 5))
        lbl = tk.Label(header, text=title, fg=M.MUTED, bg=M.BG, font=self._f(7, True),
                       cursor="hand2", anchor="w")
        lbl.pack(side="left", fill="x", expand=True)

        card = tk.Frame(parent, bg=M.BG2)
        if not collapsed:
            card.pack(fill="x", pady=(0, 10), after=header)

        def _toggle(_e=None):
            if card.winfo_ismapped():
                card.pack_forget(); caret.config(text="▸")
                self._collapsed_sections.add(key)
            else:
                card.pack(fill="x", pady=(0, 10), after=header)
                caret.config(text="▾")
                self._collapsed_sections.discard(key)
            M._cfg_set_soon("ui", "collapsed_sections",
                          ",".join(sorted(self._collapsed_sections)))
            if getattr(self, "_recalc_set_scroll", None):
                self._recalc_set_scroll()

        def _enter(_e): caret.config(fg=M.TEXT); lbl.config(fg=M.TEXT)
        def _leave(_e): caret.config(fg=M.MUTED); lbl.config(fg=M.MUTED)
        for w in (caret, lbl):
            w.bind("<Button-1>", _toggle)
            w.bind("<Enter>", _enter)
            w.bind("<Leave>", _leave)
        return card

    def _build_settings(self):
        p = tk.Frame(self._container, bg=M.BG); self._pages["SETTINGS"] = p

        container = tk.Frame(p, bg=M.BG); container.pack(fill="both", expand=True, padx=14, pady=(10,14))
        self._set_vsb = self._scrollbar(container)
        self.set_cv = tk.Canvas(container, bg=M.BG, highlightthickness=0, yscrollcommand=self._set_vsb.set)
        self.set_cv.pack(side="left", fill="both", expand=True)
        self._set_vsb.config(command=self.set_cv.yview)

        outer = tk.Frame(self.set_cv, bg=M.BG)
        self._set_hw = self.set_cv.create_window((0,0), window=outer, anchor="nw")

        def _update_set_scroll(e=None):
            self.set_cv.configure(scrollregion=self.set_cv.bbox("all"))
            if outer.winfo_reqheight() > self.set_cv.winfo_height():
                self._set_vsb.pack(side="right", fill="y")
                self._set_scroll_enabled = True
            else:
                self._set_vsb.pack_forget()
                self.set_cv.yview_moveto(0)
                self._set_scroll_enabled = False
        # Exposed so the collapsible-section helper can re-measure after a
        # section is folded/unfolded.
        self._recalc_set_scroll = _update_set_scroll

        outer.bind("<Configure>", _update_set_scroll)
        self.set_cv.bind("<Configure>",
            lambda e: (self.set_cv.itemconfig(self._set_hw, width=e.width), _update_set_scroll()))

        # ONE wheel handler for the whole page instead of binding it onto every
        # widget in the tree (the old approach did a recursive bind over ~150
        # widgets at build time). It only scrolls while Settings is the visible
        # page, so it never fights the History page's own wheel binding.
        def _on_mousewheel(e):
            if self._cur_page == "SETTINGS" and getattr(self, "_set_scroll_enabled", False):
                self.set_cv.yview_scroll(int(-1*(e.delta/120)), "units")
        self.set_cv.bind_all("<MouseWheel>", _on_mousewheel, add="+")

        # Per-section collapse state, remembered across launches.
        self._collapsed_sections = set(
            s for s in M._cfg_get("ui", "collapsed_sections", "").split(",") if s)

        # ── Section: Session Stats ─────────────────────────────────
        stats_card = self._collapsible(outer, "Listening stats", "stats")
        inner_s = tk.Frame(stats_card, bg=M.BG2); inner_s.pack(fill="x", padx=14, pady=10)
        self.lbl_stats_songs = tk.Label(inner_s, text="Songs played:  0",
                                        fg=M.TEXT2, bg=M.BG2, font=self._f(9), anchor="w")
        self.lbl_stats_songs.pack(fill="x")
        self.lbl_stats_time = tk.Label(inner_s, text="Listening time:  0m 0s",
                                       fg=M.TEXT2, bg=M.BG2, font=self._f(9), anchor="w")
        self.lbl_stats_time.pack(fill="x", pady=(4,0))
        # From the history database, so they survive restarts.
        self.lbl_stats_week = tk.Label(inner_s, text="", fg=M.TEXT2, bg=M.BG2,
                                       font=self._f(9), anchor="w", justify="left")
        self.lbl_stats_week.pack(fill="x", pady=(10,0))
        self.lbl_stats_all = tk.Label(inner_s, text="", fg=M.TEXT2, bg=M.BG2,
                                      font=self._f(9), anchor="w", justify="left")
        self.lbl_stats_all.pack(fill="x", pady=(4,0))
        self._refresh_stats()

        # ── Section: Behaviour (tray + blacklist + per-track offset) ─
        beh_card = self._collapsible(outer, "Behaviour", "behaviour")
        inner_b  = tk.Frame(beh_card, bg=M.BG2); inner_b.pack(fill="x", padx=14, pady=10)

        # Close-to-tray toggle (#12)
        row_ct = tk.Frame(inner_b, bg=M.BG2); row_ct.pack(fill="x", pady=(0,6))
        self._ct_btn = tk.Label(row_ct, text="", fg=M.ACCENT_FG, bg=M.ACCENT,
                                font=self._f(M.FS_MICRO, True), cursor="hand2",
                                padx=M.SP_MD, pady=M.SP_XS)
        self._ct_btn.pack(side="right")
        tk.Label(row_ct, text="Close hides to tray", fg=M.TEXT2, bg=M.BG2,
                 font=self._f(M.FS_BODY), anchor="w").pack(side="left", fill="x", expand=True)

        def _paint_ct():
            on = M.CLOSE_TO_TRAY
            self._ct_btn.config(text="On" if on else "Off",
                                bg=M.ACCENT if on else M.BG3,
                                fg=M.ACCENT_FG if on else M.MUTED)
        def _toggle_ct(_e=None):
            M.CLOSE_TO_TRAY = not M.CLOSE_TO_TRAY
            M._cfg_set("preferences", "close_to_tray", str(M.CLOSE_TO_TRAY).lower())
            _paint_ct()
            if M.CLOSE_TO_TRAY and not getattr(self, "_tray", None):
                M.log("Note: pystray not installed — close will minimise instead")
        self._ct_btn.bind("<Button-1>", _toggle_ct)
        _paint_ct()

        # Per-track lyric offset (#13)
        row_to = tk.Frame(inner_b, bg=M.BG2); row_to.pack(fill="x", pady=(0,6))
        self.lbl_track_off = tk.Label(row_to, text="global", fg=M.MUTED, bg=M.BG2,
                                      font=self._f(M.FS_SMALL))

        def _nudge_track_offset(delta):
            uri = getattr(M.state, "track_uri", "")
            if not uri:
                M.log("No track playing — per-track offset not saved")
                return
            M._set_track_offset_ms(uri, M._track_offset_ms(uri) + delta)
            self._refresh_track_offset()
        def _clear_track_offset(_e=None):
            uri = getattr(M.state, "track_uri", "")
            if uri:
                M._set_track_offset_ms(uri, None)
                self._refresh_track_offset()

        # side="right" stacks right-to-left, so iterate in reverse to get
        # "RESET  −250  value  +250" reading order on screen.
        b_clr = tk.Label(row_to, text="Reset", fg=M.MUTED, bg=M.BG3,
                         font=self._f(M.FS_MICRO, True), cursor="hand2",
                         padx=M.SP_SM, pady=M.SP_XS)
        b_clr.pack(side="right", padx=(M.SP_XS, 0))
        b_clr.bind("<Button-1>", _clear_track_offset)
        for label, delta in (("+250", 250), ("−250", -250)):
            b = tk.Label(row_to, text=label, fg=M.TEXT, bg=M.BG3,
                         font=self._f(M.FS_MICRO, True), cursor="hand2",
                         padx=M.SP_SM, pady=M.SP_XS)
            b.pack(side="right", padx=(M.SP_XS, 0))
            b.bind("<Button-1>", lambda e, d=delta: _nudge_track_offset(d))
        self.lbl_track_off.pack(side="right", padx=(M.SP_SM, M.SP_XS))
        tk.Label(row_to, text="Offset for this track", fg=M.TEXT2, bg=M.BG2,
                 font=self._f(M.FS_BODY), anchor="w").pack(side="left", fill="x", expand=True)
        self._refresh_track_offset()

        # Always on top
        row_top = tk.Frame(inner_b, bg=M.BG2); row_top.pack(fill="x", pady=(0, M.SP_XS + 2))
        self._top_set_btn = tk.Label(row_top, text="", fg=M.MUTED, bg=M.BG3,
                                     font=self._f(M.FS_MICRO, True), cursor="hand2",
                                     padx=M.SP_MD, pady=M.SP_XS)
        self._top_set_btn.pack(side="right")
        tk.Label(row_top, text="Always on top  ·  Ctrl+T", fg=M.TEXT2, bg=M.BG2,
                 font=self._f(M.FS_BODY), anchor="w").pack(side="left", fill="x", expand=True)

        def _paint_top_set():
            self._top_set_btn.config(text="On" if M.ALWAYS_ON_TOP else "Off",
                                     bg=M.ACCENT if M.ALWAYS_ON_TOP else M.BG3,
                                     fg=M.ACCENT_FG if M.ALWAYS_ON_TOP else M.MUTED)
        self._top_set_btn.bind("<Button-1>",
                               lambda e: (self._toggle_topmost(), _paint_top_set()))
        _paint_top_set()

        # Start minimised to tray
        row_sm = tk.Frame(inner_b, bg=M.BG2); row_sm.pack(fill="x", pady=(0, M.SP_XS + 2))
        sm_btn = tk.Label(row_sm, text="", fg=M.MUTED, bg=M.BG3,
                          font=self._f(M.FS_MICRO, True), cursor="hand2",
                          padx=M.SP_MD, pady=M.SP_XS)
        sm_btn.pack(side="right")
        tk.Label(row_sm, text="Start minimised to tray", fg=M.TEXT2, bg=M.BG2,
                 font=self._f(M.FS_BODY), anchor="w").pack(side="left", fill="x", expand=True)

        def _paint_sm():
            sm_btn.config(text="On" if M.START_MINIMIZED else "Off",
                          bg=M.ACCENT if M.START_MINIMIZED else M.BG3,
                          fg=M.ACCENT_FG if M.START_MINIMIZED else M.MUTED)
        def _toggle_sm(_e=None):
            M.START_MINIMIZED = not M.START_MINIMIZED
            M._cfg_set("preferences", "start_minimized", str(M.START_MINIMIZED).lower())
            _paint_sm()
        sm_btn.bind("<Button-1>", _toggle_sm)
        _paint_sm()

        # Lyric font size
        row_lf = tk.Frame(inner_b, bg=M.BG2); row_lf.pack(fill="x", pady=(0, M.SP_XS + 2))
        self.lbl_lyric_size = tk.Label(row_lf, text="", fg=M.MUTED, bg=M.BG2,
                                       font=self._f(M.FS_SMALL))

        def _paint_lf():
            self.lbl_lyric_size.config(
                text=("default" if M.LYRIC_FONT_BOOST == 0 else f"{M.LYRIC_FONT_BOOST:+d}"),
                fg=M.ACCENT if M.LYRIC_FONT_BOOST else M.MUTED)
        def _nudge_lf(delta):
            M.LYRIC_FONT_BOOST = max(-2, min(10, M.LYRIC_FONT_BOOST + delta))
            # Debounced: A+/A− can be tapped rapidly; coalesce the disk writes.
            M._cfg_set_soon("preferences", "lyric_font_boost", str(M.LYRIC_FONT_BOOST))
            try:
                self.lbl_lyric.config(font=self._lyric_font())
            except (AttributeError, tk.TclError):
                pass
            _paint_lf()
        for lbl, d in (("A+", 1), ("A−", -1)):
            b = tk.Label(row_lf, text=lbl, fg=M.TEXT, bg=M.BG3, font=self._f(M.FS_MICRO, True),
                         cursor="hand2", padx=M.SP_SM + 2, pady=M.SP_XS)
            b.pack(side="right", padx=(M.SP_XS, 0))
            b.bind("<Button-1>", lambda e, dd=d: _nudge_lf(dd))
        self.lbl_lyric_size.pack(side="right", padx=(M.SP_SM, M.SP_XS))
        tk.Label(row_lf, text="Lyric text size", fg=M.TEXT2, bg=M.BG2,
                 font=self._f(M.FS_BODY), anchor="w").pack(side="left", fill="x", expand=True)
        _paint_lf()

        # Discord diagnostics
        row_dx = tk.Frame(inner_b, bg=M.BG2); row_dx.pack(fill="x", pady=(M.SP_XS, M.SP_XS + 2))
        for lbl, cmd in (("Reconnect", self._reconnect_rpc), ("Test", self._test_presence)):
            b = tk.Label(row_dx, text=lbl, fg=M.TEXT2, bg=M.BG3, font=self._f(M.FS_MICRO, True),
                         cursor="hand2", padx=M.SP_MD, pady=M.SP_XS)
            b.pack(side="right", padx=(M.SP_SM, 0))
            b.bind("<Button-1>", lambda e, c=cmd: c())
            self._hoverable(b, fg=lambda: M.TEXT2, hover_fg=lambda: M.ACCENT, bg=lambda: M.BG3, hover_bg=lambda: M.ACCENT_SOFT)
        tk.Label(row_dx, text="Discord", fg=M.TEXT2, bg=M.BG2,
                 font=self._f(M.FS_BODY), anchor="w").pack(side="left", fill="x", expand=True)

        # Blacklist (#16)
        tk.Label(inner_b, text="Blacklist — one term per line; matches artist or title",
                 fg=M.MUTED, bg=M.BG2, font=self._f(7), anchor="w").pack(fill="x", pady=(4,2))
        # width=1 is deliberate. A tk.Text defaults to 80 columns, and pack()
        # will not shrink a widget below its requested size — so the default
        # forced the entire settings frame far wider than the 520 px window and
        # pushed every right-aligned control off the visible area. width=1 lets
        # fill="x" decide the real width.
        self._bl_txt = tk.Text(inner_b, bg=M.BG3, fg=M.TEXT, font=self._f(M.FS_SMALL),
                               height=4, width=1,
                               relief="flat", wrap="word", padx=6, pady=4,
                               insertbackground=M.TEXT)
        self._focus_ring(self._bl_txt)
        self._bl_txt.pack(fill="x")
        self._bl_txt.insert("1.0", "\n".join(M._BLACKLIST))

        def _save_blacklist(_e=None):
            raw = self._bl_txt.get("1.0", "end").strip()
            # configparser can't hold raw newlines in a value, so store them
            # escaped and unescape on load.
            M._cfg_set_soon("preferences", "blacklist", raw.replace("\n", "\\n"))
            M._BLACKLIST = M._load_blacklist()
            M.state.blacklisted = M._is_blacklisted(
                getattr(M.state, "artist", ""), getattr(M.state, "title", ""))
        # Auto-save: debounce while typing, and flush on focus-out — no button.
        self._bl_txt.bind("<KeyRelease>",
                          lambda e: self._schedule("blsave", 700, _save_blacklist))
        self._bl_txt.bind("<FocusOut>", _save_blacklist)
        tk.Label(inner_b, text="saves automatically", fg=M.MUTED, bg=M.BG2,
                 font=self._f(7)).pack(anchor="e", pady=(3,0))

        # ── Section: Appearance ────────────────────────────────────
        appear_card = self._collapsible(outer, "Appearance", "appearance")
        inner_a = tk.Frame(appear_card, bg=M.BG2); inner_a.pack(fill="x", padx=14, pady=10)

        # Dark/Light toggle — custom pill buttons (no ugly Tk radio circles)
        row_dm = tk.Frame(inner_a, bg=M.BG2); row_dm.pack(fill="x", pady=(0,6))
        tk.Label(row_dm, text="Theme", fg=M.TEXT2, bg=M.BG2, font=self._f(9), anchor="w").pack(side="left")

        self._theme_btns = {}  # "dark"/"light" → Label widget
        pill_frame = tk.Frame(row_dm, bg=M.BG3); pill_frame.pack(side="right")

        for label_text, key in [("Dark", "dark"), ("Light", "light")]:
            is_active = (key == "dark") == M._DARK_MODE
            b = tk.Label(pill_frame, text=label_text,
                         fg=M.TEXT if is_active else M.MUTED,
                         bg=M.BG3 if not is_active else M.BG2,
                         font=self._f(9, True),
                         cursor="hand2", padx=10, pady=4)
            b.pack(side="left")
            b.bind("<Button-1>", lambda e, k=key: self._set_theme(k))
            # Like the tabs, these are stateful: on <Leave> fade back to the
            # colour the pill's own selected/unselected state calls for, not
            # to a fixed resting colour.
            b.bind("<Enter>", lambda e, w=b: self._fade_colors(
                f"hover:{w}", w, 110, fg=M.ACCENT))
            b.bind("<Leave>", lambda e, w=b, k=key: self._fade_colors(
                f"hover:{w}", w, 110,
                fg=M.TEXT if (k == "dark") == M._DARK_MODE else M.MUTED))
            self._theme_btns[key] = b

        # Accent color picker
        row_ac = tk.Frame(inner_a, bg=M.BG2); row_ac.pack(fill="x")
        tk.Label(row_ac, text="Accent color", fg=M.TEXT2, bg=M.BG2, font=self._f(9), anchor="w").pack(side="left")
        self._accent_swatch = tk.Label(row_ac, bg=M.ACCENT, width=5, height=1,
                                       cursor="hand2", relief="groove", bd=2)
        self._accent_swatch.pack(side="right")
        self._accent_swatch.bind("<Button-1>", self._pick_accent)
        self._accent_swatch.bind("<Enter>", lambda e: self._accent_swatch.config(relief="solid"))
        self._accent_swatch.bind("<Leave>", lambda e: self._accent_swatch.config(relief="groove"))

        # Album tint: the lyric sheet takes its colours from the cover.
        row_at = tk.Frame(inner_a, bg=M.BG2); row_at.pack(fill="x", pady=(M.SP_SM, 0))
        self._tint_btn = tk.Label(row_at, text="", fg=M.MUTED, bg=M.BG3,
                                  font=self._f(M.FS_MICRO, True), cursor="hand2",
                                  padx=M.SP_MD, pady=M.SP_XS)
        self._tint_btn.pack(side="right")
        tk.Label(row_at, text="Colour the window from the album art", fg=M.TEXT2, bg=M.BG2,
                 font=self._f(M.FS_BODY), anchor="w").pack(side="left", fill="x", expand=True)

        def _paint_tint():
            on = M.ALBUM_TINT
            self._tint_btn.config(text="On" if on else "Off",
                                  bg=M.ACCENT if on else M.BG3,
                                  fg=M.ACCENT_FG if on else M.MUTED)
        def _toggle_tint(_e=None):
            M.ALBUM_TINT = not M.ALBUM_TINT
            M._cfg_set("preferences", "album_tint", str(M.ALBUM_TINT).lower())
            self._repaint_everything()
            _paint_tint()
        self._tint_btn.bind("<Button-1>", _toggle_tint)
        self._paint_album_tint_btn = _paint_tint
        _paint_tint()

        # Motion toggle. Animation is an accessibility question before it is a
        # taste one, and it doubles as the escape hatch on hardware where the
        # 30 fps progress bar is not free. Turning it off degrades every
        # transition to the instant snap this UI used to do — nothing becomes
        # unreachable or invisible.
        row_an = tk.Frame(inner_a, bg=M.BG2); row_an.pack(fill="x", pady=(M.SP_SM, 0))
        self._anim_btn = tk.Label(row_an, text="", fg=M.MUTED, bg=M.BG3,
                                  font=self._f(M.FS_MICRO, True), cursor="hand2",
                                  padx=M.SP_MD, pady=M.SP_XS)
        self._anim_btn.pack(side="right")
        tk.Label(row_an, text="Smooth animations", fg=M.TEXT2, bg=M.BG2,
                 font=self._f(M.FS_BODY), anchor="w").pack(side="left", fill="x", expand=True)

        def _paint_anim():
            on = M.ANIMATIONS_ENABLED
            self._anim_btn.config(text="On" if on else "Off",
                                  bg=M.ACCENT if on else M.BG3,
                                  fg=M.ACCENT_FG if on else M.MUTED)
        def _toggle_anim(_e=None):
            M.ANIMATIONS_ENABLED = not M.ANIMATIONS_ENABLED
            M._cfg_set("preferences", "animations", str(M.ANIMATIONS_ENABLED).lower())
            _paint_anim()
            # The progress tick reads the flag when it re-arms, so switching
            # off takes effect within one frame rather than one track.
            M.log(f"Smooth animations {'enabled' if M.ANIMATIONS_ENABLED else 'disabled'}")
        self._anim_btn.bind("<Button-1>", _toggle_anim)
        _paint_anim()

        # ── Section: Hotkeys ───────────────────────────────────────
        hotkey_card = self._collapsible(outer, "Global hotkeys", "hotkeys")
        inner_h = tk.Frame(hotkey_card, bg=M.BG2); inner_h.pack(fill="x", padx=14, pady=10)

        if not M.KEYBOARD_AVAILABLE:
            tk.Label(inner_h, text="Global hotkeys are unavailable on this system.",
                     fg=M.MUTED, bg=M.BG2, font=self._f(8), justify="left").pack(anchor="w")
        else:
            # Skip track
            row_sk = tk.Frame(inner_h, bg=M.BG2); row_sk.pack(fill="x", pady=(0,4))
            tk.Label(row_sk, text="Skip track", fg=M.TEXT2, bg=M.BG2, font=self._f(9), width=14, anchor="w").pack(side="left")
            self._skip_var = tk.StringVar(value=M._hotkey_skip_combo)
            ent_sk = tk.Entry(row_sk, textvariable=self._skip_var, bg=M.BG3, fg=M.TEXT,
                              insertbackground=M.TEXT, relief="flat", font=self._f(9), width=18)
            self._focus_ring(ent_sk); ent_sk.pack(side="left", padx=(M.SP_XS,0))

            # Skip instrumental
            row_si = tk.Frame(inner_h, bg=M.BG2); row_si.pack(fill="x", pady=(0,4))
            tk.Label(row_si, text="Skip instrumental", fg=M.TEXT2, bg=M.BG2, font=self._f(9), width=14, anchor="w").pack(side="left")
            self._skip_instr_var = tk.StringVar(value=M._hotkey_skip_instr_combo)
            ent_si = tk.Entry(row_si, textvariable=self._skip_instr_var, bg=M.BG3, fg=M.TEXT,
                              insertbackground=M.TEXT, relief="flat", font=self._f(9), width=18)
            self._focus_ring(ent_si); ent_si.pack(side="left", padx=(M.SP_XS,0))

            # Toggle RPC
            row_tg = tk.Frame(inner_h, bg=M.BG2); row_tg.pack(fill="x", pady=(0,4))
            tk.Label(row_tg, text="Toggle RPC", fg=M.TEXT2, bg=M.BG2, font=self._f(9), width=14, anchor="w").pack(side="left")
            self._toggle_var = tk.StringVar(value=M._hotkey_toggle_combo)
            ent_tg = tk.Entry(row_tg, textvariable=self._toggle_var, bg=M.BG3, fg=M.TEXT,
                              insertbackground=M.TEXT, relief="flat", font=self._f(9), width=18)
            self._focus_ring(ent_tg); ent_tg.pack(side="left", padx=(M.SP_XS,0))

            def _save_hotkeys():
                # _register_hotkeys replaces the whole set, releasing the old combos.
                M._hotkey_skip_combo       = self._skip_var.get().strip()
                M._hotkey_toggle_combo     = self._toggle_var.get().strip()
                M._hotkey_skip_instr_combo = self._skip_instr_var.get().strip()
                M._cfg_set("preferences", "hotkey_skip",       M._hotkey_skip_combo)
                M._cfg_set("preferences", "hotkey_toggle",     M._hotkey_toggle_combo)
                M._cfg_set("preferences", "hotkey_skip_instr", M._hotkey_skip_instr_combo)
                M._register_hotkeys(self)
                M.log("Hotkeys saved & re-registered")

            # Auto-save when you finish editing a field (Enter or focus-out),
            # rather than on a separate SAVE click. Saving per-keystroke would
            # try to register half-typed combos, so we wait for the edit to end.
            for _ent in (ent_sk, ent_si, ent_tg):
                _ent.bind("<Return>",   lambda e: _save_hotkeys())
                _ent.bind("<FocusOut>", lambda e: _save_hotkeys())
            tk.Label(inner_h, text="saves on Enter / when you click away",
                     fg=M.MUTED, bg=M.BG2, font=self._f(7)).pack(anchor="e", pady=(4,0))

        # ── Section: Startup ───────────────────────────────────────
        sys_card = self._collapsible(outer, "System", "system")
        inner_sy = tk.Frame(sys_card, bg=M.BG2); inner_sy.pack(fill="x", padx=14, pady=10)

        row_su = tk.Frame(inner_sy, bg=M.BG2); row_su.pack(fill="x")
        tk.Label(row_su, text="Launch at Windows startup", fg=M.TEXT2, bg=M.BG2,
                 font=self._f(9), anchor="w").pack(side="left")
        self._startup_var = tk.BooleanVar(value=M._get_startup_enabled())
        def _toggle_startup():
            M._set_startup_enabled(self._startup_var.get())
        tk.Checkbutton(row_su, variable=self._startup_var, bg=M.BG2, activebackground=M.BG2,
                       selectcolor=M.BG3, command=_toggle_startup).pack(side="right")

        row_sh = tk.Frame(inner_sy, bg=M.BG2); row_sh.pack(fill="x", pady=(6,0))
        tk.Label(row_sh, text="Remember history", fg=M.TEXT2, bg=M.BG2,
                 font=self._f(9), anchor="w").pack(side="left")
        self._save_hist_var = tk.BooleanVar(value=M.SAVE_HISTORY)
        def _toggle_save_hist():
            M.SAVE_HISTORY = self._save_hist_var.get()
            M._cfg_set("preferences", "save_history", str(M.SAVE_HISTORY).lower())
            M.log(f'Session history {"enabled" if M.SAVE_HISTORY else "disabled"}')
        tk.Checkbutton(row_sh, variable=self._save_hist_var, bg=M.BG2, activebackground=M.BG2,
                       selectcolor=M.BG3, command=_toggle_save_hist).pack(side="right")

        # Window position reset
        row_wp = tk.Frame(inner_sy, bg=M.BG2); row_wp.pack(fill="x", pady=(6,0))
        tk.Label(row_wp, text="Reset window position", fg=M.TEXT2, bg=M.BG2,
                 font=self._f(9), anchor="w").pack(side="left")
        rst_pos = tk.Label(row_wp, text="Center", fg=M.MUTED, bg=M.BG2,
                           font=self._f(7,True), cursor="hand2")
        rst_pos.pack(side="right")
        rst_pos.bind("<Button-1>", lambda e: self._center(force=True))
        self._hoverable(rst_pos, fg=lambda: M.MUTED, hover_fg=lambda: M.ACCENT)

        # ── Section: Discord RPC Behaviour ────────────────────────
        rpc_card = self._collapsible(outer, "Discord RPC behaviour", "rpc")
        inner_rpc = tk.Frame(rpc_card, bg=M.BG2); inner_rpc.pack(fill="x", padx=14, pady=10)

        # Feature 7 — paused state toggle
        row_ps = tk.Frame(inner_rpc, bg=M.BG2); row_ps.pack(fill="x", pady=(0,6))
        tk.Label(row_ps, text='Show "Paused" on Discord', fg=M.TEXT2, bg=M.BG2,
                 font=self._f(9), anchor="w").pack(side="left")
        self._paused_var = tk.BooleanVar(value=M.SHOW_PAUSED_RPC)
        def _toggle_paused_rpc():
            M.SHOW_PAUSED_RPC = self._paused_var.get()
            M._cfg_set("preferences", "show_paused_rpc", str(M.SHOW_PAUSED_RPC).lower())
            M.log(f'Paused RPC {"enabled" if M.SHOW_PAUSED_RPC else "disabled"}')
        tk.Checkbutton(row_ps, variable=self._paused_var, bg=M.BG2, activebackground=M.BG2,
                       selectcolor=M.BG3, command=_toggle_paused_rpc).pack(side="right")

        # Member-list text and track link (statusify_rpc). Both apply from
        # the next presence update; no reconnect needed.
        row_sd = tk.Frame(inner_rpc, bg=M.BG2); row_sd.pack(fill="x", pady=(0,6))
        tk.Label(row_sd, text='Member list shows the song, not the app name', fg=M.TEXT2, bg=M.BG2,
                 font=self._f(9), anchor="w").pack(side="left")
        self._status_song_var = tk.BooleanVar(
            value=M._rpc_mod.status_display_type == M._rpc_mod.STATUS_DISPLAY_DETAILS)
        def _toggle_status_song():
            on = self._status_song_var.get()
            M._rpc_mod.status_display_type = (M._rpc_mod.STATUS_DISPLAY_DETAILS if on
                                            else M._rpc_mod.STATUS_DISPLAY_NAME)
            M._cfg_set("preferences", "status_shows_song", str(on).lower())
        tk.Checkbutton(row_sd, variable=self._status_song_var, bg=M.BG2, activebackground=M.BG2,
                       selectcolor=M.BG3, command=_toggle_status_song).pack(side="right")

        row_lk = tk.Frame(inner_rpc, bg=M.BG2); row_lk.pack(fill="x", pady=(0,6))
        tk.Label(row_lk, text='Song title links to Spotify', fg=M.TEXT2, bg=M.BG2,
                 font=self._f(9), anchor="w").pack(side="left")
        self._link_var = tk.BooleanVar(value=M._rpc_mod.link_track)
        def _toggle_link():
            M._rpc_mod.link_track = self._link_var.get()
            M._cfg_set("preferences", "link_track", str(M._rpc_mod.link_track).lower())
        tk.Checkbutton(row_lk, variable=self._link_var, bg=M.BG2, activebackground=M.BG2,
                       selectcolor=M.BG3, command=_toggle_link).pack(side="right")

        row_lr = tk.Frame(inner_rpc, bg=M.BG2); row_lr.pack(fill="x", pady=(0,6))
        tk.Label(row_lr, text='Try LRCLIB when Spicy and Spotify have no lyrics', fg=M.TEXT2, bg=M.BG2,
                 font=self._f(9), anchor="w").pack(side="left")
        self._lrclib_var = tk.BooleanVar(value=M.LRCLIB_ENABLED)
        def _toggle_lrclib():
            M.LRCLIB_ENABLED = self._lrclib_var.get()
            M._cfg_set("preferences", "lrclib_fallback", str(M.LRCLIB_ENABLED).lower())
        tk.Checkbutton(row_lr, variable=self._lrclib_var, bg=M.BG2, activebackground=M.BG2,
                       selectcolor=M.BG3, command=_toggle_lrclib).pack(side="right")

        # Feature 5 — custom instrumental text
        row_it = tk.Frame(inner_rpc, bg=M.BG2); row_it.pack(fill="x")
        tk.Label(row_it, text="Instrumental text", fg=M.TEXT2, bg=M.BG2,
                 font=self._f(9), anchor="w").pack(anchor="w")
        row_it2 = tk.Frame(inner_rpc, bg=M.BG2); row_it2.pack(fill="x", pady=(2,0))
        self._instr_var = tk.StringVar(value=M.INSTRUMENTAL_TEXT)
        ent_it = tk.Entry(row_it2, textvariable=self._instr_var, bg=M.BG3, fg=M.TEXT,
                          insertbackground=M.TEXT, relief="flat", font=self._f(9))
        self._focus_ring(ent_it)
        ent_it.pack(side="left", fill="x", expand=True, padx=(0, M.SP_SM))
        def _save_instr(_e=None):
            M.INSTRUMENTAL_TEXT = self._instr_var.get() or "🎵 ─ ─ ─ ─ ─ ─ ─ ─ ─ 🎵"
            M._cfg_set_soon("preferences", "instrumental_text", M.INSTRUMENTAL_TEXT)
        # Auto-save on Enter / focus-out instead of a SAVE click.
        ent_it.bind("<Return>",   _save_instr)
        ent_it.bind("<FocusOut>", _save_instr)
        tk.Label(row_it2, text="auto", fg=M.MUTED, bg=M.BG2,
                 font=self._f(7)).pack(side="left")
        # ── Section: Discord Profiles ──────────────────────────────
        prof_card = self._collapsible(outer, "Discord profiles", "profiles")
        inner_pr = tk.Frame(prof_card, bg=M.BG2); inner_pr.pack(fill="x", padx=14, pady=10)

        tk.Label(inner_pr, text="Save multiple App IDs and switch between them.",
                 fg=M.MUTED, bg=M.BG2, font=self._f(8), anchor="w").pack(anchor="w", pady=(0,6))

        # Profile listbox
        lb_frame = tk.Frame(inner_pr, bg=M.BG2); lb_frame.pack(fill="x")
        self._prof_lb = tk.Listbox(lb_frame, bg=M.BG3, fg=M.TEXT2,
                                   selectbackground=M.ACCENT, selectforeground=M.ACCENT_FG,
                                   relief="flat", font=self._f(9), height=4,
                                   activestyle="none", bd=0)
        self._prof_lb.pack(fill="x")

        def _load_profiles():
            self._prof_lb.delete(0, "end")
            cfg = M._load_config()
            if not cfg.has_section("profiles"):
                cfg.add_section("profiles")
            for name, app_id in cfg.items("profiles"):
                marker = " ✓" if app_id == M.DISCORD_APP_ID else ""
                self._prof_lb.insert("end", f"{name}{marker}  —  {app_id}")
        _load_profiles()

        btn_row = tk.Frame(inner_pr, bg=M.BG2); btn_row.pack(fill="x", pady=(6,0))

        def _mk_btn(parent, txt, cmd):
            b = tk.Label(parent, text=txt, fg=M.MUTED, bg=M.BG2,
                         font=self._f(7,True), cursor="hand2", padx=8)
            b.pack(side="left", padx=(0,6))
            b.bind("<Button-1>", lambda e: cmd())
            self._hoverable(b, fg=lambda: M.MUTED, hover_fg=lambda: M.ACCENT)
            return b

        def _add_profile():
            dlg = tk.Toplevel(self.win); dlg.title("Add Profile")
            dlg.configure(bg=M.BG); dlg.resizable(False, False)
            dlg.geometry("340x200")
            dlg.transient(self.win); dlg.grab_set()

            tk.Label(dlg, text="Profile name:", fg=M.TEXT2, bg=M.BG, font=self._f(9)).pack(pady=(12,2))
            nv = tk.StringVar()
            ent_n = tk.Entry(dlg, textvariable=nv, bg=M.BG2, fg=M.TEXT, insertbackground=M.TEXT,
                             relief="flat", font=self._f(9))
            self._focus_ring(ent_n)
            ent_n.pack(fill="x", padx=20)
            ent_n.focus_set()

            tk.Label(dlg, text="App ID:", fg=M.TEXT2, bg=M.BG, font=self._f(9)).pack(pady=(8,2))
            av = tk.StringVar()
            self._focus_ring(
                tk.Entry(dlg, textvariable=av, bg=M.BG2, fg=M.TEXT, insertbackground=M.TEXT,
                         relief="flat", font=self._f(9))).pack(fill="x", padx=20)

            err = tk.Label(dlg, text="", fg=M.DANGER, bg=M.BG, font=self._f(M.FS_MICRO),
                           wraplength=300, justify="center")
            err.pack(pady=(4,0))

            def _ok():
                n = nv.get().strip(); a = av.get().strip()
                if not n or not a:
                    err.config(text="Both a name and an App ID are required")
                    return
                # Profile names become configparser option keys, so anything
                # the INI grammar treats as a delimiter has to go.
                if any(ch in n for ch in "=:[]\n"):
                    err.config(text="Name cannot contain  =  :  [  ]")
                    return
                if not a.isdigit() or len(a) < 16:
                    err.config(text="App ID must be a long numeric ID")
                    return
                M._cfg_set("profiles", n, a)
                _load_profiles()
                M.log(f"Profile saved  ·  {n}")
                dlg.destroy()

            # Keep a real reference to the button. This used to be an
            # unassigned tk.Label reached back through dlg.children["!label4"],
            # which is not even the SAVE label's auto-generated name — the
            # lookup raised KeyError every time the dialog opened and the
            # button was simply dead. Only the Return key ever worked.
            save_btn = tk.Label(dlg, text="Save", fg=M.ACCENT_FG, bg=M.ACCENT,
                                font=self._f(M.FS_MICRO, True), cursor="hand2",
                                padx=M.SP_MD, pady=M.SP_XS + 1)
            save_btn.pack(pady=(8,0))
            save_btn.bind("<Button-1>", lambda e: _ok())
            dlg.bind("<Return>", lambda e: _ok())
            dlg.bind("<Escape>", lambda e: dlg.destroy())

        def _del_profile():
            sel = self._prof_lb.curselection()
            if not sel: return
            text = self._prof_lb.get(sel[0])
            name = text.split(" ✓")[0].split("  —  ")[0].strip()
            cfg = M._load_config()
            if cfg.has_option("profiles", name):
                cfg.remove_option("profiles", name)
                M._save_config(cfg)
            _load_profiles()

        def _switch_profile():
            sel = self._prof_lb.curselection()
            if not sel: return
            text = self._prof_lb.get(sel[0])
            # Parse: "name [✓]  —  app_id"
            parts = text.split("  —  ")
            if len(parts) < 2: return
            new_id = parts[-1].strip()
            M.DISCORD_APP_ID = new_id
            M._cfg_set("preferences", "discord_app_id_active", new_id)
            # Update .env
            try:
                lines = open(M._ENV_PATH, encoding="utf-8").readlines()
                with open(M._ENV_PATH, "w", encoding="utf-8") as f:
                    written = False
                    for ln in lines:
                        if ln.startswith("DISCORD_APP_ID="):
                            f.write(f"DISCORD_APP_ID={new_id}\n"); written = True
                        else:
                            f.write(ln)
                    if not written:
                        f.write(f"DISCORD_APP_ID={new_id}\n")
            except Exception: pass
            _load_profiles()
            M.log(f"Switched Discord profile to: {new_id}")

        _mk_btn(btn_row, "Add",    _add_profile)
        _mk_btn(btn_row, "Delete", _del_profile)
        _mk_btn(btn_row, "Switch", _switch_profile)

        # Desktop shortcut. This used to open a SECOND settings card, also
        # headed "SYSTEM" — two identically-titled sections on one page, with
        # the startup/history toggles in one and this in the other. It belongs
        # in the existing SYSTEM card (inner_sy), so it is packed there.
        _NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        def _do_shortcut():
            app_dir  = M._APP_DIR

            if M._FROZEN:
                # A release build needs no launcher shim: the exe the user
                # downloaded is already the thing a shortcut should point at,
                # and it embeds its own icon. Running build_launcher.ps1 here
                # would try to compile a Python launcher for a machine that
                # need not have Python at all.
                exe_path = sys.executable
                ico_path = sys.executable
            else:
                exe_path = os.path.join(app_dir, "Statusify.exe")
                ico_path = os.path.join(M._RES_DIR, "statusify.ico")

                if not os.path.exists(exe_path):
                    self._log("Building Statusify.exe…")
                    try:
                        subprocess.run(
                            ["powershell.exe", "-NoProfile", "-NonInteractive",
                             "-ExecutionPolicy", "Bypass", "-File", "build_launcher.ps1"],
                            cwd=app_dir, check=True, capture_output=True,
                            timeout=120, creationflags=_NO_WINDOW)
                    except Exception as e:
                        self._log(f"Build failed: {e}")
                        self._set_error(f"Could not build Statusify.exe: {e}")
                        return

                if not os.path.exists(exe_path):
                    self._log("Build reported success but Statusify.exe is missing")
                    return

            desk = os.path.join(os.environ.get("USERPROFILE", ""), "Desktop")
            lnk  = os.path.join(desk, "Statusify.lnk")
            ps = (f"$s=(New-Object -COM WScript.Shell).CreateShortcut('{lnk}');"
                  f"$s.TargetPath='{exe_path}';$s.WorkingDirectory='{app_dir}';"
                  f"$s.IconLocation='{ico_path}';$s.Save()")
            try:
                subprocess.run(
                    ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
                    check=True, capture_output=True, timeout=30,
                    creationflags=_NO_WINDOW)
                self._log("Shortcut created on Desktop")
            except Exception as e:
                self._log(f"Shortcut failed: {e}")
                self._set_error(f"Could not create shortcut: {e}")

        row_sc = tk.Frame(inner_sy, bg=M.BG2); row_sc.pack(fill="x", pady=(6,0))
        tk.Label(row_sc, text="Desktop shortcut", fg=M.TEXT2, bg=M.BG2, font=self._f(9), anchor="w").pack(side="left")
        btn_sc = tk.Label(row_sc, text="Create shortcut", fg=M.ACCENT_FG, bg=M.ACCENT, 
                          font=self._f(7,True), cursor="hand2", padx=10, pady=4)
        btn_sc.pack(side="right")
        btn_sc.bind("<Button-1>", lambda e: _do_shortcut())

        # ── Section: Log ──────────────────────────────────────────
        # Moved off the lyric sheet: diagnostics are for when something is
        # wrong, not something to watch while listening.
        log_card = self._collapsible(outer, "Log", "log")
        lf = tk.Frame(log_card, bg=M.BG2); lf.pack(fill="x", padx=14, pady=10)
        self.log_txt = tk.Text(lf, bg=M.BG2, fg=M.TEXT2, height=12,
                               font=M.tkfont.Font(family="Consolas", size=8),
                               relief="flat", state="disabled", wrap="word", padx=8, pady=6)
        self.log_txt.pack(fill="x")
        for tag, col in [("g", M.ACCENT), ("m", M.MUTED), ("y", M.WARN), ("ts", M.MUTED)]:
            self.log_txt.tag_config(tag, foreground=col)

        # Wheel scrolling is handled by a single page-level binding set up at
        # the top of this method — no per-widget binding needed.
        self._recalc_set_scroll()

    def _refresh_stats(self, reschedule=True):
        """Update session stats labels.

        FREEZE BUG (fixed): this method re-armed itself with after(5000, ...)
        on EVERY call, but it is also called directly — once from
        _build_settings() and again for every ("stats",) event drained in
        _poll() (emitted on every track start / pause / resume). Each of those
        direct calls spawned an ADDITIONAL self-perpetuating 5 s timer chain
        that was never cancelled, so the number of concurrent chains grew
        monotonically for the whole session. After a few hours there were
        thousands of chains firing, each one appending a row to health.csv from
        the Tk thread — the main loop ended up doing nothing but disk I/O, the
        window stopped repainting/responding, and the event queue backed up
        (observed: 8000+ pending events, a 17 GB health.csv), while the asyncio
        backend thread happily kept the Discord RPC alive. Hence "GUI frozen,
        RPC still working".

        Fix: cancel any pending timer before arming a new one, so there is at
        most ONE chain, and let event-driven refreshes pass reschedule=False.
        """
        M._health_snapshot()
        total_secs = int(M._get_listen_time())
        mins, secs = divmod(total_secs, 60)
        hrs, mins  = divmod(mins, 60)
        if hrs:
            tstr = f"{hrs}h {mins}m {secs}s"
        else:
            tstr = f"{mins}m {secs}s"
        if hasattr(self, "lbl_stats_songs"):
            self.lbl_stats_songs.config(text=f"This session:  {M._session_songs} songs")
            self.lbl_stats_time.config(text=f"Listening time:  {tstr}")
            self._refresh_long_stats()
        if not reschedule:
            return
        # Named slot guarantees exactly one live chain no matter how many
        # callers invoke this method. See _schedule() for the full story.
        self._schedule("stats", 5000, self._refresh_stats)

    def _refresh_long_stats(self):
        """Last-7-days and all-time totals from the history database."""
        if not hasattr(self, "lbl_stats_week"):
            return
        st = M._store()
        if not st:
            self.lbl_stats_week.config(text="History is off — turn on \"Remember history\" for long-term stats")
            self.lbl_stats_all.config(text="")
            return
        def _fmt(label, d):
            h, m = divmod(int(d["listened_ms"] // 60000), 60)
            txt = f"{label}:  {d['plays']} plays  ·  {h}h {m}m"
            if d["top_artists"]:
                txt += "\n    top: " + ", ".join(f"{a} ({n})" for a, n in d["top_artists"])
            return txt
        try:
            week = st.stats(since=datetime.datetime.now() - datetime.timedelta(days=7))
            alltime = st.stats()
        except Exception as e:
            M.log(f"Stats query failed: {e}")
            return
        self.lbl_stats_week.config(text=_fmt("Last 7 days", week))
        self.lbl_stats_all.config(text=_fmt("All time", alltime))

    def _set_theme(self, key):
        """Set dark or light theme from the pill-button key."""
        M._DARK_MODE = (key == "dark")
        M._cfg_set("preferences", "dark_mode", str(M._DARK_MODE).lower())
        self._repaint_everything()
        self._highlight_theme_btn()

    def _repaint_everything(self):
        """Rebuild the palette (theme + accent + album tint) and push it onto
        every widget, the title bar, the scrollbars and the artwork."""
        M._repalette()
        self._apply_titlebar_theme()
        self._style_scrollbars()
        self._rebuild_all()
        self._paint_nav()
        self._retint_artwork()

    def _apply_album_tint(self, tint):
        """Recolour the window for a new cover. No-op when the tint (or the
        feature being off) means nothing on screen would change."""
        if tint == M._CUR_TINT:
            return
        M._CUR_TINT = tint
        if M.ALBUM_TINT:
            self._repaint_everything()

    def _retint_artwork(self):
        """Re-round artwork onto the new surface colour. Rounded corners are
        composited onto the colour behind them (Tk can't alpha-blend onto a
        canvas), so after a recolour the old corners would show as squares."""
        src = getattr(self, "_hero_src", None)
        if src is not None:
            self._show_hero_image(src)
        for row, e in list(getattr(self, "_hist_rows", [])):
            cv = getattr(row, "_statusify_thumb", None)
            if cv is not None and e.get("album_art"):
                self._load_thumb(cv, e["album_art"])

    def _highlight_theme_btn(self):
        """Update the Dark/Light pill visuals to reflect the current theme."""
        if not hasattr(self, "_theme_btns"):
            return
        dark = M._DARK_MODE
        for key, b in self._theme_btns.items():
            is_active = (key == "dark") == dark
            b.config(fg=M.TEXT if is_active else M.MUTED,
                     bg=M.BG2 if is_active else M.BG3)

    def _pick_accent(self, _event=None):
        color = tkcolor.askcolor(color=M.ACCENT, title="Choose accent color")[1]
        if color:
            M.USER_ACCENT = color
            M._cfg_set("preferences", "accent_color", color)
            self._repaint_everything()

    def _rebuild_all(self):
        """Recolour every widget in-place without destroying state."""
        # Build an exact before→after mapping from the palette snapshot taken
        # just before _apply_palette overwrote the globals.  Using the previous
        # *actual* values avoids any hash collision (e.g. dark BG2 == light TEXT
        # == "#111111" was previously ambiguous in a merged static dict).
        _remap = {
            M._PREV_BG:     M.BG,
            M._PREV_BG2:    M.BG2,
            M._PREV_BG3:    M.BG3,
            M._PREV_BG4:    M.BG4,
            M._PREV_MUTED:  M.MUTED,
            M._PREV_TEXT:   M.TEXT,
            M._PREV_TEXT2:  M.TEXT2,
            M._PREV_BORDER: M.BORDER,
            M._PREV_ACCENT: M.ACCENT,
            M._PREV_ACCENT_SOFT: M.ACCENT_SOFT,
            M._PREV_ACCENT_FG:   M.ACCENT_FG,
            M._PREV_HOVER_BG:    M.HOVER_BG,
            M._PREV_SHADOW:      M.SHADOW,
            M._PREV_DANGER:      M.DANGER,
            M._PREV_WARN:        M.WARN,
        }
        # A derived token can coincide with a base one (ACCENT_FG is often
        # exactly TEXT's white). Base colours are authoritative — re-assert
        # them last so a derived key can never shadow them.
        for _old, _new in ((M._PREV_BG, M.BG), (M._PREV_BG2, M.BG2), (M._PREV_BG3, M.BG3),
                           (M._PREV_MUTED, M.MUTED), (M._PREV_TEXT, M.TEXT),
                           (M._PREV_TEXT2, M.TEXT2), (M._PREV_ACCENT, M.ACCENT)):
            _remap[_old] = _new

        def _recolour(w):
            try:
                cur_bg = w.cget("bg")
                new_bg = _remap.get(cur_bg)
                if new_bg:
                    w.config(bg=new_bg)
            except tk.TclError:
                pass
            try:
                cur_fg = w.cget("fg")
                new_fg = _remap.get(cur_fg)
                if new_fg:
                    w.config(fg=new_fg)
            except tk.TclError:
                pass
            # Entry/Text focus rings (see _focus_ring). These are ordinary
            # palette colours living on different option names, so without
            # this every input kept its old border after a theme switch —
            # dark BORDER hairlines around white fields in light mode.
            for opt in ("highlightbackground", "highlightcolor",
                        "insertbackground"):
                try:
                    new = _remap.get(w.cget(opt))
                    if new:
                        w.config(**{opt: new})
                except tk.TclError:
                    pass
            for child in w.winfo_children():
                _recolour(child)

        _recolour(self.win)

        # Accent-coloured widgets need explicit update
        self._paint_nav()
        for fn in ("_paint_album_tint_btn",):
            try: getattr(self, fn)()
            except (AttributeError, tk.TclError): pass
        try: self._accent_swatch.config(bg=M.ACCENT)
        except (AttributeError, tk.TclError): pass

        # Log widget text tags
        try:
            self.log_txt.tag_config("g",  foreground=M.ACCENT)
            self.log_txt.tag_config("m",  foreground=M.MUTED)
            self.log_txt.tag_config("ts", foreground=M.MUTED)
        except (AttributeError, tk.TclError): pass

        # The lyric label fg might be ACCENT (active lyric) or MUTED — don't touch it.
        # Update canvas placeholder art to new colours
        try:
            if self._img is None:
                self._default_art()
        except (AttributeError, tk.TclError): pass

        # Repaint the Now-Playing progress bar so its fill uses the new ACCENT.
        # Colours live on persistent canvas items now, so they have to be
        # pushed explicitly — a redraw alone only moves them.
        try:
            self._repaint_progress_colors()
            self._redraw_progress()
        except Exception:
            pass
