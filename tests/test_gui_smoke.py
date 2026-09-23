"""Builds the real window and every page, then drives the history and stats
paths through Tk. The rest of the suite covers logic only, so without this a
typo in UI code surfaces only when the user opens the page."""
import datetime
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import main
from statusify_history import HistoryStore

tk = main.tk


@pytest.fixture
def app(tmp_path, monkeypatch):
    st = HistoryStore(str(tmp_path / "h.db"))
    for i in range(5):
        pid = st.record_play(f"spotify:track:t{i}", f"Artist {i % 2}", f"Song {i}",
                             played_at=(datetime.datetime.now()
                                        - datetime.timedelta(days=i)).replace(microsecond=0).isoformat())
        st.set_listened(pid, 180_000)
    st.save_lyrics("spotify:track:t0", "synced",
                   [{"startMs": 0, "words": "needle in the lyrics"}], [], "Spicy")
    monkeypatch.setattr(main, "_HISTORY_STORE", st)
    monkeypatch.setattr(main, "SAVE_HISTORY", True)
    monkeypatch.setattr(main, "history", st.recent())
    # Global hotkeys would collide with a running Statusify; not under test.
    monkeypatch.setattr(main, "_register_hotkeys", lambda *a, **k: None)
    # Creating many Tk roots in one process occasionally fails to source
    # init.tcl on Windows ("Tcl wasn't installed properly"). Retry that
    # transient error; anything else must fail, never silently skip.
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
    for fn in (a._cancel_all_timers, a._tray_stop):
        try:
            fn()
        except Exception:
            pass
    a._alive = False
    a._root.destroy()
    st.close()
    # Reclaim the dead App (and its PhotoImages) here, on the Tk thread.
    # Left to the cyclic GC, it could be freed later on whatever thread
    # triggers a collection (a later test's asyncio or executor thread), and
    # Tcl aborts the process when an image is deleted from the wrong thread.
    del a
    import gc
    gc.collect()


def _pump(app, n=20):
    for _ in range(n):
        app._root.update()


def test_every_page_builds_and_shows(app):
    app._build_deferred_pages()
    for name in ("NOW PLAYING", "HISTORY", "SETTINGS"):
        app._show(name)
        _pump(app)
    assert len(app._hist_rows) == 5


def test_history_search_reaches_lyrics_in_the_database(app):
    app._build_deferred_pages()
    app._hist_search.set("needle")
    app._run_history_search()
    assert [e["title"] for _, e in app._hist_rows] == ["Song 0"]
    app._hist_search.set("")
    app._run_history_search()
    assert len(app._hist_rows) == 5


def test_long_term_stats_render(app):
    app._build_deferred_pages()
    app._refresh_stats(reschedule=False)
    app._refresh_long_stats(sync=True)
    assert "5 plays" in app.lbl_stats_all.cget("text")
    assert app._stats_data["all"]["plays"] == 5
    assert "15m" in app.lbl_stats_all.cget("text")
    assert app.lbl_stats_week.cget("text").startswith("Last 7 days")


def test_new_play_event_adds_a_row(app):
    app._build_deferred_pages()
    before = len(app._hist_rows)
    main.state.track_uri = "spotify:track:new"
    main.state.artist, main.state.title = "New", "Track"
    main._current_play.update(id=None, entry=None)
    main._save_history("none", [], [], "fallback")
    app._poll()
    _pump(app)
    assert len(app._hist_rows) == before + 1


def test_tab_switch_raises_without_relayout(app):
    """Pages are stacked and raised; switching must not re-pack them (that
    re-laid-out the whole tree and cost ~60 ms per click)."""
    app._build_deferred_pages()
    for name in ("HISTORY", "SETTINGS", "NOW PLAYING", "HISTORY"):
        app._show(name)
        _pump(app, 3)
        assert app._cur_page == name
    assert {p.winfo_manager() for p in app._pages.values()} == {"place"}


def test_native_frame_and_theme_round_trip(app):
    """The window keeps its OS frame, and a theme switch re-tints the title
    bar and scrollbars without raising."""
    assert not app._root.overrideredirect()
    app._build_deferred_pages()
    app._set_theme("light"); _pump(app, 5)
    app._set_theme("dark"); _pump(app, 5)
    st = main.ttk.Style(app._root)
    assert st.lookup(app.SCROLLBAR_STYLE, "troughcolor") == main.BG


def test_album_tint_recolours_window_and_reverts(app, monkeypatch):
    """A cover recolours every widget in place; no cover restores the theme."""
    monkeypatch.setattr(main, "ALBUM_TINT", True)
    app._build_deferred_pages()
    neutral_bg = main.BG
    app._apply_album_tint("#c83c28")
    _pump(app, 3)
    assert main.BG != neutral_bg
    assert app.np_cv.cget("bg") == main.BG                # widgets followed
    assert app._pages["SETTINGS"].cget("bg") == main.BG
    app._apply_album_tint(None)
    _pump(app, 3)
    assert main.BG == neutral_bg and app.np_cv.cget("bg") == neutral_bg


def test_sheet_shows_lines_around_the_playhead(app, monkeypatch):
    st = main.state
    monkeypatch.setattr(st, "lyrics_mode", "synced", raising=False)
    monkeypatch.setattr(st, "synced", [{"startMs": 0, "words": "one"},
                                       {"startMs": 5000, "words": "two"},
                                       {"startMs": 9000, "words": "three"}], raising=False)
    monkeypatch.setattr(app, "_estimate_pos_ms", lambda: 6000)
    monkeypatch.setattr(main, "_track_offset_ms", lambda uri=None: 0)
    app._update_sheet(force=True)
    assert (app.lbl_prev.cget("text"), app.lbl_lyric.cget("text"),
            app.lbl_next.cget("text")) == ("one", "two", "three")


def test_settings_page_is_drawn_not_widget_per_row(app):
    """Scrolling stays tear-free only while rows are canvas items; a native
    window per row is what made the old page tear."""
    app._build_deferred_pages()
    app._show("SETTINGS"); _pump(app, 5)
    kids = app.set_cv.winfo_children()
    assert len(kids) <= 8, kids                         # only the text inputs
    assert len(app.set_cv.find_all()) > 100
    app._set_scroll_to(400, animate=False); _pump(app, 2)
    assert app.set_cv.canvasy(0) == 400


def test_settings_switch_toggles_and_animates(app, monkeypatch):
    monkeypatch.setattr(main, "_cfg_set", lambda *a, **k: None)
    app._build_deferred_pages()
    sw = app._switches[0]                                  # LRCLIB fallback
    before = sw.get()
    sw.click()
    assert sw.get() != before
    sw.click()
    assert sw.get() == before


def test_lyric_sheet_glides_to_the_next_line(app, monkeypatch):
    st = main.state
    monkeypatch.setattr(st, "lyrics_mode", "synced", raising=False)
    monkeypatch.setattr(st, "synced", [{"startMs": i * 1000, "words": f"line {i}"} for i in range(6)],
                        raising=False)
    monkeypatch.setattr(main, "_track_offset_ms", lambda uri=None: 0)
    pos = {"ms": 1500}
    monkeypatch.setattr(app, "_estimate_pos_ms", lambda: pos["ms"])
    app._np_size = (480, 600)
    app._update_sheet(force=True)
    ly = app._np_lyric_model(480)
    assert ly["idx"] == 2                                  # +1: the intro dots line
    pos["ms"] = 2500
    app._update_sheet()
    ly = app._np_lyric_model(480)
    assert ly["idx"] == 3 and ly["from"] < ly["to"]        # gliding upwards
    assert app.lbl_lyric.cget("text") == "line 2"
    assert app._np_render() is True                        # busy while gliding


def test_transport_and_seek_go_to_the_bridge(app, monkeypatch):
    sent = []
    monkeypatch.setattr(main, "_send_bridge", lambda obj: sent.append(obj) or True)
    main.state.is_playing = True
    app._np_transport("toggle")
    assert sent[-1] == {"type": "player", "action": "toggle"}
    assert app._np_playing() is False                      # optimistic until confirmed
    app._np_transport("next")
    assert sent[-1]["action"] == "next"
    main.state.duration_ms = 200_000
    app._np_seek(61_000)
    assert sent[-1] == {"type": "seek", "position_ms": 61_000}
    assert main.state.position_ms == 61_000


def test_clicking_a_lyric_line_seeks_to_its_start(app, monkeypatch):
    sent = []
    monkeypatch.setattr(main, "_send_bridge", lambda obj: sent.append(obj) or True)
    st = main.state
    monkeypatch.setattr(st, "lyrics_mode", "synced", raising=False)
    monkeypatch.setattr(st, "synced", [{"startMs": i * 1000 + 500, "words": f"l{i}"} for i in range(5)],
                        raising=False)
    monkeypatch.setattr(main, "_track_offset_ms", lambda uri=None: 200)
    app._np_seek_line(3)                                   # sheet line 3 = synced[2]
    assert sent[-1]["position_ms"] == 2500 - 200 + 40


def test_seek_without_spotify_reports_instead_of_failing(app, monkeypatch):
    monkeypatch.setattr(main, "_send_bridge", lambda obj: False)
    assert app._np_seek(1000) is False
    assert "isn't connected" in app.lbl_err.cget("text")



def test_relayout_does_not_leave_stale_click_targets(app, monkeypatch):
    """A re-render moves rows; a plain row landing where a switch row was
    must not inherit that switch's click."""
    monkeypatch.setattr(main, "_cfg_set", lambda *a, **k: None)
    app._build_deferred_pages()
    app._show("SETTINGS"); _pump(app, 3)
    cv = app.set_cv
    before = (main.START_MINIMIZED, main.CLOSE_TO_TRAY, main.ALBUM_TINT, main.ANIMATIONS_ENABLED,
              main.SAVE_HISTORY, main.LRCLIB_ENABLED, main.SHOW_PAUSED_RPC)
    old_tags = {t for i in cv.find_all() for t in cv.gettags(i) if t.startswith("row")}
    app._set_render(); _pump(app, 2)
    new_tags = {t for i in cv.find_all() for t in cv.gettags(i) if t.startswith("row")}
    assert not (old_tags & new_tags)
    for t in old_tags:
        assert not cv.tag_bind(t, "<Button-1>")
    after = (main.START_MINIMIZED, main.CLOSE_TO_TRAY, main.ALBUM_TINT, main.ANIMATIONS_ENABLED,
             main.SAVE_HISTORY, main.LRCLIB_ENABLED, main.SHOW_PAUSED_RPC)
    assert before == after


# ── History page, cover mode, sliding tabs ───────────────────────

class _Ev:
    def __init__(self, x=0, y=0, x_root=0, y_root=0, state=0):
        self.x, self.y, self.x_root, self.y_root, self.state = x, y, x_root, y_root, state


def _history_with_plays(app, n=40):
    st = main._HISTORY_STORE
    base = datetime.datetime.now().replace(microsecond=0)
    for i in range(n):
        st.record_play(f"spotify:track:m{i}", "More", f"Extra {i}",
                       played_at=(base - datetime.timedelta(minutes=5 * i + 1)).isoformat())
    main.history = st.recent()
    app._build_deferred_pages()
    app._root.geometry("520x720")
    app._show("HISTORY")
    _pump(app, 10)
    app._run_history_search()
    _pump(app, 5)


def test_history_is_one_canvas_grouped_by_day(app):
    _history_with_plays(app, 10)
    assert not app.hist_cv.winfo_children()               # no widget per row
    assert len(app._hist_rows) == 15
    labels = [app.hist_cv.itemcget(i, "text") for i in app.hist_cv.find_all()
              if app.hist_cv.type(i) == "text"]
    assert "Today" in labels
    assert "Synced" in labels                              # Song 0 has synced lyrics
    assert "plays" in app._hist_caption_text


def test_history_hover_follows_the_pointer_through_a_scroll(app, monkeypatch):
    monkeypatch.setattr(main, "ANIMATIONS_ENABLED", False)
    _history_with_plays(app, 40)
    cv = app.hist_cv
    y = 150
    app._hist_motion(_Ev(x=200, y=y))
    first = app._hist_hover
    assert first is not None
    assert app._hist_rows[first][0]["y0"] <= cv.canvasy(y) < app._hist_rows[first][0]["y1"]
    # The list moves under a still pointer: the lit row follows.
    app._hist_scroll_to(400, animate=False)
    _pump(app, 2)
    now = app._hist_hover
    assert now is not None and now != first
    assert app._hist_rows[now][0]["y0"] <= cv.canvasy(y) < app._hist_rows[now][0]["y1"]
    assert app._hist_rows[first][0]["t"] == 0.0            # the old one faded out
    # Leaving the canvas clears it.
    cv.event_generate("<Leave>")
    _pump(app, 2)
    assert app._hist_hover is None


def test_history_hover_follows_a_glide(app, monkeypatch):
    monkeypatch.setattr(main, "ANIMATIONS_ENABLED", True)
    _history_with_plays(app, 40)
    cv = app.hist_cv
    app._hist_motion(_Ev(x=200, y=200))
    app._hist_scroll_by(500)
    t0 = time.monotonic()
    while app._hist_gliding and time.monotonic() - t0 < 3:
        _pump(app, 1)
    i = app._hist_hover
    assert i is not None and app._hist_rows[i][0]["y0"] <= cv.canvasy(200) < app._hist_rows[i][0]["y1"]


def test_clear_history_asks_first(app):
    _history_with_plays(app, 3)
    n = main._HISTORY_STORE.count()
    app._hist_ask_clear()
    _pump(app, 2)
    assert main._HISTORY_STORE.count() == n                # nothing deleted yet
    texts = [app.hist_cv.itemcget(i, "text") for i in app.hist_cv.find_all()
             if app.hist_cv.type(i) == "text"]
    assert any(t.startswith("Delete all") for t in texts)
    app._hist_ask_clear(False)
    assert main._HISTORY_STORE.count() == n
    app._clear_history()
    assert main._HISTORY_STORE.count() == 0 and app._hist_rows == []


def test_lyrics_sheet_slides_in_and_escape_closes_it(app, monkeypatch):
    monkeypatch.setattr(main, "ANIMATIONS_ENABLED", False)
    _history_with_plays(app, 3)
    entry = next(e for _, e in app._hist_rows if e.get("synced"))
    app._show_lyrics(entry)
    _pump(app, 3)
    assert app._hist_sheet.winfo_ismapped()
    assert app._hist_ly_items and "needle" in app._hist_ly_items[0][3]
    app._hist_ly_search.set("needle")
    assert app._hist_sh_cv.find_withtag("lyhit")
    assert app._close_lyrics_panel() is True
    _pump(app, 3)
    assert not app._hist_sheet.winfo_ismapped()
    assert app._close_lyrics_panel() is False


def test_stats_open_in_history_still_searches(app):
    app._build_deferred_pages()
    app._stats_open_in_history({"title": "Song 3", "artist": "Artist 1"})
    assert app._cur_page == "HISTORY" and app._hist_search.get() == "Song 3"


def _cover_on(app, monkeypatch):
    from PIL import Image
    monkeypatch.setattr(main, "ALBUM_TINT", True)
    monkeypatch.setattr(main, "_DARK_MODE", True)
    cover = Image.new("RGB", (64, 64), (30, 140, 200))
    app._bd_set_cover(cover, [(30, 140, 200), (200, 80, 40)])
    app._apply_album_tint("#1e8cc8")
    _pump(app, 3)


def test_cover_mode_puts_the_backdrop_under_every_page(app, monkeypatch):
    monkeypatch.setattr(main, "ANIMATIONS_ENABLED", False)
    app._build_deferred_pages()
    _cover_on(app, monkeypatch)
    assert main._cover_mode()
    assert main.TEXT == "#f5f5f7" and main.ACCENT_FG in ("#000000", "#ffffff")
    for name, cv in (("HISTORY", app.hist_cv), ("STATS", app.st_cv), ("SETTINGS", app.set_cv)):
        app._show(name)
        _pump(app, 5)
        items = cv.find_withtag("bd")
        assert items, name
        assert cv.find_all()[0] == items[0], name          # lowest item
        assert abs(app._bd_bright - 0.24) < 1e-6
    app._show("NOW PLAYING")
    _pump(app, 3)
    assert abs(app._bd_bright - 0.55) < 1e-6
    assert app._np_render() is not None
    # Hidden pages don't keep the frame (it would be redrawn behind the page).
    assert not app.hist_cv.find_withtag("bd")
    # No cover: back to the flat theme.
    app._bd_set_cover(None)
    app._apply_album_tint(None)
    app._show("SETTINGS")
    _pump(app, 3)
    assert not main._cover_mode() and not app.set_cv.find_withtag("bd")


def test_backdrop_stays_put_while_a_canvas_scrolls(app, monkeypatch):
    monkeypatch.setattr(main, "ANIMATIONS_ENABLED", False)
    app._build_deferred_pages()
    _cover_on(app, monkeypatch)
    app._show("SETTINGS")
    _pump(app, 5)
    cv = app.set_cv
    app._set_scroll_to(300, animate=False)
    x, y = cv.coords(cv.find_withtag("bd")[0])
    assert y == cv.canvasy(0) - app._bd_offset(cv)[1]
    assert x == cv.canvasx(0) - app._bd_offset(cv)[0]


def _settle(app, limit=2.0):
    t0 = time.monotonic()
    while (app._slide is not None) and time.monotonic() - t0 < limit:
        _pump(app, 1)
        time.sleep(0.005)


def test_tabs_slide_and_settle(app, monkeypatch):
    monkeypatch.setattr(main, "ANIMATIONS_ENABLED", True)
    app._build_deferred_pages()
    _pump(app, 5)
    app._show("HISTORY")
    assert app._cur_page == "HISTORY"                     # side effects start at once
    assert set(app._page_x) == {"NOW PLAYING", "HISTORY"}
    _settle(app)
    assert app._slide is None and not app._page_x
    assert {p.place_info().get("x") for p in app._pages.values() if p.winfo_manager()} == {"0"}
    # A click mid-slide retargets from where the pages are.
    app._show("STATS")
    time.sleep(0.1)
    _pump(app, 2)
    app._show("SETTINGS")
    assert "SETTINGS" in app._page_x
    _settle(app)
    assert app._cur_page == "SETTINGS" and not app._page_x


def test_animations_off_switches_instantly(app, monkeypatch):
    monkeypatch.setattr(main, "ANIMATIONS_ENABLED", False)
    app._build_deferred_pages()
    app._show("STATS")
    assert app._slide is None and not app._page_x and app._cur_page == "STATS"


def test_drag_past_the_threshold_switches_page(app, monkeypatch):
    app._build_deferred_pages()
    app._root.geometry("520x720")
    monkeypatch.setattr(main, "ANIMATIONS_ENABLED", False)
    app._show("HISTORY")
    monkeypatch.setattr(main, "ANIMATIONS_ENABLED", True)
    _pump(app, 5)
    # A short drag springs back.
    app._drag_press(_Ev(x_root=300, y_root=300), "HISTORY", None)
    app._drag_motion(_Ev(x_root=290, y_root=300))
    assert not app._drag["on"]                            # under 12 px: still a click
    app._drag_motion(_Ev(x_root=270, y_root=301))
    assert app._drag["on"] and app._page_x["HISTORY"] == -30 and "STATS" in app._page_x
    app._drag_release(_Ev(x_root=270, y_root=301))
    _settle(app)
    assert app._cur_page == "HISTORY"
    # A long one goes to the next page.
    app._drag_press(_Ev(x_root=300, y_root=300), "HISTORY", None)
    for x in (280, 240, 200, 180):
        app._drag_motion(_Ev(x_root=x, y_root=300))
        time.sleep(0.02)
    app._drag_release(_Ev(x_root=180, y_root=300))
    _settle(app)
    assert app._cur_page == "STATS"
    # Vertical gestures are left alone.
    app._drag_press(_Ev(x_root=300, y_root=300), "STATS", None)
    app._drag_motion(_Ev(x_root=302, y_root=340))
    assert app._drag is None
