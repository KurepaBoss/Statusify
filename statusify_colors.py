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
