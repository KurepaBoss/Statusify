"""Cover mode and page transitions, mixed into main.App.

Three things live here because they share one idea — where each page is on
screen right now:

  * The backdrop (statusify_backdrop): one frame for the whole window per
    tick, shown under every page. Tk widgets are opaque, so each page canvas
    shows the same frame as its lowest image item, offset by the canvas's
    own position in the window. Moving a page (or scrolling it) only moves
    that item, which keeps the background still while the pages glide over
    it. The Lyrics page draws its whole frame in PIL, so it crops the same
    frame instead.
  * The page switcher: a small canvas whose selected pill slides between
    segments, following the pages as they move.
  * Sliding between pages: a click slides the pages sideways (420 ms,
    ease-out), a drag follows the pointer, a click mid-slide retargets.

Names that belong to main are reached through M, the live main module.
"""
import time
import tkinter as tk

try:
    from PIL import ImageTk
except ImportError:
    ImageTk = None

import statusify_backdrop as bdm

M = None   # the main module

PAGES = ("NOW PLAYING", "HISTORY", "STATS", "SETTINGS")
LABELS = {"NOW PLAYING": "Lyrics", "HISTORY": "History", "STATS": "Stats", "SETTINGS": "Settings"}
SLIDE_MS = 420
DRAG_START_PX = 12       # a press becomes a drag only past this, so clicks still click
DRAG_SWITCH_PX = 60      # released past this, the drag goes to the next page
FLICK_PX_PER_S = 900     # … or thrown at least this fast


def page_brightness(name):
    return bdm.LYRICS_BRIGHTNESS if name == "NOW PLAYING" else bdm.PAGE_BRIGHTNESS


def slide_plan(positions, target, width):
    """Where every page starts and ends for a slide to `target`.

    `positions` is {page: x} for the pages on screen now (one page at 0 when
    nothing is moving). The target comes in beside the pages already there,
    on its side of the tab order, so a click mid-slide carries on like a
    strip rather than jumping; everything else leaves towards its own side.
    Returns {page: (x_start, x_end)}."""
    order = {n: i for i, n in enumerate(PAGES)}
    plan = {}
    for p, x in positions.items():
        if p == target:
            continue
        plan[p] = (x, -width if order[p] < order[target] else width)
    if target in positions:
        start = positions[target]
    elif not positions:
        start = 0.0
    else:
        # Beside the nearest page on the target's side.
        right = [x for p, x in positions.items() if order[p] < order[target]]
        left = [x for p, x in positions.items() if order[p] > order[target]]
        if right:
            start = max(right) + width
        else:
            start = min(left) - width
    plan[target] = (start, 0.0)
    return plan


def visibility(x, width):
    """Share of a page at offset x that is on screen (0..1)."""
    return max(0.0, 1.0 - abs(x) / max(1.0, float(width)))


class BackdropMixin:

    # ── Backdrop ─────────────────────────────────────────────────
    def _bd_init(self):
        self._backdrop = bdm.Backdrop()
        self._bd_cvs = {}          # canvas -> page name
        self._bd_photo = None      # the shared window-sized PhotoImage
        self._bd_photo_seq = None
        self._bd_nav_photo = None
        self._bd_nav_seq = None
        self._bd_base = None       # (image, t, size, still, seq): brightness 1, no dither
        self._bd_shaded = None     # (image, base seq, brightness, seq)
        self._bd_seq = 0
        self._bd_bright = bdm.LYRICS_BRIGHTNESS
        self._bd_smap_cache = None
        self._page_x = {}          # page -> x while pages move; empty when still
        self._slide = None
        self._drag = None
        self._schedule("bdtick", 300, self._bd_tick)

    def _bd_on(self):
        return M._cover_mode() and ImageTk is not None

    def _bd_tier(self):
        tier = getattr(self, "_np_tier", None)
        return tier() if tier else 0

    def _bd_still(self):
        return (not M.ANIMATIONS_ENABLED) or self._bd_tier() >= 2

    def _bd_max_age(self, now):
        """Seconds a backdrop frame is reused. The drift is a minute long and
        heavily blurred: ~8 updates a second look the same as 60 behind the
        darker pages, and the Lyrics page refreshes it as the water did."""
        if self._bd_still():
            return 0.1 if self._backdrop.crossfading(now) else None
        lyrics = self._cur_page == "NOW PLAYING" and not self._page_x
        age = ((0.08, 0.15) if lyrics else (0.125, 0.2))[min(1, self._bd_tier())]
        if self._backdrop.crossfading(now):
            age = min(age, 0.05)
        return age

    def _bd_smap(self):
        """Palette colour -> RGBA over the backdrop (statusify_backdrop.to_rgba)."""
        key = (M.BG, M.BG2, M.BG3, M.BG4, M.HOVER_BG, M.ACCENT_SOFT, M.BORDER, M.ACCENT)
        c = self._bd_smap_cache
        if c is None or c[0] != key:
            toks = dict(zip(("BG", "BG2", "BG3", "BG4", "HOVER_BG", "ACCENT_SOFT", "BORDER"), key))
            c = self._bd_smap_cache = (key, bdm.surface_map(toks, M.ACCENT))
        return c[1]

    def _bd_rgba(self, color):
        return bdm.to_rgba(color, self._bd_smap())

    def _bd_set_cover(self, img, palette=None):
        """A new cover (or None). Call before the palette is rebuilt: cover
        mode and the page colour both come from it (main._COVER_BASE)."""
        now = time.monotonic()
        self._backdrop.set_cover(img, now, palette, dark=True, fallback=M.BG3)
        self._bd_base = None
        if self._backdrop.has_cover():
            M._COVER_BASE = "#%02x%02x%02x" % self._backdrop.average(bdm.PAGE_BRIGHTNESS)
        else:
            M._COVER_BASE = None

    def _bd_frame_at(self, now):
        """(image, seq): the whole-window backdrop at the current brightness."""
        root = self._root
        size = (max(1, root.winfo_width()), max(1, root.winfo_height()))
        still = self._bd_still()
        base = self._bd_base
        age = self._bd_max_age(now)
        if (base is None or base[2] != size or base[3] != still
                or (age is not None and now - base[1] >= age)):
            img = self._backdrop.render(size, now, 1.0, motion_t=40.0 if still else None,
                                        dither=False)
            self._bd_seq += 1
            base = self._bd_base = (img, now, size, still, self._bd_seq)
        moving = bool(self._page_x)
        # While pages move, brightness goes in 0.02 steps and without the
        # dither: each new value is a paste of the whole frame.
        b = round(self._bd_bright / 0.02) * 0.02 if moving else round(self._bd_bright, 3)
        sh = self._bd_shaded
        if sh is None or sh[1] != base[4] or sh[2] != b:
            img = self._backdrop.shade(base[0], b, dither=self._bd_tier() == 0 and not moving)
            self._bd_seq += 1
            sh = self._bd_shaded = (img, base[4], b, self._bd_seq)
        return sh[0], sh[3]

    def _bd_offset(self, w):
        """(x, y) of widget w in the window, counting a moving page at the
        x it is being slid to (Tk applies place() only at idle)."""
        names = {str(p): n for n, p in self._pages.items()}
        root = self._root
        x = y = 0
        while w is not None and w is not root:
            n = names.get(str(w))
            sx = getattr(w, "_bd_x", None)       # a panel sliding inside a page
            if n is not None:
                x += int(round(self._page_x.get(n, 0.0)))
            elif sx is not None:
                x += int(round(sx))
                y += w.winfo_y()
            else:
                x += w.winfo_x()
                y += w.winfo_y()
            w = w.master
        return x, y

    def _bd_visible_pages(self):
        pages = set(self._page_x)
        if self._cur_page:
            pages.add(self._cur_page)
        return pages

    def _bd_register(self, cv, page):
        """Show the backdrop under canvas `cv`, part of page `page`."""
        self._bd_cvs[cv] = page
        cv.bind("<Configure>", lambda e, c=cv: self._bd_follow(c), add="+")
        self._bd_attach(cv)

    def _bd_attach(self, cv):
        """Give `cv` its backdrop item (after a render wiped the canvas, a
        palette change or a page coming into view), or take it away."""
        try:
            page = self._bd_cvs.get(cv)
            show = self._bd_on() and page in self._bd_visible_pages()
            if show and self._bd_photo is None:
                self._bd_push()                  # creates the photo, attaches all
                return
            if not show or self._bd_photo is None:
                cv.delete("bd")
                return
            items = cv.find_withtag("bd")
            if items:
                item = items[0]
                cv.itemconfigure(item, image=self._bd_photo)
            else:
                item = cv.create_image(0, 0, anchor="nw", image=self._bd_photo, tags=("bd",))
            cv.tag_lower(item)
            self._bd_follow(cv)
        except tk.TclError:
            pass

    def _bd_follow(self, cv):
        """Keep the backdrop item where the window is, whatever the canvas's
        scroll position and the page's slide offset."""
        try:
            items = cv.find_withtag("bd")
            if items:
                ox, oy = self._bd_offset(cv)
                cv.coords(items[0], cv.canvasx(0) - ox, cv.canvasy(0) - oy)
        except tk.TclError:
            pass

    def _bd_follow_page(self, page):
        for cv, p in list(self._bd_cvs.items()):
            if p == page:
                self._bd_follow(cv)

    def _bd_refresh_all(self):
        """Cover mode, the palette or the visible pages changed."""
        if not self._bd_on():
            self._bd_photo = None
            self._bd_nav_photo = None
        for cv in list(self._bd_cvs):
            self._bd_attach(cv)
        self._nav_draw()
        if hasattr(self, "_np_invalidate"):
            self._np_invalidate()
        if self._bd_on():
            self._bd_push()

    def _bd_push(self, now=None):
        """Put the current frame on screen: the shared photo (when a page
        using it is visible) and the switcher's strip."""
        if not self._bd_on():
            return
        now = time.monotonic() if now is None else now
        try:
            img, seq = self._bd_frame_at(now)
        except Exception as e:
            M.log(f"Backdrop frame failed: {e}")
            return
        if self._bd_visible_pages() - {"NOW PLAYING"}:
            if self._bd_photo is None or (self._bd_photo.width(), self._bd_photo.height()) != img.size:
                self._bd_photo = ImageTk.PhotoImage(img)
                self._bd_photo_seq = seq
                for cv in list(self._bd_cvs):
                    self._bd_attach(cv)
            elif self._bd_photo_seq != seq:
                self._bd_photo.paste(img)
                self._bd_photo_seq = seq
        self._bd_push_nav(img, seq)

    def _bd_push_nav(self, img, seq):
        nav = getattr(self, "_nav", None)
        if nav is None:
            return
        try:
            if not nav.winfo_ismapped():
                return
            w, h = nav.winfo_width(), nav.winfo_height()
            ox, oy = self._bd_offset(nav)
        except tk.TclError:
            return
        if w < 2 or h < 2:
            return
        ph = self._bd_nav_photo
        if ph is None or (ph.width(), ph.height()) != (w, h):
            self._bd_nav_photo = ImageTk.PhotoImage(img.crop((ox, oy, ox + w, oy + h)))
            self._bd_nav_seq = seq
            self._nav_draw()
        elif self._bd_nav_seq != seq:
            ph.paste(img.crop((ox, oy, ox + w, oy + h)))
            self._bd_nav_seq = seq

    def _bd_np_frame(self, W, H, now):
        """The Lyrics page's background: its part of the window frame."""
        img, _seq = self._bd_frame_at(now)
        ox, oy = self._bd_offset(self.np_cv)
        return img.crop((ox, oy, ox + W, oy + H))

    def _bd_tick(self):
        interval = 500
        try:
            if self._bd_on() and not self._hidden and self._root.state() != "iconic":
                now = time.monotonic()
                self._bd_push(now)
                age = self._bd_max_age(now)
                interval = 1000 if age is None else int(age * 1000)
        except Exception as e:
            M.log(f"Backdrop tick failed: {e}")
            interval = 1000
        self._schedule("bdtick", max(16, interval), self._bd_tick)

    def _bd_set_brightness(self, b):
        b = round(b, 3)
        if b == round(self._bd_bright, 3):
            return
        self._bd_bright = b
        if hasattr(self, "_np_invalidate"):
            self._np_want_frame = True

    # ── Page switcher ────────────────────────────────────────────
    def _build_nav(self):
        """A segmented control pinned to the bottom, like a media app, so the
        lyric sheet gets the whole top of the window. Drawn on a canvas so the
        selected pill can slide with the pages (and sit on the backdrop)."""
        S = getattr(self, "_ss", lambda v: v)
        row = tk.Frame(self.win, bg=M.BG)
        row.pack(side="bottom", fill="x")
        font = self._f(M.FS_SMALL, True)
        seg_h = font.metrics("linespace") + 2 * (M.SP_XS + 1)
        self._nav_font = font
        self._nav_trough_h = seg_h + 6
        h = M.SP_XS + self._nav_trough_h + M.SP_MD
        cv = tk.Canvas(row, height=h, bg=M.BG, highlightthickness=0, bd=0)
        cv.pack(fill="x")
        self._nav = cv
        self._nav_hover = None
        self._nav_items = {}
        cv.bind("<Configure>", lambda e: self._nav_draw())
        cv.bind("<Button-1>", self._nav_click)
        cv.bind("<Motion>", self._nav_motion)
        cv.bind("<Leave>", lambda e: self._nav_set_hover(None))

    def _nav_labels(self):
        return [LABELS[n] for n in PAGES]

    def _nav_segments(self):
        """{page: (x, width)} of each segment, centred in the switcher."""
        cv = self._nav
        font = self._nav_font
        pad = M.SP_LG
        widths = [font.measure(LABELS[n]) + 2 * pad for n in PAGES]
        total = sum(widths) + 2 * (len(widths) - 1) + 6
        x = (max(total, cv.winfo_width()) - total) // 2 + 3
        segs = {}
        for n, w in zip(PAGES, widths):
            segs[n] = (x, w)
            x += w + 2
        return segs, total

    def _nav_pill(self, segs):
        """(x, width) of the selected pill: between the segments of the pages
        on screen, weighted by how much of each is showing."""
        W = max(1, self._container.winfo_width())
        weights = {p: visibility(x, W) for p, x in self._page_x.items()}
        if not weights or sum(weights.values()) <= 0:
            weights = {self._cur_page or "NOW PLAYING": 1.0}
        tot = sum(weights.values())
        x = sum(segs[p][0] * v for p, v in weights.items()) / tot
        w = sum(segs[p][1] * v for p, v in weights.items()) / tot
        return x, w

    def _nav_draw(self):
        cv = getattr(self, "_nav", None)
        if cv is None:
            return
        try:
            cv.delete("all")
            cv.config(bg=M.BG)
            W = cv.winfo_width()
            if W < 20:
                return
            if self._bd_on() and self._bd_nav_photo is not None:
                cv.create_image(0, 0, anchor="nw", image=self._bd_nav_photo, tags=("bd",))
            segs, total = self._nav_segments()
            th = self._nav_trough_h
            tx = segs[PAGES[0]][0] - 3
            ty = M.SP_XS
            cv.create_image(tx, ty, anchor="nw", image=self._card_photo(total, th, 8), tags=("trough",))
            self._nav_items = {"segs": segs, "y": ty}
            self._nav_items["pill"] = cv.create_image(0, ty + 3, anchor="nw", tags=("pill",))
            for n in PAGES:
                x, w = segs[n]
                self._nav_items[n] = cv.create_text(x + w // 2, ty + th // 2, text=LABELS[n],
                                                    font=self._nav_font, fill=M.MUTED)
            self._nav_update()
        except tk.TclError:
            pass

    def _nav_update(self):
        """Move the pill and recolour the labels (no redraw)."""
        cv = getattr(self, "_nav", None)
        items = getattr(self, "_nav_items", None)
        if cv is None or not items or "pill" not in items:
            return
        segs = items["segs"]
        x, w = self._nav_pill(segs)
        wi = int(round(w))
        h = self._nav_trough_h - 6
        try:
            cv.coords(items["pill"], int(round(x)), items["y"] + 3)
            cv.itemconfigure(items["pill"],
                             image=self._pill_photo(wi, h, M.BG4, M.BG2, radius=self._ss(6)))
            cx = x + w / 2
            for n in PAGES:
                sx, sw = segs[n]
                near = max(0.0, 1.0 - abs(cx - (sx + sw / 2)) / max(1.0, sw))
                rest = M.TEXT2 if (self._nav_hover == n and near < 0.5) else M.MUTED
                cv.itemconfigure(items[n], fill=M._blend(rest, M.TEXT, near))
        except tk.TclError:
            pass

    def _nav_at(self, x):
        items = getattr(self, "_nav_items", None)
        if not items:
            return None
        for n, (sx, sw) in items["segs"].items():
            if sx - 1 <= x <= sx + sw + 1:
                return n
        return None

    def _nav_click(self, e):
        n = self._nav_at(e.x)
        if n:
            self._show(n)

    def _nav_motion(self, e):
        self._nav_set_hover(self._nav_at(e.x))

    def _nav_set_hover(self, n):
        if n == self._nav_hover:
            return
        self._nav_hover = n
        try:
            self._nav.config(cursor="hand2" if n else "")
        except tk.TclError:
            pass
        self._nav_update()

    def _card_photo(self, w, h, radius):
        """A card surface as a PhotoImage: see-through with an accent rim in
        cover mode, the flat BG2 card otherwise."""
        S = getattr(self, "_ss", lambda v: v)
        r = S(radius)
        if not self._bd_on():
            return self._pill_photo(w, h, M.BG2, M.BG, radius=r, outline=M.BORDER)
        key = ("card", w, h, r, M.ACCENT)
        cache = self.__dict__.setdefault("_pill_cache", {})
        ph = cache.get(key)
        if ph is None:
            ph = ImageTk.PhotoImage(bdm.card_rgba(w, h, r, M._hex_to_rgb(M.ACCENT)))
            cache[key] = ph
        return ph

    # ── Sliding between pages ────────────────────────────────────
    def _slide_positions(self):
        """{page: x} of the pages on screen now."""
        if self._page_x:
            return dict(self._page_x)
        return {self._cur_page: 0.0} if self._cur_page else {}

    def _slide_to(self, name, positions=None):
        """Slide `name` into view from wherever the pages are now."""
        W = max(1, self._container.winfo_width())
        pos = self._slide_positions() if positions is None else positions
        plan = slide_plan(pos, name, W)
        for p in plan:
            page = self._pages[p]
            if not page.winfo_manager():
                page.place(x=0, y=0, relwidth=1, relheight=1)
        # Leaving pages first, the arriving one on top.
        for p in sorted(plan, key=lambda p: p == name):
            self._pages[p].tkraise()
        self._slide = {"plan": plan, "t0": time.monotonic(), "to": name, "W": W}
        self._slide_fine_timer(True)
        for p, (x0, _x1) in plan.items():
            self._page_x[p] = x0
        self._slide_embeds(False)
        self._bd_refresh_visible()
        self._slide_step()

    def _slide_step(self):
        sl = self._slide
        if sl is None:
            return
        start = time.monotonic()
        t = (start - sl["t0"]) * 1000.0 / SLIDE_MS
        e = bdm.ease_out_quart(t)
        for p, (x0, x1) in sl["plan"].items():
            self._page_x[p] = x0 + (x1 - x0) * e
        self._slide_apply()
        if t < 1.0:
            # Aim at 60 fps: the frame's own work comes out of the wait. The
            # redraw itself happens after this returns, so leave it room.
            spent = (time.monotonic() - start) * 1000.0
            self._schedule("slide", max(1, int(12 - spent)), self._slide_step)
        else:
            self._slide_end()

    def _slide_apply(self):
        """Place the moving pages, keep their backdrops still, move the pill,
        blend the brightness."""
        W = max(1, self._container.winfo_width())
        wsum = bsum = 0.0
        for p, x in self._page_x.items():
            try:
                self._pages[p].place_configure(x=int(round(x)))
            except tk.TclError:
                pass
            self._bd_follow_page(p)
            v = visibility(x, W)
            wsum += v
            bsum += v * page_brightness(p)
        if wsum > 0:
            self._bd_set_brightness(bsum / wsum)
        self._nav_update()
        if self._bd_on():
            self._bd_push()
        if "NOW PLAYING" in self._page_x and hasattr(self, "_np_render"):
            self._np_render(force=False)

    def _slide_fine_timer(self, on):
        """1 ms Windows timers while pages move (see _np_fine_timer); the
        lyric clock takes it back over when the slide ends."""
        fine = getattr(self, "_np_fine_timer", None)
        if fine is not None:
            fine(on or (self._cur_page == "NOW PLAYING" and M.ANIMATIONS_ENABLED))

    def _slide_embeds(self, show):
        """Settings' text inputs are native windows: moving the page would
        move each of them every frame. Show their drawn stand-ins while
        pages move, as its scroll glide does."""
        if hasattr(self, "set_cv") and ("SETTINGS" in self._page_x or show):
            try:
                self._set_embeds(show)
            except tk.TclError:
                pass

    def _slide_end(self):
        self._cancel("slide")
        self._slide = None
        self._slide_fine_timer(False)
        self._slide_embeds(True)
        cur = self._cur_page
        for p in list(self._page_x):
            try:
                self._pages[p].place_configure(x=0)
            except tk.TclError:
                pass
        self._page_x.clear()
        try:
            self._pages[cur].tkraise()
        except (KeyError, tk.TclError):
            pass
        self._bd_set_brightness(page_brightness(cur))
        self._bd_refresh_visible()
        self._nav_update()

    def _slide_stop(self):
        """Snap any slide or drag to rest (the instant path)."""
        if self._slide is not None:
            self._slide_fine_timer(False)
        self._cancel("slide")
        if self._page_x:
            self._page_x.clear()
            self._slide_embeds(True)
        self._slide = None
        self._drag = None
        for p in list(self._page_x):
            try:
                self._pages[p].place_configure(x=0)
            except tk.TclError:
                pass
        self._page_x.clear()

    def _bd_refresh_visible(self):
        """Attach the backdrop to the pages now on screen, drop it from the rest."""
        for cv in list(self._bd_cvs):
            self._bd_attach(cv)
        if self._bd_on():
            self._bd_push()

    # ── Dragging between pages ───────────────────────────────────
    def _drag_bind(self, cv, page, can_start=None):
        """Let a drag on `cv` pull the neighbouring page in. can_start(e)
        may veto a press (a slider, the seek bar)."""
        cv.bind("<ButtonPress-1>", lambda e: self._drag_press(e, page, can_start), add="+")
        cv.bind("<B1-Motion>", self._drag_motion, add="+")
        cv.bind("<ButtonRelease-1>", self._drag_release, add="+")

    def _drag_press(self, e, page, can_start):
        self._drag = None
        if page != self._cur_page or self._slide is not None or not M.ANIMATIONS_ENABLED:
            return
        if getattr(self, "_np_fs", None):
            return
        if can_start is not None and not can_start(e):
            return
        self._drag = {"x0": e.x_root, "y0": e.y_root, "page": page, "on": False,
                      "samples": [(time.monotonic(), e.x_root)], "n": None}

    def _drag_neighbour(self, page, dx):
        i = PAGES.index(page) + (1 if dx < 0 else -1)
        return PAGES[i] if 0 <= i < len(PAGES) else None

    def _drag_motion(self, e):
        d = self._drag
        if d is None:
            return
        dx, dy = e.x_root - d["x0"], e.y_root - d["y0"]
        if not d["on"]:
            if abs(dx) < DRAG_START_PX or abs(dx) <= abs(dy):
                if abs(dy) > DRAG_START_PX:
                    self._drag = None            # a vertical gesture: leave it alone
                return
            d["on"] = True
        d["samples"] = (d["samples"] + [(time.monotonic(), e.x_root)])[-6:]
        W = max(1, self._container.winfo_width())
        n = self._drag_neighbour(d["page"], dx)
        off = float(dx)
        if n is None:
            off = dx * 0.3                        # nothing that way: resist
        if n != d["n"]:
            if d["n"] is not None:
                self._page_x.pop(d["n"], None)
                try:
                    self._pages[d["n"]].place_configure(x=0)
                    self._pages[d["page"]].tkraise()
                except (KeyError, tk.TclError):
                    pass
            d["n"] = n
            if n is not None:
                if n not in self._pages:
                    builder = getattr(self, {"HISTORY": "_build_history", "STATS": "_build_stats",
                                             "SETTINGS": "_build_settings"}.get(n, ""), None)
                    if builder:
                        builder()
                page = self._pages.get(n)
                if page is None:
                    d["n"] = n = None
                else:
                    if not page.winfo_manager():
                        page.place(x=0, y=0, relwidth=1, relheight=1)
                    page.tkraise()
        self._page_x[d["page"]] = off
        if n is not None:
            self._page_x[n] = off + (W if dx < 0 else -W)
        self._slide_embeds(False)
        if n is not None and not self._bd_cvs_attached(n):
            self._bd_refresh_visible()
        self._slide_apply()
        return "break"

    def _bd_cvs_attached(self, page):
        for cv, p in self._bd_cvs.items():
            if p == page:
                try:
                    return bool(cv.find_withtag("bd")) or not self._bd_on()
                except tk.TclError:
                    return True
        return True

    def _drag_release(self, e):
        d, self._drag = self._drag, None
        if d is None or not d["on"]:
            return
        dx = e.x_root - d["x0"]
        s = d["samples"]
        v = 0.0
        if len(s) >= 2 and s[-1][0] > s[0][0]:
            v = (s[-1][1] - s[0][1]) / (s[-1][0] - s[0][0])
        n = d["n"]
        flick = abs(v) >= FLICK_PX_PER_S and (v < 0) == (dx < 0)
        if n is not None and (abs(dx) >= DRAG_SWITCH_PX or flick):
            self._show(n)
        else:
            self._slide_to(d["page"])
        return "break"
