"""Tests for lyric-line selection and per-track offset resolution.

THE BUG these cover: Statusify has two offset settings — a global "lyric
delay" and a per-track offset ("Offset for this track" in Settings). Only the
instrumental-gap position in rpc_loop ever consulted the per-track value.
Every helper that actually decided WHICH LINE to publish — get_current_line,
_cur_idx, get_line_dur, get_nth — added the raw global LYRIC_DELAY_MS
instead. So nudging a track's offset moved the 'instrumental' markers and left
the lyrics themselves exactly where they were: the feature silently did
nothing, which is indistinguishable from "my lyrics are still out of sync".

The selection logic now lives in statusify_lyrics.select_line and takes an
already-offset position, so the offset can no longer be quietly dropped on
the way in.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import configparser

from statusify_lyrics import offset_key, resolve_offset_ms, select_line


def _line(ms, words):
    return {"startMs": ms, "words": words}


# A four-line stand-in for a synced lyric sheet, one line every 10s.
SYNCED = [_line(0, "one"), _line(10_000, "two"),
          _line(20_000, "three"), _line(30_000, "four")]
DURATION = 40_000


def sel(pos, synced=SYNCED, mode="synced", plain=None, duration=DURATION):
    return select_line(mode, synced, plain or [], pos, duration)


class TestOffsetKey:
    """Per-track offsets are stored in the [offsets] section of statusify.cfg.

    They used to be keyed by the raw Spotify URI, which contains ':' — one of
    configparser's key/value delimiters. See offset_key's docstring for the
    two different ways that broke depending on Python version.
    """

    URI = "spotify:track:4cOdK2wGLETKBW3PvgPWqT"

    def test_extracts_the_bare_track_id(self):
        assert offset_key(self.URI) == "4cOdK2wGLETKBW3PvgPWqT"

    def test_key_contains_no_config_delimiter(self):
        assert ":" not in offset_key(self.URI)
        assert "=" not in offset_key(self.URI)

    def test_empty_uri_is_handled(self):
        assert offset_key("") == ""
        assert offset_key(None) == ""

    def test_bare_id_passes_through_unchanged(self):
        assert offset_key("4cOdK2wGLETKBW3PvgPWqT") == "4cOdK2wGLETKBW3PvgPWqT"

    def test_distinct_tracks_get_distinct_keys(self):
        a = offset_key("spotify:track:AAAAAAAAAAAAAAAAAAAAAA")
        b = offset_key("spotify:track:BBBBBBBBBBBBBBBBBBBBBB")
        assert a != b

    def test_key_survives_a_real_configparser_round_trip(self, tmp_path):
        """The end-to-end regression guard.

        Writing the raw URI raises InvalidWriteError on Python 3.13+, and on
        older versions reads back as the option "spotify". Either way the
        value is lost; the derived key must round-trip exactly.
        """
        path = tmp_path / "statusify.cfg"
        cfg = configparser.ConfigParser()
        cfg.add_section("offsets")
        cfg.set("offsets", offset_key(self.URI), "-750")
        with open(path, "w", encoding="utf-8") as f:
            cfg.write(f)

        back = configparser.ConfigParser()
        back.read(path)
        raw = back.get("offsets", offset_key(self.URI), fallback="")
        assert resolve_offset_ms(raw, 0) == -750

    def test_raw_uri_would_not_round_trip(self, tmp_path):
        """Demonstrates the original failure, so the fix can't be reverted."""
        cfg = configparser.ConfigParser()
        cfg.add_section("offsets")
        cfg.set("offsets", self.URI, "-750")
        path = tmp_path / "broken.cfg"
        try:
            with open(path, "w", encoding="utf-8") as f:
                cfg.write(f)
        except configparser.Error:
            return  # Python 3.13+: refused outright, which is the bug's newer form

        back = configparser.ConfigParser()
        back.read(path)
        # Older Python: written, but no longer readable under its own name.
        assert back.get("offsets", self.URI, fallback="") != "-750"


class TestResolveOffsetMs:
    def test_missing_override_falls_back_to_global(self):
        assert resolve_offset_ms("", 300) == 300
        assert resolve_offset_ms(None, 300) == 300

    def test_override_wins_over_global(self):
        assert resolve_offset_ms("-750", 300) == -750

    def test_zero_override_is_honoured_not_treated_as_missing(self):
        # "0" is falsy as a string only if you test it wrongly; an explicit
        # zero means "this track needs no offset", NOT "use the global".
        assert resolve_offset_ms("0", 300) == 0

    def test_unparseable_value_falls_back_instead_of_raising(self):
        # statusify.cfg is a plain text file users do edit by hand.
        assert resolve_offset_ms("abc", 300) == 300
        assert resolve_offset_ms("1.5", 300) == 300

    def test_negative_and_positive_round_trip(self):
        for v in (-5000, -250, 0, 250, 5000):
            assert resolve_offset_ms(str(v), 999) == v


class TestSelectLineSynced:
    def test_before_first_line_returns_nothing(self):
        assert sel(-1) == ("", "")

    def test_picks_the_line_whose_start_has_been_reached(self):
        assert sel(0)[0] == "one"
        assert sel(9_999)[0] == "one"
        assert sel(10_000)[0] == "two"
        assert sel(29_999)[0] == "three"

    def test_reports_the_following_line_as_next(self):
        assert sel(0) == ("one", "two")
        assert sel(20_000) == ("three", "four")

    def test_last_line_has_no_next(self):
        assert sel(35_000) == ("four", "")

    def test_next_skips_immediate_repeats(self):
        # A repeated line must not be offered as "next" — grouping would then
        # publish the same text twice in a row.
        synced = [_line(0, "hook"), _line(1000, "hook"), _line(2000, "verse")]
        assert sel(0, synced=synced) == ("hook", "verse")

    def test_empty_synced_returns_nothing(self):
        assert sel(5000, synced=[]) == ("", "")


class TestSelectLineHonoursOffset:
    """The regression guard for the bug in this module's docstring.

    select_line takes an ALREADY-OFFSET position, so these assert the property
    the old code violated: shifting the offset shifts the chosen line.
    """

    def test_positive_offset_advances_to_the_next_line_early(self):
        # 500 ms before line two, with a +1s offset, should already show "two".
        raw_pos = 9_500
        assert sel(raw_pos)[0] == "one"
        assert sel(raw_pos + 1000)[0] == "two"

    def test_negative_offset_holds_the_previous_line(self):
        # 200 ms after line two starts, with -1s, should still show "one".
        raw_pos = 10_200
        assert sel(raw_pos)[0] == "two"
        assert sel(raw_pos - 1000)[0] == "one"

    def test_per_track_offset_changes_the_line_the_global_would_pick(self):
        """End-to-end: the exact scenario the old code got wrong.

        Global delay 0, this track needs -2s. Under the old behaviour the
        per-track value was ignored and the global was used, so both paths
        picked the same line and the setting appeared to do nothing.
        """
        raw_pos = 20_500
        global_ms = 0
        per_track = resolve_offset_ms("-2000", global_ms)

        with_global = sel(raw_pos + resolve_offset_ms("", global_ms))
        with_track  = sel(raw_pos + per_track)

        assert with_global[0] == "three"
        assert with_track[0] == "two"
        assert with_global != with_track, "per-track offset had no effect"


class TestSelectLinePlain:
    PLAIN = ["a", "b", "c", "d"]

    def test_interpolates_across_the_duration(self):
        assert select_line("plain", [], self.PLAIN, 0, 40_000)[0] == "a"
        assert select_line("plain", [], self.PLAIN, 20_000, 40_000)[0] == "c"

    def test_clamps_past_the_end_instead_of_indexing_out_of_range(self):
        assert select_line("plain", [], self.PLAIN, 999_999, 40_000) == ("d", "d")

    def test_clamps_before_the_start(self):
        assert select_line("plain", [], self.PLAIN, -999_999, 40_000)[0] == "a"

    def test_zero_duration_returns_nothing(self):
        # No duration means no way to interpolate — must not divide by zero.
        assert select_line("plain", [], self.PLAIN, 5_000, 0) == ("", "")


class TestSelectLineOtherModes:
    def test_mode_none_returns_nothing(self):
        assert select_line("none", SYNCED, ["a"], 5_000, DURATION) == ("", "")

    def test_synced_mode_with_only_plain_available_returns_nothing(self):
        assert select_line("synced", [], ["a", "b"], 5_000, DURATION) == ("", "")
