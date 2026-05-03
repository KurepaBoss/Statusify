import base64
import os
import threading
import winreg


def startup_lnk_path() -> str:
    return os.path.join(
        os.environ.get("APPDATA", ""),
        r"Microsoft\Windows\Start Menu\Programs\Startup",
        "Statusify.lnk",
    )


def get_startup_enabled() -> bool:
    return os.path.exists(startup_lnk_path())


def cleanup_old_startup() -> None:
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


def set_startup_enabled(enabled: bool, base_dir: str, log) -> None:
    lnk = startup_lnk_path()
    cleanup_old_startup()
    if not enabled:
        try:
            os.remove(lnk)
        except FileNotFoundError:
            pass
        except Exception as e:
            log(f"Startup remove error: {e}")
        return

    def _create():
        try:
            import subprocess
            import sys

            ico = os.path.join(base_dir, "statusify.ico")
            statusify_exe = os.path.join(base_dir, "Statusify.exe")
            main_py = os.path.join(base_dir, "main.py")

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
                f'$lnk.WorkingDirectory= "{base_dir}"',
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
            log("Startup shortcut created")
        except Exception as e:
            log(f"Startup shortcut error: {e}")

    threading.Thread(target=_create, daemon=True).start()
