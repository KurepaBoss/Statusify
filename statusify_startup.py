"""The "Start with Windows" shortcut in the user's Startup folder.

Split out of main.py; main calls init() with its folders and logger.
"""
import os
import subprocess
import sys

try:
    import winreg
except ImportError:          # non-Windows: tests import this module anyway
    winreg = None

_APP_DIR = _RES_DIR = ""
_FROZEN = False
log = lambda *_a, **_k: None


def init(app_dir, res_dir, frozen, log_fn=None):
    global _APP_DIR, _RES_DIR, _FROZEN, log
    _APP_DIR, _RES_DIR, _FROZEN = app_dir, res_dir, frozen
    if log_fn is not None:
        log = log_fn


# ── Startup with Windows ──────────────────────────────────────────

def _startup_lnk_path():
    """Path to the Statusify shortcut in the user's Startup folder."""
    startup = os.path.join(os.environ.get("APPDATA", ""),
                           r"Microsoft\Windows\Start Menu\Programs\Startup",
                           "Statusify.lnk")
    return startup

def _get_startup_enabled():
    return os.path.exists(_startup_lnk_path())

def _cleanup_old_startup():
    """Remove any leftover registry Run key entries from previous versions."""
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Run",
                             0, winreg.KEY_SET_VALUE)
        try: winreg.DeleteValue(key, "Statusify")
        except FileNotFoundError: pass
        winreg.CloseKey(key)
    except Exception:
        pass

def _set_startup_enabled(enabled: bool):
    lnk = _startup_lnk_path()
    # Always clean up old registry entry regardless of enable/disable
    _cleanup_old_startup()
    if not enabled:
        try: os.remove(lnk)
        except FileNotFoundError: pass
        except Exception as e: log(f"Startup remove error: {e}")
        return
    try:
        script_dir = _APP_DIR
        ico        = os.path.join(_RES_DIR, "statusify.ico")
        # Use Windows Script Host COM to create a proper .lnk shortcut.
        # Shortcuts show their Description as the name in Task Manager's
        # startup tab.
        statusify_exe = os.path.join(script_dir, "Statusify.exe")
        main_py       = os.path.join(script_dir, "main.py")

        if _FROZEN:
            # We ARE the executable. Point the shortcut at ourselves instead of
            # guessing at a sibling Statusify.exe or a pythonw that the user may
            # not even have — a frozen build is the one case where the target is
            # known exactly. The exe carries its own icon, so use it for that too.
            target = sys.executable
            args   = ""
            ico    = sys.executable
        # Prefer Statusify.exe (compiled launcher — shows correct name+icon in Task Manager)
        # Fall back to pythonw.exe if not yet built
        elif os.path.exists(statusify_exe):
            target = statusify_exe
            args   = ""
        else:
            pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
            if not os.path.exists(pythonw):
                pythonw = sys.executable.replace("python.exe", "pythonw.exe")
            target = pythonw
            args   = f'"{main_py}"'

        # args is "" for the Statusify.exe path. The old template wrapped it
        # unconditionally, producing Arguments = '""' — a literal empty-string
        # argument handed to the launcher on every boot.
        ps = (
            f'$ws = New-Object -ComObject WScript.Shell; '
            f'$lnk = $ws.CreateShortcut("{lnk}"); '
            f'$lnk.TargetPath = "{target}"; '
            f'$lnk.Arguments = \'{args}\'; '
            f'$lnk.WorkingDirectory = "{script_dir}"; '
            f'$lnk.Description = "Statusify"; '
            f'$lnk.IconLocation = "{ico},0"; '
            f'$lnk.WindowStyle = 7; '
            f'$lnk.Save()'
        )
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0  # SW_HIDE
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", ps],
            capture_output=True, timeout=10,
            startupinfo=si,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        log("Startup shortcut created")
    except Exception as e:
        log(f"Startup shortcut error: {e}")
