"""Tests for the SQLite history store (statusify_history).

THE BUG this replaces: history.json was written only by the Quit button, so a
kill, logoff, shutdown, crash or the one-click updater lost every play since
launch — history.json went 18 days without a write in daily use. The store
commits each play as it happens, so a fresh connection (i.e. the next launch
after the process died) must see it without any close/flush.
"""
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from statusify_history import HistoryStore, display_time

SYNCED = [{"startMs": 0, "words": "first line"}, {"startMs": 4000, "words": "Second Line"}]


def test_play_survives_process_death_without_close(tmp_path):
    db = str(tmp_path / "h.db")
    st = HistoryStore(db)
    st.record_play("spotify:track:a", "Artist", "Song", "art")
    # No close(), no flush — as if the process was killed right here.
    fresh = HistoryStore(db)
    rows = fresh.recent()
    assert [(r["artist"], r["title"]) for r in rows] == [("Artist", "Song")]
    fresh.close(); st.close()


def test_each_replay_is_its_own_play(tmp_path):
    st = HistoryStore(str(tmp_path / "h.db"))
    st.record_play("u1", "A", "T")
    st.record_play("u1", "A", "T")
    assert st.count() == 2
    st.close()


def test_entries_carry_a_real_timestamp(tmp_path):
    st = HistoryStore(str(tmp_path / "h.db"))
    st.record_play("u1", "A", "T", played_at="2026-09-01T10:15:00")
    e = st.recent()[0]
    assert e["played_at"] == "2026-09-01T10:15:00"
    st.close()


def test_lyrics_are_shared_by_every_play_of_a_track(tmp_path):
    st = HistoryStore(str(tmp_path / "h.db"))
    st.record_play("u1", "A", "T")
    st.save_lyrics("u1", "synced", SYNCED, [], "Spicy")
    st.record_play("u1", "A", "T")
    assert all(e["mode"] == "synced" and e["synced"] == SYNCED for e in st.recent())
    assert st.get_lyrics("u1") == ("synced", SYNCED, [], "Spicy")
    st.close()


def test_a_failed_fetch_never_erases_cached_lyrics(tmp_path):
    st = HistoryStore(str(tmp_path / "h.db"))
    st.save_lyrics("u1", "synced", SYNCED, [], "Spicy")
    st.save_lyrics("u1", "none", [], [], "fallback")
    assert st.get_lyrics("u1")[0] == "synced"
    st.close()


def test_no_lyrics_is_not_a_cache_hit(tmp_path):
    st = HistoryStore(str(tmp_path / "h.db"))
    st.save_lyrics("u1", "none", [], [])
    assert st.get_lyrics("u1") is None
    assert st.get_lyrics("") is None
    st.close()


def test_search_covers_title_artist_and_lyrics_case_insensitively(tmp_path):
    st = HistoryStore(str(tmp_path / "h.db"))
    st.record_play("u1", "Kendrick Lamar", "Money Trees")
    st.record_play("u2", "Tyler", "IFHY")
    st.save_lyrics("u2", "synced", SYNCED, [])
    assert [e["title"] for e in st.search("kendrick")] == ["Money Trees"]
    assert [e["title"] for e in st.search("TREES")] == ["Money Trees"]
    assert [e["title"] for e in st.search("second line")] == ["IFHY"]
    assert st.search("nothing matches") == []
    st.close()


def test_stats_window_and_top_artists(tmp_path):
    st = HistoryStore(str(tmp_path / "h.db"))
    old = st.record_play("u0", "Old", "x", played_at="2020-01-01T00:00:00")
    st.set_listened(old, 60_000)
    for i in range(3):
        pid = st.record_play(f"a{i}", "Tyler", f"t{i}")
        st.set_listened(pid, 120_000)
    pid = st.record_play("b", "tyler", "lowercase counts as the same artist")
    st.record_play("c", "Kendrick", "k")
    week = st.stats(since=datetime.datetime.now() - datetime.timedelta(days=7))
    assert week["plays"] == 5
    assert week["listened_ms"] == 360_000
    assert week["top_artists"][0][1] == 4
    alltime = st.stats()
    assert alltime["plays"] == 6 and alltime["listened_ms"] == 420_000
    st.close()


def test_clear_removes_everything(tmp_path):
    st = HistoryStore(str(tmp_path / "h.db"))
    st.record_play("u1", "A", "T"); st.save_lyrics("u1", "synced", SYNCED, [])
    st.clear()
    assert st.count() == 0 and st.get_lyrics("u1") is None
    st.close()


def test_legacy_json_migrates_once_with_dates(tmp_path):
    legacy = tmp_path / "history.json"
    legacy.write_text(json.dumps([
        {"track_uri": "u1", "artist": "A", "title": "T", "album_art": "",
         "mode": "synced", "synced": SYNCED, "plain": [], "time": "14:40"},
        {"track_uri": "u2", "artist": "B", "title": "U", "album_art": "",
         "mode": "none", "synced": [], "plain": [], "time": "garbage"},
    ]), encoding="utf-8")
    mtime_day = datetime.datetime.fromtimestamp(os.path.getmtime(legacy)).date()
    st = HistoryStore(str(tmp_path / "h.db"))
    assert st.import_json(str(legacy)) == 2
    assert not legacy.exists() and (tmp_path / "history.json.migrated").exists()
    rows = st.recent()
    assert rows[0]["played_at"] == f"{mtime_day.isoformat()}T14:40:00"
    assert rows[0]["synced"] == SYNCED
    assert st.import_json(str(legacy)) == 0   # never twice
    st.close()


def test_display_time():
    now = datetime.datetime(2026, 9, 22, 20, 0)
    assert display_time("2026-09-22T14:40:00", now) == "14:40"
    assert display_time("2026-09-21T14:40:00", now) == "Sep 21 · 14:40"
    assert display_time("2025-09-21T14:40:00", now) == "2025-09-21 · 14:40"
    assert display_time("14:40", now) == "14:40"
