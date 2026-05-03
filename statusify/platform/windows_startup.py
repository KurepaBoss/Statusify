"""Windows-only startup-at-login integration for Statusify."""

import base64
import os
import subprocess
import sys
import threading
import winreg


def _startup_lnk_path() -> str:
    startup = os.path.join(
        os.environ.get("APPDATA", ""),
        r"Microsoft\Windows\Start Menu\Programs\Startup",
        "Statusify.lnk",
    )
    return startup


def get_startup_enabled() -> bool:
    return os.path.exists(_startup_lnk_path())


def _cleanup_old_startup() -> None:
    """Remove any leftover registry Run key entries from previous versions."""
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_SET_VALUE,
        )
        try:
            winreg.DeleteValue(key, "Statusify")
        except FileNotFoundError:
            pass
        winreg.CloseKey(key)
    except Exception:
        pass


def set_startup_enabled(enabled: bool, *, log_func=print) -> None:
    lnk = _startup_lnk_path()
    _cleanup_old_startup()
    if not enabled:
        try:
            os.remove(lnk)
        except FileNotFoundError:
            pass
        except Exception as e:
            log_func(f"Startup remove error: {e}")
        return

    def _create() -> None:
        try:
            script_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            ico = os.path.join(script_dir, "statusify.ico")
            statusify_exe = os.path.join(script_dir, "Statusify.exe")
            main_py = os.path.join(script_dir, "main.py")

            if os.path.exists(statusify_exe):
                target = statusify_exe
                args = ""
            else:
                pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
                if not os.path.exists(pythonw):
                    pythonw = sys.executable.replace("python.exe", "pythonw.exe")
                target = pythonw
                args = f'"{main_py}"'

            ps_lines = [
                '$ws  = New-Object -ComObject WScript.Shell',
                f'$lnk = $ws.CreateShortcut("{lnk}")',
                f'$lnk.TargetPath      = "{target}"',
                f'$lnk.Arguments       = "{args}"',
                f'$lnk.WorkingDirectory= "{script_dir}"',
                '$lnk.Description     = "Statusify"',
                f'$lnk.IconLocation    = "{ico},0"',
                '$lnk.WindowStyle     = 7',
                '$lnk.Save()',
            ]
            ps_script = "\r\n".join(ps_lines)
            encoded = base64.b64encode(ps_script.encode("utf-16-le")).decode("ascii")

            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 0
            subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-NonInteractive",
                    "-WindowStyle",
                    "Hidden",
                    "-EncodedCommand",
                    encoded,
                ],
                capture_output=True,
                timeout=15,
                startupinfo=si,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            log_func("Startup shortcut created")
        except Exception as e:
            log_func(f"Startup shortcut error: {e}")

    threading.Thread(target=_create, daemon=True).start()
