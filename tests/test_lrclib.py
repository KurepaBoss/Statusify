"""LRCLIB: third lyrics source, used only when the bridge finds nothing."""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import main
from statusify_lyrics import clean_title, parse_lrc, pick_lrclib

URI = "spotify:track:1rc11b1rc11b1rc11b1r"


def test_parse_lrc_handles_fractions_and_repeated_tags():
    got = parse_lrc("[00:26.93]Uh\n[01:02]x\n[00:10.5][00:20.05]chorus\nno tag\n[ar:Meta]")
    assert [(l["startMs"], l["words"]) for l in got] == [
        (10500, "chorus"), (20050, "chorus"), (26930, "Uh"), (62000, "x")]


def test_clean_title():
    assert clean_title("IFHY (feat. Pharrell)") == "IFHY"
    assert clean_title("Song - Remastered 2011") == "Song"
    assert clean_title("Song [Remix]") == "Song [Remix]"


def _r(duration, synced="", plain="", instrumental=False):
    return {"duration": duration, "syncedLyrics": synced, "plainLyrics": plain,
            "instrumental": instrumental}


def test_rejects_wrong_length_results():
    """A real search for "Money Trees" returned a 2355 s album upload first."""
    assert pick_lrclib([_r(2355, "[00:01.00]wrong")], 386_000) is None


def test_prefers_synced_then_closest_length():
    got = pick_lrclib([_r(386, plain="plain words"),
                       _r(388, "[00:01.00]far"),
                       _r(386.5, "[00:01.00]close")], 386_000)
    assert got == ("synced", [{"startMs": 1000, "words": "close"}], [])


def test_plain_when_no_synced_and_skips_instrumental():
    assert pick_lrclib([_r(200, "[00:01.00]x", instrumental=True)], 200_000) is None
    assert pick_lrclib([_r(200, plain="a\nb")], 200_000) == ("plain", [], ["a", "b"])


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
        # Let the LRCLIB task finish before the handler (and loop) ends.
        while main._LRCLIB_TASKS:
            await asyncio.gather(*list(main._LRCLIB_TASKS))


@pytest.fixture
def playing(monkeypatch):
    async def _nosleep(*_a, **_k):
        return None
    monkeypatch.setattr(main.asyncio, "sleep", _nosleep)
    monkeypatch.setattr(main, "LRCLIB_ENABLED", True)
    monkeypatch.setattr(main, "_LRCLIB_TRIED", set())
    monkeypatch.setattr(main, "_save_history", lambda *a, **k: None)
    monkeypatch.setattr(main, "_HISTORY_STORE", None)
    for attr, val in (("track_uri", ""), ("synced", []), ("plain", []),
                      ("instrumental_gaps", []), ("lyrics_mode", "none")):
        monkeypatch.setattr(main.state, attr, val, raising=False)
    calls = []

    def fake_search(artist, title):
        calls.append((artist, title))
        return [_r(200, "[00:01.00]from lrclib")]
    monkeypatch.setattr(main, "_lrclib_search", fake_search)
    return calls


def _run(msgs):
    asyncio.run(main.ws_handler(FakeWS(msgs)))


TRACK = {"type": "track_change", "artist": "A", "title": "T",
         "track_uri": URI, "album_art": "", "duration_ms": 200_000}
NONE = {"type": "lyrics", "track_uri": URI, "mode": "none",
        "synced": [], "plain": [], "source": "fallback"}


def test_bridge_miss_falls_back_to_lrclib(playing):
    _run([TRACK, NONE, NONE])
    assert main.state.synced == [{"startMs": 1000, "words": "from lrclib"}]
    assert playing == [("A", "T")]          # looked up once, not per miss


def test_bridge_hit_never_queries_lrclib(playing):
    _run([TRACK, dict(NONE, mode="synced", synced=[{"startMs": 0, "words": "x"}])])
    assert playing == []


def test_disabled(playing, monkeypatch):
    monkeypatch.setattr(main, "LRCLIB_ENABLED", False)
    _run([TRACK, NONE])
    assert playing == [] and main.state.synced == []


def test_one_retry_on_server_error(playing, monkeypatch):
    import urllib.error
    n = []

    def flaky(artist, title):
        n.append(1)
        if len(n) == 1:
            raise urllib.error.HTTPError("u", 503, "busy", None, None)
        return [_r(200, "[00:01.00]second try")]
    monkeypatch.setattr(main, "_lrclib_search", flaky)
    _run([TRACK, NONE])
    assert len(n) == 2 and main.state.synced[0]["words"] == "second try"
