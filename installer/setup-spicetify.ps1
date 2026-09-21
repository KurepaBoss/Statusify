<#
    Installs Spicetify (if needed) and wires Statusify's lyrics bridge into
    Spotify. Run by Statusify-Setup.exe; also safe to run by hand, and safe to
    re-run — every step checks before it acts, so it doubles as a repair tool
    after a Spotify update wipes Spicetify.

    Usage:  setup-spicetify.ps1 -Bridge <path\to\lyrics-bridge.js>
            setup-spicetify.ps1 -Uninstall
            -InstallDir <dir>   put Spicetify somewhere other than
                                %LOCALAPPDATA%\spicetify (PATH is then left
                                alone, and an existing install is not reused)

    Must run UNELEVATED. Spicetify refuses to run as admin, and it patches the
    per-user Spotify install in %APPDATA%, so an elevated run would either fail
    or patch the wrong profile.
#>
param(
    [string]$Bridge,
    [switch]$Uninstall,
    [switch]$NoPause,
    [string]$InstallDir
)

$ErrorActionPreference = "Stop"
$ProgressPreference    = "SilentlyContinue"   # Invoke-WebRequest is 10x slower with the progress bar
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$BridgeName  = "lyrics-bridge.js"
$SpiceDir    = if ($InstallDir) { $InstallDir } else { Join-Path $env:LOCALAPPDATA "spicetify" }
$SpiceExe    = Join-Path $SpiceDir "spicetify.exe"
# Respect an existing install elsewhere (Scoop, winget, a custom folder)
# rather than dropping a second copy into LOCALAPPDATA.
$onPath = Get-Command spicetify -ErrorAction SilentlyContinue
if (-not $InstallDir -and $onPath -and -not (Test-Path $SpiceExe)) { $SpiceExe = $onPath.Source }
$SpiceCfgDir = Join-Path $env:APPDATA "spicetify"
$ExtDir      = Join-Path $SpiceCfgDir "Extensions"
$CfgFile     = Join-Path $SpiceCfgDir "config-xpui.ini"

function Step($msg) { Write-Host "`n> $msg" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "  OK  $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "  !!  $msg" -ForegroundColor Yellow }

function Finish([int]$code, [string]$msg) {
    if ($msg) {
        if ($code -eq 0) { Write-Host "`n$msg" -ForegroundColor Green }
        else             { Write-Host "`n$msg" -ForegroundColor Red }
    }
    if (-not $NoPause) { Write-Host "`nPress any key to close..."; [void][Console]::ReadKey($true) }
    exit $code
}

function Spice {
    # Run spicetify, echo its output, and return it for inspection.
    # Spicetify writes progress to stderr; under Windows PowerShell 5.1 with
    # ErrorActionPreference=Stop that turns into a terminating error, so relax
    # it here and flatten the ErrorRecords back into plain text.
    $ErrorActionPreference = "Continue"
    $lines = & $SpiceExe @args 2>&1 | ForEach-Object {
        # Strip ANSI colour codes, drop the empty RemoteException records
        # PowerShell wraps blank stderr lines in, and drop spinner frames.
        ("$_" -replace "\x1b\[[0-9;]*m", "").TrimEnd()
    } | Where-Object {
        $_ -and $_ -ne "System.Management.Automation.RemoteException" -and $_ -notmatch '^\s*[-\\|/] '
    }
    $out = $lines | Out-String
    if ($out.Trim()) { Write-Host ($out.TrimEnd() -replace '(?m)^\s*', '      ') }
    return $out
}

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return (New-Object Security.Principal.WindowsPrincipal $id).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

# ── Uninstall: unregister the bridge, leave Spicetify itself alone ──
if ($Uninstall) {
    if ((Test-Path $SpiceExe) -and (Test-Path $CfgFile) -and
        (Select-String -Path $CfgFile -Pattern ([regex]::Escape($BridgeName)) -Quiet)) {
        Step "Removing Statusify's lyrics bridge from Spotify"
        Spice config extensions "$BridgeName-" | Out-Null
        Spice apply | Out-Null
    }
    Remove-Item (Join-Path $ExtDir $BridgeName) -ErrorAction SilentlyContinue
    exit 0
}

Write-Host "Statusify - Spicetify setup" -ForegroundColor White
Write-Host "This connects Spotify to Statusify so it can show synced lyrics."

if (Test-Admin) {
    Finish 1 "This must not run as administrator (Spicetify refuses to). Re-run it from a normal window."
}
if (-not $Bridge -or -not (Test-Path $Bridge)) {
    Finish 1 "Bridge file not found: '$Bridge'"
}

# ── 1. Spotify ──────────────────────────────────────────────────────
Step "Checking Spotify"
if (Get-AppxPackage -Name "SpotifyAB.SpotifyMusic" -ErrorAction SilentlyContinue) {
    Warn "You have the Microsoft Store version of Spotify. Spicetify can't modify it."
    Warn "Uninstall it, install Spotify from https://www.spotify.com/download/windows/,"
    Warn "open it once and log in, then run this setup again."
    Finish 2 "Setup stopped: Microsoft Store Spotify is not supported."
}
$SpotifyExe = Join-Path $env:APPDATA "Spotify\Spotify.exe"
if (-not (Test-Path $SpotifyExe)) {
    Warn "Spotify isn't installed (looked for $SpotifyExe)."
    Warn "Install it from https://www.spotify.com/download/windows/, open it once and log in,"
    Warn "then run this setup again."
    Finish 2 "Setup stopped: Spotify not found."
}
if (-not (Test-Path (Join-Path $env:APPDATA "Spotify\prefs"))) {
    Warn "Spotify has never been opened. Open it once, log in, then run this again."
    Finish 2 "Setup stopped: Spotify hasn't been run yet."
}
Ok "Spotify desktop found"

# ── 2. Spicetify CLI ────────────────────────────────────────────────
Step "Checking Spicetify"
if (Test-Path $SpiceExe) {
    Ok "Spicetify already installed ($((& $SpiceExe -v 2>&1 | Out-String).Trim()))"
} else {
    # Same source and layout as Spicetify's official install.ps1, minus its
    # interactive Marketplace prompt (which would hang a scripted install).
    Write-Host "  Downloading the latest Spicetify release from GitHub..."
    $rel   = Invoke-RestMethod "https://api.github.com/repos/spicetify/cli/releases/latest" `
                               -Headers @{ "User-Agent" = "Statusify-Setup" }
    $arch  = if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") { "arm64" } else { "x64" }
    $asset = $rel.assets | Where-Object { $_.name -like "spicetify-*-windows-$arch.zip" } | Select-Object -First 1
    if (-not $asset) { Finish 1 "Couldn't find a Windows $arch build in Spicetify release $($rel.tag_name)." }
    $zip = Join-Path $env:TEMP $asset.name
    Invoke-WebRequest $asset.browser_download_url -OutFile $zip -UseBasicParsing
    New-Item -ItemType Directory -Force $SpiceDir | Out-Null
    Expand-Archive $zip -DestinationPath $SpiceDir -Force
    Remove-Item $zip -ErrorAction SilentlyContinue

    $userPath = [Environment]::GetEnvironmentVariable("PATH", "User")
    if (-not $InstallDir -and ($userPath -split ";") -notcontains $SpiceDir) {
        [Environment]::SetEnvironmentVariable("PATH", "$userPath;$SpiceDir".TrimStart(";"), "User")
    }
    Ok "Installed Spicetify $($rel.tag_name)"
}

# First run generates config-xpui.ini with Spotify's paths auto-detected.
if (-not (Test-Path $CfgFile)) { Spice config | Out-Null }
if (-not (Test-Path $CfgFile)) { Finish 1 "Spicetify didn't create its config file ($CfgFile)." }

# ── 3. Bridge ───────────────────────────────────────────────────────
Step "Installing the Statusify lyrics bridge"
New-Item -ItemType Directory -Force $ExtDir | Out-Null
Copy-Item $Bridge (Join-Path $ExtDir $BridgeName) -Force
if (Select-String -Path $CfgFile -Pattern ([regex]::Escape($BridgeName)) -Quiet) {
    Ok "Bridge updated (already registered)"
} else {
    Spice config extensions $BridgeName | Out-Null
    Ok "Bridge registered"
}

# ── 4. Apply ────────────────────────────────────────────────────────
Step "Applying to Spotify (Spotify will restart)"
# `apply` needs a backup of the pristine Spotify files. A fresh install has
# none, and a Spotify self-update invalidates the old one — both are fixed by
# taking a new backup first.
$out = Spice apply
if ($out -match "(?i)(error|warn\w*)" -and $out -match "(?i)backup|version mismatch") {
    Warn "Spotify needs a fresh backup first (normal after a new install or a Spotify update)"
    $out = Spice restore backup apply
    if ($out -match "(?i)error") { $out = Spice backup apply }
}
if ($out -match "(?i)\berror\b") {
    Finish 1 "Spicetify reported an error (see above). Try running this setup again with Spotify closed."
}

$injected = Join-Path $env:APPDATA "Spotify\Apps\xpui\extensions\$BridgeName"
if ((Test-Path $injected) -and
    (Get-FileHash $injected).Hash -eq (Get-FileHash $Bridge).Hash) {
    Finish 0 "All done. Spotify now has the Statusify bridge - start Statusify and play a song."
}
Finish 1 "Apply finished, but the bridge isn't inside Spotify yet. Close Spotify completely and run this again."
