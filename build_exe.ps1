# Build the standalone Statusify.exe locally.
#
#   Right-click -> Run with PowerShell, or:  .\build_exe.ps1
#
# Produces dist\Statusify.exe — a single file with Python and every dependency
# inside it, so the machine that runs it needs no Python installed. This is the
# artifact that goes into a GitHub release; .github/workflows/release.yml runs
# the same spec on a clean runner when you push a version tag.
#
# Not to be confused with build_launcher.ps1, which compiles a 9 KB shim that
# merely launches main.py with the local pythonw.exe. That one is for running
# from source; this one is for people who just want the app.

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

# A dedicated venv keeps PyInstaller and its pinned deps out of the Python you
# actually run Statusify with. It also means the exe is built against a known
# set of packages rather than whatever has accumulated globally.
$venv = Join-Path $PSScriptRoot ".buildenv"
$py   = Join-Path $venv "Scripts\python.exe"

if (-not (Test-Path $py)) {
    Write-Host "Creating build environment in .buildenv ..." -ForegroundColor Cyan
    # 3.12 is the version the release workflow uses. Building on a much newer
    # interpreter is usually fine but is not what gets tested in CI.
    $launcher = (Get-Command py -ErrorAction SilentlyContinue)
    if ($launcher) { & py -3.12 -m venv $venv } else { & python -m venv $venv }
    if (-not (Test-Path $py)) { throw "Could not create the build venv. Is Python 3.12 installed?" }
}

Write-Host "Installing build dependencies ..." -ForegroundColor Cyan
& $py -m pip install --upgrade pip --quiet
& $py -m pip install --quiet pyinstaller -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "Dependency install failed." }

Write-Host "Building Statusify.exe ..." -ForegroundColor Cyan
& $py -m PyInstaller --clean --noconfirm --log-level WARN Statusify.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }

$exe = Join-Path $PSScriptRoot "dist\Statusify.exe"
if (-not (Test-Path $exe)) { throw "Build reported success but dist\Statusify.exe is missing." }

$size = [math]::Round((Get-Item $exe).Length / 1MB, 1)
$hash = (Get-FileHash $exe -Algorithm SHA256).Hash.ToLower()

Write-Host ""
Write-Host "SUCCESS: $exe ($size MB)" -ForegroundColor Green
Write-Host "SHA256:  $hash" -ForegroundColor Gray
Write-Host ""
Write-Host "The exe keeps its settings, history and logs in whatever folder it" -ForegroundColor Yellow
Write-Host "is run from, so put it somewhere permanent before first launch." -ForegroundColor Yellow

# Hold the window open when double-clicked, but not when the script is piped or
# run from CI — Read-Host against a redirected stdin fails, and with
# $ErrorActionPreference = "Stop" that would report a successful build as an error.
if (-not [Console]::IsInputRedirected) { Read-Host "Press Enter to exit" }
