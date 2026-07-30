"""Regression tests for the timer-chain freeze (Statusify 1.1.5).

THE BUG: App._refresh_stats re-armed itself with after(5000, ...) on every
call, but was also invoked directly from _build_settings() and from _poll()
for every ("stats",) event — which the backend emits on every track start,
pause and resume. Each direct call started an ADDITIONAL permanent timer
chain and nothing ever cancelled them. Chains accumulated for the whole
session; after ~47 hours thousands were firing per second, each appending a
row to health.csv from the Tk thread. The GUI stopped responding while the
asyncio backend kept the Discord RPC alive.

THE FIX: App._schedule(key, ms, fn) — a named timer slot that cancels the
previous timer for that key before arming a new one, making duplicate chains
structurally impossible.

These tests exercise the _schedule/_cancel contract against a fake Tk root,
so they run headless on any platform.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class FakeRoot:
    """Minimal stand-in for tk.Tk that records live timers."""

    def __init__(self):
        self._next_id = 0
        self.live = {}      # id -> (ms, fn)
        self.cancelled = []

    def after(self, ms, fn):
        self._next_id += 1
        tid = f"after#{self._next_id}"
        self.live[tid] = (ms, fn)
        return tid

    def after_cancel(self, tid):
        self.cancelled.append(tid)
        self.live.pop(tid, None)


class Scheduler:
    """The _schedule/_cancel contract, isolated from the rest of App.

    Mirrors the implementation in main.py. Kept as a copy rather than
    importing App because importing main.py pulls in tkinter, winreg,
    websockets and a Discord pipe.
    """

    def __init__(self, root):
        self._root = root
        self._timers = {}
        self._alive = True

    def _schedule(self, key, ms, fn):
        if not self._alive:
            return None
        self._cancel(key)
        tid = self._root.after(ms, fn)
        self._timers[key] = tid
        return tid

    def _cancel(self, key):
        tid = self._timers.pop(key, None)
        if tid is not None:
            self._root.after_cancel(tid)

    def _cancel_all_timers(self):
        self._alive = False
        for key in list(self._timers):
            self._cancel(key)


@pytest.fixture
def sched():
    return Scheduler(FakeRoot())


def test_single_key_holds_one_timer(sched):
    for _ in range(100):
        sched._schedule("stats", 5000, lambda: None)
    assert len(sched._root.live) == 1
    assert len(sched._timers) == 1


def test_rearming_cancels_the_previous_timer(sched):
    first = sched._schedule("stats", 5000, lambda: None)
    second = sched._schedule("stats", 5000, lambda: None)
    assert first != second
    assert first in sched._root.cancelled
    assert second in sched._root.live


def test_the_original_freeze_scenario(sched):
    """The exact call pattern that caused the freeze.

    _build_settings() calls _refresh_stats once, then every track
    start/pause/resume drains a ("stats",) event that calls it again. Under
    the old code this produced 1 + N live chains. It must now stay at 1.
    """
    sched._schedule("stats", 5000, lambda: None)          # _build_settings
    for _ in range(5000):                                  # a long session
        sched._schedule("stats", 5000, lambda: None)       # ("stats",) events
    assert len(sched._root.live) == 1, (
        f"timer chains leaked: {len(sched._root.live)} live "
        f"(this is the 1.1.5 freeze)"
    )


def test_distinct_keys_are_independent(sched):
    sched._schedule("poll", 50, lambda: None)
    sched._schedule("progress", 250, lambda: None)
    sched._schedule("stats", 5000, lambda: None)
    assert len(sched._root.live) == 3
    sched._schedule("poll", 50, lambda: None)
    assert len(sched._root.live) == 3


def test_cancel_all_stops_everything(sched):
    for key in ("poll", "progress", "stats", "rlcountdown"):
        sched._schedule(key, 100, lambda: None)
    assert len(sched._root.live) == 4
    sched._cancel_all_timers()
    assert sched._root.live == {}
    assert sched._timers == {}


def test_no_rearm_after_shutdown(sched):
    """A late callback must not resurrect a timer on a destroyed root."""
    sched._schedule("poll", 50, lambda: None)
    sched._cancel_all_timers()
    assert sched._schedule("poll", 50, lambda: None) is None
    assert sched._root.live == {}


def test_cancel_unknown_key_is_safe(sched):
    sched._cancel("never-armed")   # must not raise
