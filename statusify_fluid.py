"""The lyric sheet's moving background: album colours flowing like thick water.

Pure PIL, no Tk, so it can be tested and benchmarked on its own. The page
(statusify_ui_now_playing) owns the Tk side: it asks for a frame, draws the
lyrics and controls on top, and pushes the result into one PhotoImage.

How a frame is made:
  1. A handful of soft round blobs, one colour each from the cover's palette,
     drift along slow Lissajous paths over a base colour. This happens on a
     tiny grid (about 40 px wide), so it costs almost nothing.
  2. The grid is blurred and scaled up with bicubic filtering. Scaling a
     blurred 40 px image to 500 px *is* the heavy blur; there is no
     full-size blur pass.
  3. A fixed noise layer dithers the result. Smooth dark gradients on an
     8-bit screen band visibly; ±2 levels of noise hides the steps.
  4. The top and bottom edges fade into the window's own background colour,
     so the sheet meets the title bar and the tab switcher without a seam.

Colours are normalised to a lightness band per theme before use, so a white
cover can't wash out white lyrics and a black cover can't sink them.
"""
import colorsys
import math
import random

try:
    from PIL import Image, ImageChops, ImageDraw, ImageFilter
    PIL_AVAILABLE = True
except ImportError:          # the page falls back to a flat colour
    PIL_AVAILABLE = False


# ── Palette ───────────────────────────────────────────────────────

def palette_from_image(img, n=5):
    """Up to `n` representative (r, g, b) colours of `img`, most common first.

    Median-cut quantisation on a small copy. The most populous colour leads
    (it becomes the base the blobs float on); the rest are ordered by
    population too, but a near-duplicate of an earlier colour is skipped so
    five blobs don't all come out the same shade of the dominant colour."""
    if img is None or not PIL_AVAILABLE:
        return []
    try:
        small = img.convert("RGB").resize((48, 48), Image.BILINEAR)
        q = small.quantize(colors=10, method=Image.Quantize.MEDIANCUT)
        pal = q.getpalette() or []
        counts = sorted(q.getcolors() or [], reverse=True)
    except Exception:
        return []
    out = []
    for _count, idx in counts:
        c = tuple(pal[idx * 3: idx * 3 + 3])
        if len(c) != 3:
            continue
        if all(sum(abs(a - b) for a, b in zip(c, o)) > 60 for o in out):
            out.append(c)
        if len(out) >= n:
            break
    return out


# Lightness band per theme: (base, blobs). Dark keeps everything low enough
# for white text at full contrast; light keeps it high enough for near-black.
_BANDS = {
    True:  {"base": (0.10, 0.17), "blob": (0.20, 0.40), "sat": 1.15},
    False: {"base": (0.86, 0.91), "blob": (0.70, 0.84), "sat": 0.95},
}


def _rel_lum(rgb):
    def ch(v):
        v /= 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = rgb
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


# Luminance limits that keep lyrics readable on any blob: at most 0.16 under
# white text (>= 4.8:1), at least 0.52 under near-black text. HLS lightness
# alone can't promise this: a yellow at L=0.4 is far brighter than a blue.
_LUM_MAX_DARK, _LUM_MIN_LIGHT = 0.16, 0.52


def _clamp_l(rgb, lo, hi, sat_mul, dark=True):
    h, l, s = colorsys.rgb_to_hls(*(v / 255.0 for v in rgb))
    l = min(hi, max(lo, l))
    s = min(1.0, s * sat_mul)

    def to_rgb(l_):
        r, g, b = colorsys.hls_to_rgb(h, l_, s)
        return (int(r * 255), int(g * 255), int(b * 255))
    out = to_rgb(l)
    while dark and l > 0.02 and _rel_lum(out) > _LUM_MAX_DARK:
        l -= 0.01
        out = to_rgb(l)
    while not dark and l < 0.98 and _rel_lum(out) < _LUM_MIN_LIGHT:
        l += 0.01
        out = to_rgb(l)
    return out


def normalise(palette, dark, fallback):
    """(base, [blob colours]) ready to paint, for the given theme.

    `fallback` ('#rrggbb') is used when there's no palette at all (no cover,
    Pillow missing): a quiet field of the theme's own surface colours."""
    band = _BANDS[bool(dark)]
    if not palette:
        fr = tuple(int(fallback.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
        palette = [fr, fr, fr]
    base = _clamp_l(palette[0], *band["base"], band["sat"], dark)
    blobs = [_clamp_l(c, *band["blob"], band["sat"], dark) for c in (palette[1:] or palette)]
    # Always five blobs; cycle a short palette.
    blobs = [blobs[i % len(blobs)] for i in range(5)]
    return base, blobs


def _mix(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


# ── Field ─────────────────────────────────────────────────────────

class FluidField:
    """Renders frames of the moving background. One per page.

    Blob motion is a pure function of time, so pausing costs nothing and a
    frame can be re-rendered at any size without accumulated state. A palette
    change crossfades each blob's colour over CROSSFADE_S rather than
    snapping, which is what makes a track change feel like the water itself
    changing colour."""

    GRID_W = 40            # blob grid width in px; height follows the aspect
    CROSSFADE_S = 1.6

    def __init__(self, seed=7):
        rnd = random.Random(seed)
        # Per blob: two frequencies per axis (rad/s), phases, radius wobble.
        # Periods land between ~25 s and ~90 s: slow, heavy, never repeating
        # visibly because the axes are incommensurate.
        self._blobs = []
        for _ in range(5):
            self._blobs.append({
                "fx": rnd.uniform(0.07, 0.16), "fy": rnd.uniform(0.06, 0.14),
                "gx": rnd.uniform(0.17, 0.29), "gy": rnd.uniform(0.15, 0.27),
                "px": rnd.uniform(0, 6.3), "py": rnd.uniform(0, 6.3),
                "qx": rnd.uniform(0, 6.3), "qy": rnd.uniform(0, 6.3),
                "r": rnd.uniform(0.34, 0.50), "fr": rnd.uniform(0.10, 0.22),
                "pr": rnd.uniform(0, 6.3),
            })
        self._from = None       # (base, blobs) crossfading out
        self._to = None         # (base, blobs) current target
        self._mix_t0 = None
        self._sprite = self._soft_disc(64)
        self._noise = {}        # size -> dither layer
        self._fade = {}         # (size, top, bottom) -> edge mask

    @staticmethod
    def _soft_disc(n):
        """A disc with a smooth falloff, used as each blob's paint mask."""
        m = Image.new("L", (n, n), 0)
        d = ImageDraw.Draw(m)
        c = n / 2
        for i in range(int(c), 0, -1):
            # Quadratic falloff: dense core, long soft shoulder.
            v = int(255 * (1 - (i / c)) ** 0.9)
            d.ellipse((c - i, c - i, c + i, c + i), fill=v)
        return m

    def set_colors(self, base_blobs, now):
        """Start crossfading to (base, blobs). Idempotent for the same colours."""
        if base_blobs == self._to:
            return
        if self._to is None:
            self._to = base_blobs
            self._from = None
            return
        self._from = self.current_colors(now)
        self._to = base_blobs
        self._mix_t0 = now

    def current_colors(self, now):
        if self._to is None:
            return ((0, 0, 0), [(0, 0, 0)] * 5)
        if self._from is None or self._mix_t0 is None:
            return self._to
        t = min(1.0, (now - self._mix_t0) / self.CROSSFADE_S)
        t = t * t * (3 - 2 * t)                     # smoothstep
        if t >= 1.0:
            self._from = None
            return self._to
        fb, fbl = self._from
        tb, tbl = self._to
        return _mix(fb, tb, t), [_mix(a, b, t) for a, b in zip(fbl, tbl)]

    def crossfading(self):
        return self._from is not None

    def _dither(self, size):
        n = self._noise.get(size)
        if n is None:
            self._noise.clear()
            n = Image.effect_noise(size, 40).point(lambda v: max(0, min(4, (v - 128) // 26 + 2)))
            n = Image.merge("RGB", (n, n, n))
            self._noise[size] = n
        return n

    def _edge_mask(self, size, at_top):
        """Opaque at the window edge, clear towards the middle (smoothstep)."""
        key = (size, at_top)
        m = self._fade.get(key)
        if m is None:
            if len(self._fade) > 4:
                self._fade.clear()
            w, h = size
            col = Image.new("L", (1, h), 0)
            px = col.load()
            for y in range(h):
                v = 1.0 - y / h if at_top else (y + 1) / h
                px[0, y] = int(255 * v * v * (3 - 2 * v))
            m = col.resize((w, h))
            self._fade[key] = m
        return m

    def render(self, size, now, edge_color=None, top_fade=0, bottom_fade=0, motion_t=None,
               dither=True):
        """RGB frame of `size` at time `now` (seconds, any monotonic clock).

        `motion_t` overrides the time used for blob positions (a fixed value
        freezes the water when animations are off; colour crossfades still
        follow `now`)."""
        mt = now if motion_t is None else motion_t
        w, h = max(8, int(size[0])), max(8, int(size[1]))
        base, blobs = self.current_colors(now)
        gw = self.GRID_W
        gh = max(8, int(round(gw * h / w)))
        grid = Image.new("RGB", (gw, gh), base)
        span = max(gw, gh)
        for b, col in zip(self._blobs, blobs):
            cx = 0.5 + 0.34 * math.sin(mt * b["fx"] + b["px"]) + 0.14 * math.sin(mt * b["gx"] + b["qx"])
            cy = 0.5 + 0.34 * math.sin(mt * b["fy"] + b["py"]) + 0.14 * math.sin(mt * b["gy"] + b["qy"])
            r = span * b["r"] * (1.0 + 0.18 * math.sin(mt * b["fr"] + b["pr"]))
            d = max(2, int(r * 2))
            mask = self._sprite.resize((d, d), Image.BILINEAR)
            grid.paste(col, (int(cx * gw - d / 2), int(cy * gh - d / 2)), mask)
        grid = grid.filter(ImageFilter.GaussianBlur(2.2))
        # Two-step upscale: straight to full size from 40 px leaves faint
        # diamond artefacts from the bicubic kernel on large flat areas.
        mid = grid.resize((gw * 4, gh * 4), Image.BICUBIC).filter(ImageFilter.GaussianBlur(3))
        frame = mid.resize((w, h), Image.BILINEAR)   # mid is already smooth
        if dither:
            frame = ImageChops.add(frame, self._dither((w, h)), 1.0, -2)
        if edge_color is not None:
            # Only the two edge strips are touched, not the whole frame.
            for y0, hh in ((0, top_fade), (h - bottom_fade, bottom_fade)):
                if hh > 0:
                    m = self._edge_mask((w, hh), y0 == 0)
                    frame.paste(edge_color, (0, y0, w, y0 + hh), m)
        return frame

    def average(self, now):
        """Rough average colour of the field (used to pick blend colours)."""
        base, blobs = self.current_colors(now)
        acc = [base[i] * 2 for i in range(3)]
        for c in blobs:
            for i in range(3):
                acc[i] += c[i]
        n = 2 + len(blobs)
        return tuple(v // n for v in acc)
