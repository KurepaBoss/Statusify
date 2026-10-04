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

- Tests: `cd src-tauri && cargo test` (251 tests; fake LRCLIB HTTP server and fake
  Discord pipe — never touches the network or the real Discord profile) and
  `node --test` (the pure TS logic, plus the real lyrics-bridge.js against a fake
  player, WebSocket and clock).
- Pipeline benchmarks (Spotify -> bridge -> engine -> presence -> Discord, all fakes,
  nothing touches Spotify, Discord or port 8765): `tests/bench/run.sh [names]`,
  numbers before and after the latency work in `tests/bench/baseline.jsonl` and
  `tests/bench/after.jsonl`.
- Build: `npx tauri build --no-bundle` → `src-tauri\target\release\statusify-rs.exe` (~9 MB).
- Env: `STATUSIFY_DATA_DIR` (history.db, .env, statusify-rs.log; default = exe dir),
  `STATUSIFY_PORT` (default 8765), `DISCORD_APP_ID` (from `.env`).
- Isolated test run while the old app is live: data dir = a *copy* of history.db
  (use sqlite backup, WAL is open), `STATUSIFY_PORT=8799`, no DISCORD_APP_ID, and
  drive it with a fake bridge (websockets client sending track_change/position/lyrics).
- The old app holds 8765; the new one shows "Port 8765 is in use" instead of fighting it.
  To go live: stop the old app (between songs), set STATUSIFY_DATA_DIR to
  `Desktop\Statusify-1.2.0`, start statusify-rs.exe. No bridge change needed.

## Latency and robustness work (branch perf/bridge)

What changed, in the order it matters (measured numbers: `tests/bench/after.jsonl`):

- `presence.rs`: the settle before the first frame of a track is adaptive. 400 ms when
  the lyrics are already known (pin, prefetch, history cache, or they arrive during the
  wait), 1.5 s when they are not or when another change came within 3 s (skip bursts
  still cost no frames), 300 ms when the paused track plays on (it used to re-enter the
  1.5 s settle). A second seek within 2 s of the previous is a drag: the destination is
  published once the position has been still for 500 ms.
- `bridge.rs`: one task per connection (a stuck peer can no longer starve the real
  bridge), 3 s handshake timeout, ping every 5 s and a drop after 15 s of silence. A
  connection becomes the bridge with its first valid message; the newest bridge wins,
  older ones stay connected and take over again if it goes away.
- `discord.rs`: reconnect retries at 250 ms, 500 ms, 1 s, 2 s (then 2 s while there is
  no pipe, 5 s while a pipe does not complete the handshake) instead of a flat 5 s then
  15 s; a connection that lasted 10 s resets the sequence; failures are logged once,
  then every 30 s.
- `lyrics-bridge.js` 2.2 (**needs `spicetify apply`**, which restarts Spotify, so it is
  not applied until the user does it; Statusify offers it as soon as the new exe runs):
  songchange and onplaypause are handled at once instead of on the next 500 ms poll,
  the color-lyrics request is abandoned after 5 s and the Spicy fetch after 6 s (the
  old ~66 s "none" verdict becomes ~5 s), and the reconnect after a dropped socket backs
  off from 250 ms to 3 s instead of a flat 3 s.

Looked at and left alone, on purpose:

- Keeping the presence up for a few seconds when the bridge socket drops: a blip is rare,
  quitting Spotify is daily, and the grace would leave a stale presence for its whole
  length every time Spotify quits (the socket does not say which of the two it was).
- A presence loop that sleeps until the next planned send instead of polling every 50 ms:
  the lateness it removes (about 30 ms) is invisible next to Discord's own delay, and a
  missed wake-up would be a stuck presence.
- Not emitting identical snapshots to the UI twice a second: the saving was not measured
  and the WebView side cannot be verified without driving the live UI.
