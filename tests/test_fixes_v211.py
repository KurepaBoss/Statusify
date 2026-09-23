"""v2.1.1 fixes: garbled config text, volume/toggles snapping back."""
import configparser
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main
import statusify_config as cfgmod


def test_unmojibake_undoes_layers_and_leaves_real_text():
    twice = "Ã°Å¸Å½Âµ"
    assert cfgmod.unmojibake("ðŸŽµ â”€ â”€ ðŸŽµ") == "🎵 ─ ─ 🎵"
    assert cfgmod.unmojibake(twice) == "🎵"
    for real in ("Beyoncé", "Motörhead", "plain", "日本語", ""):
        assert cfgmod.unmojibake(real) == real


def test_config_is_read_as_utf8_and_repaired(tmp_path, monkeypatch):
    p = tmp_path / "s.cfg"
    p.write_text("[preferences]\ninstrumental_text = ðŸŽµ â”€ ðŸŽµ\nname = Beyoncé\n",
                 encoding="utf-8")
    monkeypatch.setattr(cfgmod, "_CONFIG_PATH", str(p))
    monkeypatch.setattr(cfgmod, "_CFG_CACHE", None)
    cfg = cfgmod._load_config()
    assert cfg.get("preferences", "instrumental_text") == "🎵 ─ 🎵"
    assert cfg.get("preferences", "name") == "Beyoncé"
    # Written back repaired, and stable across another read.
    monkeypatch.setattr(cfgmod, "_CFG_CACHE", None)
    assert cfgmod._load_config().get("preferences", "instrumental_text") == "🎵 ─ 🎵"
    assert "🎵" in p.read_text(encoding="utf-8")


def test_stale_player_state_does_not_undo_a_volume_change(monkeypatch):
    monkeypatch.setattr(main, "_send_bridge", lambda obj: True)
    monkeypatch.setattr(main, "_OPTIMISTIC", {})
    main.state.volume = 0.5
    assert main.set_volume(0.55)
    # The bridge's follow-up report still carries the old value.
    assert main._merge_reported("volume", 0.5) == 0.55
    # Once Spotify reports the new value the hold ends...
    assert main._merge_reported("volume", 0.55) == 0.55
    # ...and later external changes (Spotify's own slider) are taken as-is.
    assert main._merge_reported("volume", 0.2) == 0.2


def test_hold_expires(monkeypatch):
    monkeypatch.setattr(main, "_OPTIMISTIC", {})
    main._hold("shuffle", True)
    assert main._merge_reported("shuffle", False) is True
    main._OPTIMISTIC["shuffle"] = (True, time.monotonic() - 1)
    assert main._merge_reported("shuffle", False) is False


def test_stats_glide_finishes(monkeypatch):
    import tkinter as tk
    monkeypatch.setattr(main, "_register_hotkeys", lambda *a, **k: None)
    a = main.App()
    try:
        a._build_deferred_pages()
        a._show("STATS")
        a._root.geometry("540x720"); a._root.update()
        a._st_total = 3000
        a.st_cv.configure(scrollregion=(0, 0, 540, 3000))
        for target in (301, 300, 2):
            a._st_scroll_to(target)
            t0 = time.monotonic()
            while a._st_gliding and time.monotonic() - t0 < 2:
                a._root.update(); time.sleep(0.01)
            assert not a._st_gliding
            assert abs(a.st_cv.canvasy(0) - target) <= 1
    finally:
        a._root.destroy()
        import gc; gc.collect()
