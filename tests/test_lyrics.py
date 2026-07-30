"""Unit tests for the pure lyric helpers.

These import statusify_lyrics directly, so they run on any platform without
tkinter, winreg, or a Discord pipe.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from statusify_lyrics import join_lines, _calc_instrumental_gaps


def _line(ms, words="la"):
    return {"startMs": ms, "words": words}


class TestJoinLines:
    def test_single_line_unchanged(self):
        assert join_lines(["hello"]) == "hello"

    def test_comma_separator_by_default(self):
        assert join_lines(["one", "two"]) == "one, two"

    def test_space_separator_after_terminal_punctuation(self):
        # A line already ending in punctuation shouldn't get a comma glued on.
        for punct in ".!?;,":
            assert join_lines([f"one{punct}", "two"]) == f"one{punct} two"

    def test_three_lines(self):
        assert join_lines(["a", "b", "c"]) == "a, b, c"

    def test_empty_list(self):
        assert join_lines([]) == ""


class TestInstrumentalGaps:
    def test_empty_input_returns_empty(self):
        assert _calc_instrumental_gaps([], 200_000) == []
        assert _calc_instrumental_gaps([_line(0)], 0) == []

    def test_evenly_spaced_lyrics_have_no_gaps(self):
        # Lines every 3s across a 60s song: nothing should qualify as
        # instrumental (no gap reaches the 8s absolute threshold).
        synced = [_line(i * 3000) for i in range(20)]
        assert _calc_instrumental_gaps(synced, 60_000) == []

    def test_long_intro_is_detected(self):
        # 20s of silence before the first line, then dense lyrics.
        synced = [_line(20_000 + i * 2000) for i in range(20)]
        gaps = _calc_instrumental_gaps(synced, 80_000)
        intro = [g for g in gaps if g["key"] == -2]
        assert len(intro) == 1
        assert intro[0]["startMs"] == 0
        assert intro[0]["endMs"] == 20_000

    def test_mid_song_gap_is_detected(self):
        # Dense lyrics, one 30s hole in the middle, then dense again.
        synced = [_line(i * 2000) for i in range(10)]          # 0..18s
        synced += [_line(48_000 + i * 2000) for i in range(10)]  # 48s..66s
        gaps = _calc_instrumental_gaps(synced, 90_000)
        mids = [g for g in gaps if g["key"] >= 0]
        assert mids, "expected the 30s hole to be flagged"
        assert any(g["gap_ms"] >= 29_000 for g in mids)

    def test_results_are_sorted_by_start(self):
        synced = [_line(15_000)] + [_line(15_000 + 2000 * i) for i in range(1, 10)]
        synced += [_line(70_000 + 2000 * i) for i in range(10)]
        gaps = _calc_instrumental_gaps(synced, 140_000)
        starts = [g["startMs"] for g in gaps]
        assert starts == sorted(starts)

    def test_gap_windows_are_well_formed(self):
        synced = [_line(i * 2000) for i in range(10)]
        synced += [_line(60_000 + i * 2000) for i in range(10)]
        for g in _calc_instrumental_gaps(synced, 120_000):
            assert g["endMs"] > g["startMs"], g
            assert g["gap_ms"] > 0, g

    def test_no_crash_on_single_line(self):
        # Regression guard: indexing synced[-1] / synced[0] on a 1-element
        # list used to be the kind of thing that raised inside the WS handler
        # and silently killed the backend thread.
        assert isinstance(_calc_instrumental_gaps([_line(1000)], 200_000), list)
