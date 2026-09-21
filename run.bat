@echo off
REM %~dp0 is this script's own folder. The path used to be hardcoded to a
REM machine-specific directory that no longer exists, so run.bat failed for
REM anyone who moved, copied or cloned the app.
cd /d "%~dp0"
REM Prefer the `py` launcher (defaults to the newest installed Python, e.g.
REM 3.14) over a bare `python`, which may resolve to an older PATH interpreter
REM that doesn't have Statusify's dependencies installed.
where py >nul 2>nul && (py main.py > output.log 2>&1) || (python main.py > output.log 2>&1)
