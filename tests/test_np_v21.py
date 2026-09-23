"""Now Playing v2.1: karaoke highlight, instrumental dots, cover crossfade,
beat swell, fullscreen, lyric font and the per-song delay in the footer."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import main
import statusify_np_fx as fx
from statusify_textrender import DEFAULT_FAMILY, TextRenderer, available_families

tk = main.tk
_REAL_OFFSET = main._track_offset_ms


# ── Pure helpers ──────────────────────────────────────────────────

def _syl(words, t0, per=200):
    toks = words.split(" ")
    out, t = [], t0
    for n, w in enumerate(toks):
        out.append([t, t + per, w + (" " if n < len(toks) - 1 else "")])
        t += per
    return out


def test_plan_has_intro_then_lines_without_breaks():
    synced = [{"startMs": i * 1000, "words": f"l{i}"} for i in range(4)]
    plan = fx.build_plan(synced)
    assert [p["kind"] for p in plan] == ["intro", "line", "line", "line", "line"]
    assert [p["k"] for p in plan[1:]] == [0, 1, 2, 3]


def test_plan_breaks_from_empty_lines_known_ends_and_main_gaps():
    synced = [
        {"startMs": 1000, "words": "a", "endMs": 2000},     # 8 s of silence follows
        {"startMs": 10000, "words": ""},                     # an explicit break
        {"startMs": 15000, "words": "b", "syl": _syl("b", 15000)},
        {"startMs": 17000, "words": "c"},                    # no end known: main's gap
        {"startMs": 30000, "words": "d"},
    ]
    gaps = [{"startMs": 20000, "endMs": 30000, "key": 3}, {"startMs": 0, "endMs": 1000, "key": -2}]
    plan = fx.build_plan(synced, gaps, 60000)
    kinds = [(p["kind"], p["k"]) for p in plan]
    assert kinds == [("intro", -1), ("line", 0), ("gap", 1), ("line", 2), ("line", 3),
                     ("gap", 3), ("line", 4)]
    gap_after_c = plan[-2]
    assert (gap_after_c["t0"], gap_after_c["t1"]) == (20000, 30000)
    t0s = [p["t0"] for p in plan]
    assert fx.plan_index(t0s, 500) == 0            # intro
    assert fx.plan_index(t0s, 12000) == 2          # the empty-line break
    assert fx.plan_index(t0s, 25000) == 5          # main's instrumental gap


def test_plan_short_silence_is_not_a_break():
    synced = [{"startMs": 0, "words": "a", "endMs": 1000}, {"startMs": 3000, "words": "b"}]
    assert [p["kind"] for p in fx.build_plan(synced)] == ["intro", "line", "line"]


def test_kara_segments_follow_the_wrap():
    TR = TextRenderer()
    text = "one two three four five"
    lines = TR.wrap(text, "bold", 30, TR.measure("one two three", "bold", 30) + 2)
    assert len(lines) == 2
    segs = fx.kara_segments(TR, "bold", 30, text, lines, _syl(text, 1000))
    assert [s[0] for s in segs] == [0, 0, 0, 1, 1]
    assert segs[3][1] == 0                         # "four" starts row 2 at its left edge
    assert all(a[4] <= b[3] + 1e-6 for a, b in zip(segs, segs[1:]))


def test_kara_segments_reject_mismatched_syllables():
    TR = TextRenderer()
    assert fx.kara_segments(TR, "bold", 30, "hello world", ["hello world"],
                            [[0, 100, "goodbye "]]) is None


def test_kara_fills_progress_left_to_right_and_row_by_row():
    TR = TextRenderer()
    text = "one two three four"
    lines = ["one two", "three four"]
    segs = fx.kara_segments(TR, "bold", 30, text, lines, _syl(text, 0, per=100))
    edge = 10
    f, lim = fx.kara_fills(segs, 2, -50, edge)
    assert f[0] <= -edge + 0.01 and lim[0] == 0            # nothing sung yet
    f1, _ = fx.kara_fills(segs, 2, 150, edge)              # halfway through "two"
    assert segs[1][1] - edge < f1[0] < segs[1][2]
    f2, _ = fx.kara_fills(segs, 2, 250, edge)              # into row 2
    assert f2[0] >= 10 ** 5 and f2[1] > -edge


def test_textrender_family_keeps_fallback_runs():
    fams = available_families()
    assert fams[0] == DEFAULT_FAMILY
    other = next((f for f in fams if f != DEFAULT_FAMILY), None)
    if other is None:
        pytest.skip("no alternative font installed")
    tr = TextRenderer(other)
    runs = tr.runs("Hi 日本 😀")
    assert runs[0][0] == "main" and {"cjk", "emoji"} <= {r[0] for r in runs}
    assert tr.font("main", "bold", 30).getname()[0] != "Segoe UI"
    # A family without Hebrew draws it with Segoe UI instead of boxes.
    if not tr._has("ש"):
        assert ("fb", "שלום") in [tuple(r) for r in tr.runs("שלום")]


# ── App ───────────────────────────────────────────────────────────

@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr(main, "_register_hotkeys", lambda *a, **k: None)
    for attempt in range(3):
        try:
            a = main.App()
            break
        except tk.TclError as e:
            transient = ("installed properly" in str(e) or "tcl_findLibrary" in str(e))
            if not transient or attempt == 2:
                raise
            time.sleep(0.2)
    yield a
    try:
        a._np_fullscreen_exit()
    except Exception:
        pass
    for fn in (a._cancel_all_timers, a._tray_stop):
        try:
            fn()
        except Exception:
            pass
    a._alive = False
    a._root.destroy()
    del a
    import gc
    gc.collect()


class _Cap:
    def __init__(self, W, H):
        self.W, self.H, self.img = W, H, None

    def width(self):
        return self.W

    def height(self):
        return self.H

    def paste(self, im):
        self.img = im


LINES = [("first line here", 2000), ("sing it word by word now", 5000), ("third", 9000),
         ("after the break", 20000)]


@pytest.fixture
def sheet(app, monkeypatch):
    st = main.state
    synced = [{"startMs": t, "words": w, "syl": _syl(w, t)} for w, t in LINES]
    monkeypatch.setattr(st, "synced", synced, raising=False)
    monkeypatch.setattr(st, "lyrics_mode", "synced", raising=False)
    monkeypatch.setattr(st, "instrumental_gaps", [], raising=False)
    monkeypatch.setattr(st, "duration_ms", 60000, raising=False)
    monkeypatch.setattr(st, "is_playing", True, raising=False)
    monkeypatch.setattr(st, "track_uri", "spotify:track:npv21", raising=False)
    monkeypatch.setattr(main, "_track_offset_ms", lambda uri=None: 0)
    monkeypatch.setattr(main, "RENDER_QUALITY", "high")
    pos = {"ms": 0}
    monkeypatch.setattr(app, "_estimate_pos_ms", lambda: pos["ms"])
    app._np_size = (540, 720)
    app._np_photo = _Cap(540, 720)

    def at(ms, settle=True):
        pos["ms"] = ms
        app._update_sheet(force=True)
        app._np_render()
        if settle:
            app._ly["t0"] -= 10
            app._ly["born"] -= 10
            app._np_hdr_prev = None
            app._np_cov["t0"] = -10
            app._fluid._from = None
        return app._np_render()
    return at


def _row_alpha(app, i, t_ms):
    ly = app._ly
    m = app._np_kara_mask(ly, i, 0.0, 1.0, 1.0, t_ms)
    it = ly["items"][i]
    return m, it


def test_karaoke_brightens_sung_words_only(app, sheet):
    sheet(5000 + 200 + 100)                            # halfway through "it"
    ly = app._ly
    i = ly["fto"]
    assert app._np_lyric_source()[1][i] == "sing it word by word now"
    m, it = _row_alpha(app, i, 5000 + 200 + 100)
    plain = app._np_line_mask(ly, i, 0.0, 1.0)
    x_sing = it["m"] + 5                                # inside "sing"
    x_now = it["m"] + int(it["w"]) - 6                  # inside "now"
    col = lambda im, x: max(im.getpixel((x, y)) for y in range(im.size[1]))
    assert col(m, x_sing) >= col(plain, x_sing) - 2     # sung: full
    assert col(m, x_now) <= col(plain, x_now) * 0.6     # unsung: dimmed


def test_line_without_syllables_highlights_whole(app, sheet, monkeypatch):
    for e in main.state.synced:
        e.pop("syl", None)
    sheet(5300)
    assert app._np_kara_mask(app._ly, app._ly["fto"], 0.0, 1.0, 1.0, 5300) is None


def test_sheet_animates_while_sung_and_idles_between_lines(app, sheet):
    assert sheet(5000 + 300) is True                    # words being sung
    assert sheet(5000 + 5 * 200 + 1200) is False        # sung, next line not due


def test_karaoke_respects_the_lyric_offset(app, sheet, monkeypatch):
    monkeypatch.setattr(main, "_track_offset_ms", lambda uri=None: 3000)
    sheet(2300)                                          # sheet at 5300 ms
    assert app._np_lyric_source()[1][app._ly["fto"]] == "sing it word by word now"


def test_instrumental_break_shows_breathing_dots(app, sheet):
    busy = sheet(9000 + 200 + 250 + 3000)               # "third" ended at 9200; next at 20000
    ly = app._ly
    plan, _ = app._np_plan()
    assert plan[ly["fto"]]["kind"] == "gap" and ly["items"][ly["fto"]]["dots"]
    assert busy is True                                  # breathing on Smooth
    e = plan[ly["fto"]]
    s_mid, lv = app._np_dots_state(e, e["t0"] + (e["t1"] - e["t0"]) * 0.5, time.monotonic(), True, True)
    assert lv[0] == 1.0 and 0.3 < lv[1] < 1.0 and lv[2] == pytest.approx(0.3)
    s_end, _ = app._np_dots_state(e, e["t1"] - 5, time.monotonic(), True, True)
    assert s_end < 0.1 < s_mid                           # shrinks away before the next line


def test_intro_dots_fill_before_the_first_line(app, sheet):
    sheet(1000)
    assert app._ly["fto"] == 0
    plan, _ = app._np_plan()
    _s, lv = app._np_dots_state(plan[0], 1000, time.monotonic(), True, True)
    assert lv[0] == 1.0 and lv[1] == pytest.approx(0.3 + 0.7 * 0.5)


def test_fast_tier_dots_do_not_keep_the_clock_busy(app, sheet, monkeypatch):
    monkeypatch.setattr(main, "RENDER_QUALITY", "low")
    assert sheet(9000 + 200 + 250 + 3000) is False
    sig1 = app._np_fx_sig(time.monotonic(), 2)
    assert sig1[0] is not None                           # the fill still steps


def test_cover_crossfades_on_change(app, sheet):
    from PIL import Image
    sheet(3000)
    app._show_hero_image(Image.new("RGB", (100, 100), (200, 30, 30)))
    assert app._np_cov["prev"] is not None
    assert app._np_render() is True                      # crossfading
    app._np_cov["t0"] -= 1
    app._np_render()
    assert app._np_cov["prev"] is None


def test_beat_swell_only_with_data_toggle_and_smooth(app, sheet, monkeypatch):
    monkeypatch.setattr(fx, "_beat_react", True)
    sheet(3000)
    assert app._np_beat_level(0) == (None, 0)            # no beat data
    monkeypatch.setattr(main.state, "beats", [1000, 2950, 3500], raising=False)
    j, lift = app._np_beat_level(0)
    assert j == 1 and lift > 0
    assert app._np_beat_level(2)[1] == 0                 # Fast tier: never
    monkeypatch.setattr(fx, "_beat_react", False)
    assert app._np_beat_level(0)[1] == 0
    monkeypatch.setattr(fx, "_beat_react", True)
    assert app._np_ms_to_next_beat(0) == 501
    base = app._np_fluid_cache[1]
    lifted = app._np_beat_apply(base.copy(), 5)
    assert lifted.getpixel((10, 300))[0] >= base.getpixel((10, 300))[0]


def test_fullscreen_round_trip(app):
    app._root.geometry("520x700+60+60")
    app._root.update()
    geo = app._root.geometry()
    app._show("SETTINGS")
    app._np_fullscreen_toggle()
    app._root.update()
    assert app._np_fs and app._cur_page == "NOW PLAYING"
    assert app._root.attributes("-fullscreen")
    assert not app._nav.master.winfo_manager()           # page switcher hidden
    assert app._np_fs_scale() > 1.4
    assert app._np_lyric_px() > int(app._px(app.SHEET_LYRIC_PT) * 1.4)
    # Controls fade out after the pointer rests, and come back on motion.
    app._np_chrome_t -= 10
    now = time.monotonic()
    app._np_chrome = [0.2, now - 1]
    assert app._np_chrome_level(now) == 0.0 and app._np_cursor_hidden
    app._np_fs_motion(5, 5)
    assert app._np_chrome_target(time.monotonic()) == 1.0 and not app._np_cursor_hidden
    # Esc leaves (the Escape binding tries fullscreen first).
    assert app._np_fullscreen_exit() is True
    app._root.update()
    assert not app._root.attributes("-fullscreen")
    assert app._nav.master.winfo_manager() == "pack"
    assert app._root.geometry().split("+")[0] == geo.split("+")[0]
    assert app._np_fullscreen_exit() is False


def test_f11_and_escape_are_bound(app):
    assert app._root.bind("<F11>") and app._root.bind("<Escape>")


def test_lyric_font_choice_relayouts(app, sheet, monkeypatch):
    monkeypatch.setattr(main, "_cfg_set", lambda *a, **k: None)
    fams = fx.families()
    if len(fams) < 2:
        pytest.skip("no alternative font installed")
    sheet(5300)
    k0 = app._ly["key"]
    app._np_set_lyric_font(fams[1])
    try:
        assert app._np_lyric_text.family == fams[1]
        app._np_render()
        assert app._ly["key"] != k0
    finally:
        app._np_set_lyric_font(DEFAULT_FAMILY)


def test_footer_stepper_saves_per_song_and_resets(app, monkeypatch):
    monkeypatch.setattr(main, "_track_offset_ms", _REAL_OFFSET)
    st = main.state
    monkeypatch.setattr(st, "track_uri", "spotify:track:npv21offs", raising=False)
    monkeypatch.setattr(main, "LYRIC_DELAY_MS", 200)
    main._invalidate_offset_cache()
    assert not app._np_offset_is_song()
    app._np_nudge_delay(100)
    assert app._np_offset_is_song() and main._track_offset_ms() == 300
    assert main.LYRIC_DELAY_MS == 200                    # global untouched
    key = app._np_footer_key(540, 0, 0)
    assert 300 in key and True in key
    app._np_nudge_delay(100, glob=True)                  # Shift-click: global
    assert main.LYRIC_DELAY_MS == 300 and main._track_offset_ms() == 300
    app._np_reset_song_offset()
    assert not app._np_offset_is_song() and main._track_offset_ms() == 300
    main.LYRIC_DELAY_MS = 200
    app._update_delay_label()
    assert main._track_offset_ms() == 200


def test_appearance_rows_are_in_settings(app, monkeypatch):
    monkeypatch.setattr(main, "_cfg_set", lambda *a, **k: None)
    monkeypatch.setattr(main, "_cfg_set_soon", lambda *a, **k: None)
    app._build_deferred_pages()
    app._show("SETTINGS")
    for _ in range(3):
        app._root.update()
    texts = {app.set_cv.itemcget(i, "text") for i in app.set_cv.find_all()
             if app.set_cv.type(i) == "text"}
    assert {"Lyric font", "Lyric size", "React to the beat"} <= texts
