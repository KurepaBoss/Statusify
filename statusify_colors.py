"""Pure colour helpers used by the theme and animations."""

# ── Colour maths ──────────────────────────────────────────────────
# Small pure helpers, kept module-level so tests can exercise them without
# a Tk root. Everything the UI animates (hover fades, accent tinting) needs
# to interpolate between two hex colours, and the accent is user-chosen so
# nothing that derives from it can be hardcoded.

def _hex_to_rgb(c):
    """'#rrggbb' → (r, g, b). Tolerates '#rgb' and a missing '#'."""
    c = str(c).lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16))

def _rgb_to_hex(rgb):
    r, g, b = (max(0, min(255, int(round(v)))) for v in rgb)
    return f"#{r:02x}{g:02x}{b:02x}"

def _blend(c1, c2, t):
    """Mix c1→c2 by t in 0..1. t=0 is c1, t=1 is c2."""
    t = max(0.0, min(1.0, float(t)))
    a, b = _hex_to_rgb(c1), _hex_to_rgb(c2)
    return _rgb_to_hex(a[i] + (b[i] - a[i]) * t for i in range(3))

def _luminance(c):
    """Perceived luminance 0..1 (Rec. 601 weights — good enough to pick
    between black and white foreground on an arbitrary accent)."""
    r, g, b = _hex_to_rgb(c)
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255.0

def _readable_on(c):
    """Black or white, whichever stays legible on top of `c`.

    The RPC button used to hardcode fg='#000000' on an ACCENT background.
    The accent is a colour picker — choose anything dark (navy, maroon) and
    the button's label went black-on-black."""
    return "#000000" if _luminance(c) > 0.55 else "#ffffff"


# ── Album-art tint ────────────────────────────────────────────────
# The lyric-sheet window takes its colour from the playing track's cover.
# Only the cover's HUE (and a capped saturation) is used: every surface and
# text colour is then built at a FIXED lightness, so contrast is the same for
# every album — a white cover can't wash the text out, a black one can't sink
# it. A grey cover yields no tint and the neutral theme stays.
import colorsys as _colorsys


def tint_from_pixels(pixels, min_sat=0.22):
    """Dominant hue of an iterable of (r, g, b) as '#rrggbb', or None.

    Averages hue on the colour circle (so reds either side of 0° don't cancel
    to cyan), weighting each pixel by saturation × mid-lightness so vivid
    mid-tones decide and near-black/near-white pixels barely count."""
    import math
    x = y = w_sum = s_sum = 0.0
    n = 0
    for r, g, b in pixels:
        n += 1
        h, l, s = _colorsys.rgb_to_hls(r / 255.0, g / 255.0, b / 255.0)
        if s < min_sat or l < 0.12 or l > 0.9:
            continue
        w = s * (1.0 - abs(l - 0.5) * 1.6)
        if w <= 0:
            continue
        x += math.cos(h * 2 * math.pi) * w
        y += math.sin(h * 2 * math.pi) * w
        w_sum += w
        s_sum += s * w
    # Needs a real share of coloured pixels, or a mostly grey cover with one
    # small red logo would tint the whole window red.
    if not n or w_sum < n * 0.06:
        return None
    hue = (math.atan2(y, x) / (2 * math.pi)) % 1.0
    sat = min(0.55, s_sum / w_sum)
    r, g, b = _colorsys.hls_to_rgb(hue, 0.5, sat)
    return _rgb_to_hex((r * 255, g * 255, b * 255))


def _hls_hex(h, l, s):
    r, g, b = _colorsys.hls_to_rgb(h, max(0.0, min(1.0, l)), max(0.0, min(1.0, s)))
    return _rgb_to_hex((r * 255, g * 255, b * 255))


# (lightness, saturation factor) per token. Every lightness is distinct, so
# no two tokens can collide — _rebuild_all maps old->new by exact colour.
_TINT_DARK = {
    "BG": (0.085, 0.55), "BG2": (0.115, 0.50), "BG3": (0.155, 0.45),
    "BG4": (0.205, 0.40), "BORDER": (0.180, 0.40), "SHADOW": (0.045, 0.50),
    "MUTED": (0.560, 0.22), "TEXT2": (0.790, 0.20), "TEXT": (0.955, 0.18),
    "ACCENT": (0.720, 1.10),
}
_TINT_LIGHT = {
    "BG": (0.945, 0.60), "BG2": (0.985, 0.60), "BG3": (0.905, 0.45),
    "BG4": (0.860, 0.40), "BORDER": (0.880, 0.35), "SHADOW": (0.820, 0.30),
    "MUTED": (0.470, 0.25), "TEXT2": (0.300, 0.25), "TEXT": (0.085, 0.30),
    "ACCENT": (0.360, 1.10),
}


def _rel_lum(c):
    def ch(v):
        v /= 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = _hex_to_rgb(c)
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast_ratio(a, b):
    """WCAG 2 contrast ratio between two '#rrggbb' colours."""
    la, lb = sorted((_rel_lum(a), _rel_lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


# Minimum WCAG contrast of each text-ish token against every surface.
_TINT_MIN_CONTRAST = {"TEXT": 7.0, "TEXT2": 4.5, "MUTED": 3.0, "ACCENT": 3.0}


def tinted_palette(tint, dark):
    """Surface/text/accent colours built from `tint`'s hue.

    Lightness alone doesn't fix contrast: at one HLS lightness a yellow-green
    is far brighter to the eye than a blue, so a fixed table failed WCAG for
    those hues in the light theme. Each text-ish token therefore starts at
    its table lightness and steps away from the surfaces until it meets its
    minimum contrast against all of them (tests/test_album_tint.py)."""
    h, _l, s = _colorsys.rgb_to_hls(*(v / 255.0 for v in _hex_to_rgb(tint)))
    s = max(0.18, s)
    table = _TINT_DARK if dark else _TINT_LIGHT
    out = {k: _hls_hex(h, l, s * f) for k, (l, f) in table.items()}
    surfaces = [out["BG"], out["BG2"], out["BG3"]]
    step = 0.01 if dark else -0.01
    for tok, need in _TINT_MIN_CONTRAST.items():
        l, f = table[tok]
        c = out[tok]
        while min(contrast_ratio(c, bg) for bg in surfaces) < need and 0.0 < l < 1.0:
            l += step
            c = _hls_hex(h, l, s * f)
        out[tok] = c
    return out
