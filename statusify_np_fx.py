"""Now Playing extras: karaoke fill, instrumental dots, cover crossfade,
beat swell, fullscreen presentation and the lyric font.

A mixin for NowPlayingPage (statusify_ui_now_playing), kept apart so the
page's own drawing code stays readable. Everything here draws into the same
PIL frame the page composes; nothing is a Tk widget. M is the live main
module, bound by the page when it builds.

Performance notes. The page renders ~60 frames a second while something
moves and skips unchanged frames otherwise, so every effect here is split
into a cached part (text masks, sprites, ramps) and a small per-frame part:

    karaoke   the active line's mask (already cached by blur/alpha) times a
              "level" image: bright left of the fill, a soft ramp, dim right.
              One Image.new, a few pastes and one ImageChops.multiply over
              the line's own box, ~0.2 ms.
    dots      three discs drawn at 4x into a strip of ~150x40 px and reduced.
    cover     two ~60 px sprites alpha-blended for 350 ms after a change.
    beat      one ImageChops.add of a flat grey over the background for the
              180 ms after a beat, Smooth tier only.
"""
import bisect
import math
import time
import tkinter as tk

from statusify_textrender import DEFAULT_FAMILY, TextRenderer, available_families

try:
    from PIL import Image, ImageChops, ImageDraw
except ImportError:          # main refuses to start without Pillow anyway
    Image = ImageChops = ImageDraw = None

M = None   # the main module (bound by the page)

KARA_DIM = 0.5            # unsung words, relative to the sung ones
DOT_REST = 0.62           # an inactive break's dots, relative to the active size
DOTS_IN_S = 0.40          # dots grow in at a break's start …
DOTS_OUT_MS = 450         # … and shrink away this long before the next line
BREATHE_S = 2.4           # one breath of the active dots
GAP_MIN_MS = 4000         # silence between two lines that earns the dots
BEAT_MS = 180             # how long a beat's swell decays
BEAT_LIFT = 7             # peak brightness lift, levels of 255 (~2.7 %)
COVER_S = 0.35            # cover crossfade
CHROME_IDLE_S = 2.5       # fullscreen: controls hide after this long still
CHROME_FADE_S = 0.30

_beat_react = None        # "React to the beat" (Settings → Appearance)
_lyric_font = None        # chosen lyric family name
_families = None


def _cfg(key, default):
    try:
        return M._cfg_get("preferences", key, default)
    except Exception:
        return default


def beat_react():
    global _beat_react
    if _beat_react is None:
        _beat_react = str(_cfg("beat_react", "true")).lower() == "true"
    return _beat_react


def set_beat_react(on):
    global _beat_react
    _beat_react = bool(on)
    M._cfg_set("preferences", "beat_react", str(_beat_react).lower())


def families():
    global _families
    if _families is None:
        _families = available_families()
    return _families


def lyric_font():
    global _lyric_font
    if _lyric_font is None:
        name = _cfg("lyric_font", DEFAULT_FAMILY) or DEFAULT_FAMILY
        _lyric_font = name if name in families() else DEFAULT_FAMILY
    return _lyric_font


def _ease_out(t):
    return 1.0 - (1.0 - t) ** 4


def _smooth(t):
    return t * t * (3 - 2 * t)


def _clamp(v, lo=0.0, hi=1.0):
    return lo if v < lo else hi if v > hi else v


# ── The sheet plan: lines plus the breaks between them ──────────

def build_plan(synced, gaps=(), duration_ms=0):
    """Rows of the synced lyric sheet, in time order.

    Each row is {"kind": "intro" | "line" | "gap", "k": synced index,
    "t0": ms the row becomes current, "t1": ms it ends (breaks only)}.
    Row 0 is always the intro (before the first line), so a line's row is
    its synced index + 1 when a song has no breaks. A break is an empty
    lyric line, a silence of GAP_MIN_MS or more after a line whose end is
    known (endMs or its last syllable), or an instrumental gap main already
    worked out (state.instrumental_gaps, keyed by the line before it)."""
    n = len(synced)
    first = synced[0]["startMs"] if n else 0
    plan = [{"kind": "intro", "k": -1, "t0": -10 ** 9, "t1": first}]
    by_key = {}
    for g in gaps or ():
        try:
            if int(g.get("key", -1)) >= 0:
                by_key[int(g["key"])] = g
        except (TypeError, ValueError, AttributeError):
            continue
    for k, e in enumerate(synced):
        start = e["startMs"]
        nxt = synced[k + 1]["startMs"] if k + 1 < n else None
        words = (e.get("words") or "").strip()
        if not words:
            end = nxt if nxt is not None else max(start, duration_ms or start)
            plan.append({"kind": "gap", "k": k, "t0": start, "t1": end})
            continue
        plan.append({"kind": "line", "k": k, "t0": start, "t1": None})
        if nxt is None or not (synced[k + 1].get("words") or "").strip():
            continue                    # last line, or the next row is a break itself
        end = e.get("endMs")
        syl = e.get("syl")
        if not end and syl:
            try:
                end = int(syl[-1][1])
            except (TypeError, ValueError, IndexError):
                end = None
        g0 = None
        if end:
            if nxt - end >= GAP_MIN_MS:
                g0 = end + 250
        elif k in by_key:
            g0 = by_key[k].get("startMs")
        if g0 is not None and start < g0 < nxt - 800:
            plan.append({"kind": "gap", "k": k, "t0": int(g0), "t1": nxt})
    return plan


def plan_index(plan_t0, pos):
    """Row current at `pos` (ms, offset applied): the last row started."""
    return max(0, bisect.bisect_right(plan_t0, pos) - 1)


# ── Karaoke geometry ─────────────────────────────────────────────

def kara_segments(TR, weight, px, text, lines, syl):
    """Where each timed piece of `text` sits once wrapped into `lines`.

    Returns [(row, x0, x1, t0, t1)] in time order, or None when the
    syllables don't match the text (the line then highlights whole). A piece
    split over two rows (a hard-wrapped word) shares its time between them
    by width."""
    rows, cur = [], 0
    for ln in lines:
        j = text.find(ln, cur) if ln else cur
        if j < 0:
            return None
        rows.append((j, j + len(ln)))
        cur = j + len(ln)
    segs, cursor = [], 0
    for piece in syl:
        try:
            t0, t1, s = int(piece[0]), int(piece[1]), str(piece[2])
        except (TypeError, ValueError, IndexError):
            return None
        s = s.strip()
        if not s:
            continue
        a = text.find(s, cursor)
        if a < 0:
            return None
        b = a + len(s)
        cursor = b
        parts = []
        for r, (ra, rb) in enumerate(rows):
            lo, hi = max(a, ra), min(b, rb)
            if lo < hi:
                ln = lines[r]
                x0 = TR.measure(ln[:lo - ra], weight, px)
                x1 = TR.measure(ln[:hi - ra], weight, px)
                parts.append((r, x0, x1))
        if not parts:
            continue
        tot = sum(p[2] - p[1] for p in parts) or 1.0
        t = t0
        for r, x0, x1 in parts:
            dt = (t1 - t0) * (x1 - x0) / tot
            segs.append((r, x0, x1, t, t + dt))
            t += dt
    return segs or None


def kara_fills(segs, nrows, t, edge):
    """Per row: (fill x, dim-from x). Left of fill is sung; the ramp runs
    edge px to its right; from dim-from on, nothing is sung yet (keeps the
    ramp from leaking into the next word before it starts)."""
    fills = [-edge] * nrows
    limits = [None] * nrows
    last = -1
    for n, (r, x0, x1, t0, t1) in enumerate(segs):
        if t < t0:
            if limits[r] is None:
                limits[r] = x0
            continue
        span = max(1.0, t1 - t0)
        f = _clamp((t - t0) / span)
        fills[r] = x0 + (x1 + edge - x0) * f - edge
        last = r
    for r in range(max(0, last)):
        if limits[r] is None:
            fills[r] = 10 ** 6            # a row fully behind the singer
    return fills, limits


# ── The mixin ────────────────────────────────────────────────────

class NpFxMixin:

    def _np_fx_init(self):
        self._np_plan_cache = (None, None, None)
        self._np_kara_cache = {}
        self._np_fx_sprites = {}
        self._np_cov = {"cur": None, "prev": None, "t0": -10.0, "src": 0}
        self._np_fs = None
        self._np_chrome = [1.0, time.monotonic()]      # shown amount, last update
        self._np_chrome_t = time.monotonic()           # last pointer motion
        self._np_cursor_hidden = False
        self._np_beat_solid = {}
        self._np_lyric_text = TextRenderer(lyric_font() if lyric_font() != DEFAULT_FAMILY else None)
        self._np_active_fx = None                     # busy reason, for the clock

    # ── Lyric font ───────────────────────────────────────────────
    def _np_set_lyric_font(self, name):
        global _lyric_font
        if name not in families():
            name = DEFAULT_FAMILY
        _lyric_font = name
        M._cfg_set("preferences", "lyric_font", name)
        self._np_lyric_text = TextRenderer(name if name != DEFAULT_FAMILY else None)
        self._np_relayout()
        self._np_render()

    # ── Plan ─────────────────────────────────────────────────────
    def _np_plan(self):
        st = M.state
        synced = st.synced or ()
        gaps = getattr(st, "instrumental_gaps", None) or ()
        # Identity, not contents: main replaces these lists, never edits them.
        key = (id(synced), len(synced), id(gaps), len(gaps), getattr(st, "duration_ms", 0) or 0)
        k0, plan, t0s = self._np_plan_cache
        if k0 != key:
            plan = build_plan(synced, gaps, key[-1])
            t0s = [p["t0"] for p in plan]
            self._np_plan_cache = (key, plan, t0s)
        return plan, t0s

    def _np_sheet_pos(self):
        """Playback position the sheet follows (the lyric offset applied)."""
        return self._estimate_pos_ms() + M._track_offset_ms()

    # ── Karaoke ──────────────────────────────────────────────────
    def _np_kara_info(self, ly, i):
        """(segments, row widths) for sheet row i if it has syllable timing."""
        it = ly["items"][i]
        if "kara" not in it:
            it["kara"] = None
            plan, _ = self._np_plan()
            if i < len(plan) and plan[i]["kind"] == "line":
                e = M.state.synced[plan[i]["k"]]
                syl = e.get("syl")
                if syl:
                    it["kara"] = kara_segments(self._np_lyric_text, "bold", it["px"],
                                               it["text"], it["lines"], syl)
        return it["kara"]

    def _np_kara_edge(self, px):
        return max(self._S(8), int(px * 0.55))

    def _np_kara_ramp(self, edge, h, hi, lo):
        key = ("ramp", edge, h, hi, lo)
        r = self._np_fx_sprites.get(key)
        if r is None:
            row = Image.new("L", (edge, 1))
            row.putdata([int(hi + (lo - hi) * _smooth((x + 0.5) / edge)) for x in range(edge)])
            r = row.resize((edge, h))
            if len(self._np_fx_sprites) > 200:
                self._np_fx_sprites.clear()
            self._np_fx_sprites[key] = r
        return r

    def _np_kara_mask(self, ly, i, blur, alpha, near, t_ms):
        """The active line's mask with sung words bright and the rest dim.

        `near` (0..1) is how settled the sheet is on this line: while it
        glides in, the dimming fades in with it, so the line doesn't pop from
        its approaching brightness to half."""
        segs = self._np_kara_info(ly, i)
        if not segs:
            return None
        it = ly["items"][i]
        base = self._np_line_mask(ly, i, blur, 1.0)
        W, H = base.size
        m, lh = it["m"], it["lh"]
        edge = self._np_kara_edge(it["px"])
        nrows = len(it["lines"])
        fills, limits = kara_fills(segs, nrows, t_ms, edge)
        aq = round(alpha * 48) / 48
        hi = int(255 * aq)
        lo = int(255 * aq * (1.0 - (1.0 - KARA_DIM) * near))
        if hi - lo < 2:
            return self._np_line_mask(ly, i, blur, alpha)
        lvl = Image.new("L", (W, H), lo)
        for r in range(nrows):
            y0 = 0 if r == 0 else m + r * lh
            y1 = H if r == nrows - 1 else m + (r + 1) * lh
            f = fills[r]
            if f >= 10 ** 5:
                lvl.paste(hi, (0, y0, W, y1))
                continue
            fx = m + int(round(f))
            if fx > 0:
                lvl.paste(hi, (0, y0, min(W, fx), y1))
            if fx + edge > 0 and fx < W:
                lvl.paste(self._np_kara_ramp(edge, y1 - y0, hi, lo), (fx, y0))
            if limits[r] is not None:
                lx = m + int(limits[r])
                if lx < W:
                    lvl.paste(lo, (max(0, lx), y0, W, y1))
        return ImageChops.multiply(base, lvl)

    def _np_kara_busy(self, ly, t_ms):
        """True while the active line's syllables are being sung."""
        i = ly["fto"]
        segs = self._np_kara_info(ly, i) if 0 <= i < len(ly["items"]) else None
        if not segs:
            return False
        return segs[0][3] - 400 <= t_ms <= segs[-1][4] + 300

    # ── Instrumental dots ────────────────────────────────────────
    def _np_dots_geom(self, px):
        d = max(4.0, px * 0.40)
        return d, d * 0.7           # diameter, gap between dots

    def _np_dots_mask(self, px, scale, levels):
        """Three discs, each at its own level (0..1), `scale` of full size,
        anti-aliased (drawn at 4x) and centred in a fixed box so a breathing
        group never jitters by a pixel."""
        d, g = self._np_dots_geom(px)
        bw, bh = int(3 * d + 2 * g + 4), int(d + 4)
        q = (round(scale * 64), tuple(int(v * 32) for v in levels))
        key = ("dots", int(px), q)
        cached = self._np_fx_sprites.get(key)
        if cached is not None:
            return cached
        sc = 4
        m = Image.new("L", (bw * sc, bh * sc), 0)
        dr = ImageDraw.Draw(m)
        s = q[0] / 64.0
        cy = bh * sc / 2
        r = d * s * sc / 2
        for j in range(3):
            cx = (2 + d / 2 + j * (d + g)) * sc
            # Each dot shrinks towards its own centre.
            if r > 0.5:
                dr.ellipse((cx - r, cy - r, cx + r, cy + r), fill=int(255 * q[1][j] / 32))
        out = m.reduce(sc)
        if len(self._np_fx_sprites) > 200:
            self._np_fx_sprites.clear()
        self._np_fx_sprites[key] = out
        return out

    def _np_dots_state(self, entry, t_ms, now, active, playing):
        """(scale, [three levels]) for a break row."""
        if not active:
            return DOT_REST, (0.8, 0.8, 0.8)
        t0, t1 = entry["t0"], entry["t1"] or entry["t0"]
        if entry["kind"] == "intro":
            t0 = 0
        span = max(1.0, t1 - t0)
        p = _clamp((t_ms - t0) / span)
        grow = _ease_out(_clamp((t_ms - t0) / (DOTS_IN_S * 1000)))
        shrink = _ease_out(_clamp((t1 - t_ms) / DOTS_OUT_MS))
        breathe = 1.0
        if playing and M.ANIMATIONS_ENABLED and self._np_tier() < 2:
            breathe = 1.0 + 0.09 * math.sin(2 * math.pi * now / BREATHE_S)
        scale = (DOT_REST + (1 - DOT_REST) * grow) * breathe * shrink
        levels = tuple(0.30 + 0.70 * _clamp(p * 3 - j) for j in range(3))
        return scale, levels

    def _np_draw_dots(self, frame, ly, i, x, y, alpha, now, t_ms, playing):
        it = ly["items"][i]
        plan, _ = self._np_plan() if self._np_synced() else ([], None)
        entry = plan[i] if i < len(plan) else {"kind": "gap", "t0": 0, "t1": 0}
        active = self._np_synced() and i == ly["fto"] and (entry["kind"] == "intro" and t_ms < entry["t1"]
                                                           or entry["kind"] == "gap")
        scale, levels = self._np_dots_state(entry, t_ms, now, active, playing)
        if not active and i == ly["fto"] - 1:
            # Just left behind: grow back from nothing as the sheet moves on.
            scale *= _ease_out(_clamp((now - ly["t0"]) / self.LINE_MOVE_S))
        if scale <= 0.02 or alpha <= 0.02:
            return
        mask = self._np_dots_mask(it["px"], scale, levels)
        if alpha < 0.995:
            mask = mask.point([int(v * alpha) for v in range(256)])
        d, _ = self._np_dots_geom(it["px"])
        cy = y + it["h"] // 2
        frame.paste(self._np_fg(), (int(x - 2), int(cy - mask.size[1] / 2)), mask)

    def _np_dots_busy(self, ly, t_ms):
        """The active break's dots move (breathe, fill) while it plays."""
        if not self._np_synced() or not self._np_playing():
            return False
        plan, _ = self._np_plan()
        i = ly["fto"]
        if not (0 <= i < len(plan)):
            return False
        e = plan[i]
        if e["kind"] == "gap" or (e["kind"] == "intro" and t_ms < e["t1"]):
            return self._np_tier() < 2 and M.ANIMATIONS_ENABLED
        return False

    def _np_dots_sig(self, t_ms):
        """Coarse fill state of the active break, for the frame-skip check
        (the Fast tier doesn't breathe, but the dots still fill)."""
        if not self._np_synced():
            return None
        plan, t0s = self._np_plan()
        i = plan_index(t0s, t_ms)
        e = plan[i]
        if e["kind"] == "line" or not e["t1"]:
            return None
        t0 = 0 if e["kind"] == "intro" else e["t0"]
        return (i, int(_clamp((t_ms - t0) / max(1, e["t1"] - t0)) * 24),
                t_ms > e["t1"] - DOTS_OUT_MS)

    # ── Cover crossfade ──────────────────────────────────────────
    def _np_cover_changed(self):
        """A new cover (or none): fade from what's shown now."""
        cov = self._np_cov
        cov["prev"] = cov["cur"]
        cov["cur"] = None
        cov["src"] += 1
        cov["t0"] = time.monotonic()
        self._np_want_frame = True

    def _np_cover_geom(self):
        S = self._S
        return S(28), S(20), S(56)          # pad, top, size (as _np_build_header)

    def _np_cover_sprite(self, A):
        cov = self._np_cov
        cur = cov["cur"]
        if cur is not None and cur.size == (A, A):
            return cur
        S = self._S
        mask = self._np_rounded_mask((A, A), S(8))
        if self._img is not None:
            spr = self._img.convert("RGB").resize((A, A), Image.LANCZOS).convert("RGBA")
            spr.putalpha(mask)
        else:
            spr = Image.new("RGBA", (A, A), (0, 0, 0, 0))
            self._np_pill(spr, (0, 0, A, A), 0.10)
            TR = self._np_text
            TR.draw(ImageDraw.Draw(spr), (A // 2 - self._px(9) // 2, A // 2 - self._px(9) * 0.7),
                    "♫", "regular", self._px(12), self._np_rgba(0.45))
        cov["cur"] = spr
        return spr

    def _np_cover_busy(self, now):
        return M.ANIMATIONS_ENABLED and now - self._np_cov["t0"] < COVER_S

    def _np_draw_cover(self, frame, now, dy=0, fade=1.0):
        pad, top, A = self._np_cover_geom()
        top += dy
        new = self._np_cover_sprite(A)
        cov = self._np_cov
        t = _clamp((now - cov["t0"]) / COVER_S) if M.ANIMATIONS_ENABLED else 1.0
        e = _ease_out(t)
        prev = cov["prev"]
        if t >= 1.0:
            cov["prev"] = prev = None

        def put(spr, a, scale):
            if a <= 0.01:
                return
            if scale < 0.999:
                n = max(2, int(round(A * scale)))
                spr = spr.resize((n, n), Image.BICUBIC)
            al = spr.getchannel("A")
            if a < 0.999:
                al = al.point([int(v * a) for v in range(256)])
            o = (A - spr.size[0]) // 2
            frame.paste(spr.convert("RGB"), (pad + o, top + o), al)

        if prev is not None and prev.size == (A, A):
            put(prev, (1.0 - e) * fade, 1.0)
        put(new, (e if prev is not None or t < 1.0 else 1.0) * fade, 0.96 + 0.04 * e)

    # ── Beat ─────────────────────────────────────────────────────
    def _np_beat_on(self, tier):
        beats = getattr(M.state, "beats", None)
        return (bool(beats) and tier == 0 and M.ANIMATIONS_ENABLED and beat_react()
                and self._np_playing())

    def _np_beat_level(self, tier):
        """(beat index, lift 0..BEAT_LIFT) at the current position."""
        if not self._np_beat_on(tier):
            return None, 0
        beats = M.state.beats
        pos = self._estimate_pos_ms()
        j = bisect.bisect_right(beats, pos) - 1
        if j < 0:
            return None, 0
        dt = pos - beats[j]
        if dt >= BEAT_MS:
            return j, 0
        k = 1.0 - dt / BEAT_MS
        return j, int(round(BEAT_LIFT * k * k))

    def _np_beat_apply(self, frame, lift):
        if lift <= 0:
            return frame
        key = (frame.size, lift)
        solid = self._np_beat_solid.get(key)
        if solid is None:
            if len(self._np_beat_solid) > 2 * BEAT_LIFT:
                self._np_beat_solid.clear()
            solid = Image.new("RGB", frame.size, (lift, lift, lift))
            self._np_beat_solid[key] = solid
        return ImageChops.add(frame, solid)

    def _np_ms_to_next_beat(self, tier):
        if not self._np_beat_on(tier):
            return 10_000
        beats = M.state.beats
        pos = self._estimate_pos_ms()
        j = bisect.bisect_right(beats, pos)
        return int(max(4, beats[j] - pos + 1)) if j < len(beats) else 10_000

    # ── Fullscreen ───────────────────────────────────────────────
    def _np_fullscreen_toggle(self, _e=None):
        if self._np_fs:
            self._np_fullscreen_exit()
        else:
            self._np_fullscreen_enter()
        return "break"

    def _np_fullscreen_enter(self):
        root = self._root
        if self._np_fs:
            return
        if getattr(self, "_mini", None) is not None:
            return                      # mini mode owns the window
        try:
            info = {"geo": root.geometry(), "state": root.state(),
                    "top": bool(root.attributes("-topmost"))}
        except tk.TclError:
            return
        nav_row = getattr(getattr(self, "_nav", None), "master", None)
        try:
            info["nav"] = (nav_row, nav_row.pack_info()) if nav_row is not None else None
            if nav_row is not None:
                nav_row.pack_forget()
        except tk.TclError:
            info["nav"] = None
        # A full-screen frame has 4-6x the pixels; the adaptive quality
        # measures it afresh, and the window's own tier comes back on exit.
        info["tier"] = self._np_auto_tier
        self._np_auto_tier, self._np_frames, self._np_cost = 0, 0, None
        self._np_fs = info
        if self._cur_page != "NOW PLAYING":
            self._show("NOW PLAYING")
        try:
            root.attributes("-fullscreen", True)
            root.focus_force()          # restyling can drop focus; Esc must work
        except tk.TclError:
            pass
        now = time.monotonic()
        self._np_chrome_t = now
        self._np_chrome = [1.0, now]
        self._np_relayout()
        M.log("Full screen lyrics (Esc or F11 to leave)")

    def _np_fullscreen_exit(self):
        """Leave fullscreen. Returns True if it was on (Esc uses this)."""
        info = self._np_fs
        if not info:
            return False
        self._np_fs = None
        self._np_auto_tier, self._np_frames, self._np_cost = info.get("tier", 0), 0, None
        root = self._root
        try:
            root.attributes("-fullscreen", False)
            if info.get("state") == "zoomed":
                root.state("zoomed")
            else:
                root.geometry(info["geo"])
            root.attributes("-topmost", info.get("top", False))
        except tk.TclError:
            pass
        nav = info.get("nav")
        if nav:
            row, pinfo = nav
            try:
                opts = dict(pinfo)
                opts["before"] = self._container
                row.pack(opts)
            except tk.TclError:
                pass
        self._np_show_cursor()
        self._np_relayout()
        return True

    def _np_show_cursor(self):
        if self._np_cursor_hidden:
            self._np_cursor_hidden = False
            try:
                self.np_cv.config(cursor="hand2" if self._np_hover else "")
            except tk.TclError:
                pass

    def _np_fs_motion(self, x, y):
        """Pointer moved in fullscreen: bring the controls back."""
        self._np_chrome_t = time.monotonic()
        self._np_show_cursor()

    def _np_chrome_target(self, now):
        if not self._np_fs:
            return 1.0
        if self._np_drag is not None:
            return 1.0
        return 1.0 if now - self._np_chrome_t < CHROME_IDLE_S else 0.0

    def _np_chrome_level(self, now):
        """How visible the header and footer are (always 1 outside fullscreen)."""
        if not self._np_fs:
            return 1.0
        cur, t_last = self._np_chrome
        tgt = self._np_chrome_target(now)
        step = (now - t_last) / CHROME_FADE_S if M.ANIMATIONS_ENABLED else 1.0
        cur = min(tgt, cur + step) if tgt > cur else max(tgt, cur - step)
        self._np_chrome = [cur, now]
        if tgt == 0.0 and cur <= 0.0 and not self._np_cursor_hidden:
            self._np_cursor_hidden = True
            try:
                self.np_cv.config(cursor="none")
            except tk.TclError:
                pass
        return cur

    def _np_fs_scale(self):
        """Lyric size multiplier in fullscreen, by screen height."""
        if not self._np_fs:
            return 1.0
        H = self._np_size[1]
        return _clamp(H / 760.0 * 1.6, 1.6, 3.2)

    def _np_lyric_pad(self, W):
        base = self._S(28)
        return max(base, int(W * 0.08)) if self._np_fs else base

    def _np_scrim(self, frame, top_h, bottom_h, level):
        """Fullscreen: shade the edges under the controls so they read over
        the lyrics, faded with them."""
        W, H = frame.size
        col = self._hex(M.BG)
        for at_top, h in ((True, top_h), (False, bottom_h)):
            if h <= 0:
                continue
            key = ("scrim", W, h, at_top, int(level * 32))
            m = self._np_fx_sprites.get(key)
            if m is None:
                colm = Image.new("L", (1, h))
                vals = []
                for yy in range(h):
                    v = 1.0 - yy / h if at_top else (yy + 1) / h
                    vals.append(int(255 * 0.82 * _smooth(_clamp(v * 1.4)) * int(level * 32) / 32))
                colm.putdata(vals)
                m = colm.resize((W, h))
                self._np_fx_sprites[key] = m
            frame.paste(col, (0, 0 if at_top else H - h), m)

    # ── Frame-skip signature and the clock ───────────────────────
    def _np_fx_sig(self, now, tier):
        t_ms = self._np_sheet_pos() if self._np_synced() else 0
        beat = self._np_beat_level(tier)[0] if self._np_beat_on(tier) else None
        return (self._np_dots_sig(t_ms) if tier >= 2 or not self._np_playing() else None,
                self._np_chrome_target(now) if self._np_fs else None, beat,
                self._np_cov["src"], bool(self._np_fs))

    def _np_ms_to_next_event(self, tier):
        """ms until the next row of the sheet starts (a line or a break)."""
        if not self._np_synced() or not self._np_playing():
            return 10_000
        pos = self._np_sheet_pos()
        plan, t0s = self._np_plan()
        j = bisect.bisect_right(t0s, pos)
        nxt = int(max(4, t0s[j] - pos + 2)) if j < len(t0s) else 10_000
        # A break's dots start shrinking a little before the next line.
        i = j - 1
        if 0 <= i < len(plan) and plan[i]["kind"] != "line" and plan[i]["t1"]:
            out = plan[i]["t1"] - DOTS_OUT_MS - pos
            if out > 0:
                nxt = min(nxt, int(out) + 2)
        return nxt

    # ── Settings → Appearance rows ───────────────────────────────
    SIZE_PRESETS = (("Small", -2), ("Default", 0), ("Large", 4), ("Huge", 8))

    def _np_appearance_rows(self, Buttons, Segmented, T):
        """Rows the Settings page appends to its Appearance card: lyric font,
        lyric size (the same LYRIC_FONT_BOOST the old A-/A+ stepped) and the
        beat toggle. The control classes are passed in to avoid an import
        cycle with statusify_ui_settings."""
        self.lbl_lyric_font = T(lyric_font(), M.TEXT)

        def step_font(d):
            fams = families()
            cur = lyric_font()
            i = fams.index(cur) if cur in fams else 0
            name = fams[(i + d) % len(fams)]
            self._np_set_lyric_font(name)
            self.lbl_lyric_font.config(text=name)

        def size_get():
            b = M.LYRIC_FONT_BOOST
            return str(min(self.SIZE_PRESETS, key=lambda p: abs(p[1] - b))[1])

        def size_set(v):
            M.LYRIC_FONT_BOOST = int(v)
            M._cfg_set_soon("preferences", "lyric_font_boost", str(M.LYRIC_FONT_BOOST))
            self._np_relayout()

        widest = max(TextRenderer().measure(f, "semibold", 13) for f in families())
        return [
            {"title": "Lyric font",
             "desc": "Japanese, Korean, emoji and other scripts keep their own fonts.",
             "ctl": Buttons(self, ("btn", "‹", lambda: step_font(-1), "secondary"),
                            ("value", self.lbl_lyric_font, int(widest) + 16),
                            ("btn", "›", lambda: step_font(1), "secondary"))},
            {"title": "Lyric size",
             "ctl": Segmented(self, [(n, str(v)) for n, v in self.SIZE_PRESETS], size_get, size_set)},
            {"title": "React to the beat",
             "desc": "The background swells gently on each beat when Spotify shares beat data. "
                     "Smooth quality only.",
             "ctl": self._switch_ctl(beat_react, lambda: set_beat_react(not beat_react()))},
        ]
