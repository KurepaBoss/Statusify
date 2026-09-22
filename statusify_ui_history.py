"""The History page: list, search, lyrics panel and export.

Moved verbatim out of main.App (which inherits HistoryPage). Names that
belong to main are reached through M, the live main module, bound by
main at import time: palette colours and settings are rebound at
runtime, so they must be read from main on every use, never copied.
"""
import os
import tkinter as tk

M = None   # the main module


class HistoryPage:

    # ── HISTORY ───────────────────────────────────────────────────
    def _build_history(self):
        p = tk.Frame(self._container, bg=M.BG); self._pages["HISTORY"] = p

        head = tk.Frame(p, bg=M.BG); head.pack(fill="x", padx=M.SP_LG, pady=(M.SP_MD, M.SP_SM))
        # Not "SESSION HISTORY" — it is restored from disk and spans sessions.
        tk.Label(head, text="LISTENING HISTORY", fg=M.MUTED, bg=M.BG,
                 font=self._f(M.FS_SMALL, True)).pack(side="left")
        # There was no way to clear history at all — it just accumulated,
        # persisted to disk, and reloaded on every launch.
        clr = tk.Label(head, text="CLEAR", fg=M.MUTED, bg=M.BG,
                       font=self._f(M.FS_MICRO, True), cursor="hand2")
        clr.pack(side="right")
        clr.bind("<Button-1>", lambda e: self._clear_history())
        self._hoverable(clr, fg=lambda: M.MUTED, hover_fg=lambda: M.DANGER)

        # Feature 4: search bar
        sf = tk.Frame(p, bg=M.BG); sf.pack(fill="x", padx=M.SP_LG, pady=(0, M.SP_SM))
        self._hist_search = tk.StringVar()
        ent_s = tk.Entry(sf, textvariable=self._hist_search, bg=M.BG2, fg=M.TEXT2,
                         insertbackground=M.TEXT2, relief="flat",
                         font=self._f(M.FS_BODY), width=30)
        self._focus_ring(ent_s)
        ent_s.pack(fill="x", ipady=M.SP_XS)
        self._hist_search_entry = ent_s   # so Ctrl+F can focus it
        tk.Label(sf, text="🔍  search all history by title, artist, or lyrics   ·   Ctrl+F",
                 fg=M.MUTED, bg=M.BG, font=self._f(M.FS_MICRO)).pack(anchor="w", pady=(2,0))
        self._hist_search.trace_add("write", lambda *_: self._filter_history())

        outer = tk.Frame(p, bg=M.BG); outer.pack(fill="both", expand=True, padx=14, pady=(0,14))
        self._hist_vsb = tk.Scrollbar(outer, bg=M.BG3, troughcolor=M.BG, relief="flat", width=5, bd=0)
        self.hist_cv = tk.Canvas(outer, bg=M.BG, highlightthickness=0, yscrollcommand=self._hist_vsb.set)
        self.hist_cv.pack(side="left", fill="both", expand=True)
        self._hist_vsb.config(command=self.hist_cv.yview)
        self.hist_frm = tk.Frame(self.hist_cv, bg=M.BG)
        self._hw = self.hist_cv.create_window((0,0), window=self.hist_frm, anchor="nw")

        def _update_scroll(e=None):
            self.hist_cv.configure(scrollregion=self.hist_cv.bbox("all"))
            # Only show scrollbar and allow scrolling when content overflows
            content_h = self.hist_frm.winfo_reqheight()
            canvas_h  = self.hist_cv.winfo_height()
            if content_h > canvas_h:
                self._hist_vsb.pack(side="right", fill="y")
                self._scroll_enabled = True
            else:
                self._hist_vsb.pack_forget()
                self.hist_cv.yview_moveto(0)
                self._scroll_enabled = False

        self.hist_frm.bind("<Configure>", _update_scroll)
        self.hist_cv.bind("<Configure>",
            lambda e: (self.hist_cv.itemconfig(self._hw, width=e.width), _update_scroll()))

        def _on_mousewheel(e):
            if getattr(self, "_scroll_enabled", False):
                # One notch ≈ 3 text lines' worth of pixels, glided rather
                # than jumped. yview_scroll("units") teleported the canvas by
                # a whole row per notch, which on a list of 44 px thumbnails
                # meant the content visibly disappeared and reappeared
                # somewhere else with nothing linking the two positions.
                self._smooth_scroll(-(e.delta / 120.0) * M.SCROLL_NOTCH_PX)

        self.hist_cv.bind("<MouseWheel>", _on_mousewheel)

        def _bind_mw(widget):
            widget.bind("<MouseWheel>", _on_mousewheel)
            for child in widget.winfo_children():
                _bind_mw(child)
        self._bind_hist_mw = _bind_mw

        self.no_hist = tk.Label(self.hist_frm, text="Nothing played yet.",
                                fg=M.MUTED, bg=M.BG, font=self._f(9))
        self.no_hist.pack(pady=30)
        self._hist_rows = []  # list of (row_widget, entry_dict) for filtering
        self._bind_hist_mw(self.hist_frm)
        # Render restored history here, once the page exists. It used to be
        # called from __init__, which since the deferred page build runs
        # before this page is created — so it drew nothing, and the History
        # tab only ever showed tracks played in the current session.
        self._render_loaded_history()

    def _smooth_scroll(self, delta_px):
        """Glide the history canvas by delta_px using exponential approach.

        Deliberately not a fixed-duration tween: wheel notches arrive in
        bursts, and each one should extend the same glide rather than restart
        a new one. Keeping a target that the view chases means a fast flick
        accumulates into one long smooth travel, and reversing direction
        mid-scroll turns around immediately instead of finishing the old
        animation first."""
        cv = self.hist_cv
        try:
            total = max(1, self.hist_frm.winfo_reqheight())
            view  = max(1, cv.winfo_height())
        except tk.TclError:
            return
        max_frac = max(0.0, 1.0 - view / total)
        cur = cv.yview()[0]
        # Continue from the in-flight target if we're mid-glide, else from
        # wherever the view actually is.
        base = self._scroll_target if getattr(self, "_scroll_active", False) else cur
        target = max(0.0, min(max_frac, base + delta_px / total))
        self._scroll_target = target

        if not M.ANIMATIONS_ENABLED:
            cv.yview_moveto(target)
            return

        if getattr(self, "_scroll_active", False):
            return          # the chase loop below is already running

        self._scroll_active = True

        def _step():
            try:
                pos = cv.yview()[0]
            except tk.TclError:
                self._scroll_active = False
                return
            diff = self._scroll_target - pos
            if abs(diff) < 0.0008:
                try: cv.yview_moveto(self._scroll_target)
                except tk.TclError: pass
                self._scroll_active = False
                return
            try:
                cv.yview_moveto(pos + diff * 0.25)
            except tk.TclError:
                self._scroll_active = False
                return
            self._schedule("histscroll", 16, _step)

        _step()

    def _render_loaded_history(self):
        """Draw the entries restored from disk by _load_history().

        Nothing ever rendered them before: rows were only created in response
        to a ("history_add",) event, which fires solely for tracks played in
        the current session. So a user with a full history.json opened the
        History tab and was told "Nothing played yet" — while that same file
        was dutifully reloaded and re-persisted on every single launch.

        Only the newest MAX_RENDERED_ROWS get widgets; _trim_history_rows
        would destroy anything older on the spot anyway."""
        if not M.history:
            return
        for e in M.history[-M.MAX_RENDERED_ROWS:]:
            self._add_history_row(e)
        M.log(f"History restored  ·  {len(M.history)} tracks")

    def _filter_history(self):
        """Debounced: re-render the list for the current search query."""
        self._schedule("hist_search", 200, self._run_history_search)

    def _run_history_search(self):
        """Search the whole database, not just the rows on screen.

        Filtering used to show/hide the rendered rows, and only the newest
        MAX_RENDERED_ROWS exist as widgets — so anything older than the last
        60 tracks could never be found."""
        q = self._hist_search.get().strip()
        if q and M._HISTORY_STORE:
            try:
                entries = list(reversed(M._HISTORY_STORE.search(q, M.MAX_RENDERED_ROWS)))
            except Exception as e:
                M.log(f"History search failed: {e}")
                entries = []
        elif q:
            ql = q.lower()
            entries = [e for e in M.history if ql in e.get("title", "").lower()
                       or ql in e.get("artist", "").lower()]
        else:
            entries = M.history[-M.MAX_RENDERED_ROWS:]
        for row, _ in list(getattr(self, "_hist_rows", [])):
            try:
                row.destroy()
            except Exception:
                pass
        self._hist_rows = []
        for e in entries:
            self._add_history_row(e)
        if not entries:
            try:
                self.no_hist.pack(pady=30)
            except (AttributeError, tk.TclError):
                pass

    def _clear_history(self):
        """Wipe session history, its rendered rows, and the on-disk copy."""
        for row, _ in list(getattr(self, "_hist_rows", [])):
            try:
                row.destroy()
            except Exception:
                pass
        self._hist_rows = []
        M.history.clear()
        self._close_lyrics_panel()
        try:
            self.no_hist.pack(pady=30)
        except (AttributeError, tk.TclError):
            pass
        try:
            if M._HISTORY_STORE:
                M._HISTORY_STORE.clear()
            M._current_play["entry"] = None
        except Exception as e:
            M.log(f"Could not delete history: {e}")
        M.log("History cleared")

    def _trim_history_rows(self):
        """Destroy the oldest rendered rows beyond MAX_RENDERED_ROWS.

        MAX_HISTORY_ROWS is 500 and each row is a Frame plus ~5 children plus
        a decoded thumbnail — roughly 3,000 live Tk widgets at cap. Every
        layout pass, every <Configure>, and every _rebuild_all theme change
        walks all of them, so the History tab got progressively heavier the
        longer a session ran. The underlying `history` list is untouched (so
        search, persistence and lyric indices still cover everything) — this
        only bounds how many rows exist as widgets."""
        while len(self._hist_rows) > M.MAX_RENDERED_ROWS:
            old_row, _ = self._hist_rows.pop(0)
            try:
                old_row.destroy()   # also drops the thumbnail ref on the canvas
            except Exception:
                pass

    def _add_history_row(self, e):
        """Render one history entry. Takes the entry dict, not a list index —
        see _save_history for why indices can't be trusted here."""
        if not e: return
        # The History page may not be built yet (it is created lazily after the
        # window first paints). The entry is already in the `history` list via
        # _save_history, so _build_history's restore pass will render it — we
        # can safely skip rendering here until the widgets exist.
        if not hasattr(self, "hist_frm"): return

        # Hide the empty-state label once there's something to show.
        if not self._hist_rows:
            self.no_hist.pack_forget()

        row = tk.Frame(self.hist_frm, bg=M.BG2, cursor="hand2")
        row.pack(fill="x", pady=(0,2))

        # Thumbnail canvas. Its bg is the row colour, not BG3, so the rounded
        # artwork's corners land on the row rather than on a square patch.
        c = tk.Canvas(row, width=M.THUMB_PX, height=M.THUMB_PX, bg=M.BG2,
                      highlightthickness=0)
        c.pack(side="left", padx=M.SP_MD, pady=M.SP_MD)
        self._rounded_rect(c, 0, 0, M.THUMB_PX - 1, M.THUMB_PX - 1, M.THUMB_RADIUS,
                           fill=M.BG3, outline="")
        c.create_text(M.THUMB_PX // 2, M.THUMB_PX // 2, text="♫", fill=M.MUTED,
                      font=self._f(11))
        if M.PIL_AVAILABLE and e.get("album_art"):
            self.win.after(80, lambda cv=c, u=e["album_art"]: self._load_thumb(cv, u))

        inf = tk.Frame(row, bg=M.BG2); inf.pack(side="left", fill="both", expand=True)
        tk.Label(inf, text=e["title"],  fg=M.TEXT,  bg=M.BG2, font=self._f(9,True),
                 anchor="w", wraplength=260).pack(fill="x", pady=(10,0), padx=(0,6))
        tk.Label(inf, text=e["artist"], fg=M.TEXT2, bg=M.BG2, font=self._f(8),
                 anchor="w").pack(fill="x", padx=(0,6))
        src = "Spicy" if e["mode"]=="synced" else ("Plain" if e["mode"]=="plain" else "No lyrics")
        syn_len = len(e["synced"]) if e.get("synced") else 0
        pln_len = len(e["plain"]) if e.get("plain") else 0
        n = syn_len or pln_len
        tk.Label(inf, text=f"{src}  ·  {n} lines  ·  {e['time']}", fg=M.MUTED, bg=M.BG2,
                 font=self._f(7), anchor="w").pack(fill="x", pady=(0,10), padx=(0,6))

        if n > 0:
            btn = tk.Label(row, text="LYRICS ›", fg=M.MUTED, bg=M.BG2,
                           font=self._f(7,True), cursor="hand2", padx=M.SP_MD)
            btn.pack(side="right", padx=(0, M.SP_MD))
            self._hoverable(btn, fg=lambda: M.MUTED, hover_fg=lambda: M.ACCENT)
            # The row has always had cursor="hand2" across its whole width,
            # which promised a click target that only the small LYRICS label
            # actually honoured. Make the whole row do what it looks like it
            # does. (Bound before _hover_surface so the hover bindings, which
            # use add="+", don't have to care about ordering.)
            for _w in (row, inf, *inf.winfo_children()):
                _w.bind("<Button-1>", lambda ev, ent=e: self._show_lyrics(ent))
            c.bind("<Button-1>", lambda ev, ent=e: self._show_lyrics(ent))
            btn.bind("<Button-1>", lambda ev, ent=e: self._show_lyrics(ent))
        else:
            row.config(cursor="")   # nothing to open — don't advertise a click

        # Whole-row hover tint, so the pointer position is always legible in
        # a long list of visually identical rows.
        self._hover_surface(row, lambda: M.BG2, lambda: M.HOVER_BG)

        # Bind mousewheel on every widget in this row so scrolling works
        # regardless of which child the cursor is over
        if hasattr(self, "_bind_hist_mw"):
            self._bind_hist_mw(row)

        # Feature 4: register row for search filtering
        if hasattr(self, "_hist_rows"):
            self._hist_rows.append((row, e))
            self._trim_history_rows()

    def _load_thumb(self, canvas, url):
        # Fetch on a worker thread — never block the Tk main loop on network I/O.
        surface = M.BG2   # the history row behind the thumbnail
        def _fetch():
            return M._round_image(M._fetch_art(url, M.THUMB_PX), M.THUMB_RADIUS, surface)
        def _apply(result, cv=canvas):
            if result is None: return
            try:
                photo = M.ImageTk.PhotoImage(result)
                # Park the reference on the canvas itself rather than in a
                # module-level list. Tk needs *a* live reference or it GCs the
                # image; attaching it to the widget means the reference dies
                # with the widget instead of leaking for the whole session
                # (self._hist_imgs only ever grew, never shrank).
                cv._statusify_photo = photo
                cv.delete("all")
                cv.create_image(0,0, anchor="nw", image=photo)
            except Exception:
                pass
        fut = M.image_executor.submit(_fetch)
        def _done(f):
            try: res = f.result()
            except Exception: res = None
            # PhotoImage MUST be created on the Tk thread.
            self.win.after(0, lambda: _apply(res))
        fut.add_done_callback(_done)

    def _show_lyrics(self, e):
        if not e: return

        # Close any existing lyrics panel first
        self._close_lyrics_panel()

        # Build an overlay Frame that sits on top of the main window content
        panel = tk.Frame(self.win, bg=M.BG, bd=1, relief="flat",
                         highlightbackground=M.BORDER, highlightthickness=1)
        panel.place(relx=0.5, rely=0.5, anchor="center", width=460, height=560)
        panel.lift()
        self._lyrics_panel = panel

        # Title bar
        pbar = tk.Frame(panel, bg=M.BG2, height=40)
        pbar.pack(fill="x"); pbar.pack_propagate(False)

        title_str = f"{e['title']} — {e['artist']}"
        tk.Label(pbar, text=title_str[:55] + ("…" if len(title_str)>55 else ""),
                 fg=M.TEXT, bg=M.BG2, font=self._f(9, True), anchor="w").pack(
                 side="left", padx=14, fill="x", expand=True)

        cb = tk.Label(pbar, text="✕", fg=M.MUTED, bg=M.BG2, font=self._f(10),
                      cursor="hand2", padx=10)
        cb.pack(side="right")
        cb.bind("<Button-1>", lambda ev: self._close_lyrics_panel())
        self._hoverable(cb, fg=lambda: M.MUTED, hover_fg=lambda: M.DANGER, duration_ms=90)

        # Lyrics search bar
        sbar = tk.Frame(panel, bg=M.BG)
        sbar.pack(fill="x", padx=14, pady=(10, 0))
        tk.Label(sbar, text="🔍", fg=M.MUTED, bg=M.BG, font=self._f(9)).pack(side="left")
        search_var = tk.StringVar()
        ent_search = tk.Entry(sbar, textvariable=search_var, bg=M.BG2, fg=M.TEXT, insertbackground=M.TEXT,
                              relief="flat", font=self._f(9))
        self._focus_ring(ent_search)
        ent_search.pack(side="left", fill="x", expand=True, padx=(M.SP_SM,0), ipady=3)

        # Lyrics content
        frm = tk.Frame(panel, bg=M.BG)
        frm.pack(fill="both", expand=True, padx=14, pady=10)
        vsb = tk.Scrollbar(frm, command=lambda *a: txt.yview(*a),
                           bg=M.BG2, troughcolor=M.BG, relief="flat", width=5, bd=0)
        vsb.pack(side="right", fill="y")
        txt = tk.Text(frm, bg=M.BG, fg=M.TEXT2, font=self._f(10), relief="flat",
                      wrap="word", padx=10, pady=8, yscrollcommand=vsb.set)
        txt.pack(side="left", fill="both", expand=True)
        txt.bind("<MouseWheel>", lambda ev: txt.yview_scroll(int(-1*(ev.delta/120)), "units"))
        txt.tag_config("line", foreground=M.TEXT2, spacing1=3, spacing3=3)
        txt.tag_config("ts",   foreground=M.MUTED)
        txt.tag_config("highlight", background=M.ACCENT, foreground=M.ACCENT_FG)
        # Active line = the lyric currently being sung. Coloured rather than
        # background-filled so it reads differently from a search hit.
        txt.tag_config("active", foreground=M.ACCENT)

        # Remember what this panel is showing so _highlight_active_lyric can
        # follow along while the song plays.
        self._lyrics_txt   = txt
        self._lyrics_entry = e

        if e["mode"] == "synced" and e["synced"]:
            for ln in e["synced"]:
                ms = ln["startMs"]; mins, secs = divmod(ms//1000, 60)
                txt.insert("end", f"{mins}:{secs:02d}  ", "ts")
                txt.insert("end", ln["words"]+"\n", "line")
        elif e["plain"]:
            for ln in e["plain"]:
                txt.insert("end", ln+"\n", "line")
        else:
            txt.insert("end", "No lyrics available for this song.", "ts")

        txt.config(state="disabled")

        # Handle highlighting on search
        def _on_search(*args):
            q = search_var.get().lower()
            txt.tag_remove("highlight", "1.0", "end")
            if not q: return
            
            idx = "1.0"
            first_match = None
            while True:
                idx = txt.search(q, idx, nocase=True, stopindex="end")
                if not idx: break
                
                if not first_match: first_match = idx
                length = len(q)
                end_idx = f"{idx}+{length}c"
                txt.tag_add("highlight", idx, end_idx)
                idx = end_idx
                
            if first_match:
                txt.see(first_match)

        search_var.trace_add("write", _on_search)

        # ── Footer: export / copy ─────────────────────────────────
        foot = tk.Frame(panel, bg=M.BG)
        foot.pack(fill="x", padx=M.SP_LG, pady=(0, M.SP_MD))

        def _mkbtn(parent, label, cmd, accent=False):
            b = tk.Label(parent, text=label,
                         fg=M.ACCENT_FG if accent else M.TEXT2,
                         bg=M.ACCENT if accent else M.BG3,
                         font=self._f(M.FS_MICRO, True), cursor="hand2",
                         padx=M.SP_MD, pady=M.SP_XS + 1)
            b.pack(side="left", padx=(0, M.SP_SM))
            b.bind("<Button-1>", lambda ev: cmd())
            if not accent:
                self._hoverable(b, fg=lambda: M.TEXT2, hover_fg=lambda: M.ACCENT,
                                bg=lambda: M.BG3, hover_bg=lambda: M.ACCENT_SOFT)
            return b

        has_synced = bool(e.get("synced"))
        if has_synced:
            _mkbtn(foot, "EXPORT .LRC", lambda: self._do_export(e, "lrc"), accent=True)
        _mkbtn(foot, "EXPORT .TXT", lambda: self._do_export(e, "txt"))
        _mkbtn(foot, "COPY ALL",    lambda: self._copy_all_lyrics(e))
        if e.get("track_uri"):
            _mkbtn(foot, "OPEN IN SPOTIFY", lambda: self._open_in_spotify(e))

        self._highlight_active_lyric()

    def _do_export(self, entry, fmt):
        path = M._export_lyrics(entry, fmt)
        if path:
            self._set_error("")
            M.log(f"Saved to exports/{os.path.basename(path)}")

    def _copy_all_lyrics(self, entry):
        synced = entry.get("synced") or []
        plain  = entry.get("plain") or []
        body = "\n".join(ln.get("words", "") for ln in synced) if synced else "\n".join(plain)
        if body:
            self._to_clipboard(body, "Copied lyrics")
        else:
            M.log("Nothing to copy — no lyrics for this track")

    def _open_in_spotify(self, entry):
        """Open the track in the Spotify desktop client via its spotify: URI."""
        uri = entry.get("track_uri") or ""
        if not uri.startswith("spotify:"):
            M.log("No Spotify URI stored for this entry")
            return
        try:
            os.startfile(uri)          # noqa: S606 — a spotify: URI, not a shell string
            M.log(f"Opening in Spotify  ·  {entry.get('title', '')}")
        except OSError as e:
            M.log(f"Could not open Spotify: {e}")

    def _highlight_active_lyric(self):
        """Mark and scroll to the line currently being sung.

        Only applies when the open panel is showing the track that is actually
        playing — scrolling someone's browsing of an old song would be wrong."""
        txt = getattr(self, "_lyrics_txt", None)
        e   = getattr(self, "_lyrics_entry", None)
        if txt is None or not e:
            return
        try:
            if not txt.winfo_exists():
                return
        except tk.TclError:
            return
        if e.get("track_uri") != getattr(M.state, "track_uri", None):
            return
        synced = e.get("synced") or []
        if not synced:
            return
        pos = self._estimate_pos_ms() + M._track_offset_ms()
        line_no = 0
        for i, ln in enumerate(synced):
            if ln.get("startMs", 0) <= pos:
                line_no = i
            else:
                break
        try:
            txt.tag_remove("active", "1.0", "end")
            start = f"{line_no + 1}.0"
            txt.tag_add("active", start, f"{line_no + 1}.end")
            txt.see(start)
        except tk.TclError:
            pass

    def _close_lyrics_panel(self):
        panel = getattr(self, "_lyrics_panel", None)
        if panel:
            try: panel.destroy()
            except Exception: pass
            self._lyrics_panel = None
        # Drop the panel refs so _highlight_active_lyric stops doing work.
        self._lyrics_txt   = None
        self._lyrics_entry = None
