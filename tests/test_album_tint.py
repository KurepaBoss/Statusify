"""Album-art tint: readable on every cover, and neutral for grey covers."""
import colorsys
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from statusify_colors import _hex_to_rgb, tint_from_pixels, tinted_palette


def _lum(c):
    def ch(v):
        v /= 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = _hex_to_rgb(c)
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast(a, b):
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _hue_hex(h, s=0.9):
    r, g, b = colorsys.hls_to_rgb(h, 0.5, s)
    return "#%02x%02x%02x" % (int(r * 255), int(g * 255), int(b * 255))


@pytest.mark.parametrize("dark", [True, False])
@pytest.mark.parametrize("hue", [i / 24 for i in range(24)])
def test_text_stays_readable_for_every_hue(hue, dark):
    """WCAG: body text >= 7:1 (AAA), secondary >= 4.5:1, muted >= 3:1 on
    every surface, whatever colour the cover is."""
    p = tinted_palette(_hue_hex(hue), dark)
    for surface in ("BG", "BG2", "BG3"):
        assert contrast(p["TEXT"], p[surface]) >= 7.0
        assert contrast(p["TEXT2"], p[surface]) >= 4.5
        assert contrast(p["MUTED"], p[surface]) >= 3.0
    assert contrast(p["ACCENT"], p["BG"]) >= 3.0


@pytest.mark.parametrize("dark", [True, False])
def test_tokens_never_collide(dark):
    """_rebuild_all remaps old->new by exact colour, so two tokens sharing a
    value would make the recolour ambiguous."""
    for i in range(24):
        p = tinted_palette(_hue_hex(i / 24), dark)
        assert len(set(p.values())) == len(p)


def test_vivid_cover_gives_its_hue():
    t = tint_from_pixels([(200, 60, 40)] * 60 + [(10, 10, 10)] * 40)
    h, _, _ = colorsys.rgb_to_hls(*(v / 255 for v in _hex_to_rgb(t)))
    assert h < 0.06 or h > 0.97        # red


def test_grey_cover_or_small_logo_gives_no_tint():
    assert tint_from_pixels([(128, 128, 128)] * 100) is None
    assert tint_from_pixels([(128, 128, 128)] * 98 + [(255, 0, 0)] * 2) is None
    assert tint_from_pixels([]) is None


def test_reds_either_side_of_zero_average_to_red_not_cyan():
    t = tint_from_pixels([(220, 30, 60)] * 50 + [(220, 60, 30)] * 50)
    h, _, _ = colorsys.rgb_to_hls(*(v / 255 for v in _hex_to_rgb(t)))
    assert h < 0.06 or h > 0.94
