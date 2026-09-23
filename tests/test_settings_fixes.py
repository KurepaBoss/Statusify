"""Settings page: clicks answer at once, text fields always take focus, and
the sleep timer takes a custom duration.

Drives the real window through Tk events, like test_gui_smoke."""
import datetime
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import main
import statusify_sleep as sleep
from statusify_history import HistoryStore
from statusify_ui_settings import _Text

tk = main.tk


@pytest.fixture
def app(tmp_path, monkeypatch):
    st = HistoryStore(str(tmp_path / "h.db"))
    st.record_play("spotify:track:t0", "Artist", "Song",
                   played_at=datetime.datetime.now().replace(microsecond=0).isoformat())
    monkeypatch.setattr(main, "_HISTORY_STORE", st)
    monkeypatch.setattr(main, "history", st.recent())
    monkeypatch.setattr(main, "_register_hotkeys", lambda *a, **k: None)
    monkeypatch.setattr(main, "_set_startup_enabled", lambda *a, **k: None)
    monkeypatch.setattr(main, "ANIMATIONS_ENABLED", True)
    for attempt in range(3):
        try:
            a = main.App()
            break
        except tk.TclError as e:
            if "installed properly" not in str(e) and "tcl_findLibrary" not in str(e) or attempt == 2:
                raise
            time.sleep(0.2)
    a._root.geometry("620x760")
    a._build_deferred_pages()
    a._show("SETTINGS")
    _pump(a, 20)
    yield a
    for fn in (a._cancel_all_timers, a._tray_stop):
        try:
            fn()
        except Exception:
            pass
    try:
        a._sleep_timer_set(None)
    except Exception:
        pass
    a._alive = False
    a._root.destroy()
    st.close()
    del a
    import gc
    gc.collect()


def _pump(app, n=5):
    for _ in range(n):
        app._root.update()


def _settle(app, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app._root.update()
        time.sleep(0.003)


def _text_item(cv, text):
    for i in cv.find_all():
        if cv.type(i) == "text" and cv.itemcget(i, "text") == text:
            return i
    raise KeyError(text)


def _switch(app, title):
    cv = app.set_cv
    for s in app._switches:
        x0, y0, x1, y1 = cv.bbox(s.item)
        for i in cv.find_overlapping(0, y0 - 20, x0 - 20, y1 + 20):
            if cv.type(i) == "text" and cv.itemcget(i, "text") == title:
                return s
    raise KeyError(title)


def _click_canvas(app, cx, cy):
    """A real click at canvas coordinates: motion (picks the item), press, release."""
    cv = app.set_cv
    vx, vy = int(cx - cv.canvasx(0)), int(cy - cv.canvasy(0))
    cv.event_generate("<Motion>", x=vx, y=vy)
    cv.event_generate("<ButtonPress-1>", x=vx, y=vy)
    cv.event_generate("<ButtonRelease-1>", x=vx, y=vy)


def _show(app, item):
    cv = app.set_cv
    app._set_scroll_to(max(0, cv.bbox(item)[1] - 150), animate=False)
    _pump(app, 3)


def _activate(app):
    """Give the app the OS focus without choosing a widget: Tk puts it on the
    window's last focus, i.e. wherever the app itself sent it."""
    app._root.tk.call("focus", "-force", app._root.focus_lastfor())
    _pump(app, 2)


def _entry_wd(app, var):
    for wd in app.set_cv.page_widgets:
        if isinstance(wd.w, tk.Entry) and str(wd.w.cget("textvariable")) == str(var):
            return wd
    raise KeyError(var)


# ── The glide always ends, so the inputs come back ──────────────────
@pytest.mark.parametrize("dy", [300, -300, 7, -7, 1, -1, 2, -2])
def test_scroll_glide_arrives_and_shows_the_inputs(app, dy):
    """The glide set fractional positions that Tk rounded back to the
    current pixel, so it stalled 1-2 px short forever and every Entry stayed
    hidden behind its drawn stand-in (the unclickable-box report)."""
    cv = app.set_cv
    start = 400
    app._set_scroll_to(start, animate=False); _pump(app, 2)
    app._set_scroll_by(dy)
    _settle(app, 1.0)
    assert app._set_gliding is False
    assert cv.canvasy(0) == start + dy == app._set_target
    assert {cv.itemcget(wd.win, "state") for wd in cv.page_widgets} <= {"normal", ""}
    assert app._timers.get("setscroll") is None or not app._set_gliding


def test_glide_against_the_page_edge_ends(app):
    cv = app.set_cv
    app._set_scroll_to(0, animate=False); _pump(app, 2)
    app._set_scroll_by(-500)                 # already at the top
    _settle(app, 0.3)
    assert app._set_gliding is False and cv.canvasy(0) == 0
    app._set_scroll_by(10 ** 6)              # far past the bottom
    _settle(app, 2.0)
    assert app._set_gliding is False
    assert {cv.itemcget(wd.win, "state") for wd in cv.page_widgets} <= {"normal", ""}


# ── Clicking a text field focuses it ────────────────────────────────
def test_clicking_an_entry_focuses_it_and_typing_works(app):
    wd = _entry_wd(app, app._instr_var)
    _show(app, wd.win)
    e = wd.w
    e.event_generate("<ButtonPress-1>", x=10, y=8)
    e.event_generate("<ButtonRelease-1>", x=10, y=8)
    _pump(app, 2)
    assert app._root.focus_lastfor() is e
    # Key events only reach a window whose app holds the OS focus, which the
    # test runner may own. Activate the app onto the widget the click chose.
    _activate(app)
    assert app._root.focus_get() is e
    before = e.get()
    e.icursor("end")
    e.event_generate("<KeyPress>", keysym="x")
    _pump(app, 2)
    assert e.get() == before + "x"


def test_click_on_a_stand_in_mid_glide_reveals_and_focuses_the_field(app):
    cv = app.set_cv
    wd = _entry_wd(app, app._instr_var)
    _show(app, wd.win)
    app._set_scroll_by(200)                  # glide starts: inputs hidden
    assert app._set_gliding and cv.itemcget(wd.win, "state") == "hidden"
    x0, y0, x1, y1 = cv.bbox(wd.tag)
    # the item under the pointer is the stand-in, not the (hidden) Entry
    _click_canvas(app, (x0 + x1) // 2, (y0 + y1) // 2)
    assert app._set_gliding is False                       # stopped where it was
    assert {cv.itemcget(w.win, "state") for w in cv.page_widgets} <= {"normal", ""}
    assert app._root.focus_lastfor() is wd.w
    _settle(app, 0.3)
    assert cv.itemcget(wd.win, "state") != "hidden"        # and stays shown


def test_a_rerender_keeps_the_focused_field_focused(app):
    wd = _entry_wd(app, app._instr_var)
    _show(app, wd.win)
    wd.w.focus_force(); _pump(app, 2)
    wd.w.icursor(2)
    app._set_render(); _pump(app, 2)
    assert app._root.focus_lastfor() is wd.w
    assert wd.w.index("insert") == 2
    assert app.set_cv.itemcget(wd.win, "state") != "hidden"


def test_a_render_mid_glide_ends_it_with_the_inputs_shown(app):
    cv = app.set_cv
    app._set_scroll_to(0, animate=False); _pump(app, 2)
    app._set_scroll_by(300)
    assert app._set_gliding
    app._set_render()
    assert app._set_gliding is False
    assert {cv.itemcget(w.win, "state") for w in cv.page_widgets} <= {"normal", ""}


def test_stand_in_tags_are_per_render(app):
    cv = app.set_cv
    old = [wd.tag for wd in cv.page_widgets]
    app._set_render(); _pump(app, 1)
    assert not set(old) & {wd.tag for wd in cv.page_widgets}
    for t in old:
        assert not cv.tag_bind(t, "<Button-1>")


# ── Clicks answer within a frame ────────────────────────────────────
def test_switch_moves_in_the_click_frame(app, monkeypatch):
    """The first animation frame used to redraw the switch unchanged, so the
    first visible change came a frame (~17 ms) later."""
    calls = []
    monkeypatch.setattr(main, "_cfg_set", lambda *a, **k: calls.append(a))
    s = _switch(app, "Remember history")
    _show(app, s.item)
    start = s.t
    x0, y0, x1, y1 = app.set_cv.bbox(s.item)
    painted = []
    real = app._set_flush
    monkeypatch.setattr(app, "_set_flush", lambda: (real(), painted.append(s.t)))
    _click_canvas(app, (x0 + x1) // 2, (y0 + y1) // 2)
    assert painted and painted[0] != start                 # on screen before toggle()
    assert s.t != start
    assert calls == []                                     # no synchronous config write
    _settle(app, 0.3)
    assert s.t == (1.0 if s.get() else 0.0)


def test_switch_click_is_fast(app):
    s = _switch(app, "Show when paused")
    _show(app, s.item)
    x0, y0, x1, y1 = app.set_cv.bbox(s.item)
    times = []
    for _ in range(6):
        t0 = time.perf_counter()
        _click_canvas(app, (x0 + x1) // 2, (y0 + y1) // 2)
        app._root.update_idletasks()
        times.append((time.perf_counter() - t0) * 1000)
        _settle(app, 0.2)
    assert statistics.median(times) < 16, times


def test_album_tint_switch_defers_and_coalesces_the_repaint(app, monkeypatch):
    n = []
    monkeypatch.setattr(app, "_repaint_everything", lambda: n.append(1))
    s = _switch(app, "Colours from the album art")
    _show(app, s.item)
    x0, y0, x1, y1 = app.set_cv.bbox(s.item)
    before = main.ALBUM_TINT
    _click_canvas(app, (x0 + x1) // 2, (y0 + y1) // 2)
    assert n == [] and main.ALBUM_TINT != before
    _click_canvas(app, (x0 + x1) // 2, (y0 + y1) // 2)
    _settle(app, 0.4)
    assert n == [1] and main.ALBUM_TINT == before


def test_theme_segment_moves_first_and_recolours_after(app, monkeypatch):
    seg = app._theme_seg
    _show(app, seg.item)
    x0, y0, x1, y1 = app.set_cv.bbox(seg.item)
    segw = seg.dims()[0]
    other = "light" if main._DARK_MODE else "dark"
    i = [k for _, k in seg.options].index(other)
    dark_before = main._DARK_MODE
    _click_canvas(app, x0 + app._ss(3) + i * segw + segw // 2, (y0 + y1) // 2)
    assert seg.current() == other                          # shown at once
    assert main._DARK_MODE == dark_before                  # recolour not yet run
    _settle(app, 0.4)
    assert main._DARK_MODE == (other == "dark")
    app._set_theme("dark"); _pump(app, 3)


def test_value_slot_redraws_in_place_when_it_fits(app, monkeypatch):
    n = []
    monkeypatch.setattr(app, "_set_relayout_soon", lambda: n.append(1))
    slot = app.lbl_sleep
    slot.config(text="9:59")                 # fits the 72 px slot
    assert n == [] and app.set_cv.itemcget(slot.item, "text") == "9:59"
    slot.config(text="a much longer value than the slot")
    assert n == [1]


def test_value_slot_honours_its_font(app, monkeypatch):
    monkeypatch.setattr(app, "_set_relayout_soon", lambda: None)
    slot = app.lbl_sleep
    slot.font = ("Georgia", 10, "bold")
    slot.config(text="Georgia")
    assert "Georgia" in app.set_cv.itemcget(slot.item, "font")
    app._set_render(); _pump(app, 1)
    assert "Georgia" in app.set_cv.itemcget(slot.item, "font")
    slot.config(font=("Consolas", 10))
    assert "Consolas" in app.set_cv.itemcget(slot.item, "font")
    slot.font = None


# ── Custom sleep-timer duration ─────────────────────────────────────
@pytest.mark.parametrize("text,want", [
    ("45", 45), (" 1 ", 1), ("600", 600), ("90m", 90), ("20 min", 20),
    ("0", None), ("601", None), ("-5", None), ("abc", None), ("", None), ("4.5", None)])
def test_parse_minutes(text, want):
    assert sleep.parse_minutes(text) == want


def _choose_sleep(app, key):
    seg = app._sleep_seg
    _show(app, seg.item)
    x0, y0, x1, y1 = app.set_cv.bbox(seg.item)
    segw = seg.dims()[0]
    i = [k for _, k in seg.options].index(key)
    _click_canvas(app, x0 + app._ss(3) + i * segw + segw // 2, (y0 + y1) // 2)
    _pump(app, 2)


def test_custom_sleep_time_starts_the_countdown(app, monkeypatch):
    sent = []
    monkeypatch.setattr(main, "player_command", lambda a: sent.append(a) or True)
    _choose_sleep(app, "custom")
    assert app._sleep_seg.get() == "custom"
    ent = app._sleep_custom_ent
    assert any(wd.w is ent for wd in app.set_cv.page_widgets)       # the row is shown
    assert app._root.focus_lastfor() is ent
    _activate(app)
    ent.delete(0, "end"); ent.insert(0, "45")
    ent.event_generate("<Return>")
    _pump(app, 2)
    assert app._sleep_timer.value() == "45"
    assert app._sleep_seg.get() == "custom"
    assert app.lbl_sleep.cget("text") in ("45:00", "44:59")
    assert not any(wd.w is ent for wd in app.set_cv.page_widgets)   # folded away
    # the countdown keeps ticking
    app._sleep_timer.deadline -= 61
    app._sleep_timer_tick()
    assert app.lbl_sleep.cget("text") in ("43:59", "43:58")
    # and a preset still works afterwards
    _choose_sleep(app, "15")
    assert app._sleep_timer.value() == "15" and app._sleep_seg.get() == "15"


@pytest.mark.parametrize("bad", ["0", "601", "abc", ""])
def test_custom_sleep_time_rejects_out_of_range(app, bad):
    _choose_sleep(app, "custom")
    ent = app._sleep_custom_ent
    ent.delete(0, "end"); ent.insert(0, bad)
    app._sleep_custom_start()
    _pump(app, 2)
    assert app._sleep_timer.value() == "off"
    assert app._sleep_custom_msg.cget("fg") == main.DANGER
    assert "1–600" in app._sleep_custom_msg.cget("text")
    assert any(wd.w is ent for wd in app.set_cv.page_widgets)       # still open to fix it


def test_start_button_starts_the_timer(app):
    _choose_sleep(app, "custom")
    app._sleep_custom_var.set("7")
    cv = app.set_cv
    it = _text_item(cv, "Start")
    x0, y0, x1, y1 = cv.bbox(it)
    _click_canvas(app, (x0 + x1) // 2, (y0 + y1) // 2)
    _pump(app, 2)
    assert app._sleep_timer.value() == "7"


def test_custom_row_folds_away_when_the_timer_fires(app, monkeypatch):
    monkeypatch.setattr(main, "player_command", lambda a: True)
    app._sleep_timer_set(5)
    _choose_sleep(app, "custom")
    assert app._sleep_custom_open
    app._sleep_timer.deadline = app._sleep_timer.clock() - 1
    app._sleep_timer_tick()
    _pump(app, 2)
    assert app._sleep_custom_open is False
    assert app._sleep_seg.get() == "off" and app.lbl_sleep.cget("text") == "Off"


def test_sleep_control_fits_the_minimum_window_width(app):
    app._root.geometry("460x760")
    _pump(app, 10)
    _choose_sleep(app, "custom")
    cv = app.set_cv
    right = cv.winfo_width()
    x0, y0, x1, y1 = cv.bbox(app._sleep_seg.item)
    assert x1 <= right - app._ss(16)                          # inside the card
    for label, _ in app._sleep_seg.options:                    # every label fits its segment
        assert app._f(main.FS_SMALL, True).measure(label) < app._sleep_seg.dims()[0]
    msg = app._sleep_custom_msg
    assert cv.bbox(msg.item)[2] <= right
