# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build recipe for the downloadable Statusify release.

Build with:  pyinstaller --clean --noconfirm Statusify.spec
Output:      dist/Statusify.exe  (single file, no Python needed on the target)

Two things here are load-bearing and easy to undo by accident:

1. `lyrics-bridge.js` and `statusify.ico` are bundled as DATA, and main.py
   reads them through _RES_DIR (which resolves to sys._MEIPASS when frozen).
   Drop them from `datas` and the app starts fine but can never install the
   Spicetify bridge, so lyrics silently never work.

2. `upx=False`. UPX-packed binaries are a well-known heuristic trigger for
   Windows Defender and friends, and this app already looks suspicious to a
   scanner: it installs a global keyboard hook via `keyboard` and opens a
   local socket. Saving ~8 MB is not worth turning every download into a
   quarantine report.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(SPECPATH))
from version import VERSION

# ── Windows version resource ──────────────────────────────────────
# Gives the exe real metadata in Properties → Details instead of a blank
# panel, which is one of the few things a user can check on an unsigned
# download. Generated from version.py so it cannot drift.
_parts = [int(x) for x in VERSION.split(".")] + [0]
_vtuple = tuple(_parts[:4])

# PyInstaller 6 does not expose WORKPATH to the spec namespace, only SPECPATH,
# so derive the scratch location instead of relying on an undefined global.
_workdir = os.path.join(SPECPATH, "build")
os.makedirs(_workdir, exist_ok=True)
_version_res = os.path.join(_workdir, "statusify_version_info.txt")
with open(_version_res, "w", encoding="utf-8") as fh:
    fh.write(f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={_vtuple},
    prodvers={_vtuple},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904B0',
        [StringStruct('CompanyName', 'KurepaBoss'),
         StringStruct('FileDescription', 'Statusify — Discord Rich Presence & Spotify Lyrics'),
         StringStruct('FileVersion', '{VERSION}'),
         StringStruct('InternalName', 'Statusify'),
         StringStruct('LegalCopyright', 'Copyright (c) 2026 KurepaBoss. MIT Licence.'),
         StringStruct('OriginalFilename', 'Statusify.exe'),
         StringStruct('ProductName', 'Statusify'),
         StringStruct('ProductVersion', '{VERSION}')])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""")

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('lyrics-bridge.js', '.'),
        ('statusify.ico', '.'),
    ],
    hiddenimports=[
        # pystray picks its backend at import time by probing the platform;
        # the static analyser cannot see that choice.
        'pystray._win32',
        # ImageTk locates the Tcl/Tk libraries through this shim.
        'PIL._tkinter_finder',
        # websockets.serve is a lazy attribute on the package, so the real
        # implementation module is invisible to static analysis.
        'websockets.asyncio.server',
        'websockets.exceptions',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Nothing here is imported by Statusify. They are excluded rather than
        # merely absent so a build machine that happens to have them installed
        # produces the same ~40 MB exe as one that does not.
        'numpy', 'matplotlib', 'pandas', 'scipy',
        'PyQt5', 'PyQt6', 'PySide2', 'PySide6',
        'IPython', 'notebook', 'pytest', 'setuptools._distutils',
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Statusify',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,               # see module docstring — do not turn this on
    runtime_tmpdir=None,
    console=False,           # GUI app; a console window would flash on launch
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='statusify.ico',
    version=_version_res,
)
