@echo off
REM %~dp0 is this script's own folder. The path used to be hardcoded to a
REM machine-specific directory that no longer exists, so run.bat failed for
REM anyone who moved, copied or cloned the app.
cd /d "%~dp0"
python main.py > output.log 2>&1
