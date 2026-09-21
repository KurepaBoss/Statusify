"""In-app update and Spicetify-bridge repair.

Two dead ends this replaces (see tests/test_maintenance.py):
  - "Update available" only opened the releases page in a browser.
  - A stale bridge only produced a warning to run `spicetify apply` in a
    terminal, which Setup.exe users have no reason to know how to do.

Kept free of Tk and of main.py's globals so it can be tested directly.
"""
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import urllib.request

UA = {"User-Agent": "Statusify/Updater"}
CREATE_NEW_CONSOLE = 0x00000010
DETACHED = 0x00000008 | 0x00000200   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP


class ChecksumMismatch(Exception):
    pass


# ── Updates ───────────────────────────────────────────────────────

def setup_asset(release):
    """(setup_url, sha256_url) for this release's installer, or None.

    Both must exist: without a published checksum there is nothing to verify
    the download against, so no silent install happens."""
    tag = (release.get("tag_name") or "").lstrip("v")
    want = f"Statusify-Setup-{tag}.exe"
    urls = {a.get("name"): a.get("browser_download_url") for a in release.get("assets", [])}
    if urls.get(want) and urls.get(want + ".sha256"):
        return urls[want], urls[want + ".sha256"]
    return None


def _fetch(url, timeout=30):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read()


def download_verified(url, sha_url, dest_dir):
    """Download url into dest_dir and return its path — only if its SHA-256
    matches the first hex digest in sha_url. Raises ChecksumMismatch (and
    deletes the file) otherwise."""
    os.makedirs(dest_dir, exist_ok=True)
    m = re.search(rb"\b[0-9a-fA-F]{64}\b", _fetch(sha_url))
    if not m:
        raise ChecksumMismatch("checksum file has no SHA-256")
    expected = m.group(0).decode().lower()
    name = os.path.basename(url.split("?")[0]) or "Statusify-Setup.exe"
    path = os.path.join(dest_dir, name)
    data = _fetch(url, timeout=120)
    if hashlib.sha256(data).hexdigest() != expected:
        raise ChecksumMismatch(f"download does not match published checksum {expected[:12]}…")
    with open(path, "wb") as f:
        f.write(data)
    from pathlib import Path
    return Path(path)


def is_installed(app_dir, frozen):
    """True when this is a Setup.exe install (Inno leaves unins000.exe beside
    the app). Portable exes and source checkouts update via the browser."""
    return bool(frozen) and os.path.exists(os.path.join(app_dir, "unins000.exe"))


def launch_silent_update(setup_path):
    """Run the installer silently a few seconds from now, detached, so this
    process can exit first — Setup refuses to run while Statusify holds its
    single-instance mutex. Setup relaunches Statusify when it finishes."""
    cmd = (f"Start-Sleep -Seconds 3; "
           f"& '{setup_path}' /SILENT /SUPPRESSMSGBOXES /NORESTART /CLOSEAPPLICATIONS")
    subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", cmd],
                     creationflags=DETACHED, close_fds=True)


# ── Bridge repair ─────────────────────────────────────────────────

def repair_files(res_dir, app_dir):
    """(script, bridge) paths to run the Spicetify setup with, or None.

    In a frozen build both files live in the PyInstaller temp dir, which is
    deleted when the app exits — possibly mid-repair — so they are copied to a
    stable temp folder first."""
    bridge = os.path.join(res_dir, "lyrics-bridge.js")
    for script in (os.path.join(res_dir, "setup-spicetify.ps1"),
                   os.path.join(app_dir, "installer", "setup-spicetify.ps1")):
        if os.path.exists(script) and os.path.exists(bridge):
            break
    else:
        return None
    if os.path.normcase(os.path.abspath(res_dir)) == os.path.normcase(os.path.abspath(app_dir)):
        return script, bridge
    stage = os.path.join(tempfile.gettempdir(), "statusify-repair")
    os.makedirs(stage, exist_ok=True)
    s2, b2 = os.path.join(stage, "setup-spicetify.ps1"), os.path.join(stage, "lyrics-bridge.js")
    shutil.copyfile(script, s2)
    shutil.copyfile(bridge, b2)
    return s2, b2


def launch_repair(script, bridge):
    """Open the Spicetify setup script in its own visible console window."""
    return subprocess.Popen(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", script, "-Bridge", bridge],
        creationflags=CREATE_NEW_CONSOLE)
