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
