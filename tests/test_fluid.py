"""The lyric sheet's background and text helpers (PIL only, no Tk)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from PIL import Image

import statusify_fluid as F
from statusify_textrender import TextRenderer


HUES = [(255, 255, 0), (255, 255, 255), (0, 0, 0), (0, 40, 255), (255, 0, 80),
        (30, 200, 60), (250, 160, 40), (128, 128, 128)]


@pytest.mark.parametrize("col", HUES)
def test_water_stays_dark_enough_for_white_text(col):
    base, blobs = F.normalise([col, col], True, "#000000")
    for c in [base] + blobs:
        assert F._rel_lum(c) <= F._LUM_MAX_DARK + 0.005


@pytest.mark.parametrize("col", HUES)
def test_water_stays_light_enough_for_dark_text(col):
    base, blobs = F.normalise([col, col], False, "#ffffff")
    for c in [base] + blobs:
        assert F._rel_lum(c) >= F._LUM_MIN_LIGHT - 0.005


def test_palette_from_image_leads_with_the_dominant_colour():
    img = Image.new("RGB", (40, 40), (200, 30, 30))
    img.paste((20, 20, 200), (0, 0, 10, 10))
    pal = F.palette_from_image(img)
    assert pal[0][0] > 150 and len(pal) == 2


def test_render_size_and_crossfade():
    f = F.FluidField()
    f.set_colors(F.normalise([(200, 0, 0)], True, "#000000"), 0.0)
    assert f.render((120, 90), 0.0, (0, 0, 0), 10, 10).size == (120, 90)
    f.set_colors(F.normalise([(0, 0, 200)], True, "#000000"), 1.0)
    assert f.crossfading()
    mid = f.current_colors(1.0 + F.FluidField.CROSSFADE_S / 2)[0]
    end = f.current_colors(1.0 + F.FluidField.CROSSFADE_S + 0.1)[0]
    assert mid != end and not f.crossfading()


def test_wrap_breaks_words_and_cjk():
    tr = TextRenderer()
    lines = tr.wrap("Slip it in her drink, and in the blink of an eye", "bold", 28, 300)
    assert len(lines) > 1 and " ".join(lines) == "Slip it in her drink, and in the blink of an eye"
    cjk = tr.wrap("夜に駆ける沈むように溶けてゆくように", "bold", 28, 150)
    assert len(cjk) > 1 and "".join(cjk) == "夜に駆ける沈むように溶けてゆくように"
    assert all(tr.measure(l, "bold", 28) <= 150 for l in cjk)


def test_runs_pick_a_font_per_script():
    kinds = [k for k, _ in TextRenderer().runs("Hi 사랑 夜")]
    assert kinds == ["main", "hangul", "cjk"]
