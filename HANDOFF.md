# Statusify-rs — rewrite handoff

Tauri 2 rewrite of `Desktop\Statusify-1.2.0` (Python/Tk). Rust backend, vanilla
TypeScript + CSS frontend (GPU-composited in WebView2). Started 2026-10-02.

## Status: skeleton working (tested end-to-end in isolation)

| Area | State |
|---|---|
| Bridge WebSocket server (same protocol, port 8765) | done — `bridge.rs` |
| Track/position/pause/lyrics/prefetch handling | done — `engine.rs` |
| Lyric priority pin > prefetch > cache > bridge > LRCLIB | done |
| LRCLIB early fallback (2.5 s, 3 attempts, 503 = retryable) | done |
| history.db (same file + schema as Python app) | done — `db.rs` |
| Plays: commit after 20 s listened, listened_ms saved every 5 s | done |
| Discord IPC (overlapped named pipe, reconnect, newest-wins) | done — `discord.rs` |
| Presence: Listening type 2, links, button, ms timestamps | done — `presence.rs` |
| Presence scheduling | GREEDY (5 per 20 s). Port `statusify_presence_plan.py` beam planner next |
| UI: blurred cover, synced lyric scroll, progress/seek, transport, recent plays | done (basic) |
| Not yet ported | instrumental gap marker in presence, per-track lyric offset, blacklist, translation/romanisation, history/stats pages, settings, mini player, overlay, hotkeys, tray, sleep timer, lyric search/pin UI, Wrapped export, installer |

## Run / test

- Tests: `cd src-tauri && cargo test` (22 tests; fake LRCLIB HTTP server and fake
  Discord pipe — never touches the network or the real Discord profile).
- Build: `npx tauri build --no-bundle` → `src-tauri\target\release\statusify-rs.exe` (~9 MB).
- Env: `STATUSIFY_DATA_DIR` (history.db, statusify.cfg, .env, statusify-rs.log; when
  unset: the exe's folder if it already holds one of those files ("portable", this is
  how the dev setup keeps working), else `%APPDATA%\Statusify`; see `src/datadir.rs`),
  `STATUSIFY_PORT` (default 8765), `DISCORD_APP_ID` (from `.env`),
  `STATUSIFY_RELEASES_URL` (test hook: where the update check looks instead of GitHub).
- Releasing (version bump, installer, update contract): `docs/RELEASING.md`.
  `node scripts/check-version-sync.mjs` keeps Cargo, tauri.conf, package.json and the
  README badge equal; `scripts/package.ps1` builds the NSIS installer.
- Isolated test run while the old app is live: data dir = a *copy* of history.db
  (use sqlite backup, WAL is open), `STATUSIFY_PORT=8799`, no DISCORD_APP_ID, and
  drive it with a fake bridge (websockets client sending track_change/position/lyrics).
- A test exe with the SAME identifier hands off to the live app (single-instance plugin: it would focus/unhide the live
  window and exit). Build test copies with `TAURI_CONFIG='{"identifier":"com.statusify.test"}'` (the mutex, webview data
  folder and AppUserModelID all follow the identifier), and give them an empty `APPDATA` so the Spicetify folder is never touched.
- Icon: `src-tauri/icons/icon.ico` is compiled into the exe (title bar, taskbar, tray, Explorer). `build.rs` watches
  `icons/`, so swapping it re-embeds (before, the Tauri placeholder stayed baked in). `src/app_icon.rs` sets the small and
  big icon of every window at its own DPI, the tray icon at the tray size, and the AppUserModelID (the tauri identifier,
  which the NSIS shortcuts carry too).
- The old app holds 8765; the new one shows "Port 8765 is in use" instead of fighting it.
  To go live: stop the old app (between songs), set STATUSIFY_DATA_DIR to
  `Desktop\Statusify-1.2.0`, start statusify-rs.exe. No bridge change needed.

## Known bridge issue (not fixed — needs `spicetify apply`, which restarts Spotify)

Spotify's color-lyrics request hangs ~30 s then fails "Resolver not found"; the
bridge retries once, so its "none" verdict arrives ~66 s into a song. Both apps now
start LRCLIB early so it no longer matters much, but the bridge should time that
request out at ~5 s.
