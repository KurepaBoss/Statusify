"""Tests for global hotkeys (statusify_hotkeys).

THE BUGS these cover:

1. Held keys fired the action on every auto-repeat. The `keyboard` library's
   add_hotkey triggers on each repeated key-down, so holding Ctrl+Alt+N a
   fraction too long skipped a run of tracks — the log shows eight
   "Hotkey: skip track" lines inside one second on 2026-09-13.

2. Hotkeys died whenever an elevated or protected window (most games) had
   focus. `keyboard` works through a WH_KEYBOARD_LL hook, and Windows' UIPI
   withholds low-level hook events from a lower-integrity process while a
   higher-integrity window is in the foreground. The only workaround was to
   run Statusify itself as administrator.

RegisterHotKey fixes both: the system itself matches the combo and posts
WM_HOTKEY regardless of which window is focused, and MOD_NOREPEAT suppresses
auto-repeat.
"""
import ctypes
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import statusify_hotkeys as hk

win = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")


# ── parse_combo ───────────────────────────────────────────────────

def test_parse_basic_combo():
    mods, vk = hk.parse_combo("ctrl+alt+n")
    assert mods & hk.MOD_CONTROL and mods & hk.MOD_ALT
    assert not mods & hk.MOD_SHIFT
    assert vk == ord("N")


def test_parse_always_sets_norepeat():
    mods, _ = hk.parse_combo("ctrl+alt+s")
    assert mods & hk.MOD_NOREPEAT


@pytest.mark.parametrize("combo,vk", [
    ("ctrl+shift+f9", 0x78), ("alt+5", ord("5")), ("win+space", 0x20),
    ("Ctrl + Alt + Page Down", 0x22), ("control+alt+left", 0x25),
    ("ctrl+alt+num 3", 0x63),
])
def test_parse_named_keys(combo, vk):
    assert hk.parse_combo(combo)[1] == vk


@pytest.mark.parametrize("bad", ["", "ctrl+alt", "ctrl+alt+n+m", "ctrl+banana", "n"])
def test_parse_rejects_bad(bad):
    with pytest.raises(ValueError):
        hk.parse_combo(bad)


# ── Live registration against Windows ─────────────────────────────

user32 = ctypes.windll.user32 if sys.platform == "win32" else None
KEYUP = 0x0002
VK_CONTROL, VK_MENU, VK_SHIFT, VK_F24 = 0x11, 0x12, 0x10, 0x87
COMBO = "ctrl+alt+shift+f24"   # no keyboard has F24; can't clash with real use


def _press(vk, up=False):
    user32.keybd_event(vk, 0, KEYUP if up else 0, 0)


def _wait_for(cond, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


@pytest.fixture
def mgr():
    m = hk.HotkeyManager()
    yield m
    m.stop()


@win
def test_hotkey_fires_once_even_when_held(mgr):
    hits = []
    failed = mgr.set({"x": (COMBO, lambda: hits.append(1))})
    assert failed == {}
    for vk in (VK_CONTROL, VK_MENU, VK_SHIFT):
        _press(vk)
    try:
        for _ in range(6):          # key-down x6 = holding with auto-repeat
            _press(VK_F24)
            time.sleep(0.03)
        _press(VK_F24, up=True)
    finally:
        for vk in (VK_SHIFT, VK_MENU, VK_CONTROL):
            _press(vk, up=True)
    assert _wait_for(lambda: hits)
    time.sleep(0.3)
    assert len(hits) == 1


@win
def test_conflicting_combo_is_reported(mgr):
    other = hk.HotkeyManager()
    try:
        assert other.set({"a": (COMBO, lambda: None)}) == {}
        failed = mgr.set({"b": (COMBO, lambda: None)})
        assert "b" in failed and "use" in failed["b"].lower()
    finally:
        other.stop()


@win
def test_rebinding_releases_the_old_combo(mgr):
    assert mgr.set({"x": (COMBO, lambda: None)}) == {}
    assert mgr.set({"x": ("ctrl+alt+shift+f23", lambda: None)}) == {}
    other = hk.HotkeyManager()
    try:
        assert other.set({"y": (COMBO, lambda: None)}) == {}, "old combo still held"
    finally:
        other.stop()


@win
def test_invalid_combo_reported_not_raised(mgr):
    failed = mgr.set({"x": ("ctrl+banana", lambda: None), "y": ("", lambda: None)})
    assert "x" in failed
    assert "y" not in failed        # empty = hotkey disabled, not an error
