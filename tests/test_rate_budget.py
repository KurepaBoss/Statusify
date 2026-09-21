"""Tests for staying inside Discord's SET_ACTIVITY rate limit.

THE BUG these cover: Discord allows 5 SET_ACTIVITY frames per 20 s, i.e. one
call every 4.0 s sustained. Two things conspired to overrun that budget.

1. pick_group chose how many lyric lines to pack into one presence update from
   the FIRST line's duration alone, with a single-line cutoff at 3500 ms. Any
   line lasting 3.5-4.0 s was therefore published on its own, scheduling the
   next call less than 4.0 s later — under budget, every time. The live log
   bears this out: calls that published one line had a median spacing of
   exactly 4.0 s (zero headroom), against 5.0 s for two-line and 6.0 s for
   three-line calls. 327 throttle events in one log.

2. clear_activity() sends a SET_ACTIVITY frame like any other and spends a
   slot, but every call site followed it with rl["t"].clear(), wiping the
   local ledger. After a pause, a blacklisted track or an RPC toggle Statusify
   believed it had five fresh slots while Discord was still counting the
   previous ones — the one path to a real server-side 429 rather than a
   self-imposed wait.

The user-visible symptom of (1) was lyrics lagging behind the music, because
the limiter absorbs the overrun as delay.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main


def _load(durations_ms):
    """Install a synced lyric sheet whose lines last the given durations."""
    t = 0
    synced = []
    for i, d in enumerate(durations_ms):
        synced.append({"startMs": t, "words": f"line {i}"})
        t += d
    main.state.synced = synced
    main.state.plain = []
    main.state.lyrics_mode = "synced"
    main.state.duration_ms = t
    main.state.track_uri = ""
    main.state.is_playing = False
    main.state._position_ms = 0
    main.state._pos_mono = None
    return synced


def _coverage(durations_ms, group_start_index):
    """Song-time, in seconds, that one presence call buys us."""
    main.state._position_ms = sum(durations_ms[:group_start_index])
    line1 = f"line {group_start_index}"
    group, _ = main.pick_group(line1)
    return sum(durations_ms[group_start_index:group_start_index + len(group)]) / 1000.0, group


BUDGET_S = main.RATE_LIMIT_WINDOW / main.RATE_LIMIT_CALLS   # 4.0 s per call


def test_budget_period_is_four_seconds():
    assert BUDGET_S == 4.0


def test_group_covers_budget_for_midlength_lines():
    """Lines of 3.6 s each: one line per call spends the budget faster than
    it refills. The group must span at least one budget period."""
    durs = [3600] * 12
    _load(durs)
    cov, group = _coverage(durs, 2)
    assert cov >= BUDGET_S, f"group {group} covers only {cov:.1f}s of a {BUDGET_S}s budget"


def test_group_covers_budget_for_rapid_lines():
    """Fast rap verse: 1.2 s lines. Three of them is still only 3.6 s."""
    durs = [1200] * 20
    _load(durs)
    cov, group = _coverage(durs, 3)
    assert cov >= BUDGET_S, f"group {group} covers only {cov:.1f}s of a {BUDGET_S}s budget"


def test_group_covers_budget_across_a_real_verse():
    """Mixed cadence. Every call over the whole sheet must buy >= budget,
    except where the sheet itself runs out of lines."""
    durs = [2800, 3600, 1500, 3900, 2200, 3700, 1100, 3400, 2600, 3800,
            1900, 3650, 2400, 3550, 1300, 3750, 2900, 3450, 2100, 3850]
    _load(durs)
    i = 0
    short = []
    while i < len(durs) - 4:
        cov, group = _coverage(durs, i)
        if cov < BUDGET_S:
            short.append((i, group, round(cov, 2)))
        i += len(group)
    assert not short, f"{len(short)} calls under the {BUDGET_S}s budget: {short[:5]}"


def test_group_never_exceeds_state_limit():
    durs = [800] * 30
    _load(durs)
    _, group = _coverage(durs, 0)
    assert len(main.join_lines(group)) <= main.MAX_STATE


def test_unsynced_lyrics_still_single_line():
    durs = [1000] * 10
    _load(durs)
    main.state.lyrics_mode = "plain"
    group, _ = main.pick_group("line 0")
    assert group == ["line 0"]


# ── The ledger: clears spend a slot like anything else ────────────────
import asyncio

import pytest

from test_presence import FakeRPC, playing        # noqa: F401  (fixture)


async def _drive_toggling(rpc, seconds, period):
    """Run rpc_loop while flipping is_playing on and off."""
    task = asyncio.ensure_future(main.rpc_loop(rpc))
    elapsed = 0.0
    while elapsed < seconds:
        await asyncio.sleep(period)
        main.state.is_playing = not main.state.is_playing
        elapsed += period
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def test_pause_resume_churn_stays_inside_the_quota(playing):    # noqa: F811
    """clear_activity() sends a SET_ACTIVITY frame and spends a slot.

    Every clear site used to follow it with rl["t"].clear(), wiping the local
    ledger, and the pause clear was not gated on avail() at all — so flicking
    pause repeatedly emitted unbounded frames while Statusify believed its
    budget was untouched. This is the one path to a real Discord-side 429
    rather than a self-imposed wait.
    """
    main.state.is_playing = True
    rpc = FakeRPC()
    asyncio.run(_drive_toggling(rpc, seconds=4.0, period=0.25))
    frames = len(rpc.activities) + rpc.clears
    assert frames <= main.RATE_LIMIT_CALLS, (
        f"{frames} SET_ACTIVITY frames in 3s "
        f"({rpc.clears} clears, {len(rpc.activities)} updates); "
        f"Discord allows {main.RATE_LIMIT_CALLS} per {main.RATE_LIMIT_WINDOW}s"
    )


def test_redundant_clear_is_not_sent(playing):                  # noqa: F811
    """Nothing published yet means nothing to clear. A clear frame there is
    pure waste — it spends a slot to change nothing."""
    main.state.is_playing = True
    rpc = FakeRPC()

    async def run():
        task = asyncio.ensure_future(main.rpc_loop(rpc))
        # Pause inside the 1.5s calibration gate, so the loop has had no
        # chance to publish anything at all before the pause edge fires.
        await asyncio.sleep(0.5)
        main.state.is_playing = False
        await asyncio.sleep(0.5)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run())
    assert rpc.clears == 0, f"sent {rpc.clears} clears with nothing published"
