"""The Stats page: its history queries, its pure helpers, and the page itself
driven through Tk (tab, live updates, heatmap hover, Wrapped export)."""
import datetime
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import main
import statusify_ui_stats as stats
from statusify_history import HistoryStore, longest_streak

tk = main.tk


def _iso(dt):
    return dt.replace(microsecond=0).isoformat()


# ── Queries ──────────────────────────────────────────────────────
@pytest.fixture
def store(tmp_path):
    st = HistoryStore(str(tmp_path / "q.db"))
    yield st
    st.close()


def _play(st, artist, title, when, ms=0, art=""):
    pid = st.record_play(f"spotify:track:{artist}-{title}", artist, title, art, _iso(when))
    if ms:
        st.set_listened(pid, ms)
    return pid


def test_recent_plays_newest_first_with_paging(store):
    base = datetime.datetime(2026, 9, 1, 12, 0)
    for i in range(5):
        _play(store, "A", f"S{i}", base + datetime.timedelta(minutes=i), ms=1000 * i)
    got = store.recent_plays(limit=3)
    assert [p["title"] for p in got] == ["S4", "S3", "S2"]
    assert got[0]["listened_ms"] == 4000 and "synced" not in got[0]
    assert [p["title"] for p in store.recent_plays(limit=3, offset=3)] == ["S1", "S0"]


def test_plays_per_day_counts_local_dates_from_a_start(store):
    _play(store, "A", "x", datetime.datetime(2026, 9, 10, 23, 59))
    _play(store, "A", "x", datetime.datetime(2026, 9, 10, 0, 0))
    _play(store, "A", "x", datetime.datetime(2026, 9, 11, 8, 0))
    _play(store, "A", "x", datetime.datetime(2026, 9, 1, 8, 0))
    assert store.plays_per_day(datetime.date(2026, 9, 5)) == {"2026-09-10": 2, "2026-09-11": 1}


def test_top_tracks_merges_case_and_respects_since(store):
    now = datetime.datetime(2026, 9, 20, 12, 0)
    for _ in range(3):
        _play(store, "Radiohead", "Reckoner", now - datetime.timedelta(days=60), ms=1000)
    _play(store, "radiohead", "reckoner", now - datetime.timedelta(days=1), ms=1000, art="u")
    _play(store, "Björk", "Jóga", now - datetime.timedelta(days=1))
    _play(store, "Björk", "Jóga", now - datetime.timedelta(days=2))
    allt = store.top_tracks(limit=5)
    assert (allt[0]["plays"], allt[0]["title"].lower()) == (4, "reckoner")
    assert allt[0]["album_art"] == "u" and allt[0]["listened_ms"] == 4000
    recent = store.top_tracks(since=now - datetime.timedelta(days=30), limit=5)
    assert [(t["title"], t["plays"]) for t in recent] == [("Jóga", 2), ("reckoner", 1)]


def test_month_summary(store):
    d = datetime.datetime
    _play(store, "A", "One", d(2026, 8, 31, 23, 0), ms=60_000)        # previous month
    _play(store, "A", "One", d(2026, 9, 1, 21, 5), ms=120_000)
    _play(store, "A", "One", d(2026, 9, 2, 21, 30), ms=120_000)
    _play(store, "B", "Two", d(2026, 9, 3, 9, 0), ms=60_000)
    _play(store, "b", "Two", d(2026, 9, 3, 21, 10), ms=60_000)
    _play(store, "B", "Two", d(2026, 9, 7, 10, 0), ms=60_000)
    _play(store, "C", "Three", d(2026, 10, 1, 0, 0), ms=60_000)       # next month
    m = store.month_summary(2026, 9)
    assert m["plays"] == 5 and m["listened_ms"] == 420_000
    assert m["artists"] == 2
    assert m["top_song"]["title"] == "Two" and m["top_song"]["plays"] == 3
    assert m["top_artist"] == ("B", 3)
    assert m["busiest_hour"] == (21, 3)
    assert m["longest_streak"] == 3            # 1st, 2nd, 3rd
    assert m["active_days"] == 4


def test_month_summary_december_and_empty(store):
    _play(store, "A", "X", datetime.datetime(2025, 12, 31, 23, 59))
    _play(store, "A", "X", datetime.datetime(2026, 1, 1, 0, 0))
    assert store.month_summary(2025, 12)["plays"] == 1
    empty = store.month_summary(2024, 2)
    assert empty["plays"] == 0 and empty["top_song"] is None
    assert empty["top_artist"] is None and empty["busiest_hour"] is None
    assert empty["longest_streak"] == 0
    assert store.first_played() == "2025-12-31T23:59:00"


def test_longest_streak():
    assert longest_streak([]) == 0
    assert longest_streak(["2026-09-01"]) == 1
    assert longest_streak(["2026-09-03", "2026-09-01", "2026-09-02", "2026-09-02",
                           "2026-09-05", "2026-09-06"]) == 3
    assert longest_streak(["2026-02-28", "2026-03-01"]) == 2


# ── Pure helpers ─────────────────────────────────────────────────
def test_relative_time():
    now = datetime.datetime(2026, 9, 23, 15, 0)
    ago = lambda **k: _iso(now - datetime.timedelta(**k))
    assert stats.rel_time(ago(seconds=20), now) == "Just now"
    assert stats.rel_time(ago(minutes=3), now) == "3 min ago"
    assert stats.rel_time(ago(hours=5), now) == "Today 10:00"
    assert stats.rel_time("2026-09-22T21:14:00", now) == "Yesterday 21:14"
    assert stats.rel_time("2026-09-19T08:05:00", now) == "Sat 08:05"
    assert stats.rel_time("2026-09-14T08:05:00", now) == "14 Sep"
    assert stats.rel_time("2025-09-14T08:05:00", now) == "14 Sep 2025"
    assert stats.rel_time("garbage", now) == "garbage"


def test_duration_and_heat_helpers():
    assert stats.fmt_duration(0) == "" and stats.fmt_duration(187_000) == "3:07"
    assert stats.fmt_duration(3_725_000) == "1:02:05"
    assert [stats.heat_level(n, 12) for n in (0, 1, 3, 4, 7, 12)] == [0, 1, 1, 2, 3, 4]
    assert stats.heat_tip(datetime.date(2026, 9, 14), 12) == "12 plays · Mon 14 Sep"
    assert stats.heat_tip(datetime.date(2026, 9, 14), 1) == "1 play · Mon 14 Sep"
    assert stats.heat_tip(datetime.date(2026, 9, 14), 0) == "No plays · Mon 14 Sep"


# ── The page ─────────────────────────────────────────────────────
def _make_app():
    for attempt in range(3):
        try:
            return main.App()
        except tk.TclError as e:
            transient = ("installed properly" in str(e) or "tcl_findLibrary" in str(e))
            if not transient or attempt == 2:
                raise
            time.sleep(0.2)


def _teardown(a):
    for fn in (a._cancel_all_timers, a._tray_stop):
        try:
            fn()
        except Exception:
            pass
    a._alive = False
    a._root.destroy()


@pytest.fixture
def app(tmp_path, monkeypatch):
    st = HistoryStore(str(tmp_path / "h.db"))
    now = datetime.datetime.now()
    for i in range(12):                     # oldest first, as plays arrive
        when = now - datetime.timedelta(days=(11 - i) // 2, hours=1 + (11 - i) % 2)
        _play(st, f"Artist {i % 3}", f"Song {i % 4}", when, ms=150_000)
    monkeypatch.setattr(main, "_HISTORY_STORE", st)
    monkeypatch.setattr(main, "SAVE_HISTORY", True)
    monkeypatch.setattr(main, "history", st.recent())
    monkeypatch.setattr(main, "_register_hotkeys", lambda *a, **k: None)
    a = _make_app()
    a._store_for_test = st
    yield a
    _teardown(a)
    st.close()
    del a
    import gc
    gc.collect()


def _pump(app, n=20):
    for _ in range(n):
        app._root.update()


def _texts(cv):
    return [cv.itemcget(i, "text") for i in cv.find_all() if cv.type(i) == "text"]


def _open_stats(app):
    app._build_deferred_pages()
    app._show("STATS")
    _pump(app, 5)
    app._refresh_long_stats(sync=True)
    app._stats_render()
    _pump(app, 3)


def test_stats_tab_sits_between_history_and_settings(app):
    labels = [w.cget("text") for w in app._nav.winfo_children()]
    assert labels == ["Lyrics", "History", "Stats", "Settings"]
    app._build_deferred_pages()
    for name in ("STATS", "HISTORY", "STATS", "SETTINGS", "STATS"):
        app._show(name)
        _pump(app, 3)
        assert app._cur_page == name
    assert "STATS" in app._pages


def test_stats_page_builds_on_demand_and_loads_off_thread(app):
    app._show("STATS")                       # before the deferred build ran
    assert "STATS" in app._pages
    deadline = time.monotonic() + 5
    while not (app._st_data and "recent" in app._st_data) and time.monotonic() < deadline:
        _pump(app, 2)
        time.sleep(0.01)
    assert app._st_data["all"]["plays"] == 12
    texts = _texts(app.st_cv)
    for heading in ("Overview", "Activity", "Wrapped", "Top songs", "Recently played"):
        assert heading in texts


def test_settings_no_longer_shows_listening(app):
    app._build_deferred_pages()
    app._show("SETTINGS"); _pump(app, 5)
    assert ("section", "Listening") not in app._set_spec
    texts = _texts(app.set_cv)
    assert "Listening" not in texts and "songs this session" not in texts


def test_session_stats_refresh_lands_on_the_stats_page(app, monkeypatch):
    _open_stats(app)
    monkeypatch.setattr(main, "_session_songs", 7)
    app._refresh_stats(reschedule=False)
    item = app.lbl_stats_songs.item
    assert item is not None and app.st_cv.itemcget(item, "text") == "7"


def test_recently_played_lists_newest_first_and_opens_history(app):
    _open_stats(app)
    recent = app._st_data["recent"]
    assert len(recent) == 12
    assert recent[0]["played_at"] >= recent[-1]["played_at"]
    assert recent[0]["title"] in _texts(app.st_cv)
    app._stats_open_in_history(recent[0])
    assert app._cur_page == "HISTORY"
    assert app._hist_search.get() == recent[0]["title"]


def test_new_play_updates_the_open_page(app):
    _open_stats(app)
    st = app._store_for_test
    _play(st, "Fresh Artist", "Brand New Song", datetime.datetime.now(), ms=0)
    main.event_queue.put(("history_add", {"title": "Brand New Song", "artist": "Fresh Artist",
                                          "mode": "none", "synced": [], "plain": [],
                                          "time": "", "album_art": "", "track_uri": ""}))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        app._poll()
        _pump(app, 2)
        if app._st_data["recent"][0]["title"] == "Brand New Song":
            break
        time.sleep(0.02)
    assert app._st_data["recent"][0]["title"] == "Brand New Song"
    deadline = time.monotonic() + 2               # the redraw is debounced
    while "Brand New Song" not in _texts(app.st_cv) and time.monotonic() < deadline:
        _pump(app, 2)
        time.sleep(0.01)
    assert "Brand New Song" in _texts(app.st_cv)


def test_heatmap_hover_shows_a_tooltip(app):
    _open_stats(app)
    h = app._st_heat
    assert h is not None and h["weeks"] >= 4
    today = h["today"]
    idx = (today - h["start"]).days
    x = h["x"] + (idx // 7) * h["step"] + 2
    y = h["y"] + (idx % 7) * h["step"] + 2
    n = h["counts"][today]
    assert app._stats_heat_at(x, y) == (today, n)
    cv = app.st_cv
    ev = type("E", (), {"x": int(x - cv.canvasx(0)), "y": int(y - cv.canvasy(0))})()
    app._stats_heat_motion(ev)
    tip = [cv.itemcget(i, "text") for i in cv.find_withtag("sttip") if cv.type(i) == "text"]
    assert tip == [stats.heat_tip(today, n)]
    # Off the grid: the tooltip goes away.
    app._stats_heat_motion(type("E", (), {"x": 1, "y": 1})())
    assert not cv.find_withtag("sttip")


def test_wrapped_month_navigation(app):
    _open_stats(app)
    today = datetime.date.today()
    assert app._st_month == (today.year, today.month)
    app._stats_month_step(1, sync=True)                 # no future months
    assert app._st_month == (today.year, today.month)
    first = datetime.date.fromisoformat(app._st_data["first"][:10])
    if (first.year, first.month) < (today.year, today.month):
        app._stats_month_step(-1, sync=True)
        assert app._st_month != (today.year, today.month)
        assert app._st_data["month"]["month"] == app._st_month[1]
    else:
        app._stats_month_step(-1, sync=True)            # nothing earlier
        assert app._st_month == (today.year, today.month)


def test_wrapped_export_writes_a_png(app, monkeypatch, tmp_path):
    _open_stats(app)
    out = tmp_path / "wrapped.png"
    seen = {}

    def fake_dialog(**kw):
        seen.update(kw)
        return str(out)
    monkeypatch.setattr(stats.filedialog, "asksaveasfilename", fake_dialog)
    # Make sure the month shown has plays (early in a month, the fixture's
    # plays may all fall in the previous one).
    st = app._store_for_test
    ym = app._st_month
    if not app._st_data["month"]["plays"]:
        _play(st, "A", "B", datetime.datetime(ym[0], ym[1], 1, 12, 0), ms=1000)
        app._refresh_long_stats(sync=True)
    assert app._stats_save_wrapped(sync=True) == str(out)
    assert seen["defaultextension"] == ".png"
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    from PIL import Image
    with Image.open(out) as im:
        assert im.size == (1080, 1350)


def test_wrapped_export_cancelled_writes_nothing(app, monkeypatch, tmp_path):
    _open_stats(app)
    monkeypatch.setattr(stats.filedialog, "asksaveasfilename", lambda **kw: "")
    assert app._stats_save_wrapped(sync=True) is None
    assert not list(tmp_path.glob("*.png"))


def test_copy_image_uses_the_clipboard_helper(app, monkeypatch):
    _open_stats(app)
    got = []
    monkeypatch.setattr(stats, "copy_image_to_clipboard", lambda img: got.append(img.size) or True)
    if app._st_data["month"]["plays"]:
        app._stats_copy_wrapped(sync=True)
        assert got == [(1080, 1350)]


def test_top_songs_toggle(app):
    _open_stats(app)
    assert app._st_top_mode == "all"
    gen = app._st_gen
    app._stats_set_top_mode("30")
    assert app._st_top_mode == "30" and app._st_gen == gen + 1


def test_show_more_extends_recent(app, monkeypatch):
    st = app._store_for_test
    base = datetime.datetime.now() - datetime.timedelta(days=3)
    for i in range(40):
        _play(st, "Bulk", f"Bulk {i}", base - datetime.timedelta(minutes=i))
    _open_stats(app)
    assert len(app._st_data["recent"]) == stats.RECENT_PAGE
    assert "Show more" in _texts(app.st_cv)
    app._stats_show_more(sync=True)
    assert len(app._st_data["recent"]) == 52
    assert "Show more" not in _texts(app.st_cv)


def test_history_off_shows_an_intentional_empty_state(app, monkeypatch):
    monkeypatch.setattr(main, "SAVE_HISTORY", False)
    _open_stats(app)
    app._stats_render()
    assert "History is off" in _texts(app.st_cv)


def test_empty_history_invites_you_to_play(app, monkeypatch, tmp_path):
    empty = HistoryStore(str(tmp_path / "empty.db"))
    monkeypatch.setattr(main, "_HISTORY_STORE", empty)
    try:
        _open_stats(app)
        texts = _texts(app.st_cv)
        assert "Nothing here yet" in texts
        assert any(t.startswith("Play something and your stats will appear here") for t in texts)
    finally:
        empty.close()


def test_palette_change_rerenders_the_stats_page(app):
    _open_stats(app)
    gen = app._st_gen
    app._set_theme("light"); _pump(app, 3)
    assert app._st_gen > gen
    app._set_theme("dark"); _pump(app, 3)


def test_stats_page_scrolls_without_native_children(app):
    _open_stats(app)
    assert app.st_cv.winfo_children() == []
    app._st_scroll_to(300, animate=False); _pump(app, 2)
    assert app.st_cv.canvasy(0) == min(300, max(0, app._st_total - app.st_cv.winfo_height()))
