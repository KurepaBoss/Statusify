"""History page helpers and page-slide planning (no Tk)."""
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import statusify_ui_history as H
import statusify_ui_backdrop as UB

NOW = datetime.datetime(2026, 9, 23, 20, 0)


def _e(minutes_ago, **kw):
    t = NOW - datetime.timedelta(minutes=minutes_ago)
    return dict({"title": f"t{minutes_ago}", "artist": "a", "played_at": t.isoformat(),
                 "listened_ms": 180_000}, **kw)


def test_day_labels():
    today = NOW.date()
    assert H.day_label(today, today) == "Today"
    assert H.day_label(today - datetime.timedelta(days=1), today) == "Yesterday"
    assert H.day_label(datetime.date(2026, 9, 21), today) == "Mon 21 Sep"
    assert H.day_label(datetime.date(2025, 9, 21), today) == "Sun 21 Sep 2025"


def test_group_by_day_newest_first_with_sessions_and_time():
    entries = [_e(5), _e(10), _e(120), _e(60 * 24), _e(60 * 24 + 5)]
    groups = H.group_by_day(entries, NOW)
    assert [g["label"] for g in groups] == ["Today", "Yesterday"]
    today = groups[0]
    assert [e["title"] for e in today["entries"]] == ["t5", "t10", "t120"]
    assert today["sessions"] == 2                   # 110 minutes between 120 and 10
    assert today["listened_ms"] == 3 * 180_000
    assert H.day_summary(today) == "2 sessions · 9 min"
    assert H.day_summary(groups[1]) == "1 session · 6 min"


def test_group_by_day_sorts_by_time_not_input_order():
    groups = H.group_by_day([_e(60 * 24), _e(5), _e(60 * 24 * 3), _e(1)], NOW)
    assert [g["label"] for g in groups] == ["Today", "Yesterday", "Sun 20 Sep"]
    assert [e["title"] for e in groups[0]["entries"]] == ["t1", "t5"]


def test_session_count_and_totals():
    assert H.count_sessions([]) == 0
    t = NOW
    assert H.count_sessions([t, t + datetime.timedelta(minutes=29)]) == 1
    assert H.count_sessions([t, t + datetime.timedelta(minutes=31)]) == 2
    assert H.fmt_total(52 * 60000) == "52 min"
    assert H.fmt_total(72 * 60000) == "1 h 12 min"
    assert H.fmt_total(120 * 60000) == "2 h"


def test_badge_and_caption():
    assert H.lyric_badge({"synced": [{"startMs": 0}]}) == "Synced"
    assert H.lyric_badge({"synced": [], "plain": ["x"]}) == "Plain"
    assert H.lyric_badge({}) is None
    assert H.plays_caption(1284, "2026-08-03T10:00:00", NOW.date()) == "1,284 plays · since 3 Aug"
    assert H.plays_caption(1, "2025-08-03T10:00:00", NOW.date()) == "1 play · since 3 Aug 2025"
    assert H.plays_caption(0, None, NOW.date()) == "0 plays"


def test_slide_plan_neighbours_and_retarget():
    W = 500
    plan = UB.slide_plan({"NOW PLAYING": 0.0}, "HISTORY", W)
    assert plan == {"NOW PLAYING": (0.0, -W), "HISTORY": (W, 0.0)}
    back = UB.slide_plan({"HISTORY": 0.0}, "NOW PLAYING", W)
    assert back == {"HISTORY": (0.0, W), "NOW PLAYING": (-W, 0.0)}
    # Mid-slide from Lyrics to History, a click on Stats: it comes in beside
    # History and everything else leaves to the left.
    mid = UB.slide_plan({"NOW PLAYING": -250.0, "HISTORY": 250.0}, "STATS", W)
    assert mid["STATS"] == (750.0, 0.0)
    assert mid["NOW PLAYING"] == (-250.0, -W) and mid["HISTORY"] == (250.0, -W)
    # … and a click back on Lyrics reverses from where the pages are.
    rev = UB.slide_plan({"NOW PLAYING": -250.0, "HISTORY": 250.0}, "NOW PLAYING", W)
    assert rev == {"HISTORY": (250.0, W), "NOW PLAYING": (-250.0, 0.0)}


def test_visibility():
    assert UB.visibility(0, 500) == 1.0
    assert UB.visibility(250, 500) == 0.5
    assert UB.visibility(-600, 500) == 0.0
