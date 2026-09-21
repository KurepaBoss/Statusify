# Spicetify Bridge Install

**Most people don't need this page.** `Statusify-Setup-<version>.exe` from the
[Releases page](https://github.com/KurepaBoss/Statusify/releases) installs
Spicetify and the bridge for you. This covers doing it by hand.

## Requirements
- Desktop Spotify from spotify.com (not the Microsoft Store version), opened
  and logged in at least once.
- Do **not** run any of this as administrator. Spicetify refuses to.

## Scripted (recommended)
From the Statusify folder, in a normal PowerShell window:
```
powershell -ExecutionPolicy Bypass -File installer\setup-spicetify.ps1 -Bridge lyrics-bridge.js
```
It installs Spicetify if missing, registers the bridge, applies it, and checks
that the bridge actually reached Spotify. Safe to re-run at any time; after a
Spotify update, re-running it is the fix.

## Fully manual
1. Install [Spicetify](https://spicetify.app/docs/getting-started).
2. Copy `lyrics-bridge.js` to `%APPDATA%\spicetify\Extensions\`.
3. Run:
   ```
   spicetify config extensions lyrics-bridge.js
   spicetify backup apply
   ```
   (Use `spicetify apply` if you've applied Spicetify before.)

## How it works
- The extension pushes the current track and playback position to Statusify
  over a local WebSocket on port 8765.
- It fetches lyrics itself (Spicy Lyrics API first, Spotify's own lyrics as a
  fallback), so no separate lyrics extension or API key is needed.
- Statusify maps the position to a lyric line and updates your Discord status.

## Troubleshooting
- **Lyrics stopped after a Spotify update:** re-run the script, or Start Menu →
  Statusify → Repair Spicetify bridge. Restarting Spotify alone never helps:
  Spotify runs the copy injected by `spicetify apply`, not the file in
  `Extensions`.
- Open Spotify DevTools (Ctrl+Shift+I) and look for `[LyricsBridge] Connected.`
  in the Console tab to confirm the bridge is running.
