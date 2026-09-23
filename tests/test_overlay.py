"""Desktop lyrics overlay (statusify_ui_overlay).

The pure helpers are tested directly. The GUI tests build the real App with
statusify_ui_overlay.NATIVE off, so the whole pipeline (scene building,
karaoke fill, slide, fade, chrome, settings rows) runs through Tk and PIL
but nothing is handed to UpdateLayeredWindow and no window appears."""
import os
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import main
import statusify_ui_overlay as ov
from statusify_lyrics import select_line

tk = main.tk


# ── Pure logic ───────────────────────────────────────────────────────
SYNCED = [
    {"startMs": 1000, "words": "one"},
    {"startMs": 2000, "words": "two"},
    {"startMs": 3000, "words": ""},
    {"startMs": 9000, "words": "four"},
]


def test_current_index_matches_the_sheet_rule():
    assert ov.current_index(SYNCED, 0) == -1
    assert ov.current_index(SYNCED, 999) == -1
    assert ov.current_index(SYNCED, 1000) == 0
    assert ov.current_index(SYNCED, 2500) == 1
    assert ov.current_index(SYNCED, 99999) == 3
    assert ov.current_index([], 5) == -1


def test_current_line_agrees_with_select_line():
    # select_line is what Discord gets; the overlay must show the same line.
    for pos in range(1000, 12000, 250):
        view = ov.pick_view(SYNCED, pos, 12000)
        cur, _ = select_line("synced", SYNCED, [], pos, 12000)
        if view["kind"] == "line":
            assert view["cur"] == cur


def test_pick_view_gaps_and_dots():
    v = ov.pick_view(SYNCED, 0, 12000)
    assert v["kind"] == "gap" and v["next"] == "one" and v["dots"] == 0
    v = ov.pick_view(SYNCED, 1500, 12000)
    assert v["kind"] == "line" and v["cur"] == "one" and v["next"] == "two"
    # Empty line = instrumental; dots light up across the 3 s -> 9 s gap.
    assert ov.pick_view(SYNCED, 3100, 12000)["dots"] == 0
    assert ov.pick_view(SYNCED, 5500, 12000)["dots"] == 1
    assert ov.pick_view(SYNCED, 8900, 12000)["dots"] == 2
    assert ov.pick_view([], 0) is None


def test_line_with_end_time_turns_into_a_gap():
    lines = [{"startMs": 0, "words": "sung", "endMs": 1000},
             {"startMs": 8000, "words": "later"}]
    assert ov.pick_view(lines, 500)["kind"] == "line"
    v = ov.pick_view(lines, 1500)
    assert v["kind"] == "gap" and v["start"] == 1000 and v["next"] == "later"
    # ...and the clock wakes exactly when the line ends.
    assert ov.ms_to_next_event(lines, 500, ov.pick_view(lines, 500)) == 502
    # A short pause after endMs is not a gap.
    short = [{"startMs": 0, "words": "a", "endMs": 1000}, {"startMs": 2000, "words": "b"}]
    assert ov.pick_view(short, 1500)["kind"] == "line"


def test_ms_to_next_event():
    v = ov.pick_view(SYNCED, 1500)
    assert ov.ms_to_next_event(SYNCED, 1500, v) == 502
    v = ov.pick_view(SYNCED, 3000, 12000)      # gap 3000..9000, next dot at 5000
    assert ov.ms_to_next_event(SYNCED, 3000, v) == 2002


def test_line_syl_and_sung_chars():
    line = {"startMs": 1000, "words": "hello world",
            "syl": [[1000, 1500, "hel"], [1500, 2000, "lo "], [2000, 3000, "world"]]}
    syl = ov.line_syl(line)
    assert "".join(t for _a, _b, t in syl) == "hello world"
    assert ov.sung_chars(syl, 900) == 0
    assert ov.sung_chars(syl, 1250) == pytest.approx(1.5)
    assert ov.sung_chars(syl, 1500) == 3
    assert ov.sung_chars(syl, 2500) == pytest.approx(8.5)
    assert ov.sung_chars(syl, 5000) == len("hello world")
    # No / junk timing -> plain line.
    assert ov.line_syl({"startMs": 0, "words": "x"}) is None
    assert ov.line_syl({"startMs": 0, "words": "x", "syl": [["a", 1, "x"]]}) is None
    assert ov.line_syl({"startMs": 0, "words": "x", "syl": [[0, 1, "  "]]}) is None
    # Relative timings are moved onto the track timeline.
    rel = ov.line_syl({"startMs": 60000, "words": "hi", "syl": [[0, 400, "hi"]]})
    assert rel == [(60000, 60400, "hi")]


def test_geometry_round_trip_keeps_negative_coordinates():
    g = ov.format_geometry(900, 160, -1500, 20)
    assert g == "900x160+-1500+20"          # never Tk's "from the right" '-X'
    assert ov.parse_geometry(g) == (900, 160, -1500, 20)
    assert ov.parse_geometry("") is None
    assert ov.parse_geometry("garbage") is None
    assert ov.parse_geometry("0x10+1+1") is None


def test_clamp_rect_keeps_the_strip_on_the_work_area():
    work = (0, 0, 1920, 1040)
    assert ov.clamp_rect(-50, 2000, 800, 150, work) == (0, 890)
    assert ov.clamp_rect(100, 100, 800, 150, work) == (100, 100)
    assert ov.clamp_rect(1800, 0, 800, 150, work) == (1120, 0)


def test_readable_accent_lifts_dark_colours():
    r, g, b = ov.readable_accent("#101060")
    assert 0.2126 * r + 0.7152 * g + 0.0722 * b >= 0.42 * 255
    assert ov.readable_accent("#1db954") == ov.readable_accent("#1db954")
    assert ov.readable_accent("nonsense") == (29, 185, 84)


# ── GUI (NATIVE off) ─────────────────────────────────────────────────
@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr(ov, "NATIVE", False)
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
    saved = {k: getattr(main.state, k) for k in
             ("synced", "lyrics_mode", "is_playing", "duration_ms", "track_uri")}
    yield a
    for k, v in saved.items():
        setattr(main.state, k, v)
    # The config is one in-process cache: leave no overlay prefs behind for
    # later tests (an enabled overlay would open a real window there).
    cfg = main._load_config()
    for sec, key in (("preferences", "overlay_enabled"), ("preferences", "overlay_size"),
                     ("preferences", "overlay_next_line"), ("preferences", "hotkey_overlay"),
                     ("window", "overlay_geometry"), ("window", "overlay_locked")):
        if cfg.has_section(sec):
            cfg.remove_option(sec, key)
    monkeypatch.setattr(ov, "_HOTKEY", None)
    for fn in (a._ov_close, a._cancel_all_timers, a._tray_stop):
        try:
            fn()
        except Exception:
            pass
    a._alive = False
    a._root.destroy()
    del a
    import gc
    gc.collect()


def _pump(app, secs=0.3):
    end = time.monotonic() + secs
    while time.monotonic() < end:
        app._root.update()
        time.sleep(0.005)


def _pump_until(app, cond, timeout=3.0):
    """Run the Tk loop until cond() holds (timings are the machine's)."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app._root.update()
        if cond():
            return True
        time.sleep(0.005)
    return cond()


def _song(start_ms):
    st = main.state
    t = start_ms
    syl = []
    for w in ("never", "gonna", "give"):
        syl.append([t, t + 400, w + " "])
        t += 400
    syl[-1][2] = "give"
    st.synced = [{"startMs": 0, "words": "intro line"},
                 {"startMs": start_ms, "words": "never gonna give", "syl": syl},
                 {"startMs": start_ms + 3000, "words": "plain next line"}]
    st.lyrics_mode = "synced"
    st.duration_ms = 200_000
    st.track_uri = "spotify:track:overlaytest"
    st.is_playing = True


def _accent_pixels(img, accent):
    b = img.convert("RGB").tobytes()
    r0, g0, b0 = accent
    return sum(1 for i in range(0, len(b), 3)
               if abs(b[i] - r0) + abs(b[i + 1] - g0) + abs(b[i + 2] - b0) < 30)


def test_overlay_toggles_on_and_off_without_errors(app):
    assert app._ov_prefs()["enabled"] is False
    _song(10_000)
    main.state.position_ms = 10_100
    app._toggle_overlay()
    assert app._ov_top is not None and isinstance(app._ov_win, ov._NullWindow)
    assert main._cfg_get("preferences", "overlay_enabled", "") == "true"
    assert _pump_until(app, lambda: app._ov_alpha == 1.0)
    win = app._ov_win
    assert win.presents > 0 and win.shown
    assert win.clickthrough is True                   # locked = click-through
    assert "overlay" in app._timers

    app._toggle_overlay()
    assert app._ov_top is None and app._ov_win is None
    assert "overlay" not in app._timers
    assert main._cfg_get("preferences", "overlay_enabled", "") == "false"
    _pump(app, 0.1)
    # And back on again, through the hotkey's event.
    main.event_queue.put(("overlay_toggle",))
    assert _pump_until(app, lambda: app._ov_top is not None)


def test_karaoke_fills_words_as_they_are_sung(app):
    _song(10_000)
    main.state.position_ms = 10_050
    app._overlay_set_enabled(True)
    _pump(app, 0.1)
    app._ov_step(time.monotonic())
    sc = app._ov_scene
    assert sc["karaoke"] and sc["view"]["idx"] == 1
    early = _accent_pixels(app._ov_scene_frame(10_050), sc["accent"])
    mid = _accent_pixels(app._ov_scene_frame(10_600), sc["accent"])
    done = _accent_pixels(app._ov_scene_frame(11_300), sc["accent"])
    assert early < mid < done
    # A frame is only produced when the fill moved a pixel.
    app._ov_fill_key = None
    assert app._ov_karaoke_band(10_600) is not None
    assert app._ov_karaoke_band(10_600) is None


def test_line_change_slides_and_pause_fades_out(app, monkeypatch):
    _song(10_000)
    main.state.position_ms = 10_000
    app._overlay_set_enabled(True)
    assert _pump_until(app, lambda: app._ov_alpha == 1.0)
    main.state.position_ms = 13_100                  # next line
    app._ov_step(time.monotonic())
    assert app._ov_trans is not None                 # sliding
    assert app._ov_scene["view"]["cur"] == "plain next line"
    assert not app._ov_scene["karaoke"]
    assert _pump_until(app, lambda: app._ov_trans is None)
    # Paused: stays for PAUSE_HIDE_S, then fades out and hides.
    monkeypatch.setattr(ov, "PAUSE_HIDE_S", 0.2)
    main.state.is_playing = False
    assert _pump_until(app, lambda: app._ov_alpha == 0.0)
    assert app._ov_win.shown is False
    main.state.is_playing = True
    assert _pump_until(app, lambda: app._ov_alpha == 1.0)
    assert app._ov_win.shown is True


def test_nothing_playing_hides_but_unlocked_shows_a_placeholder(app):
    main.state.synced = []
    main.state.lyrics_mode = "none"
    app._overlay_set_enabled(True)
    _pump(app, 0.4)
    assert app._ov_alpha == 0.0
    app._overlay_set_locked(False)
    assert app._ov_win.clickthrough is False
    assert _pump_until(app, lambda: app._ov_alpha == 1.0)
    assert app._ov_scene["view"]["kind"] == "hint"
    assert set(app._ov_buttons) == {"close", "lock"}
    assert main._cfg_get("window", "overlay_locked", "") == "false"
    app._overlay_set_locked(True)
    assert app._ov_win.clickthrough is True


def test_drag_wheel_and_buttons_when_unlocked(app):
    app._overlay_set_enabled(True)
    app._overlay_set_locked(False)
    _pump(app, 0.3)
    W, H, x, y = app._ov_geo
    ev = lambda **k: SimpleNamespace(**{"x": 200, "y": H - 10, "x_root": x + 200,
                                        "y_root": y + H - 10, "delta": 0, **k})
    app._ov_on_press(ev())
    app._ov_on_motion(ev(x_root=x + 150, y_root=y + H - 60))
    app._ov_on_release(ev())
    W2, H2, x2, y2 = app._ov_geo
    assert (x2, y2) == ov.clamp_rect(x - 50, y - 50, W2, H2, app._ov_monitor(0, 0)[0])
    assert ov.parse_geometry(main._cfg_get("window", "overlay_geometry", "")) == app._ov_geo

    size = app._ov_size
    app._ov_on_wheel(ev(delta=120))
    assert app._ov_size == size + 2
    assert main._cfg_get("preferences", "overlay_size", "") == str(size + 2)
    assert app._ov_geo[1] > H2                       # bigger text, taller strip

    lx0, ly0, lx1, ly1 = app._ov_buttons["lock"]
    app._ov_on_press(ev(x=(lx0 + lx1) // 2, y=(ly0 + ly1) // 2))
    assert app._ov_locked is True
    # Locked: presses do nothing (and the OS would not deliver them anyway).
    assert app._ov_on_press(ev()) is None
    app._overlay_set_locked(False)
    _pump(app, 0.1)
    cx0, cy0, cx1, cy1 = app._ov_buttons["close"]
    app._ov_on_press(ev(x=(cx0 + cx1) // 2, y=(cy0 + cy1) // 2))
    assert app._ov_top is None


def test_prefs_persist_and_reload(app):
    app._overlay_set_enabled(True)
    app._overlay_set_size(40)
    app._overlay_set_show_next(False)
    geo = app._ov_geo
    main._cfg_flush()
    app._ov_close()
    app._ov_loaded = False                           # as on the next launch
    p = app._ov_prefs()
    assert p == {"enabled": True, "locked": True, "size": 40, "next": False}
    assert app._ov_anchor[0] == geo[2] + geo[0] // 2
    assert app._ov_anchor[1] in (geo[3], geo[3] + geo[1])
    app._overlay_init()                              # startup restores it
    assert app._ov_top is not None
    assert app._ov_geo == geo


def test_settings_rows_drive_the_overlay(app):
    app._build_deferred_pages()
    app._show("SETTINGS")
    _pump(app, 0.2)
    assert app.lbl_ov_size.cget("text") == str(ov.DEFAULT_SIZE)
    app._overlay_toggle_lock()                       # "Unlock to move" also turns it on
    assert app._ov_enabled and not app._ov_locked
    assert app.lbl_ov_lock.cget("text") == "Movable"
    app._overlay_set_size(ov.DEFAULT_SIZE + 4)
    assert app.lbl_ov_size.cget("text") == str(ov.DEFAULT_SIZE + 4)
    # The Global hotkeys row saves the combo and re-registers.
    calls = []
    main._register_hotkeys = lambda *a, **k: calls.append(1)   # fixture restores it
    app._ov_hotkey_var.set("ctrl+alt+l")
    app._ov_hotkey_save()                            # what <Return> / focus-out run
    assert ov.hotkey_combo() == "ctrl+alt+l" and calls
    assert main._cfg_get("preferences", "hotkey_overlay", "") == "ctrl+alt+l"


def test_resizing_keeps_the_strip_in_place(app):
    """Text size changes must not walk the overlay around the screen."""
    app._overlay_set_enabled(True)
    work, _dpi = app._ov_monitor(0, 0, primary=True)
    for place in ("top", "bottom"):
        W, H, x, _y = app._ov_geo
        y = work[1] + 40 if place == "top" else work[3] - H - 40
        app._ov_anchor = ov.anchor_for(x, y, W, H, work)
        app._ov_relayout()
        W0, H0, x0, y0 = app._ov_geo
        for size in (48, 20, 60, 30):
            app._overlay_set_size(size)
        app._overlay_set_size(app._ov_size)   # no-op
        W1, H1, x1, y1 = app._ov_geo
        assert x1 + W1 // 2 == x0 + W0 // 2                # same centre
        if place == "top":
            assert y1 == y0                                  # top edge stays
        else:
            assert y1 + H1 == y0 + H0                        # bottom edge stays
