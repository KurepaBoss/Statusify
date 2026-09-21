"""Tests for in-app update and bridge repair (statusify_maintenance).

Before this, an available update only opened the releases page in a browser,
and a stale Spicetify bridge only produced a warning telling the user to run
`spicetify apply` in a terminal — both dead ends for anyone who installed
with Setup.exe and has never opened a terminal.

The update path downloads the release's Setup.exe and REFUSES to run it unless
its SHA-256 matches the checksum file published alongside it.
"""
import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import statusify_maintenance as mt


def _file_url(p):
    return "file:///" + str(p).replace("\\", "/")


RELEASE = {
    "tag_name": "v1.4.0",
    "assets": [
        {"name": "Statusify.exe", "browser_download_url": "https://x/Statusify.exe"},
        {"name": "Statusify-Setup-1.4.0.exe.sha256", "browser_download_url": "https://x/s.sha256"},
        {"name": "Statusify-Setup-1.4.0.exe", "browser_download_url": "https://x/setup.exe"},
    ],
}


def test_setup_asset_found():
    assert mt.setup_asset(RELEASE) == ("https://x/setup.exe", "https://x/s.sha256")


def test_setup_asset_missing_checksum_means_no_auto_update():
    rel = {"tag_name": "v1.4.0", "assets": [RELEASE["assets"][2]]}
    assert mt.setup_asset(rel) is None


def test_setup_asset_ignores_other_versions():
    rel = {"tag_name": "v1.4.0", "assets": [
        {"name": "Statusify-Setup-1.3.0.exe", "browser_download_url": "a"},
        {"name": "Statusify-Setup-1.3.0.exe.sha256", "browser_download_url": "b"}]}
    assert mt.setup_asset(rel) is None


@pytest.fixture
def published(tmp_path):
    payload = b"MZ fake installer bytes"
    exe = tmp_path / "Statusify-Setup-1.4.0.exe"
    exe.write_bytes(payload)
    sha = tmp_path / "Statusify-Setup-1.4.0.exe.sha256"
    sha.write_text(hashlib.sha256(payload).hexdigest() + "  Statusify-Setup-1.4.0.exe")
    return exe, sha


def test_download_verified_accepts_matching_hash(tmp_path, published):
    exe, sha = published
    out = mt.download_verified(_file_url(exe), _file_url(sha), tmp_path / "dl")
    assert out.read_bytes() == exe.read_bytes()


def test_download_verified_rejects_tampered_file(tmp_path, published):
    exe, sha = published
    sha.write_text("0" * 64 + "  Statusify-Setup-1.4.0.exe")
    with pytest.raises(mt.ChecksumMismatch):
        mt.download_verified(_file_url(exe), _file_url(sha), tmp_path / "dl")
    assert not any((tmp_path / "dl").glob("*.exe")), "unverified exe left on disk"


def test_repair_files_from_source_checkout(tmp_path):
    (tmp_path / "installer").mkdir()
    (tmp_path / "installer" / "setup-spicetify.ps1").write_text("x")
    (tmp_path / "lyrics-bridge.js").write_text("y")
    script, bridge = mt.repair_files(res_dir=str(tmp_path), app_dir=str(tmp_path))
    assert script.endswith("setup-spicetify.ps1") and bridge.endswith("lyrics-bridge.js")


def test_repair_files_from_frozen_bundle(tmp_path):
    res = tmp_path / "meipass"; res.mkdir()
    (res / "setup-spicetify.ps1").write_text("x")
    (res / "lyrics-bridge.js").write_text("y")
    script, bridge = mt.repair_files(res_dir=str(res), app_dir=str(tmp_path / "app"))
    # Copied out of the PyInstaller temp dir, which vanishes when the app exits
    # — possibly while the repair window is still running.
    assert str(res) not in script and os.path.exists(script)
    assert str(res) not in bridge and os.path.exists(bridge)


def test_repair_files_missing(tmp_path):
    assert mt.repair_files(res_dir=str(tmp_path), app_dir=str(tmp_path)) is None


def test_is_installed(tmp_path):
    assert not mt.is_installed(str(tmp_path), frozen=True)
    (tmp_path / "unins000.exe").write_text("")
    assert mt.is_installed(str(tmp_path), frozen=True)
    assert not mt.is_installed(str(tmp_path), frozen=False)
