"""ws_handler <-> history store: plays are committed once really heard (20 s), and the lyric cache
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
    monkeypatch.setattr(main, "_current_play", {"id": None, "listen_start": 0.0, "entry": None,
                                                 "pending": None})
    clock = {"t": 0.0}
    monkeypatch.setattr(main, "_get_listen_time", lambda: clock["t"])
    main._test_clock = clock
    monkeypatch.setattr(main.asyncio, "sleep", _nosleep)
    monkeypatch.setattr(main, "_maybe_fetch_lrclib", lambda *a, **k: None, raising=False)
    for attr, val in (("track_uri", ""), ("synced", []), ("plain", []),
                      ("instrumental_gaps", []), ("lyrics_mode", "none")):
        monkeypatch.setattr(main.state, attr, val, raising=False)
    yield st
    st.close()


def position(playing=True):
    return {"type": "position", "position_ms": 30_000, "duration_ms": 200_000,
            "is_playing": playing}


class Listen:
    """A pseudo-message: advance the listening clock by `s` seconds."""
    def __init__(self, s):
        self.s = s


def run(msgs):
    # Split on Listen markers: each chunk is one bridge session's messages.
    chunk = []
    for m in msgs + [None]:
        if isinstance(m, Listen) or m is None:
            if chunk:
                asyncio.run(main.ws_handler(FakeWS(chunk)))
                chunk = []
            if isinstance(m, Listen):
                main._test_clock["t"] += m.s
        else:
            chunk.append(m)



def test_play_is_committed_after_20s_of_listening(store):
    run([track(), Listen(25), position()])
    assert store.count() == 1          # before any lyrics, before quit


def test_paused_or_skipped_track_is_not_a_play(store):
    run([track(), position(playing=False)])
    assert store.count() == 0 and main.history == []
    run([track("spotify:track:other"), Listen(5), track()])   # skipped after 5 s
    assert store.count() == 0


def test_previous_track_commits_on_track_change(store):
    run([track(), Listen(40), track("spotify:track:next")])
    assert store.count() == 1 and main.history[-1]["track_uri"] == URI


def test_bridge_lyrics_are_cached(store):
    run([track(), lyrics("synced", LINES), Listen(25), position()])
    assert store.get_lyrics(URI)[1] == LINES
    assert main.history[-1]["synced"] == LINES


def test_cached_lyrics_apply_immediately(store):
    store.save_lyrics(URI, "synced", LINES, [], "Spicy")
    run([track()])
    assert main.state.lyrics_mode == "synced" and main.state.synced == LINES


def test_failed_fetch_keeps_cached_lyrics(store):
    store.save_lyrics(URI, "synced", LINES, [], "Spicy")
    run([track(), lyrics("none", []), Listen(25), position()])
    assert main.state.synced == LINES
    assert len(main.history) == 1       # one play, one row


def test_history_off_records_nothing(store, monkeypatch):
    monkeypatch.setattr(main, "SAVE_HISTORY", False)
    run([track(), lyrics("synced", LINES)])
    assert store.count() == 0 and store.get_lyrics(URI) is None
