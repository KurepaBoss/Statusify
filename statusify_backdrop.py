"""The window's backdrop in cover mode: the playing song's cover, enlarged,
heavily blurred and slowly drifting, with the statusify_fluid water flowing
faintly over it. Every page shows it; Lyrics brighter, the others darker.

Pure PIL, no Tk, like statusify_fluid: the UI side (statusify_ui_backdrop)
asks for a frame and shows it.

How a frame is made (all of the per-frame work happens on a grid about 40 px
wide, so it costs about as much as the water alone did):
  1. set_cover() shrinks the cover to 24 px, blurs it and scales it back up
     to a smooth 96 px texture. That shrink *is* the heavy blur.
  2. Each frame samples that texture onto the grid through one affine
     transform: the cover drawn 1.8x the window, scaling 1.0 -> 1.18,
     turning 0 -> 8 degrees and shifting a few per cent, there and back
     over two minutes.
  3. The water grid (FluidField.grid) is mixed in at 30 %.
  4. Brightness, a vignette and the top/bottom edge darkening are one
     multiply by a precomputed mask.
  5. The grid is scaled up in two steps (as FluidField.render does) and
     dithered, so smooth dark gradients don't band on an 8-bit screen.

A new cover crossfades over CROSSFADE_S.

The module also holds the small RGBA helpers the pages use to draw
see-through cards and controls over the backdrop (Tk's canvas alpha-blends
PhotoImages with partial transparency; widgets themselves can't be
transparent, so everything that sits on the backdrop is an image item).
"""
import math

try:
    from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

from statusify_fluid import FluidField, normalise

# Brightness of the backdrop per page (the design's "Final" round).
LYRICS_BRIGHTNESS = 0.55
PAGE_BRIGHTNESS = 0.24

# Cards: rgba(10,10,14,0.42) with a 1 px rim in the accent at ~33 %.
CARD_FILL = (10, 10, 14, 107)
RIM_ALPHA = 84

# Text colours on the darkened backdrop, all >= 4.5:1 against it.
TEXT = "#f5f5f7"
TEXT2 = "#d0d0d6"
MUTED = "#a3a3ad"

# Translucent surfaces, as (white alpha) — the design's rgba(255,255,255,a).
WHITE_BG3 = 23       # 0.09: secondary buttons, the Plain badge
WHITE_BG4 = 41       # 0.16: the selected nav segment, a switch that is off
WHITE_HOVER = 15     # 0.06: row hover
ACCENT_SOFT_ALPHA = 51   # 0.20: the Synced badge, accent-flavoured fills


def ease_out_quart(t):
    """cubic-bezier(.22,1,.36,1), near enough: fast start, long settle."""
    t = min(1.0, max(0.0, t))
    return 1.0 - (1.0 - t) ** 4


def _smoothstep(t):
    t = min(1.0, max(0.0, t))
    return t * t * (3 - 2 * t)


def _hex(c):
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


# ── Backdrop ──────────────────────────────────────────────────────

class Backdrop:
    GRID_W = 40
    CROSSFADE_S = 1.2
    DRIFT_S = 60.0            # one way; the drift goes there and back
    WATER_ALPHA = 0.30
    COVER_SCALE = 1.8         # the cover is drawn this much larger than the window
    TEX_PX = 96

    def __init__(self, seed=7):
        self.fluid = FluidField(seed)
        self._tex = None          # current cover texture (RGB, TEX_PX square) or None
        self._prev = None         # texture fading out (or None: fading in from water only)
        self._t0 = None
        self._had = False         # whether a crossfade is running at all
        self._masks = {}
        self._noise = {}
        self._avg = None

    # ── Cover ────────────────────────────────────────────────────
    @classmethod
    def soft_texture(cls, img):
        """The cover as a smooth, heavily blurred, slightly saturated texture."""
        small = img.convert("RGB").resize((24, 24), Image.BILINEAR)
        small = ImageEnhance.Color(small).enhance(1.3)
        small = small.filter(ImageFilter.GaussianBlur(1.5))
        mid = small.resize((cls.TEX_PX, cls.TEX_PX), Image.BICUBIC)
        return mid.filter(ImageFilter.GaussianBlur(3))

    def set_cover(self, img, now, palette=None, dark=True, fallback="#1a1f26"):
        """Start crossfading to a new cover (None: water only).

        `palette` ([(r, g, b)], statusify_fluid.palette_from_image) colours
        the water; without one the water takes the cover's own average."""
        tex = self.soft_texture(img) if (img is not None and PIL_AVAILABLE) else None
        if tex is None and self._tex is None:
            self._avg = None
        cur = self._tex
        if cur is not None or tex is not None:
            self._prev = cur
            self._t0 = now
            self._had = True
        self._tex = tex
        if tex is not None:
            self._avg = tex.resize((1, 1), Image.BOX).getpixel((0, 0))
        else:
            self._avg = None
        if not palette and self._avg is not None:
            palette = [self._avg]
        self.fluid.set_colors(normalise(palette or [], dark, fallback), now)

    def has_cover(self):
        return self._tex is not None

    def crossfading(self, now):
        if not self._had or self._t0 is None:
            return self.fluid.crossfading()
        if now - self._t0 >= self.CROSSFADE_S:
            self._had = False
            self._prev = None
            return self.fluid.crossfading()
        return True

    def average(self, brightness=PAGE_BRIGHTNESS):
        """Rough colour of the backdrop at `brightness` (cover mixed with the
        water, then darkened), for the few surfaces that must be opaque."""
        water = self.fluid.average(0.0)
        if self._avg is None:
            base = water
        else:
            base = tuple(int(a * (1 - self.WATER_ALPHA) + w * self.WATER_ALPHA)
                         for a, w in zip(self._avg, water))
        # The vignette darkens the average a little more.
        return tuple(int(v * brightness * 0.85) for v in base)

    # ── Frame ────────────────────────────────────────────────────
    def drift(self, t):
        """(scale, angle in degrees, tx, ty) of the cover at time t: there
        and back over 2 * DRIFT_S, eased at both ends."""
        p = 0.5 - 0.5 * math.cos(math.pi * (t / self.DRIFT_S))
        return 1.0 + 0.18 * p, 8.0 * p, 0.04 * p, -0.03 * p

    def _cover_grid(self, tex, gw, gh, W, H, mt):
        s, deg, tx, ty = self.drift(mt)
        th = math.radians(deg)
        c, sn = math.cos(th), math.sin(th)
        n = tex.size[0]
        D = self.COVER_SCALE * max(W, H)            # the cover's side on screen
        # Translation is a share of the drawn cover, as in the CSS mock-up.
        Tx, Ty = tx * D, ty * D
        kx, ky = W / gw, H / gh
        cx0, cy0 = kx / 2 - W / 2 - Tx, ky / 2 - H / 2 - Ty
        k = n / (s * D)
        data = (k * c * kx, k * sn * ky, k * (c * cx0 + sn * cy0) + n / 2,
                -k * sn * kx, k * c * ky, k * (-sn * cx0 + c * cy0) + n / 2)
        return tex.transform((gw, gh), Image.AFFINE, data, Image.BICUBIC)

    def _mask(self, gw, gh):
        """Vignette x edge darkening, as an RGB multiply layer (grid size)."""
        key = (gw, gh)
        m = self._masks.get(key)
        if m is None:
            if len(self._masks) > 6:
                self._masks.clear()
            m = Image.new("L", (gw, gh))
            px = m.load()
            for j in range(gh):
                y = (j + 0.5) / gh
                if y < 0.22:
                    edge = 0.45 * (1 - y / 0.22)
                elif y > 0.70:
                    edge = 0.6 * (y - 0.70) / 0.30
                else:
                    edge = 0.0
                for i in range(gw):
                    x = (i + 0.5) / gw
                    r = math.hypot((x - 0.5) / 0.9, (y - 0.45) / 0.7)
                    vig = 0.5 * min(1.0, r)
                    px[i, j] = int(255 * (1 - vig) * (1 - edge))
            m = Image.merge("RGB", (m, m, m))
            self._masks[key] = m
        return m

    def _dither(self, size):
        n = self._noise.get(size)
        if n is None:
            self._noise.clear()
            n = Image.effect_noise(size, 40).point(lambda v: max(0, min(4, (v - 128) // 26 + 2)))
            n = Image.merge("RGB", (n, n, n))
            self._noise[size] = n
        return n

    def render(self, size, now, brightness, motion_t=None, dither=True):
        """RGB frame of `size` at time `now`.

        `motion_t` fixes the time used for the drift and the water (animations
        off); cover and colour crossfades still follow `now`."""
        mt = now if motion_t is None else motion_t
        W, H = max(8, int(size[0])), max(8, int(size[1]))
        gw = self.GRID_W
        gh = max(8, int(round(gw * H / W)))
        water = self.fluid.grid((gw, gh), now, mt)
        if self._tex is not None:
            cov = self._cover_grid(self._tex, gw, gh, W, H, mt)
            grid = Image.blend(cov, water, self.WATER_ALPHA)
        else:
            grid = water
        if self.crossfading(now) and self._t0 is not None:
            t = _smoothstep((now - self._t0) / self.CROSSFADE_S)
            if self._prev is not None:
                old = Image.blend(self._cover_grid(self._prev, gw, gh, W, H, mt), water,
                                  self.WATER_ALPHA)
            else:
                old = water
            grid = Image.blend(old, grid, t)
        grid = ImageChops.multiply(grid, self._mask(gw, gh))
        mid = grid.resize((gw * 4, gh * 4), Image.BICUBIC).filter(ImageFilter.GaussianBlur(3))
        return self.shade(mid.resize((W, H), Image.BILINEAR), brightness, dither)

    def shade(self, frame, brightness, dither=True):
        """`frame` darkened to `brightness`, then dithered. Cheap (a lookup
        table), so a page switch can move the brightness every frame from a
        frame rendered once at brightness 1."""
        b = max(0.0, min(1.0, float(brightness)))
        if b < 0.999:
            frame = frame.point([int(v * b) for v in range(256)] * 3)
        if dither:
            frame = ImageChops.add(frame, self._dither(frame.size), 1.0, -2)
        return frame


# ── RGBA drawing helpers ──────────────────────────────────────────

def _downsample(big, size):
    """Supersampled RGBA -> size, without dark fringes at the edges
    (premultiplied while resizing)."""
    return big.convert("RGBa").resize(size, Image.BOX).convert("RGBA")


def pill_rgba(w, h, radius, fill, bg=(0, 0, 0, 0), outline=None, sc=4):
    """Rounded rectangle as an RGBA image. Colours are (r, g, b, a).

    Only the corners need anti-aliasing, so a large one is built from a
    small supersampled patch whose middle row and column are stretched
    (a card 480 px tall took ~20 ms supersampled whole; this takes ~1)."""
    w, h = max(1, int(w)), max(1, int(h))
    r = max(0, int(radius))
    k = 2 * r + 3
    if r and w > k + 8 and h > k + 8:
        small = pill_rgba(k, k, r, fill, bg, outline, sc)
        c = r + 1                                  # the middle row/column
        out = Image.new("RGBA", (w, h), tuple(fill))
        e = k - c - 1                              # corner size, incl. the edge
        for (sx, sy), (dx, dy) in (((0, 0), (0, 0)), ((k - e, 0), (w - e, 0)),
                                   ((0, k - e), (0, h - e)), ((k - e, k - e), (w - e, h - e))):
            out.paste(small.crop((sx, sy, sx + e, sy + e)), (dx, dy))
        mw, mh = w - 2 * e, h - 2 * e
        out.paste(small.crop((c, 0, c + 1, e)).resize((mw, e), Image.NEAREST), (e, 0))
        out.paste(small.crop((c, k - e, c + 1, k)).resize((mw, e), Image.NEAREST), (e, h - e))
        out.paste(small.crop((0, c, e, c + 1)).resize((e, mh), Image.NEAREST), (0, e))
        out.paste(small.crop((k - e, c, k, c + 1)).resize((e, mh), Image.NEAREST), (w - e, e))
        return out
    big = Image.new("RGBA", (w * sc, h * sc), tuple(bg))
    d = ImageDraw.Draw(big)
    if outline is not None:
        d.rounded_rectangle((0, 0, w * sc - 1, h * sc - 1), radius=r * sc, fill=tuple(outline))
        d.rounded_rectangle((sc, sc, w * sc - 1 - sc, h * sc - 1 - sc),
                            radius=max(0, r * sc - sc), fill=tuple(fill))
    else:
        d.rounded_rectangle((0, 0, w * sc - 1, h * sc - 1), radius=r * sc, fill=tuple(fill))
    return _downsample(big, (w, h))


def card_rgba(w, h, radius, rim, fill=CARD_FILL, rim_alpha=RIM_ALPHA):
    """A see-through card: `fill` with a 1 px rim in `rim` (r, g, b)."""
    return pill_rgba(w, h, radius, fill, outline=tuple(rim[:3]) + (rim_alpha,))


def round_rgba(img, radius, sc=4):
    """`img` with anti-aliased transparent rounded corners (RGBA)."""
    img = img.convert("RGBA")
    w, h = img.size
    r = max(0, min(int(radius), min(w, h) // 2))
    mask = Image.new("L", (w * sc, h * sc), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w * sc - 1, h * sc - 1), radius=r * sc, fill=255)
    mask = mask.resize((w, h), Image.BOX)
    out = img.copy()
    out.putalpha(ImageChops.multiply(img.getchannel("A"), mask))
    return out


def surface_map(tokens, accent):
    """{'#rrggbb': (r, g, b, a)} for the palette's surface tokens in cover
    mode. `tokens` is {name: '#rrggbb'} for BG, BG2, BG3, BG4, HOVER_BG,
    ACCENT_SOFT and BORDER; `accent` is '#rrggbb'."""
    a = _hex(accent)
    want = {
        "BG": (0, 0, 0, 0), "BG2": (0, 0, 0, 0),
        "BG3": (255, 255, 255, WHITE_BG3), "BG4": (255, 255, 255, WHITE_BG4),
        "HOVER_BG": (255, 255, 255, WHITE_HOVER),
        "ACCENT_SOFT": a + (ACCENT_SOFT_ALPHA,), "BORDER": a + (RIM_ALPHA,),
    }
    out = {}
    for name, rgba in want.items():
        c = tokens.get(name)
        if c and c.lower() not in out:
            out[c.lower()] = rgba
    return out


def to_rgba(color, smap):
    """The RGBA a palette colour stands for over the backdrop.

    Surface tokens map to their see-through equivalents. Hover fades hand
    over a blend of two tokens (M._blend(BG2, HOVER_BG, t)); such a colour
    is recognised as lying between two tokens and gets the same blend of
    their RGBA. Anything else is drawn solid."""
    c = color.lower()
    hit = smap.get(c)
    if hit is not None:
        return hit
    p = _hex(c)
    keys = list(smap)
    for i, ka in enumerate(keys):
        a = _hex(ka)
        for kb in keys[i + 1:]:
            b = _hex(kb)
            d = [b[k] - a[k] for k in range(3)]
            dd = sum(v * v for v in d)
            if dd == 0:
                continue
            t = sum((p[k] - a[k]) * d[k] for k in range(3)) / dd
            if not -0.01 <= t <= 1.01:
                continue
            if all(abs(a[k] + d[k] * t - p[k]) <= 2.5 for k in range(3)):
                ra, rb = smap[ka], smap[kb]
                t = min(1.0, max(0.0, t))
                # A fully transparent end has no colour of its own: fade the
                # other end's colour in, don't drag it through black.
                if ra[3] == 0:
                    ra = rb[:3] + (0,)
                if rb[3] == 0:
                    rb = ra[:3] + (0,)
                return tuple(int(round(ra[k] + (rb[k] - ra[k]) * t)) for k in range(4))
    return p + (255,)
