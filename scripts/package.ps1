<#
    Builds the Windows installer and lays out the files a GitHub release needs.

        powershell -ExecutionPolicy Bypass -File scripts\package.ps1

    Produces, in release-assets\ (git-ignored):

        Statusify_<version>_x64-setup.exe   the versioned installer. The app's own updater
                                            looks for exactly this name on a release.
        Statusify-Setup.exe                 the same file under a name that never changes, so
                                            ".../releases/latest/download/Statusify-Setup.exe"
                                            always works.
        SHA256SUMS.txt                      sha256sum format; the updater verifies a download
                                            against the line for the versioned name.

    Steps: version check -> npm ci -> npm run build -> `tauri build --bundles nsis`
    -> copy, checksum, verify. The version comes from Cargo.toml, and
    scripts\check-version-sync.mjs refuses to go on if tauri.conf.json,
    package.json or the README badge disagree with it.

    Parameters
      -OutDir       where the three files go (default: release-assets next to this repo's root)
      -TargetDir    cargo's target directory (sets CARGO_TARGET_DIR). Default: whatever
                    CARGO_TARGET_DIR already is, else src-tauri\target.
      -SkipNpmCi    do not run `npm ci`. It is skipped by itself when node_modules is a
                    link to another checkout's (npm ci deletes node_modules first, and
                    through a link that would be the other checkout's).
      -Strict       also fail when the README has no version badge (use for a real release).

    Nothing here signs the installer: it is unsigned, and Windows SmartScreen will say so.
#>
[CmdletBinding()]
param(
    [string]$OutDir,
    [string]$TargetDir,
    [switch]$SkipNpmCi,
    [switch]$Strict
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Step($msg) { Write-Host "`n> $msg" -ForegroundColor Cyan }
function Run([string]$what, [scriptblock]$cmd) {
    & $cmd
    if ($LASTEXITCODE -ne 0) { throw "$what failed (exit code $LASTEXITCODE)" }
}

# ── Version ──────────────────────────────────────────────────────────
Step "Checking the version strings agree"
$syncArgs = @("scripts/check-version-sync.mjs")
if ($Strict) { $syncArgs += "--strict" }
Run "version check" { node @syncArgs }
$Version = (Get-Content (Join-Path $Root "src-tauri/tauri.conf.json") -Raw | ConvertFrom-Json).version
if ($Version -notmatch '^\d+\.\d+\.\d+$') { throw "tauri.conf.json version '$Version' is not MAJOR.MINOR.PATCH" }
Write-Host "  version $Version"

# ── Where things go ──────────────────────────────────────────────────
if ($TargetDir) { $env:CARGO_TARGET_DIR = $TargetDir }
$CargoTarget = if ($env:CARGO_TARGET_DIR) { $env:CARGO_TARGET_DIR } else { Join-Path $Root "src-tauri\target" }
if (-not $OutDir) { $OutDir = Join-Path $Root "release-assets" }
$VersionedName = "Statusify_${Version}_x64-setup.exe"
$StableName = "Statusify-Setup.exe"
$Built = Join-Path $CargoTarget "release\bundle\nsis\$VersionedName"
Write-Host "  cargo target: $CargoTarget"
Write-Host "  output:       $OutDir"

# ── Dependencies ─────────────────────────────────────────────────────
Step "Installing npm dependencies"
$nm = Join-Path $Root "node_modules"
$linked = (Test-Path $nm) -and ((Get-Item $nm -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)
if ($SkipNpmCi) {
    Write-Host "  skipped (-SkipNpmCi)"
} elseif ($linked) {
    Write-Host "  skipped: node_modules is a link to another checkout, and npm ci would delete what it points to"
} else {
    Run "npm ci" { npm ci }
}

# ── Build ────────────────────────────────────────────────────────────
Step "Building the front end (tsc + vite)"
Run "npm run build" { npm run build }

Step "Building the app and the NSIS installer (the Tauri CLI fetches the NSIS tooling the first time)"
if (Test-Path $Built) { Remove-Item $Built -Force }
Run "tauri build" { npx tauri build --bundles nsis }
if (-not (Test-Path $Built)) { throw "Expected the installer at $Built but it is not there." }

# ── Lay out the release files ────────────────────────────────────────
Step "Writing release files"
New-Item -ItemType Directory -Force $OutDir | Out-Null
$Versioned = Join-Path $OutDir $VersionedName
$Stable = Join-Path $OutDir $StableName
Copy-Item $Built $Versioned -Force
Copy-Item $Built $Stable -Force

# sha256sum format ("<hash>  <name>", LF line ends, no BOM), the format the updater reads.
$lines = foreach ($f in @($Versioned, $Stable)) {
    $h = (Get-FileHash $f -Algorithm SHA256).Hash.ToLower()
    "$h  $(Split-Path -Leaf $f)"
}
$sums = Join-Path $OutDir "SHA256SUMS.txt"
[IO.File]::WriteAllText($sums, (($lines -join "`n") + "`n"), (New-Object Text.UTF8Encoding($false)))

# Read it back the way the updater will, and check the two installers are the same bytes.
$recorded = @{}
foreach ($l in (Get-Content $sums)) { $p = $l -split '  ', 2; $recorded[$p[1]] = $p[0] }
foreach ($f in @($Versioned, $Stable)) {
    $name = Split-Path -Leaf $f
    $actual = (Get-FileHash $f -Algorithm SHA256).Hash.ToLower()
    if ($recorded[$name] -ne $actual) { throw "SHA256SUMS.txt does not match $name" }
}
if ($recorded[$VersionedName] -ne $recorded[$StableName]) { throw "The two installers differ" }

Write-Host ""
foreach ($f in @($Versioned, $Stable, $sums)) {
    $i = Get-Item $f
    "{0,-40} {1,12:N0} bytes" -f $i.Name, $i.Length | Write-Host
}
Write-Host "`nDone. Version $Version, SHA-256 $($recorded[$VersionedName])" -ForegroundColor Green
Write-Host "Unsigned: Windows SmartScreen will warn on first run (More info > Run anyway)."
