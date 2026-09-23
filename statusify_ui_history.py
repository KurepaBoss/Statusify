"""The History page: every play, grouped by day, searchable, with each
play's lyrics on a sheet that slides in over the list.

Drawn like Settings and Stats: canvases of text items and small
anti-aliased images, no native widget per row. That is what fixes the old
page's hover. It used <Enter>/<Leave> on every row widget, which Tk only
fires when the pointer itself moves: a row gliding away under a still
pointer stayed lit, the one arriving never lit up, the 2 px gaps between
rows flickered, and the rounded covers showed square corners on hover.
Now there is one hovered row per page, found by hit-testing the pointer's
canvas position on <Motion> and again after every scroll step.

  header canvas   title, "1,284 plays · since 3 Aug", the search field
  list canvas     day headings with session totals, cards of rows,
                  "Show more" and "Clear history…" (which asks first)
  sheet           a play's lyrics, sliding in from the right; Esc or
                  "‹ History" slides it back and the list is where it was

Names that belong to main are reached through M, the live main module:
palette colours and settings are rebound at runtime, so they must be read
from main on every use, never copied.
"""
import datetime
import os
import tkinter as tk

try:
    from PIL import Image
except ImportError:
    Image = None

import statusify_backdrop as bdm

M = None   # the main module

SESSION_GAP = datetime.timedelta(minutes=30)   # a longer pause starts a new session
SHEET_MS = 140


# ── Pure helpers (tested directly) ───────────────────────────────
def _parse(played_at):
    try:
        return datetime.datetime.fromisoformat(played_at)
    except (TypeError, ValueError):
        return None


def day_label(day, today):
    """'Today', 'Yesterday', 'Mon 21 Sep', 'Mon 21 Sep 2025'."""
    d = (today - day).days
    if d == 0:
        return "Today"
    if d == 1:
        return "Yesterday"
    s = f"{day.strftime('%a')} {day.day} {day.strftime('%b')}"
    return s if day.year == today.year else f"{s} {day.year}"


def fmt_total(ms):
    """'52 min', '1 h 12 min', '2 h'."""
    m = int((ms or 0) // 60000)
    if m < 60:
        return f"{m} min"
    h, m = divmod(m, 60)
    return f"{h} h {m} min" if m else f"{h} h"


def count_sessions(times):
    """Listening sessions among play start times: a gap over SESSION_GAP
    between two plays starts a new one."""
    ts = sorted(t for t in times if t is not None)
    if not ts:
        return 0
    return 1 + sum(1 for a, b in zip(ts, ts[1:]) if b - a > SESSION_GAP)


def group_by_day(entries, now=None):
    """Entries (newest first) grouped by local day:
    [{"day", "label", "entries", "sessions", "listened_ms"}], newest day first."""
    now = now or datetime.datetime.now()
    groups, by_day = [], {}
    # Newest first by when it was played, whatever order the rows came in
    # (an imported history's ids needn't follow its timestamps).
    entries = sorted(entries, key=lambda e: _parse(e.get("played_at")) or now, reverse=True)
    for e in entries:
        t = _parse(e.get("played_at"))
        day = t.date() if t else now.date()
        g = by_day.get(day)
        if g is None:
            g = by_day[day] = {"day": day, "label": day_label(day, now.date()),
                               "entries": [], "times": [], "listened_ms": 0}
            groups.append(g)
        g["entries"].append(e)
        g["times"].append(t)
        g["listened_ms"] += int(e.get("listened_ms") or 0)
    for g in groups:
        g["sessions"] = count_sessions(g.pop("times"))
    return groups


def day_summary(group):
    """'2 sessions · 1 h 12 min' (the time only once some was recorded)."""
    n = group["sessions"]
    s = f"{n} session{'s' if n != 1 else ''}"
    if group["listened_ms"] >= 60000:
        s += f" · {fmt_total(group['listened_ms'])}"
    return s


def lyric_badge(e):
    """'Synced', 'Plain' or None for a play's stored lyrics."""
    if e.get("synced"):
        return "Synced"
    if e.get("plain"):
        return "Plain"
    return None


def plays_caption(n, first_played, today=None):
    """'1,284 plays · since 3 Aug'."""
    today = today or datetime.date.today()
    s = f"{n:,} play{'s' if n != 1 else ''}"
    t = _parse(first_played)
    if t is not None and n:
        d = t.date()
        s += f" · since {d.day} {d.strftime('%b')}" + ("" if d.year == today.year else f" {d.year}")
    return s


def fmt_ts(ms):
    m, s = divmod(int(ms or 0) // 1000, 60)
    return f"{m}:{s:02d}"


# ── A text field drawn on a canvas ───────────────────────────────
class _CanvasEntry:
    """A one-line text field drawn on a canvas, backed by a StringVar.

    A native Entry is an opaque window: over the moving backdrop it showed
    as a flat patch. This is a canvas text item using the canvas's own
    insertion cursor. Keys it handles return "break", so typing a space
    doesn't also reach the window's play/pause shortcut."""

    def __init__(self, page, cv, var, placeholder, hint=""):
        self.p, self.cv, self.var = page, cv, var
        self.placeholder, self.hint = placeholder, hint
        self.focused = False
        self.item = None
        self.box = None
        cv.bind("<Key>", self._key)
        cv.bind("<FocusOut>", lambda e: self._focus(False))
        var.trace_add("write", lambda *_: self._sync())

    # The Entry calls _focus_history_search makes.
    def focus_set(self):
        self.cv.focus_set()
        self._focus(True)

    def select_range(self, a, b):
        if self.item is not None and self.var.get():
            try:
                self.cv.select_from(self.item, 0)
                self.cv.select_to(self.item, "end")
            except tk.TclError:
                pass

    def get(self):
        return self.var.get()

    def draw(self, x, y, w, h):
        p, cv, S = self.p, self.cv, self.p._ss
        self.box = (x, y, x + w, y + h)
        cv.delete(f"fld{id(self)}")
        tag = f"fld{id(self)}"
        img = (p._pill_photo(w, h, M.BG2, M.BG, radius=S(8), outline=M.ACCENT)
               if self.focused and not M._cover_mode() else p._card_photo(w, h, 8))
        cv.create_image(x, y, anchor="nw", image=img, tags=(tag, "field"))
        if self.focused and M._cover_mode():
            # Focus ring: the rim at full strength.
            cv.create_image(x, y, anchor="nw", tags=(tag,),
                            image=p._pill_photo(w, h, M.BG2, M.BG2, radius=S(8), outline=M.ACCENT))
        font = p._f(M.FS_BODY)
        tx = x + S(10)
        self._room = w - S(20) - (p._f(M.FS_SMALL).measure(self.hint) + S(8) if self.hint else 0)
        self.ph = cv.create_text(tx, y + h // 2, anchor="w", text=self.placeholder, fill=M.MUTED,
                                 font=font, tags=(tag,))
        self.item = cv.create_text(tx, y + h // 2, anchor="w", text=self.var.get(), fill=M.TEXT,
                                   font=font, tags=(tag,))
        self.hint_item = None
        if self.hint:
            self.hint_item = cv.create_text(x + w - S(10), y + h // 2, anchor="e", text=self.hint,
                                            fill=M.MUTED, font=p._f(M.FS_SMALL), tags=(tag,))
        cv.config(insertbackground=M.TEXT, selectbackground=M.ACCENT_SOFT, selectforeground=M.TEXT)
        cv.tag_bind(tag, "<Button-1>", self._click)
        cv.tag_bind(tag, "<Enter>", lambda e: cv.config(cursor="xterm"))
        cv.tag_bind(tag, "<Leave>", lambda e: cv.config(cursor=""))
        self._sync()
        if self.focused:
            cv.focus(self.item)
            cv.icursor(self.item, "end")

    def _sync(self):
        cv = self.cv
        if self.item is None:
            return
        t = self.var.get()
        try:
            if cv.itemcget(self.item, "text") != t:
                cv.itemconfigure(self.item, text=t)
            cv.itemconfigure(self.ph, state="hidden" if t else "normal")
            if self.hint_item is not None:
                cv.itemconfigure(self.hint_item, state="hidden" if (t or self.focused) else "normal")
        except tk.TclError:
            pass

    def _click(self, e):
        self.focus_set()
        try:
            self.cv.select_clear()
            self.cv.icursor(self.item, f"@{int(self.cv.canvasx(e.x))},{int(self.cv.canvasy(e.y))}")
        except tk.TclError:
            pass
        return "break"

    def _focus(self, on):
        if on == self.focused:
            return
        self.focused = on
        if self.box:
            x0, y0, x1, y1 = self.box
            self.draw(x0, y0, x1 - x0, y1 - y0)
        if not on:
            try:
                self.cv.focus("")
                self.cv.select_clear()
            except tk.TclError:
                pass

    def _set(self, text, cursor):
        self.var.set(text)
        try:
            self.cv.icursor(self.item, cursor)
        except tk.TclError:
            pass

    def _fits(self, text):
        return self.p._f(M.FS_BODY).measure(text) <= self._room

    def _key(self, e):
        if not self.focused or self.item is None:
            return None
        cv, t = self.cv, self.var.get()
        try:
            i = cv.index(self.item, "insert")
            sel = (cv.index(self.item, "sel.first"), cv.index(self.item, "sel.last") + 1) \
                if cv.select_item() else None
        except tk.TclError:
            i, sel = len(t), None
        ctrl = bool(e.state & 0x0004)
        k = e.keysym

        def cut():
            nonlocal t, i
            if sel:
                t = t[:sel[0]] + t[sel[1]:]
                i = sel[0]
                cv.select_clear()
                return True
            return False
        if k == "Escape":
            if t:
                self._set("", 0)
                return "break"
            self.p.win.focus_set()
            return None                       # Esc also closes a sheet
        if ctrl and k.lower() == "a":
            self.select_range(0, "end")
            return "break"
        if ctrl and k.lower() == "v":
            try:
                clip = self.p._root.clipboard_get().replace("\n", " ")
            except tk.TclError:
                return "break"
            cut()
            new = t[:i] + clip + t[i:]
            while clip and not self._fits(new):
                clip = clip[:-1]
                new = t[:i] + clip + t[i:]
            self._set(new, i + len(clip))
            return "break"
        if k == "BackSpace":
            if not cut():
                if ctrl:
                    j = len(t[:i].rstrip().rsplit(" ", 1)[0]) if " " in t[:i].rstrip() else 0
                    t, i = t[:j] + t[i:], j
                elif i > 0:
                    t, i = t[:i - 1] + t[i:], i - 1
            self._set(t, i)
            return "break"
        if k == "Delete":
            if not cut() and i < len(t):
                t = t[:i] + t[i + 1:]
            self._set(t, i)
            return "break"
        if k in ("Left", "Right", "Home", "End"):
            cv.select_clear()
            j = {"Left": max(0, i - 1), "Right": min(len(t), i + 1), "Home": 0, "End": len(t)}[k]
            cv.icursor(self.item, j)
            return "break"
        if ctrl or k in ("Return", "Tab", "Up", "Down"):
            return None
        ch = e.char
        if ch and ch.isprintable():
            cut()
            new = t[:i] + ch + t[i:]
            if self._fits(new):
                self._set(new, i + 1)
            return "break"
        return None


# ── The page ─────────────────────────────────────────────────────
class HistoryPage:

    def _build_history(self):
        S = self._ss
        p = tk.Frame(self._container, bg=M.BG); self._pages["HISTORY"] = p
        self._hist_page = p
        self._hist_search = tk.StringVar()
        self._hist_entries = []         # what the list shows, newest first
        self._hist_rows = []            # [(row, entry)] drawn, top to bottom
        self._hist_limit = M.MAX_RENDERED_ROWS
        self._hist_hover = None         # index into _hist_rows
        self._hist_ptr = None           # pointer (x, y) over the list, widget coords
        self._hist_press = None
        self._hist_confirm = False
        self._hist_total = 1
        self._hist_target = 0.0
        self._hist_gliding = False
        self._hist_tags = []
        self._hist_caption_text = ""
        self._hist_sheet_entry = None
        self.__dict__.setdefault("_st_art", {})
        self._hist_thumb_items = {}

        # Header: title, caption, search. Its own canvas, so the search
        # field stays put while the list scrolls.
        font_t = self._f(M.FS_HERO + 4, True)
        font_c = self._f(M.FS_SMALL)
        hh = S(18) + font_t.metrics("linespace") + font_c.metrics("linespace") + S(12) + S(30) + S(6)
        self._hist_head = head = tk.Canvas(p, height=hh, bg=M.BG, highlightthickness=0, bd=0)
        head.pack(side="top", fill="x")
        self._hist_search_entry = _CanvasEntry(self, head, self._hist_search,
                                               "Search titles, artists and lyrics", "Ctrl F")
        head.bind("<Configure>", lambda e: self._hist_draw_head())
        head.bind("<Button-1>", lambda e: self._hist_blur(e, self._hist_search_entry), add="+")

        area = tk.Frame(p, bg=M.BG)
        area.pack(fill="both", expand=True)
        self._hist_sb = tk.Canvas(area, width=S(24), bg=M.BG, highlightthickness=0, bd=0)
        self._hist_sb.pack(side="right", fill="y")
        self.hist_cv = cv = tk.Canvas(area, bg=M.BG, highlightthickness=0, bd=0,
                                      yscrollincrement=1, confine=True)
        cv.pack(side="left", fill="both", expand=True)

        last_w = {"w": 0}
        def _cfg(e):
            if e.width != last_w["w"]:
                last_w["w"] = e.width
                self._hist_render()
            else:
                self._hist_scroll_to(cv.canvasy(0), animate=False)
        cv.bind("<Configure>", _cfg)
        cv.bind("<Motion>", self._hist_motion)
        cv.bind("<Leave>", lambda e: (setattr(self, "_hist_ptr", None), self._hist_set_hover(None)))
        cv.bind("<ButtonPress-1>", self._hist_on_press)
        cv.bind("<ButtonRelease-1>", self._hist_on_release)
        cv.bind("<Button-3>", self._hist_on_right)

        def _wheel(e):
            if self._cur_page != "HISTORY":
                return
            d = -(e.delta / 120.0) * M.SCROLL_NOTCH_PX
            if self._hist_sheet_entry is not None:
                self._hist_sheet_scroll_by(d)
            else:
                self._hist_scroll_by(d)
        cv.bind_all("<MouseWheel>", _wheel, add="+")

        sb = self._hist_sb
        def _sb_drag(e):
            view = cv.winfo_height()
            frac = max(0.0, min(1.0, e.y / max(1, sb.winfo_height())))
            self._hist_scroll_to(frac * max(0, self._hist_total - view), animate=False)
        sb.bind("<Button-1>", _sb_drag)
        sb.bind("<B1-Motion>", _sb_drag)
        sb.bind("<Configure>", lambda e: self._hist_draw_thumb())

        self._hist_build_sheet(p)
        for c in (head, cv, sb):
            self._bd_register(c, "HISTORY")
        self._drag_bind(cv, "HISTORY")
        self._drag_bind(head, "HISTORY")
        # Laid out under the current page now, not mid-slide (see Settings).
        if self._cur_page != "HISTORY":
            p.place(x=0, y=0, relwidth=1, relheight=1)
            p.lower()
        self._hist_search.trace_add("write", lambda *_: self._filter_history())
        self._render_loaded_history()

    def _hist_blur(self, e, entry):
        """A click on a header outside its field takes the focus out of it."""
        b = entry.box
        if b and b[0] <= e.x <= b[2] and b[1] <= e.y <= b[3]:
            return
        self.win.focus_set()

    # ── Data ─────────────────────────────────────────────────────
    def _render_loaded_history(self):
        """Show what was restored from disk (and log it once)."""
        self._hist_load()
        self._hist_render()
        if M.history:
            M.log(f"History restored  ·  {len(M.history)} tracks")

    def _filter_history(self):
        """Debounced: re-query for the current search text."""
        self._schedule("hist_search", 200, self._run_history_search)

    def _run_history_search(self):
        """Search the whole database, not just the plays in memory."""
        self._hist_limit = M.MAX_RENDERED_ROWS
        self._hist_load()
        self._hist_render()
        self._hist_scroll_to(0, animate=False)

    def _hist_load(self):
        q = self._hist_search.get().strip()
        n = self._hist_limit
        st = M._HISTORY_STORE
        entries = []
        if q and st:
            try:
                entries = st.search(q, n)
            except Exception as e:
                M.log(f"History search failed: {e}")
        elif q:
            ql = q.lower()
            entries = [e for e in reversed(M.history) if ql in e.get("title", "").lower()
                       or ql in e.get("artist", "").lower()][:n]
        elif n <= len(M.history) or not st:
            entries = list(reversed(M.history[-n:]))
        else:
            try:
                entries = list(reversed(st.recent(n)))
            except Exception as e:
                M.log(f"History query failed: {e}")
                entries = list(reversed(M.history))
        self._hist_entries = entries
        self._hist_more = len(entries) >= n and (bool(st) or len(M.history) > n)
        self._hist_caption()

    def _hist_caption(self):
        st = M._HISTORY_STORE
        try:
            n = st.count() if st else len(M.history)
            first = st.first_played() if st else (M.history[0].get("played_at") if M.history else None)
        except Exception:
            n, first = len(M.history), None
        text = plays_caption(n, first)
        if text != self._hist_caption_text:
            self._hist_caption_text = text
            self._hist_draw_head()

    def _add_history_row(self, e):
        """A new play (the "history_add" event): put it on top."""
        if not e or not hasattr(self, "hist_cv"):
            return
        if not any(x is e for x in self._hist_entries):
            self._hist_entries.insert(0, e)
            del self._hist_entries[self._hist_limit:]
        self._hist_caption()
        self._hist_render()

    def _hist_show_more(self):
        self._hist_limit += M.MAX_RENDERED_ROWS
        self._hist_load()
        self._hist_render()

    def _clear_history(self):
        """Wipe the history, in memory and on disk (after _hist_ask_clear)."""
        self._hist_confirm = False
        self._close_lyrics_panel()
        M.history.clear()
        try:
            if M._HISTORY_STORE:
                M._HISTORY_STORE.clear()
            M._current_play["entry"] = None
        except Exception as e:
            M.log(f"Could not delete history: {e}")
        self._hist_entries = []
        self._hist_more = False
        self._hist_caption()
        self._hist_render()
        M.log("History cleared")

    def _hist_ask_clear(self, on=True):
        self._hist_confirm = on
        self._hist_render()
        if on:
            self._hist_scroll_to(self._hist_total, animate=True)

    # ── Header ───────────────────────────────────────────────────
    def _hist_draw_head(self):
        cv = getattr(self, "_hist_head", None)
        if cv is None:
            return
        W = cv.winfo_width()
        if W < 120:
            return
        S = self._ss
        cv.delete("hdr")
        x0, x1 = S(22), W - S(26)
        y = S(18)
        t = cv.create_text(x0, y, anchor="nw", text="History", fill=M.TEXT,
                           font=self._f(M.FS_HERO + 4, True), tags=("hdr",))
        y = cv.bbox(t)[3]
        t = cv.create_text(x0, y, anchor="nw", text=self._hist_caption_text or " ", fill=M.TEXT2,
                           font=self._f(M.FS_SMALL), tags=("hdr",))
        y = cv.bbox(t)[3] + S(12)
        self._hist_search_entry.draw(x0, y, x1 - x0, S(30))
        self._bd_attach(cv)

    # ── List ─────────────────────────────────────────────────────
    def _hist_render(self):
        cv = getattr(self, "hist_cv", None)
        if cv is None:
            return
        W = cv.winfo_width()
        if W < 120:
            # Not on screen yet: lay out at the requested width; the first
            # <Configure> renders again at the real one.
            W = cv.winfo_reqwidth()
        S = self._ss
        top = cv.canvasy(0)
        for t in self._hist_tags:
            for seq in ("<Button-1>", "<Enter>", "<Leave>"):
                try:
                    cv.tag_unbind(t, seq)
                except tk.TclError:
                    pass
        self._hist_tags = []
        cv.delete("all")
        self._hist_rows = []
        self._hist_hover = None
        self._hist_thumb_items = {}
        x0, x1 = S(22), W - S(2)
        y = S(2)
        now = datetime.datetime.now()
        cur = M._current_play.get("entry")
        cur_uri = getattr(M.state, "track_uri", None)
        f_head, f_small = self._f(M.FS_LARGE + 1, True), self._f(M.FS_SMALL)
        f_title, f_badge = self._f(M.FS_BODY, True), self._f(M.FS_MICRO, True)
        size = S(40)
        row_h = size + S(16)
        pad = S(4)
        groups = group_by_day(self._hist_entries, now)
        if not groups:
            q = self._hist_search.get().strip()
            cv.create_text((x0 + x1) // 2, y + S(48), anchor="n", fill=M.TEXT2, font=f_small,
                           text="No plays match that search." if q else "Nothing played yet.")
            y += S(96)
        for gi, g in enumerate(groups):
            y += S(10) if gi == 0 else S(20)
            h = cv.create_text(x0, y, anchor="nw", text=g["label"], fill=M.TEXT, font=f_head)
            cv.create_text(x1, cv.bbox(h)[3], anchor="se", text=day_summary(g),
                           fill=M.MUTED, font=f_small)
            y = cv.bbox(h)[3] + S(8)
            card_top = y
            y += pad
            for ri, e in enumerate(g["entries"]):
                if ri:
                    line = cv.create_line(x0 + pad + S(8) + size + S(12), y, x1 - pad - S(8), y,
                                          fill=M.BORDER)
                    if self._hist_rows:
                        self._hist_rows[-1][0]["lines"].append(line)
                else:
                    line = None
                row = self._hist_draw_row(cv, x0 + pad, x1 - pad, y, row_h, size, e,
                                          (e is cur and e.get("track_uri") == cur_uri),
                                          f_title, f_small, f_badge)
                if line is not None:
                    row["lines"].append(line)
                self._hist_rows.append((row, e))
                y += row_h
            y += pad
            self._set_card_bg(cv, x0, card_top, x1, y, f"hcard{gi}")
        y += S(16)
        if self._hist_confirm:
            n = len(self._hist_entries)
            try:
                n = M._HISTORY_STORE.count() if M._HISTORY_STORE else len(M.history)
            except Exception:
                pass
            t = cv.create_text(x0, y, anchor="nw", fill=M.TEXT, font=self._f(M.FS_BODY, True),
                               text=f"Delete all {n:,} play{'s' if n != 1 else ''}?")
            y = cv.bbox(t)[3]
            t = cv.create_text(x0, y, anchor="nw", fill=M.TEXT2, font=f_small,
                               text="Your listening history and stats go with them. This can't be undone.",
                               width=x1 - x0)
            y = cv.bbox(t)[3] + S(10)
            w = self._st_button(cv, x0, y, "Delete history", self._clear_history, "danger")
            self._st_button(cv, x0 + w + S(8), y, "Cancel", lambda: self._hist_ask_clear(False),
                            "secondary")
            y += S(28)
        elif self._hist_entries or self._hist_more:
            if self._hist_more:
                self._st_button(cv, x0, y, "Show more", self._hist_show_more, "secondary")
            if self._hist_entries and not self._hist_search.get().strip():
                self._st_button(cv, x1, y, "Clear history…", self._hist_ask_clear,
                                "ghost-danger", anchor="ne")
            y += S(28)
        total = y + S(24)
        self._hist_total = total
        cv.config(scrollregion=(0, 0, W, total))
        self._bd_attach(cv)
        self._hist_scroll_to(top, animate=False)

    def _hist_draw_row(self, cv, rx0, rx1, y, h, size, e, now_playing, f_title, f_small, f_badge):
        S = self._ss
        rw = rx1 - rx0
        bg = cv.create_image(rx0, y, anchor="nw",
                             image=self._pill_photo(rw, h, M.BG2, M.BG2, radius=S(8)))
        tx0 = rx0 + S(8)
        ty = y + S(8)
        url = e.get("album_art")
        thumb = self._hist_thumb(cv, tx0, ty, url, size, M.BG2)
        when = "Now playing" if now_playing else (_parse(e.get("played_at")) or datetime.datetime.now()).strftime("%H:%M")
        badge = lyric_badge(e)
        right_w = f_small.measure(when) if not now_playing else self._f(M.FS_SMALL, True).measure(when)
        if badge:
            right_w = max(right_w, f_badge.measure(badge) + S(14))
        tx = tx0 + size + S(12)
        room = rx1 - S(12) - tx - right_w - S(12)
        cv.create_text(tx, ty + S(1), anchor="nw", fill=M.TEXT, font=f_title,
                       text=self._st_fit(e.get("title") or "Unknown", f_title, room))
        cv.create_text(tx, ty + f_title.metrics("linespace") + S(1), anchor="nw", fill=M.MUTED,
                       font=f_small, text=self._st_fit(e.get("artist") or "", f_small, room))
        rx = rx1 - S(12)
        if now_playing:
            cv.create_text(rx, ty + S(1), anchor="ne", text=when, fill=M.ACCENT,
                           font=self._f(M.FS_SMALL, True))
        else:
            cv.create_text(rx, ty + S(1), anchor="ne", text=when, fill=M.TEXT2, font=f_small)
        if badge:
            bh = f_badge.metrics("linespace") + S(2)
            bw = f_badge.measure(badge) + S(14)
            by = ty + f_title.metrics("linespace") + S(3)
            fill = M.ACCENT_SOFT if badge == "Synced" else M.BG3
            cv.create_image(rx - bw, by, anchor="nw",
                            image=self._pill_photo(bw, bh, fill, M.BG2, radius=bh // 2))
            cv.create_text(rx - bw // 2, by + bh // 2, text=badge, font=f_badge,
                           fill=M.TEXT if badge == "Synced" else M.TEXT2)
        return {"y0": y, "y1": y + h, "bg": bg, "thumb": thumb, "url": url, "w": rw, "h": h,
                "size": size, "t": 0.0, "lines": []}

    def _hist_thumb(self, cv, x, y, url, size, bg):
        """Rounded cover; a note glyph until the art arrives (fetched off-thread)."""
        S = self._ss
        ph = self._st_thumb_photo(url, size, bg) if url else None
        if ph is not None:
            return cv.create_image(x, y, anchor="nw", image=ph)
        item = cv.create_image(x, y, anchor="nw",
                               image=self._pill_photo(size, size, M.BG3, bg, radius=S(6)))
        note = cv.create_text(x + size // 2, y + size // 2, text="♫", fill=M.MUTED,
                              font=self._f(M.FS_SMALL))
        if url and M.PIL_AVAILABLE:
            self._hist_thumb_items.setdefault(url, []).append((item, note, size))
            self._hist_fetch_art(url)
        return item

    def _hist_fetch_art(self, url):
        pending = self.__dict__.setdefault("_hist_art_pending", set())
        if url in pending:
            return
        pending.add(url)

        def apply(img):
            pending.discard(url)
            if img is None:
                return
            self._st_art[url] = img
            for item, note, size in self._hist_thumb_items.pop(url, []):
                try:
                    self.hist_cv.itemconfigure(item, image=self._st_thumb_photo(url, size, M.BG2))
                    self.hist_cv.delete(note)
                except tk.TclError:
                    pass
            self._hist_repaint_hover()
        self._st_run(lambda: M._fetch_art(url, M.THUMB_PX), apply)

    # ── Hover, by hit-testing ────────────────────────────────────
    def _hist_row_at(self, canvas_y):
        for i, (row, _e) in enumerate(self._hist_rows):
            if row["y0"] <= canvas_y < row["y1"]:
                return i
        return None

    def _hist_motion(self, e):
        self._hist_ptr = (e.x, e.y)
        self._hist_update_hover()

    def _hist_update_hover(self):
        """Light the row under the pointer; called on motion and after every
        scroll step, so rows gliding under a still pointer follow it."""
        ptr = self._hist_ptr
        if ptr is None or self._hist_sheet_entry is not None:
            self._hist_set_hover(None)
            return
        try:
            x0 = self._ss(22)
            cy = self.hist_cv.canvasy(ptr[1])
            inside = x0 <= ptr[0] <= self.hist_cv.winfo_width() - self._ss(2)
        except tk.TclError:
            return
        self._hist_set_hover(self._hist_row_at(cy) if inside else None)

    def _hist_set_hover(self, i):
        if i == self._hist_hover:
            return
        old, self._hist_hover = self._hist_hover, i
        try:
            self.hist_cv.config(cursor="hand2" if i is not None else "")
        except tk.TclError:
            pass
        if old is not None:
            self._hist_fade(old, 0.0)
        if i is not None:
            self._hist_fade(i, 1.0)
        self._hist_lines()

    def _hist_lines(self):
        """Hide the hairlines touching the hovered row so its pill reads cleanly."""
        hide = set()
        if self._hist_hover is not None and self._hist_hover < len(self._hist_rows):
            hide = set(self._hist_rows[self._hist_hover][0]["lines"])
        for row, _e in self._hist_rows:
            for ln in row["lines"]:
                try:
                    self.hist_cv.itemconfigure(ln, state="hidden" if ln in hide else "normal")
                except tk.TclError:
                    pass

    def _hist_fade(self, i, to):
        if i >= len(self._hist_rows):
            return
        row = self._hist_rows[i][0]
        a = row["t"]

        def apply(e):
            row["t"] = a + (to - a) * e
            self._hist_paint_row(row, round(row["t"] * 6) / 6)
        self._animate(f"histrow:{id(row)}", 120, apply)

    def _hist_paint_row(self, row, q):
        """The row pill at hover level q; the cover re-rounded onto that
        colour (in cover mode its corners are simply clear)."""
        cv, S = self.hist_cv, self._ss
        f = M._blend(M.BG2, M.HOVER_BG, q)
        cv.itemconfigure(row["bg"], image=self._pill_photo(row["w"], row["h"], f, M.BG2, radius=S(8)))
        url, size = row["url"], row["size"]
        ph = self._st_thumb_photo(url, size, f) if url else None
        if ph is not None:
            cv.itemconfigure(row["thumb"], image=ph)
        elif not url or url not in self._st_art:
            cv.itemconfigure(row["thumb"], image=self._pill_photo(size, size, M.BG3, f, radius=S(6)))

    def _hist_repaint_hover(self):
        i = self._hist_hover
        if i is not None and i < len(self._hist_rows):
            try:
                self._hist_paint_row(self._hist_rows[i][0], round(self._hist_rows[i][0]["t"] * 6) / 6)
            except tk.TclError:
                pass

    # ── Clicks ───────────────────────────────────────────────────
    def _hist_on_press(self, e):
        self.win.focus_set()                  # out of the search field
        self._hist_press = (e.x, e.y, self._hist_row_at(self.hist_cv.canvasy(e.y)))

    def _hist_on_release(self, e):
        press, self._hist_press = self._hist_press, None
        d = getattr(self, "_drag", None)
        if press is None or (d and d.get("on")):
            return
        if abs(e.x - press[0]) > 6 or abs(e.y - press[1]) > 6:
            return
        i = press[2]
        if i is not None and i == self._hist_row_at(self.hist_cv.canvasy(e.y)):
            self._show_lyrics(self._hist_rows[i][1])

    def _hist_on_right(self, e):
        i = self._hist_row_at(self.hist_cv.canvasy(e.y))
        if i is not None:
            ent = self._hist_rows[i][1]
            text = f"{ent.get('artist', '')} — {ent.get('title', '')}".strip(" —")
            self._to_clipboard(text, "Copied")

    # ── Scrolling (the Settings/Stats glide) ─────────────────────
    def _hist_scroll_to(self, y, animate=True):
        cv = self.hist_cv
        total = max(1, self._hist_total)
        view = cv.winfo_height()
        y = float(int(round(max(0.0, min(float(max(0, total - view)), float(y))))))
        self._hist_target = y
        if not animate or not M.ANIMATIONS_ENABLED:
            self._cancel("histscroll")
            self._hist_gliding = False
            cv.yview_moveto(y / total)
            self._hist_draw_thumb()
            return
        if self._hist_gliding:
            return
        self._hist_gliding = True

        def step():
            cur = cv.canvasy(0)
            diff = self._hist_target - cur
            if abs(diff) >= 1.0:
                move = int(round(diff * 0.25)) or (1 if diff > 0 else -1)
                cv.yview_scroll(move, "units")
                self._hist_draw_thumb()
                if cv.canvasy(0) != cur:
                    self._schedule("histscroll", 15, step)
                    return
            self._hist_gliding = False
            self._hist_target = cv.canvasy(0)
            self._hist_draw_thumb()
        step()

    def _hist_scroll_by(self, dy):
        base = self._hist_target if self._hist_gliding else self.hist_cv.canvasy(0)
        self._hist_scroll_to(base + dy)

    def _hist_draw_thumb(self):
        """Scrollbar thumb; also keeps the backdrop still and the hover true
        after every scroll step."""
        sb = getattr(self, "_hist_sb", None)
        if sb is None:
            return
        self._bd_follow(self.hist_cv)
        self._bd_attach(sb)
        self._hist_update_hover()
        try:
            total = max(1, self._hist_total)
            view = max(1, self.hist_cv.winfo_height())
            h = sb.winfo_height()
            top = self.hist_cv.canvasy(0)
        except tk.TclError:
            return
        sb.delete("thumb")
        if total <= view:
            return
        th = max(self._ss(28), h * view / total)
        ty = (h - th) * (top / max(1, total - view))
        w = self._ss(4)
        x = self._ss(12) - w // 2
        self._rounded_rect(sb, x, ty, x + w, ty + th, w // 2, fill=M.BG4, outline="", tags=("thumb",))

    # ── Lyrics sheet ─────────────────────────────────────────────
    def _hist_build_sheet(self, p):
        S = self._ss
        sh = tk.Frame(p, bg=M.BG)
        sh._bd_x = None
        self._hist_sheet = sh
        self._hist_sh_head = hd = tk.Canvas(sh, height=S(200), bg=M.BG, highlightthickness=0, bd=0)
        hd.pack(side="top", fill="x")
        self._hist_sh_cv = lc = tk.Canvas(sh, bg=M.BG, highlightthickness=0, bd=0,
                                          yscrollincrement=1, confine=True)
        lc.pack(fill="both", expand=True)
        self._hist_ly_search = tk.StringVar()
        self._hist_ly_entry = _CanvasEntry(self, hd, self._hist_ly_search, "Search these lyrics")
        self._hist_ly_search.trace_add("write", lambda *_: self._hist_mark_hits())
        self._hist_sh_total = 1
        self._hist_sh_target = 0.0
        self._hist_sh_gliding = False
        self._hist_ly_items = []
        self._hist_ly_active = None
        hd.bind("<Configure>", lambda e: self._hist_sheet_draw_head() if self._hist_sheet_entry else None)
        lc.bind("<Configure>", lambda e: self._hist_sheet_draw_lyrics() if self._hist_sheet_entry else None)
        hd.bind("<Button-1>", lambda e: self._hist_blur(e, self._hist_ly_entry), add="+")
        lc.bind("<Button-1>", lambda e: self.win.focus_set(), add="+")
        self._bd_register(hd, "HISTORY")
        self._bd_register(lc, "HISTORY")

    def _show_lyrics(self, e):
        """Open a play's lyrics on the sheet (slides in from the right)."""
        if not e:
            return
        self._hist_sheet_entry = e
        self._lyrics_entry = e
        self._hist_ly_search.set("")
        self._hist_set_hover(None)
        sh = self._hist_sheet
        W = max(1, self._hist_page.winfo_width())
        opening = not sh.winfo_manager()
        if opening:
            sh.place(x=W, y=0, relwidth=1, relheight=1)
            sh._bd_x = W
        sh.tkraise()
        self._hist_sheet_draw_head()
        self._hist_sheet_draw_lyrics()
        self._highlight_active_lyric()
        self._hist_slide_sheet(0)

    def _hist_slide_sheet(self, to, then=None):
        sh = self._hist_sheet
        start = sh._bd_x if sh._bd_x is not None else 0

        def apply(e):
            x = start + (to - start) * e
            sh._bd_x = x
            sh.place_configure(x=int(round(x)))
            for c in (self._hist_sh_head, self._hist_sh_cv):
                self._bd_follow(c)
            if e >= 1.0 and then:
                then()
        self._animate("histsheet", SHEET_MS, apply, ease=bdm.ease_out_quart)

    def _hist_close_sheet(self, animate=True):
        """Slide the sheet away; the list is where it was. True if it was open."""
        if getattr(self, "_hist_sheet_entry", None) is None:
            return False
        self._hist_sheet_entry = None
        self._lyrics_entry = None
        self._hist_ly_items = []
        sh = self._hist_sheet

        def gone():
            try:
                sh.place_forget()
            except tk.TclError:
                pass
            sh._bd_x = None
        if animate and M.ANIMATIONS_ENABLED:
            self._hist_slide_sheet(max(1, self._hist_page.winfo_width()), gone)
        else:
            self._cancel("histsheet")
            gone()
        self._hist_update_hover()
        return True

    def _close_lyrics_panel(self):
        return self._hist_close_sheet()

    def _hist_sheet_draw_head(self):
        e = getattr(self, "_hist_sheet_entry", None)
        cv = self._hist_sh_head
        W = cv.winfo_width()
        if e is None or W < 120:
            return
        S = self._ss
        for t in getattr(self, "_hist_sh_tags", []):
            for seq in ("<Button-1>", "<Enter>", "<Leave>"):
                try:
                    cv.tag_unbind(t, seq)
                except tk.TclError:
                    pass
        cv.delete("all")
        before = len(self.__dict__.get("_hist_tags", []))
        x0, x1 = S(22), W - S(26)
        y = S(12)
        self._st_button(cv, x0 - S(8), y, "‹ History", self._hist_close_sheet, "ghost")
        y += S(28) + S(12)
        art = S(64)
        url = e.get("album_art")
        raw = self._st_art.get(url) if url else None
        if raw is not None and Image is not None:
            small = raw.resize((art, art), Image.LANCZOS)
            img = (bdm.round_rgba(small, S(8)) if M._cover_mode()
                   else M._round_image(small, S(8), M.BG))
            self._hist_sh_art = M.ImageTk.PhotoImage(img)
            cv.create_image(x0, y, anchor="nw", image=self._hist_sh_art)
        else:
            cv.create_image(x0, y, anchor="nw", image=self._pill_photo(art, art, M.BG3, M.BG, radius=S(8)))
            cv.create_text(x0 + art // 2, y + art // 2, text="♫", fill=M.MUTED, font=self._f(M.FS_LARGE))
            if url and M.PIL_AVAILABLE:
                self._st_run(lambda: M._fetch_art(url, M.THUMB_PX),
                             lambda img: (img is not None and self._st_art.__setitem__(url, img),
                                          img is not None and self._hist_sheet_entry is e
                                          and self._hist_sheet_draw_head()))
        tx = x0 + art + S(12)
        f_t = self._f(M.FS_HERO, True)
        room = x1 - tx
        t = cv.create_text(tx, y, anchor="nw", fill=M.TEXT, font=f_t,
                           text=self._st_fit(e.get("title") or "Unknown", f_t, room))
        yy = cv.bbox(t)[3]
        t = cv.create_text(tx, yy, anchor="nw", fill=M.TEXT2, font=self._f(M.FS_BODY),
                           text=self._st_fit(e.get("artist") or "", self._f(M.FS_BODY), room))
        yy = cv.bbox(t)[3]
        self._hist_sh_meta = cv.create_text(tx, yy, anchor="nw", fill=M.MUTED,
                                            font=self._f(M.FS_SMALL), text=self._hist_meta(e))
        y = max(y + art, cv.bbox(self._hist_sh_meta)[3]) + S(14)
        # Actions: only the ones that can do something for this play.
        synced, plain = e.get("synced") or [], e.get("plain") or []
        x = x0
        acts = []
        if synced:
            acts.append(("Export .lrc", lambda: self._do_export(e, "lrc"), "primary"))
        if synced or plain:
            acts.append(("Export .txt", lambda: self._do_export(e, "txt"),
                         "secondary" if synced else "primary"))
            acts.append(("Copy", lambda: self._copy_all_lyrics(e), "ghost"))
        if (e.get("track_uri") or "").startswith("spotify:"):
            acts.append(("Open in Spotify", lambda: self._open_in_spotify(e), "ghost"))
        for label, cmd, kind in acts:
            w = self._st_button(cv, x, y, label, cmd, kind)
            x += w + S(8)
        if acts:
            y += S(28) + S(12)
        if synced or plain:
            self._hist_ly_entry.draw(x0, y, x1 - x0, S(30))
            y += S(30) + S(12)
        self._hist_sh_tags = self.__dict__.get("_hist_tags", [])[before:]
        del self.__dict__.get("_hist_tags", [])[before:]
        cv.config(height=y)
        self._bd_attach(cv)

    def _hist_meta(self, e):
        t = _parse(e.get("played_at"))
        when = ""
        if t is not None:
            when = f"Played {day_label(t.date(), datetime.date.today())} {t.strftime('%H:%M')}"
        badge = lyric_badge(e) or "No lyrics"
        n = len(e.get("synced") or e.get("plain") or [])
        parts = [p for p in (when, badge, f"{n} line{'s' if n != 1 else ''}" if n else "") if p]
        return " · ".join(parts)

    def _hist_flash(self, text):
        cv = self._hist_sh_head
        item = getattr(self, "_hist_sh_meta", None)
        e = getattr(self, "_hist_sheet_entry", None)
        if e is None or item is None:
            return
        try:
            cv.itemconfigure(item, text=text, fill=M.ACCENT)
        except tk.TclError:
            return

        def back():
            if self._hist_sheet_entry is e:
                try:
                    cv.itemconfigure(item, text=self._hist_meta(e), fill=M.MUTED)
                except tk.TclError:
                    pass
        self._schedule("histflash", 2500, back)

    def _hist_sheet_draw_lyrics(self):
        e = getattr(self, "_hist_sheet_entry", None)
        cv = self._hist_sh_cv
        W = cv.winfo_width()
        if e is None or W < 120:
            return
        S = self._ss
        cv.delete("all")
        x0, x1 = S(22), W - S(26)
        font = self._f(M.FS_LARGE)
        f_ts = self._f(M.FS_SMALL)
        synced, plain = e.get("synced") or [], e.get("plain") or []
        ts_w = f_ts.measure("00:00") + S(12) if synced else 0
        y = S(14)
        self._hist_ly_items = []
        lines = ([(fmt_ts(ln.get("startMs")), ln.get("words", "")) for ln in synced]
                 if synced else [("", ln) for ln in plain])
        if not lines:
            cv.create_text(x0 + S(16), y, anchor="nw", text="No lyrics for this track.",
                           fill=M.MUTED, font=font)
            y += font.metrics("linespace")
        for ts, words in lines:
            if ts:
                cv.create_text(x0 + S(16), y + S(3), anchor="nw", text=ts, fill=M.MUTED, font=f_ts)
            t = cv.create_text(x0 + S(16) + ts_w, y, anchor="nw", text=words or " ", fill=M.TEXT2,
                               font=font, width=max(40, x1 - x0 - S(32) - ts_w))
            b = cv.bbox(t)
            self._hist_ly_items.append((t, b[1], b[3], (words or "").lower()))
            y = b[3] + S(6)
        total = y + S(14)
        self._hist_sh_total = total
        cv.config(scrollregion=(0, 0, W, total))
        self._hist_ly_active = None
        self._hist_ly_card()
        self._hist_mark_hits()
        self._bd_attach(cv)
        self._hist_sheet_scroll_to(0, animate=False)

    def _hist_ly_card(self):
        """The card behind the lyrics, fixed to the view (it doesn't scroll)."""
        cv = self._hist_sh_cv
        S = self._ss
        W, H = cv.winfo_width(), cv.winfo_height()
        cv.delete("lycard")
        if W < 120 or H < 40:
            return
        # Runs past the bottom edge, so lines scroll out under the canvas
        # edge rather than out of the card.
        w, h = W - S(48), H + S(16)
        cv.create_image(cv.canvasx(0) + S(22), cv.canvasy(0), anchor="nw", tags=("lycard",),
                        image=self._card_photo(w, h, 10))
        cv.tag_lower("lycard")
        cv.tag_lower("bd")

    def _hist_mark_hits(self):
        cv = self._hist_sh_cv
        cv.delete("lyhit")
        q = self._hist_ly_search.get().strip().lower()
        if not q or not self._hist_ly_items:
            return
        S = self._ss
        first = None
        for item, y0, y1, words in self._hist_ly_items:
            if q in words:
                b = cv.bbox(item)
                w, h = b[2] - b[0] + S(12), y1 - y0 + S(4)
                cv.create_image(b[0] - S(6), y0 - S(2), anchor="nw", tags=("lyhit",),
                                image=self._pill_photo(w, h, M.ACCENT_SOFT, M.BG2, radius=S(6)))
                cv.itemconfigure(item, fill=M.TEXT)
                if first is None:
                    first = y0
            elif item != self._hist_ly_active:
                cv.itemconfigure(item, fill=M.TEXT2)
        cv.tag_raise("lyhit", "lycard")
        if first is not None:
            self._hist_sheet_scroll_to(first - cv.winfo_height() // 3)

    def _hist_sheet_scroll_to(self, y, animate=True):
        cv = self._hist_sh_cv
        total = max(1, self._hist_sh_total)
        view = cv.winfo_height()
        y = float(int(round(max(0.0, min(float(max(0, total - view)), float(y))))))
        self._hist_sh_target = y

        def after():
            self._bd_follow(cv)
            try:
                cv.coords("lycard", cv.canvasx(0) + self._ss(22), cv.canvasy(0))
            except tk.TclError:
                pass
        if not animate or not M.ANIMATIONS_ENABLED:
            self._cancel("histshscroll")
            self._hist_sh_gliding = False
            cv.yview_moveto(y / total)
            after()
            return
        if self._hist_sh_gliding:
            return
        self._hist_sh_gliding = True

        def step():
            cur = cv.canvasy(0)
            diff = self._hist_sh_target - cur
            if abs(diff) >= 1.0:
                cv.yview_scroll(int(round(diff * 0.25)) or (1 if diff > 0 else -1), "units")
                after()
                if cv.canvasy(0) != cur:
                    self._schedule("histshscroll", 15, step)
                    return
            self._hist_sh_gliding = False
            self._hist_sh_target = cv.canvasy(0)
            after()
        step()

    def _hist_sheet_scroll_by(self, dy):
        base = self._hist_sh_target if self._hist_sh_gliding else self._hist_sh_cv.canvasy(0)
        self._hist_sh_user_t = M.time.monotonic()
        self._hist_sheet_scroll_to(base + dy)

    def _highlight_active_lyric(self):
        """Mark the line being sung — only when the sheet shows the track
        that is playing — and keep it in view unless the reader scrolled."""
        e = getattr(self, "_hist_sheet_entry", None)
        if e is None or not self._hist_ly_items:
            return
        if e.get("track_uri") != getattr(M.state, "track_uri", None):
            return
        synced = e.get("synced") or []
        if not synced:
            return
        pos = self._estimate_pos_ms() + M._track_offset_ms()
        idx = 0
        for i, ln in enumerate(synced):
            if ln.get("startMs", 0) <= pos:
                idx = i
            else:
                break
        if idx >= len(self._hist_ly_items):
            return
        cv = self._hist_sh_cv
        item, y0, y1, _w = self._hist_ly_items[idx]
        if item == self._hist_ly_active:
            return
        try:
            if self._hist_ly_active is not None:
                cv.itemconfigure(self._hist_ly_active, fill=M.TEXT2, font=self._f(M.FS_LARGE))
            cv.itemconfigure(item, fill=M.ACCENT, font=self._f(M.FS_LARGE, True))
        except tk.TclError:
            return
        self._hist_ly_active = item
        if M.time.monotonic() - getattr(self, "_hist_sh_user_t", 0) > 4:
            top, view = cv.canvasy(0), cv.winfo_height()
            if not (top + view * 0.2 <= y0 and y1 <= top + view * 0.8):
                self._hist_sheet_scroll_to(y0 - view // 3)

    # ── Actions ──────────────────────────────────────────────────
    def _do_export(self, entry, fmt):
        path = M._export_lyrics(entry, fmt)
        if path:
            self._set_error("")
            M.log(f"Saved to exports/{os.path.basename(path)}")
            self._hist_flash(f"Saved to exports/{os.path.basename(path)}")

    def _copy_all_lyrics(self, entry):
        synced = entry.get("synced") or []
        plain  = entry.get("plain") or []
        body = "\n".join(ln.get("words", "") for ln in synced) if synced else "\n".join(plain)
        if body:
            self._to_clipboard(body, "Copied lyrics")
            self._hist_flash("Copied lyrics")
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
