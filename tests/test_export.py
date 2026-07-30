"""Tests for LRC timestamp formatting and lyric export.

_lrc_timestamp is copied from main.py rather than imported, for the same
reason as test_helpers.py: importing main.py needs tkinter + winreg.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _lrc_timestamp(ms):
    ms = max(0, int(ms))
    minutes, rem = divmod(ms, 60_000)
    seconds, hundredths = divmod(rem, 1000)
    return f"[{minutes:02d}:{seconds:02d}.{hundredths // 10:02d}]"


class TestLrcTimestamp:
    def test_zero(self):
        assert _lrc_timestamp(0) == "[00:00.00]"

    def test_sub_second(self):
        assert _lrc_timestamp(450) == "[00:00.45]"

    def test_seconds(self):
        assert _lrc_timestamp(12_340) == "[00:12.34]"

    def test_minutes_roll_over(self):
        assert _lrc_timestamp(61_000) == "[01:01.00]"

    def test_long_track(self):
        assert _lrc_timestamp(605_000) == "[10:05.00]"

    def test_negative_clamps_to_zero(self):
        # A negative per-track offset could push an early line below zero.
        assert _lrc_timestamp(-500) == "[00:00.00]"

    def test_always_two_digit_hundredths(self):
        # [00:00.5] is malformed LRC; it must be [00:00.05].
        assert _lrc_timestamp(50) == "[00:00.05]"

    def test_is_parseable_by_standard_lrc_regex(self):
        import re
        pattern = re.compile(r"^\[\d{2}:\d{2}\.\d{2}\]$")
        for ms in (0, 1, 999, 1000, 59_999, 60_000, 3_599_999):
            assert pattern.match(_lrc_timestamp(ms)), ms


class TestFilenameSanitising:
    """The export filename strips characters Windows rejects."""

    @staticmethod
    def _safe(artist, title):
        return "".join(
            c for c in f"{artist} - {title}" if c not in '\\/:*?"<>|'
        ).strip()[:120]

    def test_strips_illegal_characters(self):
        out = self._safe('AC/DC', 'Who Made Who?')
        for bad in '\\/:*?"<>|':
            assert bad not in out

    def test_keeps_readable_text(self):
        assert self._safe("Rick Astley", "Never Gonna Give You Up") == \
            "Rick Astley - Never Gonna Give You Up"

    def test_truncates_long_names(self):
        assert len(self._safe("a" * 200, "b" * 200)) <= 120

    def test_unicode_survives(self):
        assert "Björk" in self._safe("Björk", "Jóga")
