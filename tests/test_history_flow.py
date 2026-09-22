"""ws_handler <-> history store: plays are committed live, and the lyric cache
gives a track heard before its lyrics before the bridge answers."""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import main
from statusify_history import HistoryStore

URI = "spotify:track:h1st0ryh1st0ryh1st0ry"
LINES = [{"startMs": 1000, "words": "cached one"},
         {"startMs": 20000, "words": "cached two"}]


class FakeWS:
    def __init__(self, msgs):
        self._msgs = [json.dumps(m) for m in msgs]

    async def send(self, data):
        pass

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for m in self._msgs:
            yield m


async def _nosleep(*_a, **_k):
    return None


def track(uri=URI):
    return {"type": "track_change", "artist": "A", "title": "T",
            "track_uri": uri, "album_art": "", "duration_ms": 200_000}


def lyrics(mode, synced, uri=URI):
    return {"type": "lyrics", "track_uri": uri, "mode": mode,
            "synced": synced, "plain": [], "source": "Spicy"}


@pytest.fixture
def store(tmp_path, monkeypatch):
    st = HistoryStore(str(tmp_path / "h.db"))
    monkeypatch.setattr(main, "_HISTORY_STORE", st)
    monkeypatch.setattr(main, "SAVE_HISTORY", True)
    monkeypatch.setattr(main, "history", [])
    monkeypatch.setattr(main, "_current_play", {"id": None, "listen_start": 0.0, "entry": None})
    monkeypatch.setattr(main.asyncio, "sleep", _nosleep)
    monkeypatch.setattr(main, "_maybe_fetch_lrclib", lambda *a, **k: None, raising=False)
    for attr, val in (("track_uri", ""), ("synced", []), ("plain", []),
                      ("instrumental_gaps", []), ("lyrics_mode", "none")):
        monkeypatch.setattr(main.state, attr, val, raising=False)
    yield st
    st.close()


def run(msgs):
    asyncio.run(main.ws_handler(FakeWS(msgs)))


def test_play_is_committed_on_track_change(store):
    run([track()])
    assert store.count() == 1          # before any lyrics, before quit


def test_bridge_lyrics_are_cached(store):
    run([track(), lyrics("synced", LINES)])
    assert store.get_lyrics(URI)[1] == LINES
    assert main.history[-1]["synced"] == LINES


def test_cached_lyrics_apply_immediately(store):
    store.save_lyrics(URI, "synced", LINES, [], "Spicy")
    run([track()])
    assert main.state.lyrics_mode == "synced" and main.state.synced == LINES


def test_failed_fetch_keeps_cached_lyrics(store):
    store.save_lyrics(URI, "synced", LINES, [], "Spicy")
    run([track(), lyrics("none", [])])
    assert main.state.synced == LINES
    assert len(main.history) == 1       # one play, one row


def test_history_off_records_nothing(store, monkeypatch):
    monkeypatch.setattr(main, "SAVE_HISTORY", False)
    run([track(), lyrics("synced", LINES)])
    assert store.count() == 0 and store.get_lyrics(URI) is None
