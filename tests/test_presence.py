"""Tests for publishing presence when a track has no lyrics.

THE BUG these cover: rpc_loop had exactly one path to set_activity for a
normal playing track, and it sat behind `if not line1: continue`. `line1` is
the current lyric line, so a track with no lyrics — not in Spicy's catalogue,
an instrumental, a failed fetch, a podcast — fell through that `continue` on
every single tick and Statusify published NOTHING to Discord. Not the title,
not the artist, not the album art, not the elapsed timestamps, all of which it
already had in hand.

That made two unrelated-looking symptoms the same bug: "it says 0 lyrics" and
"the Rich Presence isn't working" were one failure reported twice, and the
lyric-side failure was the only one visible in the log.

The dead giveaway was `title_sent`: assigned in three places, read in none.
A title-only publish was clearly intended at some point and lost.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import main


class FakeRPC:
    """Stands in for DiscordRPC, recording what would reach Discord."""

    def __init__(self):
        self._connected = True
        self.activities = []   # (title, artist, lines, art)
        self.clears = 0

    async def set_activity(self, title, artist, lines, art,
                           position_ms=None, duration_ms=None):
        self.activities.append((title, artist, list(lines), art))

    async def clear_activity(self):
        self.clears += 1


@pytest.fixture
def playing(monkeypatch):
    """A track that is playing, with everything except lyrics."""
    st = main.state
    monkeypatch.setattr(st, "artist", "ARTIST", raising=False)
    monkeypatch.setattr(st, "title", "TITLE", raising=False)
    monkeypatch.setattr(st, "album_art", "http://art", raising=False)
    monkeypatch.setattr(st, "track_uri", "spotify:track:aaaaaaaaaaaaaaaaaaaaaa",
                        raising=False)
    monkeypatch.setattr(st, "position_ms", 5_000, raising=False)
    monkeypatch.setattr(st, "duration_ms", 200_000, raising=False)
    monkeypatch.setattr(st, "is_playing", True, raising=False)
    monkeypatch.setattr(st, "blacklisted", False, raising=False)
    monkeypatch.setattr(st, "lyrics_mode", "none", raising=False)
    monkeypatch.setattr(st, "synced", [], raising=False)
    monkeypatch.setattr(st, "plain", [], raising=False)
    monkeypatch.setattr(st, "instrumental_gaps", [], raising=False)
    monkeypatch.setattr(main, "_rpc_enabled", True, raising=False)
    return st


async def _drive(rpc, seconds=2.2):
    """Run rpc_loop long enough to clear the 1.5s calibration gate."""
    task = asyncio.ensure_future(main.rpc_loop(rpc))
    await asyncio.sleep(seconds)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


class TestPresenceWithoutLyrics:
    def test_a_track_with_no_lyrics_still_reaches_discord(self, playing):
        """The regression guard. Empty lyrics must not mean empty presence."""
        rpc = FakeRPC()
        asyncio.run(_drive(rpc))
        assert rpc.activities, (
            "no lyrics meant no presence at all — title, artist and art were "
            "available and never published"
        )

    def test_title_only_presence_carries_title_artist_and_art(self, playing):
        rpc = FakeRPC()
        asyncio.run(_drive(rpc))
        title, artist, lines, art = rpc.activities[0]
        assert title == "TITLE"
        assert artist == "ARTIST"
        assert art == "http://art"

    def test_title_only_presence_is_published_once_not_every_tick(self, playing):
        """rpc_loop ticks every 50ms. A title-only publish must not spam
        Discord — that is what the rate limiter exists to prevent, and
        re-sending an identical activity 20x/second would burn the budget."""
        rpc = FakeRPC()
        asyncio.run(_drive(rpc, seconds=2.6))
        assert len(rpc.activities) == 1, (
            f"published {len(rpc.activities)} times; expected exactly one"
        )

    def test_lyrics_arriving_later_supersede_the_title_only_presence(self, playing):
        """Lyrics often land a second or two after the track change. When they
        do, the lyric line must take over from the title-only placeholder."""
        rpc = FakeRPC()

        async def run():
            task = asyncio.ensure_future(main.rpc_loop(rpc))
            await asyncio.sleep(2.2)          # title-only publish happens here
            main.state.lyrics_mode = "synced"
            main.state.synced = [{"startMs": 0, "words": "FIRST LINE"}]
            await asyncio.sleep(0.5)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(run())
        published = [lines for _, _, lines, _ in rpc.activities]
        assert any("FIRST LINE" in l for lines in published for l in lines), (
            f"lyrics never superseded the placeholder; published {published}"
        )


class TestPresenceStillSuppressedWhenItShouldBe:
    """The fix must not resurrect presence in the cases that deliberately
    publish nothing."""

    def test_blacklisted_track_publishes_no_activity(self, playing, monkeypatch):
        monkeypatch.setattr(main.state, "blacklisted", True, raising=False)
        rpc = FakeRPC()
        asyncio.run(_drive(rpc))
        assert rpc.activities == []

    def test_untitled_track_publishes_no_activity(self, playing, monkeypatch):
        monkeypatch.setattr(main.state, "title", "", raising=False)
        rpc = FakeRPC()
        asyncio.run(_drive(rpc))
        assert rpc.activities == []

    def test_rpc_disabled_publishes_no_activity(self, playing, monkeypatch):
        monkeypatch.setattr(main, "_rpc_enabled", False, raising=False)
        rpc = FakeRPC()
        asyncio.run(_drive(rpc))
        assert rpc.activities == []
