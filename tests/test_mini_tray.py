"""Mini player pill and tray hover card: pure maths plus GUI smoke tests."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import main
import statusify_ui_mini as mini
from statusify_history import HistoryStore
from statusify_ui_mini import FadeState, parse_position, snap_position, tray_tooltip

tk = main.tk
Image = main.Image


# ── Snapping ─────────────────────────────────────────────────────
AREA = (0, 0, 1920, 1040)       # work area (taskbar at the bottom)


def test_snap_to_top_left_corner_within_threshold():
    assert snap_position(20, 30, 440, 68, AREA) == (12, 12)


def test_snap_to_bottom_right_corner():
    x, y = snap_position(1920 - 440 - 20, 1040 - 68 - 5, 440, 68, AREA)
    assert (x, y) == (1920 - 12 - 440, 1040 - 12 - 68)


def test_no_snap_in_open_space():
    assert snap_position(500, 400, 440, 68, AREA) == (500, 400)


def test_snap_to_horizontal_centre():
    cx = (1920 - 440) // 2
    assert snap_position(cx + 15, 400, 440, 68, AREA) == (cx, 400)


def test_dragged_past_an_edge_comes_back_on_screen():
    assert snap_position(-300, -50, 440, 68, AREA) == (12, 12)
    assert snap_position(1900, 1100, 440, 68, AREA) == (1920 - 12 - 440, 1040 - 12 - 68)


def test_snap_uses_the_monitor_it_is_on():
    second = (1920, 0, 3840, 1080)       # monitor to the right
    assert snap_position(1930, 500, 440, 68, second) == (1932, 500)


def test_threshold_edge_is_inclusive_and_bounded():
    assert snap_position(12 + 24, 500, 440, 68, AREA)[0] == 12
    assert snap_position(12 + 25, 500, 440, 68, AREA)[0] == 37


def test_parse_position():
    assert parse_position("560x76+100+-5") == (100, -5)
    assert parse_position("+3+4") == (3, 4)
    assert parse_position("") is None
    assert parse_position("garbage") is None


# ── Fade state machine ──────────────────────────────────────────
def test_fade_holds_then_dims_after_delay():
    f = FadeState(idle=0.35, delay=1.5, fade_out=0.4, fade_in=0.15)
    f.enter(0.0)
    assert f.alpha(1.0) == pytest.approx(1.0)
    f.leave(10.0)
    assert f.alpha(10.0) == pytest.approx(1.0)
    assert f.alpha(11.4) == pytest.approx(1.0)          # still inside the delay
    assert f.animating(11.4)
    mid = f.alpha(11.7)
    assert 0.35 < mid < 1.0                             # fading
    assert f.alpha(12.0) == pytest.approx(0.35)
    assert not f.animating(12.0)


def test_fade_reenter_mid_fade_rises_from_current_alpha():
    f = FadeState(idle=0.35, delay=1.5, fade_out=0.4, fade_in=0.15)
    f.leave(0.0)
    at = f.alpha(1.7)
    assert 0.35 < at < 1.0
    f.enter(1.7)
    assert f.alpha(1.7) == pytest.approx(at)            # no jump
    assert f.alpha(1.9) == pytest.approx(1.0)
    assert f.hovered


def test_fade_reenter_during_delay_never_dims():
    f = FadeState()
    f.leave(0.0)
    f.enter(1.0)
    for t in (1.0, 1.6, 2.0, 5.0):
        assert f.alpha(t) == pytest.approx(1.0)


# ── Tray tooltip ────────────────────────────────────────────────
def test_tooltip_title_artist_and_lyric():
    assert tray_tooltip("Song", "Band", "hello there") == "Song — Band\n♪ hello there"


def test_tooltip_without_track_or_lyric():
    assert tray_tooltip("", "", "").startswith("Statusify")
    assert tray_tooltip("Song", "Band", "") == "Song — Band"
    assert tray_tooltip("Song", "Band", "♪") == "Song — Band"
    assert tray_tooltip("Song", "", "x") == "Song\n♪ x"


def test_tooltip_paused():
    assert tray_tooltip("Song", "Band", "line", playing=False) == "Song — Band\nPaused"


def test_tooltip_truncates_to_127_utf16_units():
    t = tray_tooltip("T" * 50, "A" * 30, "lyric " * 40)
    assert len(t.encode("utf-16-le")) // 2 <= 127
    assert t.startswith("T" * 50 + " — " + "A" * 30 + "\n♪ lyric")
    assert t.endswith("…")


def test_tooltip_long_title_keeps_room_or_drops_lyric():
    t = tray_tooltip("T" * 200, "A" * 200, "some lyric")
    assert len(t.encode("utf-16-le")) // 2 <= 127
    assert t.split("\n")[0].endswith("…")


def test_tooltip_counts_astral_chars_as_two_units():
    t = tray_tooltip("😀" * 80, "x", "😀" * 80)
    assert len(t.encode("utf-16-le")) // 2 <= 127
    t.encode("utf-16-le")                               # no lone surrogates


def test_tooltip_flattens_newlines():
    assert tray_tooltip("A\nB", "C", "d\ne") == "A B — C\n♪ d e"


# ── GUI smoke ────────────────────────────────────────────────────
@pytest.fixture
def app(tmp_path, monkeypatch):
    st = HistoryStore(str(tmp_path / "h.db"))
    monkeypatch.setattr(main, "_HISTORY_STORE", st)
    monkeypatch.setattr(main, "history", [])
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
    for fn in (a._close_mini, a._cancel_all_timers, a._tray_stop):
        try:
            fn()
        except Exception:
            pass
    a._alive = False
    a._root.destroy()
    st.close()
    del a
    import gc
    gc.collect()


@pytest.fixture
def playing(monkeypatch):
    s = main.state
    for k, v in (("title", "Song Title"), ("artist", "The Artist"), ("is_playing", True),
                 ("lyrics_mode", "synced"), ("duration_ms", 200_000),
                 ("synced", [{"startMs": 0, "words": "first line"},
                             {"startMs": 10_000, "words": "second line"}]),
                 ("plain", [])):
        monkeypatch.setattr(s, k, v, raising=False)
    # Paused, so position_ms is exactly _position_ms (no wall-clock drift).
    monkeypatch.setattr(s, "is_playing", False)
    monkeypatch.setattr(s, "_position_ms", 1000, raising=False)
    monkeypatch.setattr(s, "_pos_mono", None, raising=False)
    yield s


def _pump(app, n=10):
    for _ in range(n):
        app._root.update()


def test_open_close_and_geometry_saved(app, playing):
    app._open_mini()
    _pump(app)
    assert app._mini is not None and app._mini.winfo_exists()
    L = app._mini_layout()
    fr = app._mini_frame
    assert fr.size == (L["W"], L["H"])
    # Rounded: corners are the colour key, the middle is painted.
    assert fr.getpixel((0, 0)) == mini.KEY_RGB
    assert fr.getpixel((L["W"] - 1, L["H"] - 1)) == mini.KEY_RGB
    assert fr.getpixel((L["W"] // 2, L["H"] // 2)) != mini.KEY_RGB
    app._mini.geometry("+123+87")
    _pump(app)
    app._close_mini()
    assert app._mini is None
    assert parse_position(main._cfg_get("window", "mini_geometry", "")) == (123, 87)
    app._toggle_mini()
    _pump(app)
    assert (app._mini.winfo_x(), app._mini.winfo_y()) == (123, 87)
    app._toggle_mini()
    assert app._mini is None


def test_render_with_and_without_art(app, playing):
    app._hero_src = None
    app._open_mini()
    _pump(app)
    no_art = app._mini_frame.copy()
    L = app._mini_layout()
    c = (L["pad"] + L["cover"] // 2, L["H"] // 2)
    app._hero_src = Image.new("RGB", (300, 300), (220, 20, 20))
    app._np_palette = [(220, 20, 20), (20, 20, 220)]
    app._refresh_mini()
    with_art = app._mini_frame
    r, g, b = with_art.getpixel(c)
    assert r > 180 and g < 60 and b < 60          # the cover is drawn
    assert no_art.getpixel(c) != with_art.getpixel(c)


def test_render_is_cached_when_nothing_changes(app, playing):
    app._open_mini()
    _pump(app)
    first = app._mini_frame
    app._refresh_mini()
    app._refresh_mini()
    assert app._mini_frame is first


def test_lyric_change_animates_and_settles(app, playing, monkeypatch):
    app._open_mini()
    _pump(app)
    assert app._mini_lyric_cur == "first line"
    playing.position_ms = 12_000
    app._refresh_mini()
    assert app._mini_lyric_cur == "second line"
    assert app._mini_ly_anim is not None
    deadline = time.monotonic() + 2
    while app._mini_ly_anim is not None and time.monotonic() < deadline:
        _pump(app, 2)
        time.sleep(0.02)
    assert app._mini_ly_anim is None


def test_play_button_sends_toggle_and_hover_reveals_prev_next(app, playing, monkeypatch):
    sent = []
    monkeypatch.setattr(main, "player_command", lambda a, **k: sent.append(a) or True)
    app._open_mini()
    _pump(app)
    L = app._mini_layout()
    cx, cy = app._mini_button_centres(L, 0.0)["toggle"]
    assert app._mini_hit(cx, cy) == "toggle"
    assert app._mini_hit(L["prev_x"], cy) is None       # hidden until hover
    app._mini_command(app._mini_hit(cx, cy))
    assert sent == ["toggle"]
    app._mini_enter()
    deadline = time.monotonic() + 2
    while not app._mini_hover.done(time.monotonic()) and time.monotonic() < deadline:
        _pump(app, 2)
        time.sleep(0.02)
    _pump(app)
    assert app._mini_hit(L["prev_x"], cy) == "prev"
    assert app._mini_hit(L["next_x"], cy) == "next"
    app._mini_command("next")
    app._mini_command("prev")
    assert sent == ["toggle", "next", "prev"]


def test_fade_drives_window_alpha(app, playing, monkeypatch):
    app._open_mini()
    app._mini_fade = FadeState(delay=0.05, fade_out=0.1)
    app._mini_leave()
    deadline = time.monotonic() + 2
    while app._mini_fade.animating(time.monotonic()) and time.monotonic() < deadline:
        _pump(app, 2)
        time.sleep(0.02)
    _pump(app, 4)
    assert float(app._mini.attributes("-alpha")) == pytest.approx(0.35, abs=0.02)
    app._mini_enter()
    deadline = time.monotonic() + 2
    while app._mini_fade.animating(time.monotonic()) and time.monotonic() < deadline:
        _pump(app, 2)
        time.sleep(0.02)
    _pump(app, 4)
    assert float(app._mini.attributes("-alpha")) == pytest.approx(1.0, abs=0.02)


def test_tray_tooltip_follows_track(app, playing):
    if app._tray is None:
        pytest.skip("no tray backend")
    app._tray_update_tip(force=True)
    assert app._tray.title.startswith("Song Title — The Artist")
    assert len(app._tray.title) <= 127


def test_tray_middle_click_toggles_playback(app, monkeypatch):
    if app._tray is None or not app._tray_can_hook_middle():
        pytest.skip("no win32 tray backend")
    sent = []
    monkeypatch.setattr(main, "player_command", lambda a, **k: sent.append(a) or True)
    from pystray._util import win32 as pw
    app._tray._message_handlers[pw.WM_NOTIFY](0, 0x0208)   # WM_MBUTTONUP
    _pump(app)
    assert sent == ["toggle"]


def test_tray_menu_has_playback_items(app):
    if app._tray is None:
        pytest.skip("no tray backend")
    labels = [i.text for i in app._tray.menu.items if i.text]
    for want in ("Play/Pause", "Next", "Previous", "Show mini player",
                 "Show Statusify", "Hide to tray", "Always on top",
                 "Toggle Discord RPC", "Reconnect RPC", "Quit"):
        assert want in labels
    app._open_mini()
    assert "Hide mini player" in [i.text for i in app._tray.menu.items if i.text]
