"""Tests for statusify_presence_plan: which lyric lines reach Discord, and when.

Two bugs made lyrics reach Discord late or not at all.

1. rpc_loop de-duplicated by the TEXT of the last line it published
   (`line1 == last_line or line1 in skip`), not by what Discord was showing.
   Once the instrumental marker or a title-only presence replaced that line,
   a returning line with the same words — a hook after a break — was treated
   as already on screen and never sent. In one real track the marker stayed
   pinned for the rest of the song.

2. It spent the SET_ACTIVITY budget greedily: publish the instant a line
   starts, pad to ~4.75 s of song, and when the ledger ran dry, sleep and
   publish whatever was current by then. A frame spent early on a slow line
   is a frame missing when the fast passage arrives a few seconds later.
   Replayed over 211 real lyric sheets the sung line was off-screen 2.5 % of
   the time; planning ahead brings that to 1.1 %.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import statusify_presence_plan as P
from statusify_lyrics import _calc_instrumental_gaps, join_lines

CALLS, WINDOW = 5, 20000


def _sheet(spec):
    """[(duration_ms, words), ...] -> synced lyric list, total duration."""
    t, out = 0, []
    for dur, words in spec:
        out.append({"startMs": t, "words": words})
        t += dur
    return out, t


def _units(synced, duration, gaps=()):
    return P.build_units("synced", synced, [], duration, list(gaps))


def _run(units, gate=0, history=(), shown=frozenset(), max_steps=500):
    """Execute plans the way rpc_loop does: act on the first publish, re-plan.
    Returns the publishes actually sent."""
    sent, hist, pos = [], list(history), 0
    for _ in range(max_steps):
        evs = P.plan(units, pos, hist, shown, gate, calls=CALLS, window_ms=WINDOW)
        if not evs:
            later = [u for u in units if u.start >= pos + P.PLAN_HORIZON_MS]
            if not later:
                return sent
            pos = later[0].start - P.PLAN_HORIZON_MS + 1
            continue
        ev = evs[0]
        sent.append(ev); hist.append(ev.t)
        shown = P.shown_for(ev)
        pos = ev.t
    raise AssertionError("planner did not converge")


def _offscreen_ms(units, sent):
    """Sung time during which Discord was not showing the sung line."""
    total = 0
    for u in units:
        if u.kind != "line":
            continue
        # piecewise over the sends that were showing during this unit
        x = u.start
        while x < u.end:
            prev = [p for p in sent if p.t <= x]
            nxt = [p.t for p in sent if x < p.t < u.end]
            stop = min(nxt) if nxt else u.end
            if not prev or u.text not in P.shown_for(prev[-1]):
                total += stop - x
            x = stop
    return total


def _max_per_window(sent):
    ts = sorted(p.t for p in sent)
    return max((sum(1 for y in ts if x <= y < x + WINDOW) for x in ts), default=0)


# ── build_units ─────────────────────────────────────────────────────────
class TestBuildUnits:
    def test_title_only_covers_a_track_without_lyrics(self):
        units = P.build_units("none", [], [], 180000, [])
        assert [(u.kind, u.start, u.end) for u in units] == [("title", 0, 180000)]

    def test_title_only_before_the_first_line(self):
        synced, dur = _sheet([(3000, "a"), (3000, "b")])
        synced = [{"startMs": e["startMs"] + 2000, "words": e["words"]} for e in synced]
        units = _units(synced, dur + 2000)
        assert (units[0].kind, units[0].start, units[0].end) == ("title", 0, 2000)
        assert [u.text for u in units[1:]] == ["a", "b"]

    def test_instrumental_gap_cuts_into_the_line_it_starts_in(self):
        gap = {"startMs": 7000, "endMs": 30000, "gap_ms": 26000, "key": 0}
        synced = [{"startMs": 4000, "words": "hook"}, {"startMs": 30000, "words": "hook"}]
        units = _units(synced, 40000, [gap])
        kinds = [(u.kind, u.start, u.end) for u in units]
        assert ("line", 4000, 7000) in kinds
        assert ("gap", 7000, 30000) in kinds
        assert ("line", 30000, 40000) in kinds

    def test_intro_gap_replaces_the_title_only_stretch(self):
        gap = {"startMs": 0, "endMs": 12000, "gap_ms": 12000, "key": -2}
        synced = [{"startMs": 12000, "words": "a"}, {"startMs": 14000, "words": "b"}]
        units = _units(synced, 20000, [gap])
        assert units[0].kind == "gap" and units[0].start == 0
        assert not any(u.kind == "title" for u in units)

    def test_plain_lyrics_split_the_track_evenly_like_select_line(self):
        units = P.build_units("plain", [], ["a", "b", "c", "d"], 40000, [])
        assert [(u.start, u.end) for u in units] == [(0, 10000), (10000, 20000),
                                                     (20000, 30000), (30000, 40000)]

    def test_empty_lines_are_blank_not_publishable(self):
        synced, dur = _sheet([(3000, "a"), (3000, ""), (3000, "b")])
        assert [u.kind for u in _units(synced, dur)] == ["line", "blank", "line"]


# ── plan: the ledger and the settle gate ────────────────────────────────
class TestConstraints:
    def test_nothing_is_sent_before_the_gate(self):
        synced, dur = _sheet([(5000, "a"), (5000, "b")])
        evs = P.plan(_units(synced, dur), 0, [], frozenset(), 1500)
        assert evs[0].t == 1500

    def test_a_full_ledger_holds_the_next_send_until_a_slot_frees(self):
        synced, dur = _sheet([(5000, c) for c in "abcdef"])
        history = [-4000, -3000, -2000, -1000, -500]
        evs = P.plan(_units(synced, dur), 0, history, frozenset(), 0)
        assert evs[0].t == -4000 + WINDOW
        assert evs[0].lines == ("d",)          # the line being sung at 16 s

    def test_an_update_never_exceeds_the_state_limit(self):
        synced, dur = _sheet([(400, "x" * 50)] * 40)
        for ev in _run(_units(synced, dur)):
            assert len(join_lines(list(ev.lines))) <= 128

    @pytest.mark.parametrize("spec", [
        [(3600, "line %d" % i) for i in range(30)],
        [(1200, "line %d" % i) for i in range(60)],
        [(3800, "%02d " % i + "x" * 85) for i in range(40)],       # too long to pair
        [(d, "line %d" % i) for i, d in enumerate(
            [2800, 3600, 1500, 3900, 2200, 3700, 1100, 3400, 2600, 3800,
             1900, 3650, 2400, 3550, 1300, 3750, 2900, 3450, 2100, 3850] * 2)],
    ])
    def test_never_more_than_five_frames_in_any_twenty_seconds(self, spec):
        synced, dur = _sheet(spec)
        sent = _run(_units(synced, dur))
        assert _max_per_window(sent) <= CALLS


# ── plan: getting lines on screen on time ───────────────────────────────
class TestPunctuality:
    @pytest.mark.parametrize("spec", [
        # 3.6 s lines: one line per frame outruns the 4 s budget refill.
        [(3600, "line %d" % i) for i in range(30)],
        # a fast rap verse, 1.2 s a line
        [(1200, "line %d" % i) for i in range(60)],
        # a mixed cadence
        [(d, "line %d" % i) for i, d in enumerate(
            [2800, 3600, 1500, 3900, 2200, 3700, 1100, 3400, 2600, 3800,
             1900, 3650, 2400, 3550, 1300, 3750, 2900, 3450, 2100, 3850] * 2)],
    ])
    def test_every_packable_line_is_on_screen_the_whole_time_it_is_sung(self, spec):
        """When lines are short enough to share the status, packing them costs
        nothing, so no line may ever be late."""
        synced, dur = _sheet(spec)
        units = _units(synced, dur)
        sent = _run(units)
        assert _offscreen_ms(units, sent) == 0

    def test_slow_lines_go_out_one_at_a_time_as_they_start(self):
        """With budget to spare, show exactly the line being sung."""
        synced, dur = _sheet([(6000, "line %d" % i) for i in range(10)])
        units = _units(synced, dur)
        sent = _run(units)
        assert [(p.t, p.lines) for p in sent] == [
            (i * 6000, ("line %d" % i,)) for i in range(10)]

    def test_packs_a_slow_line_with_the_next_when_a_crunch_is_coming(self):
        """The look-ahead the greedy loop lacked. Two frames are free and the
        next frees at 17 s. A lasts 6 s — "enough" by the old rule, so it went
        out alone, B took the second frame, and C (too long to share with B)
        sat off-screen for all ten of its seconds. Sending A with B leaves the
        frame C needs."""
        A, B, C, D = "A" * 30, "B" * 30, "C" * 100, "D" * 100
        synced = [{"startMs": 0, "words": A}, {"startMs": 6000, "words": B},
                  {"startMs": 7000, "words": C}, {"startMs": 17000, "words": D}]
        evs = P.plan(_units(synced, 30000), 0, [-3000, -2000, -1000], frozenset(), 0)
        assert [(p.t, p.lines) for p in evs] == [(0, (A, B)), (7000, (C,)), (17000, (D,))]

    def test_same_sheet_with_budget_to_spare_sends_the_slow_line_alone(self):
        A, B, C, D = "A" * 30, "B" * 30, "C" * 100, "D" * 100
        synced = [{"startMs": 0, "words": A}, {"startMs": 6000, "words": B},
                  {"startMs": 7000, "words": C}, {"startMs": 17000, "words": D}]
        evs = P.plan(_units(synced, 30000), 0, [], frozenset(), 0)
        assert [(p.t, p.lines) for p in evs] == [(0, (A,)), (6000, (B,)), (7000, (C,)),
                                                 (17000, (D,))]

    def test_a_line_already_showing_is_not_sent_again(self):
        """Repeated words ("Shoo, shoo, shoo" twice) are already on screen;
        a second identical frame would spend budget to change nothing."""
        synced, dur = _sheet([(6000, "shoo"), (6000, "shoo"), (6000, "next")])
        sent = _run(_units(synced, dur))
        assert [p.lines for p in sent] == [("shoo",), ("next",)]

    def test_a_repeated_line_after_the_instrumental_marker_is_sent(self):
        """The de-dupe bug: the marker replaced "hook" on screen, so "hook"
        coming back must be published again."""
        synced = [{"startMs": 0, "words": "x1"}, {"startMs": 2000, "words": "x2"},
                  {"startMs": 4000, "words": "hook"}, {"startMs": 30000, "words": "hook"},
                  {"startMs": 32000, "words": "y1"}, {"startMs": 34000, "words": "y2"},
                  {"startMs": 36000, "words": "y3"}]
        gaps = _calc_instrumental_gaps(synced, 40000)
        assert gaps, "fixture should contain an instrumental gap"
        units = _units(synced, 40000, gaps)
        sent = _run(units)
        kinds = [(p.kind, p.lines) for p in sent]
        g = kinds.index(("gap", ()))
        assert any("hook" in lines for _, lines in kinds[g + 1:]), kinds
        assert _offscreen_ms(units, sent) == 0

    def test_title_only_for_a_track_without_lyrics_is_sent_once(self):
        units = P.build_units("none", [], [], 180000, [])
        sent = _run(units, gate=1500)
        assert [(p.kind, p.t) for p in sent] == [("title", 1500)]

    def test_planning_is_cheap_on_a_pathological_sheet(self):
        """Sub-second repeating lines (the worst case seen in the replay)."""
        import time
        words = ["Smoke, das in the blem", "Free up the guys then dip out",
                 "Jump in the ride", "Jump don't caught", "See the void",
                 "Now we's in the block", "Wrought for revenge"]
        synced, dur = _sheet([(500 + 300 * (i % 4), words[i % 7]) for i in range(120)])
        units = _units(synced, dur)
        t = time.perf_counter()
        P.plan(units, 0, [], frozenset(), 0)
        assert time.perf_counter() - t < 1.0


# ── rpc_loop: the de-dupe bug, end to end ───────────────────────────────
import main  # noqa: E402

from test_presence import FakeRPC, playing   # noqa: E402,F401  (fixture)


def test_rpc_loop_republishes_a_hook_after_the_instrumental_marker(playing):  # noqa: F811
    """rpc_loop compared each line with the text it last published, so once
    the marker had replaced "hook" on Discord, "hook" returning after the
    break was skipped as a duplicate and the marker stayed up."""
    synced = [{"startMs": 0, "words": "x1"}, {"startMs": 2000, "words": "x2"},
              {"startMs": 4000, "words": "hook"}, {"startMs": 30000, "words": "hook"},
              {"startMs": 32000, "words": "y1"}, {"startMs": 34000, "words": "y2"},
              {"startMs": 36000, "words": "y3"}]
    st = main.state
    st.duration_ms = 40000
    st.synced = synced
    st.lyrics_mode = "synced"
    st.instrumental_gaps = _calc_instrumental_gaps(synced, 40000)
    st.position_ms = 4500
    rpc = FakeRPC()

    async def run():
        task = asyncio.ensure_future(main.rpc_loop(rpc))
        await asyncio.sleep(1.8)            # settle gate, then "hook" goes out
        st.position_ms = 8000               # into the instrumental break
        await asyncio.sleep(0.4)
        st.position_ms = 30500              # the hook is back
        await asyncio.sleep(0.4)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run())
    published = [lines for _, _, lines, _ in rpc.activities]
    assert main.INSTRUMENTAL_TEXT in [l for lines in published for l in lines], published
    marker_at = max(i for i, lines in enumerate(published) if main.INSTRUMENTAL_TEXT in lines)
    assert any("hook" in lines for lines in published[marker_at + 1:]), (
        f"the hook never replaced the instrumental marker: {published}")
