# Releasing Statusify

How a release is built, what the installer does, and the contracts the app
relies on. Everything here is checked by tests unless it says otherwise.

## Version

`src-tauri/Cargo.toml` is the source of truth: the app reports
`env!("CARGO_PKG_VERSION")` in Settings, the log, the LRCLIB user agent and the
update check. These must say the same thing, and `scripts/check-version-sync.mjs`
(also a `node --test` test) fails when they do not:

- `src-tauri/Cargo.lock` (the `statusify-rs` entry)
- `src-tauri/tauri.conf.json` (`version`; it names the installer file)
- `package.json` and `package-lock.json` (both version fields)
- the README's version badge (`badge/Statusify-vX.Y.Z-...`), header
  (`Statusify vX.Y.Z`) and `What's New in vX.Y.Z` heading, whichever exist.
  `--strict` (and `scripts/package.ps1 -Strict`) also fails when the README
  has no badge at all; use it for a real release.

To bump: edit those five places, then `node scripts/check-version-sync.mjs`.

## Building the installer

```powershell
powershell -ExecutionPolicy Bypass -File scripts\package.ps1 -Strict
```

Runs the version check, `npm ci` (skipped when `node_modules` is a link to another
checkout), `npm run build`, `npx tauri build --bundles nsis`, then writes
`release-assets\`:

| File | Purpose |
|---|---|
| `Statusify_<version>_x64-setup.exe` | the versioned installer; the app's updater looks for exactly this name |
| `Statusify-Setup.exe` | identical bytes under a stable name (`.../releases/latest/download/Statusify-Setup.exe`) |
| `SHA256SUMS.txt` | `sha256sum` format, one line per installer |

Attach all three to the GitHub release (tag `vX.Y.Z`, body = the README's
"What's New" section). The installer is **unsigned**: SmartScreen shows
"Windows protected your PC" on first run.

Use `-TargetDir <dir>` to keep cargo's output out of `src-tauri\target` (for example
while another copy of the app is running from there).

## The Release workflow

`.github/workflows/release.yml` runs when a `v*` tag is pushed. It checks the
versions and the README notes first, then runs the same tests as the Tests
workflow, builds the installer, and creates the GitHub release. Both workflows
use one Rust build cache key (`shared-key`), so the release build can start from
the cache a Tests run saved.

Started by hand (Actions tab, *Release*, *Run workflow*) it does everything except
create the release, and leaves the installer, `SHA256SUMS.txt` and the notes as the
workflow artifact `Statusify-release`. Do that once before the first tag, and after
changing the build, to see that it still packages. The first run has a cold cache
and compiles everything with LTO, so allow most of the 90-minute limit.

## What the installer does

- Per-user install (`installMode: currentUser`): no administrator prompt, into
  `%LOCALAPPDATA%\Statusify`. A Start-menu shortcut; the finish page offers to
  start the app and to create a desktop shortcut (ticked by default; it replaces
  an existing `Statusify` shortcut on the Desktop, such as the old Python app's).
- Installs the WebView2 runtime through Microsoft's bootstrapper when it is missing.
- English only; publisher `KurepaBoss`; the Statusify icon on the installer, the
  uninstaller and the exe.
- **The uninstaller never touches `%APPDATA%\Statusify`**, where history, settings
  and the Discord Application ID live. It does remove the app's "launch when
  Windows starts" entry (`HKCU\...\Run\StatusifyDesktop`), which would otherwise
  point at a deleted program.
- It does not install Spicetify or the lyrics bridge, and does not remove them
  again. The app offers that itself (Settings > Spotify lyrics > Set up), because
  setting it up restarts Spotify.

## Where the app keeps its data

Decided at start-up (`src-tauri/src/datadir.rs`), first match wins, and the log
says which one was chosen:

1. `STATUSIFY_DATA_DIR`, when set;
2. the exe's own folder, when `history.db`, `statusify.cfg` or `.env` already sits
   there (portable mode: a setup that keeps its data beside the exe keeps working);
3. `%APPDATA%\Statusify`.

Nothing is ever moved or copied between them. Settings > Diagnostics shows the
folder and opens it. A person coming from the Python app starts with an empty
`%APPDATA%\Statusify`; its `history.db`, `statusify.cfg` and `.env` (same formats)
can be copied there by hand while neither app is running.

## Start with Windows

The app's switch owns one thing: `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`
value **`StatusifyDesktop`**. The old Python app uses a Startup-folder shortcut and,
on every toggle, deletes a Run value named exactly `Statusify`. So the two switches
never touch each other. If both are on, both start at sign-in and race for port
8765 (first one wins); Settings says so and offers "Turn off the old one", which
deletes the Python app's Startup shortcut, only when asked. An entry named
`Statusify` left by the first builds of this app is moved to the new name, but only
if it starts this exe.

## Updates

The app reads `https://api.github.com/repos/KurepaBoss/Statusify/releases`, takes the
highest stable `vX.Y.Z` release (drafts, pre-releases and other tag shapes are
ignored), and offers it only if it is newer than the running version. It never
says anything when equal or newer, and fails silently offline (the log notes it).

Rate limiting: one background check per 6 hours (counted across restarts), at most
one a minute for the "Check now" button, half an hour of quiet after a failure, and
nothing until GitHub's reset time after a rate-limit response.

"Install update" works when the app was installed by the installer and the release
carries `Statusify_<version>_x64-setup.exe` plus `SHA256SUMS.txt`: it downloads the
installer, checks its SHA-256 against the checksum list, runs it with `/S /R /UPDATE`
(silent, restart the app afterwards, and treat it as an update: shortcuts and the
"launch when Windows starts" entry stay as they were) and quits. In any other case (portable copy, missing
asset, failed check) it opens the release page instead.
