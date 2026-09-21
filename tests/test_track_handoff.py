"""Tests for state leaking from one track into the next.

THE BUGS these cover, both read straight out of statusify.log:

1. Late lyrics adopted by the wrong song. ws_handler accepted a "lyrics"
   message when `uri == state.track_uri or state.lyrics_mode == "none"`. The
   second clause was meant for bridges that omit track_uri, but lyrics_mode is
   *always* "none" right after a track_change — so lyrics for the PREVIOUS
   track, still in flight when the user skipped, were adopted by the new one.
   Logged instance: skipping Drake → Mac Miller published Drake lines on Mac
   Miller's presence for 11 s until the real lyrics landed, and saved them to
   Mac Miller's history entry.

2. Instrumental gaps outliving their track. track_change reset synced/plain
   but not instrumental_gaps, and rpc_loop checks gaps before lyrics — so a
   lyric-less track inherited the previous song's gaps and got an
   "instrumental" presence for a break that doesn't exist, spending a
   rate-limit slot on it.
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import main

OLD = "spotify:track:0ld0ld0ld0ld0ld0ld0ld0"
NEW = "spotify:track:n3wn3wn3wn3wn3wn3wn3wn"
LINES = [{"startMs": 1000, "words": "old line one"},
         {"startMs": 20000, "words": "old line two"}]


class FakeWS:
    """Feeds scripted bridge messages into ws_handler."""

    def __init__(self, msgs):
        self._msgs = [json.dumps(m) for m in msgs]
        self.sent = []

    async def send(self, data):
        self.sent.append(data)

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for m in self._msgs:
            yield m


def track(uri):
    return {"type": "track_change", "artist": "A", "title": uri[-4:],
            "track_uri": uri, "album_art": "", "duration_ms": 200_000}


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(main, "_save_history", lambda *a, **k: None)
    monkeypatch.setattr(main.asyncio, "sleep", _nosleep)
    monkeypatch.setattr(main.state, "track_uri", "", raising=False)
    monkeypatch.setattr(main.state, "synced", [], raising=False)
    monkeypatch.setattr(main.state, "instrumental_gaps", [], raising=False)
    monkeypatch.setattr(main.state, "lyrics_mode", "none", raising=False)


async def _nosleep(*_a, **_k):
    return None


def run(msgs):
    asyncio.run(main.ws_handler(FakeWS(msgs)))


def test_late_lyrics_for_previous_track_are_rejected():
    run([track(OLD), track(NEW),
         {"type": "lyrics", "track_uri": OLD, "mode": "synced",
          "synced": LINES, "plain": [], "source": "Spicy"}])
    assert main.state.track_uri == NEW
    assert main.state.synced == []
    assert main.state.lyrics_mode == "none"


def test_lyrics_for_current_track_are_accepted():
    run([track(NEW),
         {"type": "lyrics", "track_uri": NEW, "mode": "synced",
          "synced": LINES, "plain": [], "source": "Spicy"}])
    assert main.state.synced == LINES


def test_lyrics_without_uri_still_accepted_for_legacy_bridges():
    run([track(NEW),
         {"type": "lyrics", "mode": "synced",
          "synced": LINES, "plain": [], "source": "Spicy"}])
    assert main.state.synced == LINES


def test_track_change_clears_instrumental_gaps():
    run([track(OLD),
         {"type": "lyrics", "track_uri": OLD, "mode": "synced",
          "synced": LINES, "plain": [], "source": "Spicy"}])
    assert main.state.instrumental_gaps, "precondition: OLD has a gap"
    run([track(NEW)])
    assert main.state.instrumental_gaps == []
