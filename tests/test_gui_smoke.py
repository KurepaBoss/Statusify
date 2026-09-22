"""Builds the real window and every page, then drives the history and stats
paths through Tk. The rest of the suite covers logic only, so without this a
typo in UI code surfaces only when the user opens the page."""
import datetime
import os
import sys

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
    try:
        a = main.App()
    except tk.TclError as e:
        pytest.skip(f"no display: {e}")
    yield a
    for fn in (a._cancel_all_timers, a._tray_stop):
        try:
            fn()
        except Exception:
            pass
    a._alive = False
    a._root.destroy()
    st.close()


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
    assert "5 plays" in app.lbl_stats_all.cget("text")
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
