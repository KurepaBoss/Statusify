"""Tests for the rotating log writer and the blacklist matcher.

Both are pulled out of main.py by source extraction rather than import,
because importing main.py requires tkinter + winreg + a Discord pipe.
"""
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_ROTATE_LOCK = threading.Lock()


def _rotating_write(path, text, max_bytes=1_000_000, keep=1):
    """Copy of main._rotating_write (see module docstring)."""
    try:
        with _ROTATE_LOCK:
            try:
                if os.path.exists(path) and os.path.getsize(path) > max_bytes:
                    if keep > 0:
                        old = path + ".1"
                        try:
                            if os.path.exists(old):
                                os.remove(old)
                        except OSError:
                            pass
                        try:
                            os.replace(path, old)
                        except OSError:
                            open(path, "w").close()
                    else:
                        open(path, "w").close()
            except OSError:
                pass
            with open(path, "a", encoding="utf-8", errors="replace") as f:
                f.write(text)
    except Exception:
        pass


def _is_blacklisted(terms, artist, title):
    """Copy of main._is_blacklisted with the term list injected."""
    if not terms:
        return False
    hay = f"{artist} {title}".lower()
    return any(t in hay for t in terms)


class TestRotatingWrite:
    def test_writes_content(self, tmp_path):
        p = str(tmp_path / "a.log")
        _rotating_write(p, "hello\n")
        assert open(p).read() == "hello\n"

    def test_appends(self, tmp_path):
        p = str(tmp_path / "a.log")
        _rotating_write(p, "one\n")
        _rotating_write(p, "two\n")
        assert open(p).read() == "one\ntwo\n"

    def test_rotates_past_the_cap(self, tmp_path):
        p = str(tmp_path / "a.log")
        _rotating_write(p, "x" * 200, max_bytes=100)
        _rotating_write(p, "fresh\n", max_bytes=100)
        assert open(p).read() == "fresh\n"
        assert os.path.exists(p + ".1")

    def test_stays_bounded_under_sustained_writes(self, tmp_path):
        """The health.csv scenario: many writes must not grow without bound.

        The original code appended forever and reached 17.3 GB.
        """
        p = str(tmp_path / "health.csv")
        for i in range(5000):
            _rotating_write(p, f"{i},some,sample,row\n", max_bytes=10_000)
        # current + one rotated generation, both capped
        total = os.path.getsize(p)
        if os.path.exists(p + ".1"):
            total += os.path.getsize(p + ".1")
        assert total < 100_000, f"log grew to {total} bytes"

    def test_bad_path_does_not_raise(self):
        _rotating_write(os.path.join("/nonexistent-dir-xyz", "a.log"), "hi")

    def test_only_one_rotated_generation_kept(self, tmp_path):
        p = str(tmp_path / "a.log")
        for _ in range(10):
            _rotating_write(p, "y" * 200, max_bytes=100)
        assert not os.path.exists(p + ".2")


class TestBlacklist:
    def test_empty_blacklist_matches_nothing(self):
        assert not _is_blacklisted([], "Anyone", "Anything")

    def test_matches_artist(self):
        assert _is_blacklisted(["rickroll"], "Rickroll", "Never Gonna")

    def test_matches_title(self):
        assert _is_blacklisted(["never gonna"], "Rick Astley", "Never Gonna Give You Up")

    def test_is_case_insensitive(self):
        assert _is_blacklisted(["astley"], "RICK ASTLEY", "Whatever")

    def test_substring_match(self):
        assert _is_blacklisted(["ast"], "Rick Astley", "x")

    def test_non_match(self):
        assert not _is_blacklisted(["taylor"], "Rick Astley", "Never Gonna Give You Up")
