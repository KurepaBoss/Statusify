"""The Settings page and theming.

Layout: a page title, then grouped cards. Every row is the same shape: a
title with an optional one-line explanation on the left, one control on the
right, hairline dividers between rows.

The whole page is drawn on ONE canvas: text items, and small anti-aliased
PIL images for cards, switches and buttons. Only the text inputs are real
widgets. That's what keeps scrolling clean. When every row, label and
switch was its own native window, Windows repainted them one by one as the
page moved, so mid-scroll the view was a patchwork of rows at their old and
new positions. A canvas redraws its items double-buffered in one pass.

The page is rebuilt from self._set_spec by _set_render() whenever the width,
the palette or a multi-line text changes; it takes a few milliseconds.

Names that belong to main are reached through M, the live main module:
palette colours and settings are rebound at runtime, so they must be read
from main on every use, never copied.
"""
import os
import subprocess
import sys
import tkinter as tk
import tkinter.colorchooser as tkcolor

try:
    from PIL import Image, ImageDraw
except ImportError:
    Image = ImageDraw = None

M = None   # the main module


def _rgb(c):
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


class _Text:
    """Text on the settings canvas, standing in for the Label it replaced
    (main and the tests call .config(text=…) / .cget("text") on these)."""

    def __init__(self, page, text="", fg=None, font=None):
        self._page = page
        self._o = {"text": text, "fg": fg}
        self.font = font
        self.item = None
        self.slot_w = None      # width of its slot in a _Buttons row, once drawn

    def config(self, **kw):
        font_changed = False
        if "font" in kw:
            f = kw.pop("font")
            font_changed = f != self.font
            self.font = f
        if not font_changed and not any(self._o.get(k) != v for k, v in kw.items()):
            return
        self._o.update(kw)
        self._page._set_text_changed(self)
    configure = config

    def cget(self, key):
        return M.BG2 if key == "bg" else self._o.get(key, "")


class _Switch:
    """Toggle switch. The knob slides and the track fades over 160 ms."""

    def __init__(self, page, get, toggle):
        self.p, self.get, self.toggle = page, get, toggle
        self.t = 1.0 if get() else 0.0
        self.item = None
        self.tag = f"sw{id(self)}"

    def size(self):
        return self.p._ss(40), self.p._ss(22)

    def image(self):
        w, h = self.size()
        q = round(self.t * 20) / 20
        key = ("sw", w, h, q, M.BG2, M.BG4, M.ACCENT, M.TEXT2, M.ACCENT_FG)
        cache = self.p.__dict__.setdefault("_pill_cache", {})
        ph = cache.get(key)
        if ph is None:
            sc = 4
            off, on = _rgb(M.BG4), _rgb(M.ACCENT)
            col = tuple(int(off[i] + (on[i] - off[i]) * q) for i in range(3))
            ko, kn = _rgb(M.TEXT2), _rgb(M.ACCENT_FG)
            kcol = tuple(int(ko[i] + (kn[i] - ko[i]) * q) for i in range(3))
            big = Image.new("RGB", (w * sc, h * sc), _rgb(M.BG2))
            d = ImageDraw.Draw(big)
            d.rounded_rectangle((0, 0, w * sc - 1, h * sc - 1), radius=h * sc // 2, fill=col)
            pad = 3 * sc
            k = h * sc - 2 * pad
            x = pad + (w * sc - 2 * pad - k) * q
            d.ellipse((x, pad, x + k, pad + k), fill=kcol)
            ph = M.ImageTk.PhotoImage(big.resize((w, h), Image.LANCZOS))
            cache[key] = ph
        self.ph = ph            # the shared cache may be trimmed; the item keeps its image
        return ph

    def draw(self, cv, x, y):
        self.item = cv.create_image(x, y, anchor="nw", image=self.image(), tags=(self.tag,))
        self.p._set_bind(self.tag, self.click)

    def _run(self, target, animate=True):
        start = self.t
        if start == target:
            return

        def apply(e):
            self.t = start + (target - start) * e
            try:
                self.p.set_cv.itemconfigure(self.item, image=self.image())
            except tk.TclError:
                pass
        if animate:
            self.p._set_animate(f"switch:{self.tag}", 160, apply)
        else:
            apply(1.0)

    def sync(self, animate=True):
        self._run(1.0 if self.get() else 0.0, animate)

    def click(self, _e=None):
        # Move the knob and get it on screen before the setting's side
        # effects run (a config write, an overlay window, a repaint), so the
        # switch answers the click at once however long those take.
        want = 0.0 if self.get() else 1.0
        self._run(want)
        self.p._set_flush()
        self.toggle()
        if (1.0 if self.get() else 0.0) != want:
            self.sync()             # the setting refused the change


class _Buttons:
    """One or more rounded buttons in a row, with an optional value between.

    items: ("btn", text, cmd, kind) or ("value", _Text, min_width)."""

    def __init__(self, page, *items):
        self.p = page
        self.items = items

    def _w(self, it):
        if it[0] == "btn":
            return self.p._f(M.FS_SMALL, True).measure(it[1]) + self.p._ss(24)
        f = self.p._slot_font(it[1])
        return max(self.p._ss(it[2]), f.measure(it[1].cget("text")) + self.p._ss(12))

    def size(self):
        gap = self.p._ss(6)
        return sum(self._w(i) for i in self.items) + gap * (len(self.items) - 1), self.p._ss(28)

    def draw(self, cv, x, y):
        gap = self.p._ss(6)
        h = self.p._ss(28)
        for it in self.items:
            w = self._w(it)
            if it[0] == "btn":
                self.p._set_button(cv, x, y, w, h, it[1], it[2], it[3])
            else:
                slot = it[1]
                col = slot.cget("fg") or M.TEXT2
                slot.item = cv.create_text(x + w // 2, y + h // 2, text=slot.cget("text"),
                                           fill=col, font=slot.font or self.p._f(M.FS_SMALL, True))
                slot.slot_w = w     # a new value that still fits is redrawn in place
            x += w + gap


class _Segmented:
    """A rounded track with the chosen segment raised; it slides on change."""

    def __init__(self, page, options, get, choose):
        self.p, self.options, self.get, self.choose = page, options, get, choose
        self.x = None
        self.hover = None
        self.item = None
        self.tag = f"seg{id(self)}"
        self.want = None        # the key just clicked, shown before choose() returns
        self.max_w = None       # set by _full_seg: squeeze the segments to fit
        self._imgs = {}

    def dims(self):
        f = self.p._f(M.FS_SMALL, True)
        n = len(self.options)
        label = max(f.measure(t) for t, _ in self.options)
        seg = label + self.p._ss(28)
        if self.max_w and seg * n + self.p._ss(6) > self.max_w:
            seg = max(label + self.p._ss(8), (self.max_w - self.p._ss(6)) // n)
        return seg, seg * n + self.p._ss(6), self.p._ss(30)

    def size(self):
        _, w, h = self.dims()
        return w, h

    def current(self):
        return self.want if self.want is not None else self.get()

    def target(self):
        keys = [k for _, k in self.options]
        seg, _, _ = self.dims()
        cur = self.current()
        return (keys.index(cur) if cur in keys else 0) * seg

    def image(self):
        seg, tw, h = self.dims()
        x = int(round(self.x if self.x is not None else self.target()))
        # Rendering the track costs ~2 ms (4x supersampled, then LANCZOS).
        # A page render draws every segmented control, and hover and the
        # slide revisit the same few states, so keep what was drawn.
        key = (seg, tw, h, x, self.hover, self.current(), M._DARK_MODE,
               M.BG2, M.BG3, M.BG4, M.TEXT, M.TEXT2, M.MUTED)
        ph = self._imgs.get(key)
        if ph is not None:
            self.ph = ph
            return ph
        if len(self._imgs) > 96:
            self._imgs.clear()
        self.ph = self._imgs[key] = self._render(seg, tw, h, x)
        return self.ph

    def _render(self, seg, tw, h, x):
        S = self.p._ss
        sc = 4
        big = Image.new("RGB", (tw * sc, h * sc), _rgb(M.BG2))
        d = ImageDraw.Draw(big)
        d.rounded_rectangle((0, 0, tw * sc - 1, h * sc - 1), radius=S(8) * sc, fill=_rgb(M.BG3))
        p = S(3) * sc
        d.rounded_rectangle((x * sc + p, p, (x + seg) * sc + p - 1, h * sc - p - 1),
                            radius=S(6) * sc, fill=_rgb(M.BG4 if M._DARK_MODE else M.BG2))
        img = big.resize((tw, h), Image.LANCZOS)
        dr = ImageDraw.Draw(img)
        TR = self.p._np_text
        px = self.p._px(M.FS_SMALL + 1)
        cur = self.current()
        for i, (label, key) in enumerate(self.options):
            sx = S(3) + i * seg
            col = M.TEXT if key == cur else (M.TEXT2 if self.hover == i else M.MUTED)
            lw = TR.measure(label, "semibold", px)
            TR.draw(dr, (sx + (seg - lw) / 2, (h - TR.line_height("semibold", px)) / 2),
                    label, "semibold", px, _rgb(col))
        return M.ImageTk.PhotoImage(img)

    def refresh(self):
        try:
            self.p.set_cv.itemconfigure(self.item, image=self.image())
        except tk.TclError:
            pass

    def draw(self, cv, x, y):
        self.x = self.target()
        self.item = cv.create_image(x, y, anchor="nw", image=self.image(), tags=(self.tag,))
        self.ox = x
        cv.tag_bind(self.tag, "<Button-1>", self.click)
        cv.tag_bind(self.tag, "<Motion>", self.motion)
        cv.tag_bind(self.tag, "<Enter>", lambda e: cv.config(cursor="hand2"))
        cv.tag_bind(self.tag, "<Leave>", self.leave)

    def _at(self, e):
        seg, _, _ = self.dims()
        x = self.p.set_cv.canvasx(e.x) - self.ox - self.p._ss(3)
        return max(0, min(len(self.options) - 1, int(x // seg)))

    def click(self, e):
        key = self.options[self._at(e)][1]
        # Show the choice first (see _Switch.click), then apply it.
        self.want = key
        self.slide()
        self.p._set_flush()
        try:
            self.choose(key)
        finally:
            self.want = None
        if self.get() != key:
            self.slide()            # refused, or it means something else now

    def slide(self, animate=True):
        tx = self.target()
        sx = self.x if self.x is not None else tx
        if sx == tx:
            self.refresh()
            return

        def apply(e):
            self.x = sx + (tx - sx) * e
            self.refresh()
        if animate:
            self.p._set_animate(f"seg:{self.tag}", 220, apply)
        else:
            apply(1.0)

    def motion(self, e):
        i = self._at(e)
        if i != self.hover:
            self.hover = i
            self.refresh()

    def leave(self, _e):
        self.p.set_cv.config(cursor="")
        self.hover = None
        self.refresh()


class _Swatch:
    def __init__(self, page, cmd):
        self.p, self.cmd = page, cmd
        self.tag = f"swatch{id(self)}"

    def size(self):
        return self.p._ss(44), self.p._ss(24)

    def draw(self, cv, x, y):
        w, h = self.size()
        ph = self.p._pill_photo(w, h, M.USER_ACCENT, M.BG2, radius=self.p._ss(7), outline=M.BORDER)
        cv.create_image(x, y, anchor="nw", image=ph, tags=(self.tag,))
        self.p._set_bind(self.tag, self.cmd)


class _Widget:
    """A real widget embedded in a row (an Entry) or across a card.

    Under each one sits a drawn stand-in: a field box with a snapshot of the
    widget's text. While the page glides, the real widget is hidden and the
    stand-in scrolls with the canvas, because a native child window lags a
    frame behind a canvas that moves (the last source of tearing here)."""

    def __init__(self, widget, full=False, height=None):
        self.w, self.full, self.h = widget, full, height
        self.win = self.ph = None

    def size(self):
        return self.w.winfo_reqwidth(), self.h or self.w.winfo_reqheight()

    def draw(self, cv, x, y, width=None):
        w = width or self.w.winfo_reqwidth()
        h = self.h or self.w.winfo_reqheight()
        # Its own tag per render, so a click that lands on the stand-in (the
        # real widget is hidden mid-glide) can bring the widget back and
        # focus it; see SettingsPage._set_embed_click.
        self.tag = f"embedph{getattr(cv, 'page_gen', 0)}_{len(cv.page_widgets)}"
        cv.create_rectangle(x, y, x + w - 1, y + h - 1, fill=self.w.cget("bg"),
                            outline=M.BORDER, tags=("embedph", self.tag))
        self.ph = cv.create_text(x + 6, y + 4, anchor="nw", text="", fill=self.w.cget("fg"),
                                 font=self.w.cget("font"), width=max(10, w - 12),
                                 tags=("embedph", self.tag))
        self.ph_h = h
        kw = {"width": width} if width else {}
        if self.h:
            kw["height"] = self.h
        self.win = cv.create_window(x, y, anchor="nw", window=self.w, tags=("embed",), **kw)
        cv.page_widgets.append(self)

    def snapshot(self):
        w = self.w
        try:
            if isinstance(w, tk.Entry):
                # One line, cut to the field like the Entry itself would.
                t, f = w.get(), M.tkfont.Font(font=w.cget("font"))
                room = w.winfo_width() - 12
                while t and f.measure(t) > room:
                    t = t[:-1]
                return t
            if isinstance(w, tk.Listbox):
                return chr(10).join(w.get(0, "end"))
            if isinstance(w, tk.Text):
                first = w.index("@0,0")
                last = w.index(f"@0,{w.winfo_height()}")
                return w.get(first, f"{last} lineend")
        except tk.TclError:
            pass
        return ""


class SettingsPage:

    # ── Helpers ──────────────────────────────────────────────────
    def _ss(self, v):
        """Scale a 96-dpi pixel length to this screen."""
        s = getattr(self, "_np_s", None)
        if s is None:
            try:
                s = float(self._root.winfo_fpixels("1p")) / (96 / 72)
            except tk.TclError:
                s = 1.0
        return int(round(v * s))

    def _pill_photo(self, w, h, fill, bg, radius=None, outline=None):
        """Rounded rectangle composited onto `bg`, as a PhotoImage. Cached."""
        key = (w, h, fill, bg, radius, outline)
        cache = self.__dict__.setdefault("_pill_cache", {})
        ph = cache.get(key)
        if ph is not None:
            return ph
        r = h // 2 if radius is None else radius
        sc = 4
        big = Image.new("RGB", (w * sc, h * sc), _rgb(bg))
        d = ImageDraw.Draw(big)
        if outline:
            d.rounded_rectangle((0, 0, w * sc - 1, h * sc - 1), radius=r * sc, fill=_rgb(outline))
            d.rounded_rectangle((sc, sc, w * sc - 1 - sc, h * sc - 1 - sc),
                                radius=max(0, r * sc - sc), fill=_rgb(fill))
        else:
            d.rounded_rectangle((0, 0, w * sc - 1, h * sc - 1), radius=r * sc, fill=_rgb(fill))
        ph = M.ImageTk.PhotoImage(big.resize((w, h), Image.LANCZOS))
        if len(cache) > 600:
            # Keep the last generation alive: items drawn from it earlier in
            # this same render would otherwise lose their image (Tk deletes a
            # PhotoImage with its last Python reference) and show as blanks.
            self._pill_cache_old = dict(cache)
            cache.clear()
        cache[key] = ph
        return ph

    def _set_animate(self, key, duration_ms, apply):
        """_animate, but the click frame already shows movement.

        _animate's first call is apply(0): the control redrawn exactly as it
        was, so the first visible change waited a whole frame (~17 ms
        measured). Start one frame in instead."""
        frame = 1000.0 / getattr(self, "ANIM_FPS", 60)
        lead = 1.0 - (1.0 - min(1.0, frame / max(1, duration_ms))) ** 3
        self._animate(key, duration_ms, lambda e: apply(e if e >= 1.0 else max(e, lead)))

    def _set_flush(self):
        """Paint pending canvas changes now, inside the click handler, so a
        slow side effect that follows can't hold back the feedback."""
        try:
            self.set_cv.update_idletasks()
        except (tk.TclError, AttributeError):
            pass

    def _slot_font(self, slot):
        """The Tk font a _Buttons value slot is drawn in."""
        f = getattr(slot, "font", None)
        if not f:
            return self._f(M.FS_SMALL, True)
        cache = self.__dict__.setdefault("_slot_fonts", {})
        k = repr(f)
        if k not in cache:
            try:
                cache[k] = M.tkfont.Font(font=f)
            except tk.TclError:
                return self._f(M.FS_SMALL, True)
        return cache[k]

    def _corner(self, r, q):
        """Anti-aliased card corner (quadrant q: 0 tl, 1 tr, 2 bl, 3 br)."""
        key = ("corner", r, q, M.BG, M.BG2, M.BORDER)
        cache = self.__dict__.setdefault("_pill_cache", {})
        ph = cache.get(key)
        if ph is None:
            sc = 4
            d2 = 2 * r * sc
            big = Image.new("RGB", (d2, d2), _rgb(M.BG))
            d = ImageDraw.Draw(big)
            d.ellipse((0, 0, d2 - 1, d2 - 1), fill=_rgb(M.BORDER))
            d.ellipse((sc, sc, d2 - 1 - sc, d2 - 1 - sc), fill=_rgb(M.BG2))
            img = big.resize((2 * r, 2 * r), Image.LANCZOS)
            box = [(0, 0, r, r), (r, 0, 2 * r, r), (0, r, r, 2 * r), (r, r, 2 * r, 2 * r)][q]
            ph = M.ImageTk.PhotoImage(img.crop(box))
            cache[key] = ph
        return ph

    def _set_bind(self, tag, cmd):
        cv = self.set_cv
        self.__dict__.setdefault("_set_live_tags", []).append(tag)
        cv.tag_bind(tag, "<Button-1>", lambda e: cmd())
        cv.tag_bind(tag, "<Enter>", lambda e: cv.config(cursor="hand2"))
        cv.tag_bind(tag, "<Leave>", lambda e: cv.config(cursor=""))

    def _set_button(self, cv, x, y, w, h, text, cmd, kind="secondary"):
        """A rounded button drawn on the canvas, with a 120 ms hover fade."""
        tag = f"btn{self._set_gen}_{int(x)}_{int(y)}"
        def colours(hover):
            if kind == "primary":
                return (M._blend(M.ACCENT, M.TEXT, 0.15) if hover else M.ACCENT), M.ACCENT_FG
            if kind == "ghost":
                return (M.BG3 if hover else M.BG2), (M.TEXT if hover else M.TEXT2)
            return (M.BG4 if hover else M.BG3), M.TEXT
        fill, fg = colours(False)
        img = cv.create_image(x, y, anchor="nw", tags=(tag,),
                              image=self._pill_photo(w, h, fill, M.BG2, radius=self._ss(7)))
        txt = cv.create_text(x + w // 2, y + h // 2, text=text, fill=fg,
                             font=self._f(M.FS_SMALL, True), tags=(tag,))
        st = {"t": 0.0}

        def fade(to):
            a = st["t"]
            def apply(e):
                st["t"] = a + (to - a) * e
                f0, g0 = colours(False)
                f1, g1 = colours(True)
                f = M._blend(f0, f1, round(st["t"] * 6) / 6)
                try:
                    cv.itemconfigure(img, image=self._pill_photo(w, h, f, M.BG2, radius=self._ss(7)))
                    cv.itemconfigure(txt, fill=M._blend(g0, g1, st["t"]))
                except tk.TclError:
                    pass
            self._animate(f"hover:{tag}", 120, apply)
        self.__dict__.setdefault("_set_live_tags", []).append(tag)
        cv.tag_bind(tag, "<Enter>", lambda e: (cv.config(cursor="hand2"), fade(1.0)))
        cv.tag_bind(tag, "<Leave>", lambda e: (cv.config(cursor=""), fade(0.0)))
        cv.tag_bind(tag, "<Button-1>", lambda e: cmd())

    def _wbutton(self, parent, text, cmd, kind="secondary", bg_tok="BG"):
        """Widget version of the rounded button, for dialogs."""
        font = self._f(M.FS_SMALL, True)
        h, w = self._ss(28), font.measure(text) + self._ss(24)
        lbl = tk.Label(parent, text=text, compound="center", bd=0, font=font,
                       cursor="hand2", highlightthickness=0, padx=0, pady=0)
        def paint(hover=False):
            bg = getattr(M, bg_tok)
            if kind == "primary":
                fill, fg = (M._blend(M.ACCENT, M.TEXT, 0.15) if hover else M.ACCENT), M.ACCENT_FG
            elif kind == "ghost":
                fill, fg = (M.BG3 if hover else bg), (M.TEXT if hover else M.TEXT2)
            else:
                fill, fg = (M.BG4 if hover else M.BG3), M.TEXT
            lbl.config(image=self._pill_photo(w, h, fill, bg, radius=self._ss(7)), fg=fg, bg=bg)
        lbl.bind("<Enter>", lambda e: paint(True))
        lbl.bind("<Leave>", lambda e: paint(False))
        lbl.bind("<Button-1>", lambda e: cmd())
        paint()
        return lbl

    def _entry(self, var, width):
        e = tk.Entry(self.set_cv, textvariable=var, bg=M.BG3, fg=M.TEXT, insertbackground=M.TEXT,
                     relief="flat", font=self._f(M.FS_BODY), width=width)
        self._focus_ring(e)
        return e

    def _sync_settings_switches(self):
        for s in getattr(self, "_switches", []):
            s.sync()

    # ── Spec helpers ─────────────────────────────────────────────
    def _switch_ctl(self, get, toggle):
        sw = _Switch(self, get, toggle)
        self.__dict__.setdefault("_switches", []).append(sw)
        return sw

    def _set_text_changed(self, slot):
        cv = getattr(self, "set_cv", None)
        if cv is None or slot.item is None:
            return
        kw = {"text": slot.cget("text"), "fill": slot.cget("fg") or M.TEXT2}
        if slot.font:
            kw["font"] = slot.font
        try:
            before = cv.bbox(slot.item)
            cv.itemconfigure(slot.item, **kw)
            after = cv.bbox(slot.item)
        except tk.TclError:
            return
        if slot.slot_w:
            # A value in a button row is centred in a slot at least min_width
            # wide. Only a value that no longer fits needs the row laid out
            # again; everything else was just redrawn in place. (Comparing
            # against the previous text's width re-rendered the whole page,
            # ~25 ms, whenever a value got a few pixels wider.)
            if self._slot_font(slot).measure(kw["text"]) + self._ss(12) > slot.slot_w:
                self._set_relayout_soon()
            return
        if not before or not after or (before[3] - before[1]) != (after[3] - after[1]) \
                or (after[2] - after[0]) > (before[2] - before[0]) + 4:
            self._set_relayout_soon()

    def _set_relayout_soon(self):
        self._schedule("setrender", 30, self._set_render)

    # ── Rendering ────────────────────────────────────────────────
    def _set_render(self):
        cv = getattr(self, "set_cv", None)
        if cv is None:
            return
        W = cv.winfo_width()
        if W < 120:
            return
        top = cv.canvasy(0)
        focused = self._set_focused_embed()
        cv.delete("all")
        cv.page_widgets = []
        # Tag bindings outlive the items that carried them. Position-based
        # tags from the last layout must not be reused by this one, or a
        # plain row that lands where a switch row used to be inherits that
        # switch's click (and silently flips a setting). A new generation
        # per render keeps every tag unique; the old ones are unbound.
        for t in getattr(self, "_set_live_tags", ()):
            for seq in ("<Button-1>", "<Enter>", "<Leave>"):
                try:
                    cv.tag_unbind(t, seq)
                except tk.TclError:
                    pass
        self._set_live_tags = []
        self._set_gen = getattr(self, "_set_gen", 0) + 1
        cv.page_gen = self._set_gen
        S = self._ss
        x0, x1 = S(2), W - S(2)
        y = S(18)
        for spec in self._set_spec:
            y = getattr(self, "_set_draw_" + spec[0])(cv, x0, x1, y, *spec[1:])
        for wd in cv.page_widgets:
            self._set_live_tags.append(wd.tag)
            cv.tag_bind(wd.tag, "<Button-1>", lambda e, wd=wd: self._set_embed_click(wd, e))
        total = y + S(28)
        self._set_total = total
        cv.config(scrollregion=(0, 0, W, total))
        # Not animated: this also ends any glide, so every real widget is
        # shown again (a render mid-glide left them hidden behind stand-ins).
        self._set_scroll_to(top, animate=False)
        # The inputs are re-embedded, not recreated; keep the caret where it was.
        if focused is not None and any(wd.w is focused for wd in cv.page_widgets):
            try:
                if cv.focus_get() is not focused:
                    focused.focus_set()
            except (tk.TclError, KeyError):
                pass

    def _set_focused_embed(self):
        """The embedded input that has keyboard focus, if any."""
        cv = self.set_cv
        try:
            f = cv.focus_get()
        except (tk.TclError, KeyError):
            return None
        for wd in getattr(cv, "page_widgets", ()):
            if wd.w is f:
                return f
        return None

    def _set_draw_title(self, cv, x0, x1, y, title, sub):
        S = self._ss
        t = cv.create_text(x0, y, anchor="nw", text=title, fill=M.TEXT,
                           font=self._f(M.FS_HERO + 4, True))
        y = cv.bbox(t)[3]
        s = cv.create_text(x0, y, anchor="nw", text=sub, fill=M.MUTED, font=self._f(M.FS_SMALL))
        return cv.bbox(s)[3] + S(4)

    def _set_draw_section(self, cv, x0, x1, y, title, sub=None):
        S = self._ss
        y += S(22)
        t = cv.create_text(x0, y, anchor="nw", text=title, fill=M.TEXT,
                           font=self._f(M.FS_LARGE + 1, True))
        y = cv.bbox(t)[3]
        if sub:
            s = cv.create_text(x0, y, anchor="nw", text=sub, fill=M.MUTED,
                               font=self._f(M.FS_SMALL), width=x1 - x0)
            y = cv.bbox(s)[3]
        return y + S(8)

    def _set_draw_card(self, cv, x0, x1, y, rows):
        S = self._ss
        top = y
        pad, vpad = S(16), S(12)
        first = True
        tag = f"card{top}"
        for row in rows:
            if not first and not row.get("nodiv"):
                cv.create_line(x0 + pad, y, x1, y, fill=M.BORDER)
            first = False
            kind = row.get("kind", "row")
            if kind == "row":
                y += self._set_draw_row(cv, x0 + pad, x1 - pad, y, row, vpad)
            elif kind == "full":
                w = row["widget"]
                y += row.get("top", 0)
                w.draw(cv, x0 + pad, y, width=x1 - x0 - 2 * pad)
                y += w.size()[1] + row.get("bottom", vpad)
            elif kind == "extra":
                y += row["draw"](cv, x0 + pad, x1 - pad, y)
        bottom = y
        self._set_card_bg(cv, x0, top, x1, bottom, tag)
        return bottom

    def _set_card_bg(self, cv, x0, y0, x1, y1, tag):
        r = self._ss(10)
        b = M.BORDER
        ids = [
            cv.create_rectangle(x0 + r, y0, x1 - r, y1, fill=M.BG2, outline="", tags=(tag,)),
            cv.create_rectangle(x0, y0 + r, x1, y1 - r, fill=M.BG2, outline="", tags=(tag,)),
            cv.create_image(x0, y0, anchor="nw", image=self._corner(r, 0), tags=(tag,)),
            cv.create_image(x1, y0, anchor="ne", image=self._corner(r, 1), tags=(tag,)),
            cv.create_image(x0, y1, anchor="sw", image=self._corner(r, 2), tags=(tag,)),
            cv.create_image(x1, y1, anchor="se", image=self._corner(r, 3), tags=(tag,)),
            cv.create_line(x0 + r, y0, x1 - r, y0, fill=b, tags=(tag,)),
            cv.create_line(x0 + r, y1 - 1, x1 - r, y1 - 1, fill=b, tags=(tag,)),
            cv.create_line(x0, y0 + r, x0, y1 - r, fill=b, tags=(tag,)),
            cv.create_line(x1 - 1, y0 + r, x1 - 1, y1 - r, fill=b, tags=(tag,)),
        ]
        for i in ids:
            cv.tag_lower(i)

    def _set_draw_row(self, cv, x0, x1, y, row, vpad):
        S = self._ss
        ctl = row.get("ctl")
        cw, ch = ctl.size() if ctl else (0, 0)
        tw = max(S(80), (x1 - x0) - (cw + S(16) if ctl else 0))
        tag = f"row{self._set_gen}_{y}"
        t = cv.create_text(x0, y + vpad, anchor="nw", text=row["title"], fill=M.TEXT,
                           font=self._f(M.FS_BODY), width=tw, tags=(tag,))
        th = cv.bbox(t)[3] - (y + vpad)
        if row.get("desc"):
            d = cv.create_text(x0, y + vpad + th + S(1), anchor="nw", text=row["desc"],
                               fill=M.MUTED, font=self._f(M.FS_SMALL), width=tw, tags=(tag,))
            th = cv.bbox(d)[3] - (y + vpad)
        h = max(th, ch) + 2 * vpad
        if ctl:
            ctl.draw(cv, x1 - cw, y + (h - ch) // 2)
            if isinstance(ctl, _Switch):
                # The whole row toggles, like a native settings list.
                self._set_bind(tag, ctl.click)
        return h

    # ── Scrolling ────────────────────────────────────────────────
    def _set_scroll_to(self, y, animate=True):
        """Glide the view's top edge to y pixels. Wheel notches extend the
        same glide instead of restarting it."""
        cv = self.set_cv
        total = max(1, getattr(self, "_set_total", 1))
        view = cv.winfo_height()
        maxy = max(0, total - view)
        # Whole pixels: the canvas scrolls in 1 px units (yscrollincrement).
        y = int(round(max(0.0, min(float(maxy), float(y)))))
        self._set_target = float(y)
        if not animate or not M.ANIMATIONS_ENABLED:
            was_gliding = self._set_glide_stop()
            self._set_target = float(y)
            cv.yview_moveto(y / total)
            self._set_draw_thumb()
            if was_gliding:
                self._set_embeds(True)
            return
        if getattr(self, "_set_gliding", False):
            return
        self._set_gliding = True
        self._set_embeds(False)

        def step():
            # The glide used to set a fractional position with yview_moveto.
            # Tk rounds that to a whole pixel, so a quarter of a 1 px (or,
            # upwards, 2 px) gap rounded back to where the view already was:
            # the glide never arrived, re-armed itself every 15 ms for good,
            # and kept every text box hidden behind its unclickable stand-in.
            # Step in whole pixels, at least one, and stop at an edge.
            cur = cv.canvasy(0)
            diff = self._set_target - cur
            if abs(diff) >= 1.0:
                move = int(round(diff * 0.25)) or (1 if diff > 0 else -1)
                cv.yview_scroll(move, "units")
                self._set_draw_thumb()
                if cv.canvasy(0) != cur:
                    self._schedule("setscroll", 15, step)
                    return
            self._set_gliding = False
            self._set_target = cv.canvasy(0)
            self._set_draw_thumb()
            self._set_embeds(True)
        step()

    def _set_glide_stop(self):
        """End a glide where it is. Returns whether one was running."""
        was = getattr(self, "_set_gliding", False)
        self._cancel("setscroll")
        self._set_gliding = False
        if was:
            try:
                self._set_target = self.set_cv.canvasy(0)
            except tk.TclError:
                pass
        return was

    def _set_embed_click(self, wd, e):
        """A click on a stand-in: the real widget was hidden for a glide.
        Stop the glide right there, so the field stays under the pointer,
        bring the widgets back and focus this one as its own click would."""
        self._set_glide_stop()
        self._set_embeds(True)
        self._set_draw_thumb()
        self._set_flush()
        self._set_focus_widget(wd.w, e.x_root, e.y_root)
        return "break"

    @staticmethod
    def _set_focus_widget(w, x_root, y_root):
        try:
            w.focus_set()
            x, y = x_root - w.winfo_rootx(), y_root - w.winfo_rooty()
            if isinstance(w, tk.Entry):
                w.icursor(f"@{x}")
                w.selection_clear()
            elif isinstance(w, tk.Text):
                w.mark_set("insert", f"@{x},{y}")
            elif isinstance(w, tk.Listbox):
                i = w.nearest(y)
                w.selection_clear(0, "end")
                w.selection_set(i)
                w.activate(i)
        except tk.TclError:
            pass

    def _set_embeds(self, show):
        """Swap the real input widgets for their drawn stand-ins (see _Widget)."""
        cv = self.set_cv
        for wd in getattr(cv, "page_widgets", []):
            try:
                if not show:
                    cv.itemconfigure(wd.ph, text=wd.snapshot())
                cv.itemconfigure(wd.win, state="normal" if show else "hidden")
            except tk.TclError:
                pass

    def _set_scroll_by(self, dy):
        base = self._set_target if getattr(self, "_set_gliding", False) else self.set_cv.canvasy(0)
        self._set_scroll_to(base + dy)

    def _set_draw_thumb(self):
        sb = getattr(self, "_set_sb", None)
        if sb is None:
            return
        try:
            total = max(1, getattr(self, "_set_total", 1))
            view = max(1, self.set_cv.winfo_height())
            h = sb.winfo_height()
            top = self.set_cv.canvasy(0)
        except tk.TclError:
            return
        sb.delete("all")
        if total <= view:
            return
        th = max(self._ss(28), h * view / total)
        ty = (h - th) * (top / max(1, total - view))
        w = self._ss(4) if not getattr(self, "_set_sb_hot", False) else self._ss(6)
        x = (sb.winfo_width() - w) // 2
        col = M.MUTED if getattr(self, "_set_sb_hot", False) else M.BG4
        self._rounded_rect(sb, x, ty, x + w, ty + th, w // 2, fill=col, outline="")

    # ── Build ────────────────────────────────────────────────────
    def _build_settings(self):
        p = tk.Frame(self._container, bg=M.BG); self._pages["SETTINGS"] = p
        S = self._ss
        area = tk.Frame(p, bg=M.BG)
        area.pack(fill="both", expand=True, padx=(S(20), S(6)), pady=(0, S(4)))
        # The scrollbar is packed first and never unpacked, so the content
        # width can't change while the page scrolls.
        self._set_sb = tk.Canvas(area, width=S(12), bg=M.BG, highlightthickness=0, bd=0)
        self._set_sb.pack(side="right", fill="y", padx=(S(6), 0))
        self.set_cv = tk.Canvas(area, bg=M.BG, highlightthickness=0, bd=0,
                                yscrollincrement=1, confine=True)
        self.set_cv.pack(side="left", fill="both", expand=True)
        cv = self.set_cv
        self._set_total = 1
        self._set_target = 0.0
        self._switches = []

        last_w = {"w": 0}
        def _cfg(e):
            if e.width != last_w["w"]:
                last_w["w"] = e.width
                self._set_render()
            else:
                self._set_scroll_to(cv.canvasy(0), animate=False)
        cv.bind("<Configure>", _cfg)

        def _wheel(e):
            if self._cur_page != "SETTINGS":
                return
            if e.widget is getattr(self, "log_txt", None):
                return          # the log scrolls itself
            self._set_scroll_by(-(e.delta / 120.0) * S(84))
        cv.bind_all("<MouseWheel>", _wheel, add="+")

        sb = self._set_sb
        def _sb_hot(v):
            self._set_sb_hot = v
            self._set_draw_thumb()
        sb.bind("<Enter>", lambda e: _sb_hot(True))
        sb.bind("<Leave>", lambda e: _sb_hot(False))
        sb.bind("<Configure>", lambda e: self._set_draw_thumb())
        def _sb_drag(e):
            view = cv.winfo_height()
            frac = max(0.0, min(1.0, e.y / max(1, sb.winfo_height())))
            self._set_scroll_to(frac * max(0, self._set_total - view), animate=False)
        sb.bind("<Button-1>", _sb_drag)
        sb.bind("<B1-Motion>", _sb_drag)

        T = lambda text="", fg=None: _Text(self, text, fg)
        spec = [("title", "Settings", "Changes save as you make them.")]

        # ── Lyrics ─────────────────────────────────────────────────
        self.lbl_lyric_size = T()
        def _paint_lf():
            b = M.LYRIC_FONT_BOOST
            self.lbl_lyric_size.config(text="Default" if b == 0 else f"{b:+d}",
                                       fg=M.ACCENT if b else M.TEXT2)
        def _nudge_lf(delta):
            M.LYRIC_FONT_BOOST = max(-2, min(10, M.LYRIC_FONT_BOOST + delta))
            M._cfg_set_soon("preferences", "lyric_font_boost", str(M.LYRIC_FONT_BOOST))
            try:
                self._np_relayout()
            except AttributeError:
                pass
            _paint_lf()
        _paint_lf()

        self.lbl_track_off = T()
        def _nudge_track_offset(delta):
            uri = getattr(M.state, "track_uri", "")
            if not uri:
                M.log("No track playing — per-track offset not saved")
                return
            M._set_track_offset_ms(uri, M._track_offset_ms(uri) + delta)
            self._refresh_track_offset()
        def _clear_track_offset():
            uri = getattr(M.state, "track_uri", "")
            if uri:
                M._set_track_offset_ms(uri, None)
                self._refresh_track_offset()
        self._refresh_track_offset()

        # Switches save with _cfg_set_soon: the value is live in memory at
        # once and the file is written off the Tk thread when clicking stops.
        def _toggle_lrclib():
            M.LRCLIB_ENABLED = not M.LRCLIB_ENABLED
            M._cfg_set_soon("preferences", "lrclib_fallback", str(M.LRCLIB_ENABLED).lower())

        spec += [("section", "Lyrics"), ("card", [
            {"title": "Offset for this track",
             "desc": "Shifts the timing of the song that's playing, like the Lyrics page's delay "
                     "stepper. Shift-click that stepper to change the global delay instead.",
             "ctl": _Buttons(self, ("btn", "−250", lambda: _nudge_track_offset(-250), "secondary"),
                             ("value", self.lbl_track_off, 64),
                             ("btn", "+250", lambda: _nudge_track_offset(250), "secondary"),
                             ("btn", "Reset", _clear_track_offset, "ghost"))},
            {"title": "Search LRCLIB as a fallback",
             "desc": "Used only when Spicy Lyrics and Spotify have nothing for a track.",
             "ctl": self._switch_ctl(lambda: M.LRCLIB_ENABLED, _toggle_lrclib)},
            *self._overlay_settings_rows(T),     # Desktop overlay (statusify_ui_overlay)
        ] + self._translate_rows())]

        # ── Appearance ─────────────────────────────────────────────
        self._theme_seg = _Segmented(
            self, [("Dark", "dark"), ("Light", "light")],
            lambda: getattr(self, "_theme_want", None) or ("dark" if M._DARK_MODE else "light"),
            self._set_theme_soon)

        def _toggle_tint():
            M.ALBUM_TINT = not M.ALBUM_TINT
            M._cfg_set_soon("preferences", "album_tint", str(M.ALBUM_TINT).lower())
            # Recolouring rebuilds every page (~50 ms). Let the knob finish
            # its 160 ms slide first; repeated clicks make one repaint.
            self._schedule("setrepaint", 170, self._repaint_everything)
        def _toggle_anim():
            M.ANIMATIONS_ENABLED = not M.ANIMATIONS_ENABLED
            M._cfg_set_soon("preferences", "animations", str(M.ANIMATIONS_ENABLED).lower())
            M.log(f"Smooth animations {'enabled' if M.ANIMATIONS_ENABLED else 'disabled'}")
        def _set_quality(q):
            M.RENDER_QUALITY = q
            M._cfg_set_soon("preferences", "render_quality", q)
            # Auto starts measuring afresh.
            self._np_auto_tier, self._np_frames, self._np_cost = 0, 0, None
            self._np_fluid_cache = None
        spec += [("section", "Appearance"), ("card", [
            {"title": "Theme", "ctl": self._theme_seg},
            {"title": "Colours from the album art",
             "desc": "The window and the moving background take their colours from the cover.",
             "ctl": self._switch_ctl(lambda: M.ALBUM_TINT, _toggle_tint)},
            {"title": "Accent colour", "desc": "Used when album colours are off.",
             "ctl": _Swatch(self, self._pick_accent)},
            {"title": "Motion",
             "desc": "Moving background, gliding lyrics and animated controls. "
                     "Turn off to save CPU or reduce motion.",
             "ctl": self._switch_ctl(lambda: M.ANIMATIONS_ENABLED, _toggle_anim)},
            {"title": "Performance",
             "desc": "Auto measures how long each frame takes and eases off on slower PCs. "
                     "Fast keeps the background still.",
             "ctl": _Segmented(self, [("Auto", "auto"), ("Smooth", "high"), ("Fast", "low")],
                               lambda: M.RENDER_QUALITY, _set_quality)},
        ] + self._np_appearance_rows(_Buttons, _Segmented, T))]

        # ── Playback ───────────────────────────────────────────────
        spec += [("section", "Playback"), ("card", self._sleep_rows())]

        # ── Window ─────────────────────────────────────────────────
        def _toggle_ct():
            M.CLOSE_TO_TRAY = not M.CLOSE_TO_TRAY
            M._cfg_set_soon("preferences", "close_to_tray", str(M.CLOSE_TO_TRAY).lower())
            if M.CLOSE_TO_TRAY and not getattr(self, "_tray", None):
                M.log("Note: pystray not installed — close will minimise instead")
        def _toggle_sm():
            M.START_MINIMIZED = not M.START_MINIMIZED
            M._cfg_set_soon("preferences", "start_minimized", str(M.START_MINIMIZED).lower())
        self._startup_on = M._get_startup_enabled()
        def _toggle_startup():
            # A registry write; after the knob has moved, once per burst.
            self._startup_on = not self._startup_on
            self._schedule("setstartup", 200, lambda: M._set_startup_enabled(self._startup_on))
        spec += [("section", "Window and startup"), ("card", [
            {"title": "Always on top", "desc": "Ctrl+T",
             "ctl": self._switch_ctl(lambda: M.ALWAYS_ON_TOP, lambda: self._toggle_topmost())},
            {"title": "Close to the tray",
             "desc": "Closing the window keeps Statusify and your Discord status running.",
             "ctl": self._switch_ctl(lambda: M.CLOSE_TO_TRAY, _toggle_ct)},
            {"title": "Start minimised to the tray",
             "ctl": self._switch_ctl(lambda: M.START_MINIMIZED, _toggle_sm)},
            {"title": "Launch when Windows starts",
             "ctl": self._switch_ctl(lambda: self._startup_on, _toggle_startup)},
            {"title": "Window position", "desc": "Move the window back to the centre of the screen.",
             "ctl": _Buttons(self, ("btn", "Centre", lambda: self._center(force=True), "secondary"))},
            {"title": "Desktop shortcut",
             "ctl": _Buttons(self, ("btn", "Create", self._do_shortcut, "secondary"))},
        ])]

        # ── Discord ────────────────────────────────────────────────
        rpc = M._rpc_mod
        def _toggle_paused_rpc():
            M.SHOW_PAUSED_RPC = not M.SHOW_PAUSED_RPC
            M._cfg_set_soon("preferences", "show_paused_rpc", str(M.SHOW_PAUSED_RPC).lower())
            M.log(f'Paused RPC {"enabled" if M.SHOW_PAUSED_RPC else "disabled"}')
        def _toggle_status_song():
            on = rpc.status_display_type != rpc.STATUS_DISPLAY_DETAILS
            rpc.status_display_type = rpc.STATUS_DISPLAY_DETAILS if on else rpc.STATUS_DISPLAY_NAME
            M._cfg_set_soon("preferences", "status_shows_song", str(on).lower())
        def _toggle_link():
            rpc.link_track = not rpc.link_track
            M._cfg_set_soon("preferences", "link_track", str(rpc.link_track).lower())
        def _toggle_listen_btn():
            rpc.listen_button = not rpc.listen_button
            M._cfg_set_soon("preferences", "listen_button", str(rpc.listen_button).lower())

        self._instr_var = tk.StringVar(value=M.INSTRUMENTAL_TEXT)
        ent_it = self._entry(self._instr_var, 20)
        def _save_instr(_e=None):
            M.INSTRUMENTAL_TEXT = self._instr_var.get() or "🎵 ─ ─ ─ ─ ─ ─ ─ ─ ─ 🎵"
            M._cfg_set_soon("preferences", "instrumental_text", M.INSTRUMENTAL_TEXT)
        ent_it.bind("<Return>", _save_instr)
        ent_it.bind("<FocusOut>", _save_instr)

        self._prof_lb = tk.Listbox(cv, bg=M.BG3, fg=M.TEXT2, selectbackground=M.ACCENT,
                                   selectforeground=M.ACCENT_FG, relief="flat", font=self._f(M.FS_BODY),
                                   height=3, activestyle="none", bd=0, highlightthickness=0)
        self._load_profiles()

        def _prof_buttons(cv, x0, x1, y):
            h = S(28)
            f = self._f(M.FS_SMALL, True)
            x = x0
            for text, cmd in (("Add…", self._add_profile), ("Switch to selected", self._switch_profile)):
                w = f.measure(text) + S(24)
                self._set_button(cv, x, y, w, h, text, cmd)
                x += w + S(6)
            w = f.measure("Delete") + S(24)
            self._set_button(cv, x1 - w, y, w, h, "Delete", self._del_profile, "ghost")
            return h + S(14)

        spec += [("section", "Discord"), ("card", [
            {"title": "Connection", "desc": "Reconnect if your status stopped updating.",
             "ctl": _Buttons(self, ("btn", "Test", self._test_presence, "secondary"),
                             ("btn", "Reconnect", self._reconnect_rpc, "secondary"))},
            {"title": "Show when paused", "desc": 'Keep the status up with a "Paused" state.',
             "ctl": self._switch_ctl(lambda: M.SHOW_PAUSED_RPC, _toggle_paused_rpc)},
            {"title": "Song in the member list",
             "desc": "Show the song under your name instead of the app name.",
             "ctl": self._switch_ctl(lambda: rpc.status_display_type == rpc.STATUS_DISPLAY_DETAILS,
                                     _toggle_status_song)},
            {"title": "Link the song to Spotify", "desc": "Clicking the title opens the track.",
             "ctl": self._switch_ctl(lambda: rpc.link_track, _toggle_link)},
            {"title": "Show 'Listen on Spotify' button",
             "desc": "Friends can open the track from your profile. Discord hides it from you.",
             "ctl": self._switch_ctl(lambda: rpc.listen_button, _toggle_listen_btn)},
            {"title": "Instrumental text", "desc": "Shown on Discord between sung lines.",
             "ctl": _Widget(ent_it, height=S(28))},
            {"title": "App profiles", "desc": "Save several Discord App IDs and switch between them."},
            {"kind": "full", "widget": _Widget(self._prof_lb), "nodiv": True, "bottom": S(8)},
            {"kind": "extra", "draw": _prof_buttons, "nodiv": True},
        ])]

        # ── Hotkeys ────────────────────────────────────────────────
        hk_rows = []
        if not M.KEYBOARD_AVAILABLE:
            hk_rows.append({"title": "Unavailable", "desc": "Global hotkeys aren't supported on this system."})
        else:
            self._skip_var = tk.StringVar(value=M._hotkey_skip_combo)
            self._skip_instr_var = tk.StringVar(value=M._hotkey_skip_instr_combo)
            self._toggle_var = tk.StringVar(value=M._hotkey_toggle_combo)

            def _save_hotkeys():
                M._hotkey_skip_combo       = self._skip_var.get().strip()
                M._hotkey_toggle_combo     = self._toggle_var.get().strip()
                M._hotkey_skip_instr_combo = self._skip_instr_var.get().strip()
                M._cfg_set_soon("preferences", "hotkey_skip",       M._hotkey_skip_combo)
                M._cfg_set_soon("preferences", "hotkey_toggle",     M._hotkey_toggle_combo)
                M._cfg_set_soon("preferences", "hotkey_skip_instr", M._hotkey_skip_instr_combo)
                M._register_hotkeys(self)
                M.log("Hotkeys saved & re-registered")
            for title, var in (("Skip track", self._skip_var),
                               ("Skip instrumental", self._skip_instr_var),
                               ("Pause Discord status", self._toggle_var)):
                e = self._entry(var, 18)
                e.bind("<Return>", lambda ev: _save_hotkeys())
                e.bind("<FocusOut>", lambda ev: _save_hotkeys())
                hk_rows.append({"title": title, "ctl": _Widget(e, height=S(28))})
            hk_rows.append(self._overlay_hotkey_row(_Widget, S(28)))
        spec += [("section", "Global hotkeys",
                  "Work while other apps have focus. Saved when you press Enter or click away."),
                 ("card", hk_rows)]

        # ── History & privacy ──────────────────────────────────────
        def _toggle_save_hist():
            M.SAVE_HISTORY = not M.SAVE_HISTORY
            M._cfg_set_soon("preferences", "save_history", str(M.SAVE_HISTORY).lower())
            M.log(f'Session history {"enabled" if M.SAVE_HISTORY else "disabled"}')
        self._bl_txt = tk.Text(cv, bg=M.BG3, fg=M.TEXT, font=self._f(M.FS_BODY), height=4, width=1,
                               relief="flat", wrap="word", padx=S(8), pady=S(6),
                               insertbackground=M.TEXT)
        self._focus_ring(self._bl_txt)
        self._bl_txt.insert("1.0", "\n".join(M._BLACKLIST))

        def _save_blacklist(_e=None):
            raw = self._bl_txt.get("1.0", "end").strip()
            M._cfg_set_soon("preferences", "blacklist", raw.replace("\n", "\\n"))
            M._BLACKLIST = M._load_blacklist()
            M.state.blacklisted = M._is_blacklisted(
                getattr(M.state, "artist", ""), getattr(M.state, "title", ""))
        self._bl_txt.bind("<KeyRelease>", lambda e: self._schedule("blsave", 700, _save_blacklist))
        self._bl_txt.bind("<FocusOut>", _save_blacklist)
        spec += [("section", "History and privacy"), ("card", [
            {"title": "Remember history", "desc": "Keep plays and lyrics between sessions.",
             "ctl": self._switch_ctl(lambda: M.SAVE_HISTORY, _toggle_save_hist)},
            {"title": "Blacklist",
             "desc": "Songs whose artist or title contains one of these terms never show on "
                     "Discord. One term per line."},
            {"kind": "full", "widget": _Widget(self._bl_txt), "nodiv": True},
        ])]

        # ── Diagnostics ────────────────────────────────────────────
        self.log_txt = tk.Text(cv, bg=M.BG3, fg=M.TEXT2, height=12, width=1,
                               font=M.tkfont.Font(family="Consolas", size=9),
                               relief="flat", state="disabled", wrap="word", padx=S(10), pady=S(8))
        for tag, col in [("g", M.ACCENT), ("m", M.MUTED), ("y", M.WARN), ("ts", M.MUTED)]:
            self.log_txt.tag_config(tag, foreground=col)
        self.log_txt.bind("<MouseWheel>",
                          lambda e: (self.log_txt.yview_scroll(int(-e.delta / 40), "units"), "break")[1])
        self._log_shown = "log" not in set(M._cfg_get("ui", "collapsed_sections", "log").split(","))

        def _toggle_log():
            self._log_shown = not self._log_shown
            M._cfg_set_soon("ui", "collapsed_sections", "" if self._log_shown else "log")
            log_card[:] = _log_rows()
            self._set_render()
            if self._log_shown:
                self.log_txt.see("end")

        def _log_rows():
            rows = [{"title": "Log", "desc": "What Statusify has been doing. Useful when something's wrong.",
                     "ctl": _Buttons(self, ("btn", "Hide" if self._log_shown else "Show",
                                            _toggle_log, "secondary"))}]
            if self._log_shown:
                rows.append({"kind": "full", "widget": _Widget(self.log_txt), "nodiv": True})
            return rows
        log_card = _log_rows()
        spec += [("section", "Diagnostics"), ("card", log_card)]

        self._set_spec = spec
        self._refresh_stats()          # arms the 5 s session-stats timer (Stats page)
        self._set_render()

    # ── Lyric sublines and sleep timer rows ──────────────────────
    def _full_seg(self, seg):
        """A segmented control on its own full-width line under a row."""
        def draw(cv, x0, x1, y):
            seg.max_w = x1 - x0         # narrower segments at the minimum window width
            seg.draw(cv, x0, y)
            return seg.size()[1] + self._ss(12)
        return {"kind": "extra", "draw": draw, "nodiv": True}

    def _translate_rows(self):
        import statusify_translate as tr
        self._subline_seg = _Segmented(self, list(tr.SUBLINE_MODES),
                                       tr.subline_mode, tr.set_subline_mode)
        self.lbl_translate_to = _Text(self, "")
        codes = [c for c, _ in tr.LANGUAGES]

        def _paint():
            code = tr.translate_to()
            name = tr.language_name(code)
            if code == "auto":
                name = f"Auto ({tr.language_name(tr.system_lang())})"
            self.lbl_translate_to.config(text=name, fg=M.TEXT2)

        def _step(d):
            cur = tr.translate_to()
            i = codes.index(cur) if cur in codes else 0
            tr.set_translate_to(codes[(i + d) % len(codes)])
            _paint()
        _paint()
        return [
            {"title": "Under each line",
             "desc": "Romanised text for non-Latin scripts, a translation, or both. "
                     "Translations come from Google Translate and are kept for replays."},
            self._full_seg(self._subline_seg),
            {"title": "Translate to",
             "ctl": _Buttons(self, ("btn", "‹", lambda: _step(-1), "secondary"),
                             ("value", self.lbl_translate_to, 120),
                             ("btn", "›", lambda: _step(1), "secondary"))},
        ]

    def _sleep_rows(self):
        import statusify_sleep as sleep
        S = self._ss
        self.lbl_sleep = _Text(self, "Off")
        self._sleep_custom_open = False
        hint = f"{sleep.CUSTOM_MIN}–{sleep.CUSTOM_MAX} minutes"
        self._sleep_custom_msg = _Text(self, hint, M.MUTED)
        self._sleep_custom_var = tk.StringVar(value="45")
        self._sleep_custom_ent = None
        custom = {}

        def _field():
            # Made on first use: most people never open Custom, and the page
            # keeps its native children to the inputs everyone sees.
            if "w" not in custom:
                ent = self._entry(self._sleep_custom_var, 4)
                ent.config(justify="center")
                ent.bind("<Return>", _start)
                ent.bind("<KP_Enter>", _start)
                ent.bind("<Escape>", lambda e: (_open(False), "break")[1])
                self._sleep_custom_ent = ent
                custom["w"] = _Widget(ent, height=S(28))
            return custom["w"]

        def _open(on):
            if on == self._sleep_custom_open:
                return
            self._sleep_custom_open = on
            self._sleep_custom_msg.config(text=hint, fg=M.MUTED)
            if on:
                _field()
            ent = self._sleep_custom_ent
            self._set_render()          # the minutes row comes or goes
            if ent is None:
                return
            try:
                if on:
                    ent.focus_set()
                    ent.select_range(0, "end")
                    ent.icursor("end")
                elif self.set_cv.focus_get() is ent:
                    self.set_cv.focus_set()     # don't type into a hidden field
            except (tk.TclError, KeyError):
                pass
        self._sleep_custom_close = lambda: _open(False)

        def _start(_e=None):
            m = sleep.parse_minutes(self._sleep_custom_var.get())
            if m is None:
                self._sleep_custom_msg.config(text=f"Use {hint}", fg=M.DANGER)
                ent = self._sleep_custom_ent
                if ent is not None:
                    ent.focus_set()
                    ent.select_range(0, "end")
                return "break"
            self._sleep_custom_var.set(str(m))
            self._sleep_timer_set(m)
            _open(False)
            return "break"
        self._sleep_custom_start = _start

        def _get():
            t = getattr(self, "_sleep_timer", None)
            v = t.value() if t else "off"
            if self._sleep_custom_open or v not in sleep.PRESETS:
                return "custom"
            return v

        def _choose(v):
            if v == "custom":
                _open(True)
                return
            _open(False)
            self._sleep_timer_set(None if v == "off" else ("eos" if v == "eos" else int(v)))
        self._sleep_seg = _Segmented(
            self, [("Off", "off"), ("15m", "15"), ("30m", "30"), ("1h", "60"), ("Song", "eos"),
                   ("Custom", "custom")],
            _get, _choose)

        def _draw_custom(cv, x0, x1, y):
            """[ 45 ] min  (Start)  1–600 minutes, while Custom is open."""
            if not self._sleep_custom_open:
                return 0
            h = S(28)
            custom_w = _field()
            custom_w.draw(cv, x0, y)
            x = x0 + custom_w.size()[0] + S(6)
            t = cv.create_text(x, y + h // 2, anchor="w", text="min", fill=M.TEXT2,
                               font=self._f(M.FS_SMALL))
            x = cv.bbox(t)[2] + S(12)
            bw = self._f(M.FS_SMALL, True).measure("Start") + S(24)
            self._set_button(cv, x, y, bw, h, "Start", _start, "primary")
            x += bw + S(10)
            msg = self._sleep_custom_msg
            msg.item = cv.create_text(x, y + h // 2, anchor="w", text=msg.cget("text"),
                                      fill=msg.cget("fg") or M.MUTED, font=self._f(M.FS_SMALL),
                                      width=max(S(40), x1 - x))
            return max(h, cv.bbox(msg.item)[3] - y) + S(12)

        try:
            self._sleep_timer_changed()
        except Exception:
            pass
        return [
            {"title": "Sleep timer",
             "desc": "Pause Spotify after a while, or when this song ends (Song). "
                     "Forgotten when Statusify closes.",
             "ctl": _Buttons(self, ("value", self.lbl_sleep, 72))},
            self._full_seg(self._sleep_seg),
            {"kind": "extra", "draw": _draw_custom, "nodiv": True},
        ]

    # ── Profiles ─────────────────────────────────────────────────
    def _load_profiles(self):
        self._prof_lb.delete(0, "end")
        cfg = M._load_config()
        if not cfg.has_section("profiles"):
            cfg.add_section("profiles")
        items = cfg.items("profiles")
        for name, app_id in items:
            marker = "  ✓" if app_id == M.DISCORD_APP_ID else ""
            self._prof_lb.insert("end", f"{name}{marker}  —  {app_id}")
        if not items:
            self._prof_lb.insert("end", "No saved profiles")
            self._prof_lb.itemconfig(0, fg=M.MUTED)

    def _add_profile(self):
        S = self._ss
        dlg = tk.Toplevel(self.win); dlg.title("Add profile")
        dlg.configure(bg=M.BG); dlg.resizable(False, False)
        dlg.transient(self.win); dlg.grab_set()
        body = tk.Frame(dlg, bg=M.BG); body.pack(fill="both", expand=True, padx=S(20), pady=S(18))
        tk.Label(body, text="Add a Discord profile", fg=M.TEXT, bg=M.BG,
                 font=self._f(M.FS_LARGE + 1, True), anchor="w").pack(fill="x", pady=(0, S(10)))
        nv, av = tk.StringVar(), tk.StringVar()
        for label, var in (("Name", nv), ("Application ID", av)):
            tk.Label(body, text=label, fg=M.TEXT2, bg=M.BG, font=self._f(M.FS_SMALL),
                     anchor="w").pack(fill="x", pady=(S(6), S(2)))
            e = tk.Entry(body, textvariable=var, bg=M.BG2, fg=M.TEXT, insertbackground=M.TEXT,
                         relief="flat", font=self._f(M.FS_BODY), width=34)
            self._focus_ring(e); e.pack(fill="x", ipady=S(4))
            if var is nv:
                e.focus_set()
        err = tk.Label(body, text="", fg=M.DANGER, bg=M.BG, font=self._f(M.FS_SMALL),
                       anchor="w", justify="left", wraplength=S(300))
        err.pack(fill="x", pady=(S(6), 0))

        def _ok():
            n = nv.get().strip(); a = av.get().strip()
            if not n or not a:
                err.config(text="Both a name and an App ID are required"); return
            if any(ch in n for ch in "=:[]\n"):
                err.config(text="Name cannot contain  =  :  [  ]"); return
            if not a.isdigit() or len(a) < 16:
                err.config(text="App ID must be a long numeric ID"); return
            M._cfg_set("profiles", n, a)
            self._load_profiles()
            M.log(f"Profile saved  ·  {n}")
            dlg.destroy()
        btns = tk.Frame(body, bg=M.BG); btns.pack(fill="x", pady=(S(12), 0))
        self._wbutton(btns, "Save", _ok, kind="primary").pack(side="right")
        self._wbutton(btns, "Cancel", dlg.destroy, kind="ghost").pack(side="right", padx=(0, S(6)))
        dlg.bind("<Return>", lambda e: _ok())
        dlg.bind("<Escape>", lambda e: dlg.destroy())

    def _selected_profile(self):
        sel = self._prof_lb.curselection()
        if not sel:
            return None, None
        parts = self._prof_lb.get(sel[0]).split("  —  ")
        if len(parts) < 2:
            return None, None
        return parts[0].replace("✓", "").strip(), parts[-1].strip()

    def _del_profile(self):
        name, _ = self._selected_profile()
        if not name:
            return
        cfg = M._load_config()
        if cfg.has_option("profiles", name):
            cfg.remove_option("profiles", name)
            M._save_config(cfg)
        self._load_profiles()

    def _switch_profile(self):
        _, new_id = self._selected_profile()
        if not new_id:
            return
        M.DISCORD_APP_ID = new_id
        M._cfg_set("preferences", "discord_app_id_active", new_id)
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
        except Exception:
            pass
        self._load_profiles()
        M.log(f"Switched Discord profile to: {new_id}")

    # ── Desktop shortcut ─────────────────────────────────────────
    def _do_shortcut(self):
        _NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        app_dir = M._APP_DIR
        if M._FROZEN:
            # A release build is already the thing a shortcut should point at.
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
        lnk = os.path.join(desk, "Statusify.lnk")
        ps = (f"$s=(New-Object -COM WScript.Shell).CreateShortcut('{lnk}');"
              f"$s.TargetPath='{exe_path}';$s.WorkingDirectory='{app_dir}';"
              f"$s.IconLocation='{ico_path}';$s.Save()")
        try:
            subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
                           check=True, capture_output=True, timeout=30, creationflags=_NO_WINDOW)
            self._log("Shortcut created on Desktop")
        except Exception as e:
            self._log(f"Shortcut failed: {e}")
            self._set_error(f"Could not create shortcut: {e}")

    # ── Theming ──────────────────────────────────────────────────
    def _set_theme(self, key):
        """Set dark or light theme from the segmented control."""
        self._theme_want = None
        M._DARK_MODE = (key == "dark")
        M._cfg_set_soon("preferences", "dark_mode", str(M._DARK_MODE).lower())
        self._repaint_everything()

    def _set_theme_soon(self, key):
        """The Theme control's click. The segment moves at once; the
        recolour (~50 ms, every page) runs as soon as that frame is on
        screen, once per burst of clicks."""
        self._theme_want = key
        self._schedule("settheme", 0, lambda: self._set_theme(key))

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
        seg = getattr(self, "_theme_seg", None)
        if seg is not None:
            seg.slide(animate=False)

    def _pick_accent(self, _event=None):
        color = tkcolor.askcolor(color=M.USER_ACCENT, title="Choose accent colour")[1]
        if color:
            M.USER_ACCENT = color
            M._cfg_set_soon("preferences", "accent_color", color)
            self._repaint_everything()

    def _rebuild_all(self):
        """Recolour every widget in-place without destroying state."""
        # Exact before→after mapping from the palette snapshot taken just
        # before _apply_palette overwrote the globals.
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
        # exactly TEXT's white). Base colours are authoritative.
        for _old, _new in ((M._PREV_BG, M.BG), (M._PREV_BG2, M.BG2), (M._PREV_BG3, M.BG3),
                           (M._PREV_MUTED, M.MUTED), (M._PREV_TEXT, M.TEXT),
                           (M._PREV_TEXT2, M.TEXT2), (M._PREV_ACCENT, M.ACCENT)):
            _remap[_old] = _new

        def _recolour(w):
            for opt in ("bg", "fg", "highlightbackground", "highlightcolor",
                        "insertbackground", "selectbackground", "selectforeground",
                        "activebackground"):
                try:
                    new = _remap.get(w.cget(opt))
                    if new:
                        w.config(**{opt: new})
                except tk.TclError:
                    pass
            for child in w.winfo_children():
                _recolour(child)

        _recolour(self.win)

        # The settings canvas draws its own items; recolour its text stand-ins
        # and redraw it.
        for name in ("lbl_track_off", "lbl_lyric_size"):
            slot = getattr(self, name, None)
            if isinstance(slot, _Text) and slot._o.get("fg") in _remap:
                slot._o["fg"] = _remap[slot._o["fg"]]
        try:
            self._set_render()
        except (AttributeError, tk.TclError):
            pass
        try:
            self._stats_render()
        except (AttributeError, tk.TclError):
            pass

        self._paint_nav()
        try:
            self.log_txt.tag_config("g",  foreground=M.ACCENT)
            self.log_txt.tag_config("m",  foreground=M.MUTED)
            self.log_txt.tag_config("y",  foreground=M.WARN)
            self.log_txt.tag_config("ts", foreground=M.MUTED)
        except (AttributeError, tk.TclError): pass

        # The lyric sheet caches colours in its layers; drop them.
        try:
            self._repaint_progress_colors()
        except Exception:
            pass
