"""The cover backdrop and its RGBA helpers (PIL only, no Tk)."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image, ImageStat

import statusify_backdrop as B


def _cover(rgb=(40, 170, 200)):
    img = Image.new("RGB", (128, 128), rgb)
    img.paste((230, 90, 40), (0, 0, 64, 64))
    return img


def _mean(img):
    return sum(ImageStat.Stat(img).mean) / 3


def _bd(cover=True):
    bd = B.Backdrop()
    bd.set_cover(_cover() if cover else None, 0.0)
    return bd


def test_frame_has_the_requested_size_and_mode():
    bd = _bd()
    for size in ((520, 720), (300, 200), (9, 9)):
        f = bd.render(size, 100.0, 0.55)
        assert f.mode == "RGB" and f.size == size


def test_brightness_scales_the_frame():
    bd = _bd()
    lyr = _mean(bd.render((200, 280), 100.0, B.LYRICS_BRIGHTNESS, dither=False))
    page = _mean(bd.render((200, 280), 100.0, B.PAGE_BRIGHTNESS, dither=False))
    assert page < lyr
    assert 0.3 < page / lyr < 0.6                    # ~0.24 / 0.55


def test_page_brightness_keeps_white_text_readable():
    """#a3a3ad (muted) needs 4.5:1 on the darkest-to-lightest backdrop pixel."""
    from statusify_colors import contrast_ratio
    bd = B.Backdrop()
    bd.set_cover(Image.new("RGB", (64, 64), (255, 255, 255)), 0.0)   # worst case: white cover
    f = bd.render((120, 160), 100.0, B.PAGE_BRIGHTNESS, dither=False)
    hi = max(f.get_flattened_data() if hasattr(f, "get_flattened_data") else f.getdata(), key=sum)
    assert contrast_ratio(B.MUTED, "#%02x%02x%02x" % hi) >= 4.5


def test_the_cover_colours_the_frame():
    warm, cold = B.Backdrop(), B.Backdrop()
    warm.set_cover(Image.new("RGB", (64, 64), (220, 80, 30)), 0.0)
    cold.set_cover(Image.new("RGB", (64, 64), (30, 80, 220)), 0.0)
    w = ImageStat.Stat(warm.render((100, 140), 50.0, 0.55, dither=False)).mean
    c = ImageStat.Stat(cold.render((100, 140), 50.0, 0.55, dither=False)).mean
    assert w[0] > w[2] and c[2] > c[0]


def test_new_cover_crossfades():
    bd = B.Backdrop()
    bd.set_cover(Image.new("RGB", (64, 64), (220, 80, 30)), 0.0)
    assert not bd.crossfading(10.0)
    bd.set_cover(Image.new("RGB", (64, 64), (30, 80, 220)), 10.0)
    assert bd.crossfading(10.1)
    start = ImageStat.Stat(bd.render((80, 100), 10.0, 0.55, motion_t=5, dither=False)).mean
    mid = ImageStat.Stat(bd.render((80, 100), 10.0 + B.Backdrop.CROSSFADE_S / 2, 0.55,
                                   motion_t=5, dither=False)).mean
    end = ImageStat.Stat(bd.render((80, 100), 10.0 + B.Backdrop.CROSSFADE_S + 2, 0.55,
                                   motion_t=5, dither=False)).mean
    assert start[0] > mid[0] > end[0]                 # red fades out
    assert start[2] < mid[2] < end[2]                 # blue fades in
    assert not bd.crossfading(10.0 + B.Backdrop.CROSSFADE_S + 2)


def test_frozen_motion_gives_the_same_frame():
    bd = _bd()
    a = bd.render((120, 160), 100.0, 0.55, motion_t=40.0, dither=False)
    b = bd.render((120, 160), 130.0, 0.55, motion_t=40.0, dither=False)
    assert a.tobytes() == b.tobytes()
    c = bd.render((120, 160), 130.0, 0.55, dither=False)
    assert a.tobytes() != c.tobytes()                 # it does move when not frozen


def test_drift_goes_there_and_back():
    bd = B.Backdrop()
    assert bd.drift(0) == (1.0, 0.0, 0.0, 0.0)
    s, deg, tx, ty = bd.drift(B.Backdrop.DRIFT_S)
    assert abs(s - 1.18) < 1e-9 and abs(deg - 8) < 1e-9 and tx > 0 > ty
    assert abs(bd.drift(2 * B.Backdrop.DRIFT_S)[0] - 1.0) < 1e-9


def test_without_a_cover_it_is_water_only():
    bd = _bd(cover=False)
    assert not bd.has_cover()
    f = bd.render((100, 120), 5.0, 0.55)
    assert f.size == (100, 120)


def test_frame_is_cheap_enough():
    bd = _bd()
    bd.render((520, 720), 1.0, 0.55)
    t = time.perf_counter()
    for i in range(10):
        bd.render((520, 720), 1.0 + i * 0.08, 0.55)
    ms = (time.perf_counter() - t) * 100
    assert ms < 40, f"{ms:.1f} ms per frame"


def test_card_is_see_through_with_a_rim():
    img = B.card_rgba(100, 60, 10, (114, 214, 224))
    assert img.mode == "RGBA" and img.size == (100, 60)
    assert all(abs(a - b) <= 1 for a, b in zip(img.getpixel((50, 30)), B.CARD_FILL))  # inside
    assert img.getpixel((0, 0))[3] == 0                   # outside the rounded corner
    rim = img.getpixel((50, 0))
    assert all(abs(a - b) <= 3 for a, b in zip(rim, (114, 214, 224, B.RIM_ALPHA)))


def test_round_rgba_clears_the_corners():
    img = B.round_rgba(Image.new("RGB", (40, 40), (200, 10, 10)), 6)
    assert img.getpixel((0, 0))[3] == 0 and img.getpixel((20, 20)) == (200, 10, 10, 255)


def test_surface_tokens_map_to_see_through_colours():
    toks = {"BG": "#0c1418", "BG2": "#0b1015", "BG3": "#20252a", "BG4": "#31363a",
            "HOVER_BG": "#1a1f24", "ACCENT_SOFT": "#1f3a40", "BORDER": "#2c5157"}
    smap = B.surface_map(toks, "#72d6e0")
    assert B.to_rgba("#0B1015", smap) == (0, 0, 0, 0)
    assert B.to_rgba(toks["BG3"], smap) == (255, 255, 255, B.WHITE_BG3)
    assert B.to_rgba(toks["BORDER"], smap) == (114, 214, 224, B.RIM_ALPHA)
    # A hover fade half-way from BG2 to HOVER_BG: white at half the alpha.
    from statusify_colors import _blend
    half = B.to_rgba(_blend(toks["BG2"], toks["HOVER_BG"], 0.5), smap)
    assert half[:3] == (255, 255, 255) and abs(half[3] - B.WHITE_HOVER / 2) <= 2
    # Anything else stays solid.
    assert B.to_rgba("#ff0000", smap) == (255, 0, 0, 255)


def test_ease_out_quart():
    assert B.ease_out_quart(0) == 0 and B.ease_out_quart(1) == 1
    assert B.ease_out_quart(0.25) > 0.6               # most of the travel early
