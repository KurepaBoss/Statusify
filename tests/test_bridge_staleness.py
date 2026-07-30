"""Tests for detecting a stale Spicetify bridge inside Spotify.

THE BUG these cover: Spicetify has two copies of every extension.

  source:   %APPDATA%\\spicetify\\Extensions\\lyrics-bridge.js
  injected: %APPDATA%\\Spotify\\Apps\\xpui\\extensions\\lyrics-bridge.js

`spicetify apply` copies source -> injected. Spotify only ever runs the
injected one. _install_bridge() wrote the source and then compared the source
against the folder it had just written to — always equal — so its staleness
check never fired, and it advised "restart Spotify", which cannot help because
restarting re-runs the injected copy.

Observed consequence: Spotify ran a bridge eleven days older than the shipped
one, still pinned to a Spicy Lyrics API version the server had stopped
accepting. Every track returned no lyrics, while the official Spicy Lyrics
panel — applied separately and current — displayed them normally. The user
restarted Spotify and Spicetify repeatedly, exactly as instructed, and it
could never have made a difference.

The check must compare against the INJECTED copy, and must stay quiet when it
cannot tell.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import main


@pytest.fixture
def fake_spicetify(tmp_path, monkeypatch):
    """A stand-in APPDATA with both copies of the bridge."""
    injected_dir = tmp_path / "Spotify" / "Apps" / "xpui" / "extensions"
    injected_dir.mkdir(parents=True)
    injected = injected_dir / "lyrics-bridge.js"

    src_dir = tmp_path / "app"
    src_dir.mkdir()
    src = src_dir / "lyrics-bridge.js"

    monkeypatch.setenv("APPDATA", str(tmp_path))
    # _RES_DIR is where the SHIPPED bridge lives — the same folder as _APP_DIR
    # when running from source, but sys._MEIPASS in a frozen build, which is
    # why the staleness check reads from it rather than the user-data dir.
    # Patch both so the fixture keeps describing one directory.
    monkeypatch.setattr(main, "_APP_DIR", str(src_dir))
    monkeypatch.setattr(main, "_RES_DIR", str(src_dir))
    return src, injected


class TestBridgeNeedsApply:
    def test_identical_copies_need_no_apply(self, fake_spicetify):
        src, injected = fake_spicetify
        src.write_text("const SPICY_VERSION = '6.1.1';")
        injected.write_text("const SPICY_VERSION = '6.1.1';")
        assert main._bridge_needs_apply() is False

    def test_stale_injected_copy_is_detected(self, fake_spicetify):
        """The real-world case: Spotify running an older pinned version."""
        src, injected = fake_spicetify
        src.write_text("const SPICY_VERSION = '6.1.1';")
        injected.write_text("const SPICY_VERSION = '5.19.12';")
        assert main._bridge_needs_apply() is True

    def test_same_size_difference_is_detected(self, fake_spicetify):
        """Content comparison, not size. A version bump can be size-neutral,
        and a size check is what let the original bug hide."""
        src, injected = fake_spicetify
        src.write_text("const V = '6.1.1';")
        injected.write_text("const V = '6.1.2';")
        assert len(src.read_text()) == len(injected.read_text())
        assert main._bridge_needs_apply() is True

    def test_missing_injected_copy_stays_quiet(self, fake_spicetify):
        """Spicetify may not be installed at all. A confident-sounding
        'run spicetify apply' nag would be noise, so report nothing."""
        src, injected = fake_spicetify
        src.write_text("x")
        assert not injected.exists()
        assert main._bridge_needs_apply() is False

    def test_missing_source_stays_quiet(self, fake_spicetify):
        src, injected = fake_spicetify
        injected.write_text("x")
        assert not src.exists()
        assert main._bridge_needs_apply() is False

    def test_no_appdata_stays_quiet(self, fake_spicetify, monkeypatch):
        src, injected = fake_spicetify
        src.write_text("a")
        injected.write_text("b")
        monkeypatch.delenv("APPDATA", raising=False)
        assert main._bridge_needs_apply() is False


class TestInjectedBridgePath:
    def test_points_into_spotifys_xpui_bundle_not_the_source_folder(self, tmp_path,
                                                                    monkeypatch):
        """Guards the distinction the whole bug turned on."""
        monkeypatch.setenv("APPDATA", str(tmp_path))
        p = main._injected_bridge_path()
        assert "Spotify" in p and "xpui" in p
        assert "spicetify" not in p.lower().replace("spotify", "")

    def test_returns_none_without_appdata(self, monkeypatch):
        monkeypatch.delenv("APPDATA", raising=False)
        assert main._injected_bridge_path() is None
