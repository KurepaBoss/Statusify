# Spicetify Extension Install

## Requirements
- Spicetify installed: https://spicetify.app/docs/getting-started

## Steps

1. **Copy the extension file** to your Spicetify extensions folder:
   ```
   %appdata%\spicetify\Extensions\
   ```
   So the full path is:
   ```
   C:\Users\<YOU>\AppData\Roaming\spicetify\Extensions\lyrics-bridge.js
   ```

2. **Open a terminal** (PowerShell or CMD) and run:
   ```
   spicetify config extensions lyrics-bridge.js
   spicetify apply
   ```

3. **Spotify will restart** with the extension active.

4. **Run the Python script** (`run.bat`) — it starts a WebSocket server
   that the extension connects to automatically.

## How it works
- The extension pushes the current track + exact playback position (ms)
  to the Python script every 500ms via WebSocket on port 8765.
- Python maps the position to a lyric line and updates your Discord status.
- No API keys, no rate limits on the Spicetify side.

## Troubleshooting
- If Spotify doesn't restart after `spicetify apply`, restart it manually.
- Open Spotify DevTools (Ctrl+Shift+I) and check the Console tab for
  `[LyricsBridge] Connected to Python.` to confirm it's working.
