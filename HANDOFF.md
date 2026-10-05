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

What changed, in the order it matters (measured numbers: `tests/bench/after.jsonl`,
compared with `tests/bench/baseline.jsonl`; all on a fake Spotify and a fake Discord pipe):

- `presence.rs`, the first frame of a track: the settle is adaptive. 400 ms when the
  lyrics are already known (pin, prefetch, history cache, or they arrive during the
  wait), 1.5 s when they are not or when another change came within 3 s. A track that is
  skipped before its settle ends costs no frame, so skipping faster than once per 1.5 s
  costs at most one extra frame: the first track's, when the second change comes after
  its 0.4 s (lyrics known) settle. Slower skipping publishes the tracks that are
  listened to for longer than the settle, budget permitting. 300 ms
  when the paused track plays on and its lyrics are known (1.5 s when they are not). A
  second seek within 2 s of the previous is a drag: the destination is published once
  the position has been still for 500 ms.
- `presence.rs`, the rate budget (Discord drops what is over 5 SET_ACTIVITY per 20 s):
  - Every frame is in one ledger, clears and the paused indicator included. Nothing
    leaves while the ledger is full. A clear that has to wait (it is "owed") waits,
    and is dropped if the next frame replaces the profile anyway; if no frame is
    coming (paused), it stays owed.
  - The ledger survives a Discord reconnect (only what Discord showed is forgotten):
    a Discord that hangs up right after READY used to hand out 5 fresh frames per cycle.
  - A pause is acted on only once it has lasted `PAUSE_HOLD` (1.5 s). A quick pause and
    resume sends nothing; the progress bar of the profile is then behind by the time the
    song stood still, and one frame puts it right once play has been steady for 2 s, if
    it is more than 1.5 s behind. A song that kept playing through a bridge blip is not
    behind.
  - A pause clears the profile only if that leaves `PAUSE_RESERVE` (1) slot free, so the
    resume that follows always has a slot at once. The price: on a song with a lyric
    line every 4 s or faster (which keeps the whole budget busy) the clear after a
    pause waits for slots to free: median 5-8 s, worst case about 15 s
    (`report_pause_to_clear`; with a line every 8 s or slower it is the 1.5 s hold). Without the reserve a flapping pause leaves the profile
    blank while music plays (6-8 s per minute of flapping, `report_flapping`); that is
    the trade this makes, on purpose. Master sent the clear at once and un-gated, which
    Discord would drop when the ledger is full, leaving a stale lyric on the profile.
  - Proven by: unit tests on `Presence::tick` (flapping at 0.3-3 s cadences, hang-up
    loops, reconnects, owed clears), a seeded randomised stress test
    (`STATUSIFY_STRESS_SEEDS=20000 cargo test --release --lib random_pausing`;
    pausing, skipping, seeking, Discord hang-ups, RPC switching and blacklisting at
    random paces; it found the "switch back on while paused" hole) and whole-pipeline
    tests in `bench_bridge.rs` (real bridge socket, engine, presence and Discord client
    against the fake pipe). Each of them fails when the ledger is dropped on
    reconnect, when the hold or the reserve is removed, or when clears are not counted.
- `bridge.rs`: one task per connection (a stuck peer can no longer starve the real
  bridge), 3 s handshake timeout, ping every 5 s and a drop after 15 s of silence. A
  connection becomes the bridge with its first valid message; the newest bridge wins,
  older ones stay connected. When the newest leaves, the next one is asked for the state
  and dropped after 3 s if it says nothing (a half-open socket left by a reload). A
  connection's slot is released by a drop guard, also when the handler panics.
- `discord.rs`: reconnect retries at 250 ms, 500 ms, 1 s, 2 s (then 2 s while there is
  no pipe, 5 s while a pipe does not complete the handshake) instead of a flat 5 s then
  15 s; a connection that lasted 10 s resets the sequence. The log gets one line per
  outage, then at most one per 30 s, and one when a handshake ends a logged outage.
- `lyrics-bridge.js` 2.2 (**needs `spicetify apply`**, which restarts Spotify, so it is
  not applied until the user does it; Statusify offers it as soon as the new exe runs):
  songchange and onplaypause are handled at once instead of on the next 500 ms poll
  (checked against the installed Spicetify wrapper: it sets `Player.data` and the
  player state before it dispatches `songchange`, then `onplaypause`), the
  color-lyrics request is abandoned after 5 s and the Spicy fetch after 6 s (the old
  ~66 s "none" verdict becomes ~5 s), a track change abandons the lyric requests of
  the track before it (a skip burst no longer queues a Spicy and a color-lyrics request
  per skipped song), and the reconnect after a dropped socket backs off from 250 ms to
  3 s instead of a flat 3 s.

Measured (medians; `tests/bench/`: `baseline.jsonl` and `baseline-session.jsonl` are master, `after.jsonl`
and `js_e2e_24_trials.jsonl` the branch; fake Spotify and fake Discord, on a shared PC, so read them to
within 10-15 %):

| What | Master | Branch |
|---|---|---|
| song change -> first frame, lyrics already known (prefetch, cache) | 1.57-1.59 s | 0.45 s |
| song change -> first frame, lyrics arrive within 0.8 s | 1.58-1.59 s | 0.87 s |
| song change -> first frame, lyrics unknown or late | ~1.57 s | ~1.57 s (unchanged) |
| pause longer than 1.5 s, then resume -> first frame (lyrics known) | 1.57-1.60 s | 0.38 s |
| pause -> profile cleared | 0.03-0.05 s | 1.6 s (the hold); 5-8 s on songs with a line every <= 4 s |
| 10 seeks in 2.5 s: frames sent / final line shown after the last seek | 5 / 15.7 s | 2 / 0.6 s |
| Discord's pipe closes for 0.3 s -> first frame again | 6.6 s | 1.2 s |
| Discord's pipe closes for 7 s -> first frame after it is back | 14.6 s | 1.3 s |
| bridge socket blip of 0.5 s -> profile | cleared 16 ms later, back after 2.1 s | not touched |
| a stuck TCP peer in front of the real bridge | real client never served | served at once |
| (new bridge JS) song change -> engine, p50 / p95 | 264 / 461 ms | 1 / 9.5 ms |
| (new bridge JS) pause or resume -> engine, p50 | 251 / 209 ms | 0.6 / 0.9 ms |
| (new bridge JS) song change -> first frame | 1.85 s | 0.46 s |
| (new bridge JS) a lyric request that hangs -> "none" verdict | 63 s | 5 s |
| seek -> engine (the 500 ms poll, unchanged) | 259 ms mean | 248 ms mean |
| most frames in any 20 s, 60 s of pause/resume flapping at 0.3-3 s, ending playing or paused | not measured | 5 |
| the same with a Discord that hangs up 0.6-2 s after every READY, and with a bridge socket that drops every 0.3-6 s | not measured | 5 |

The rows marked "new bridge JS" need the updated `lyrics-bridge.js` in Spotify (see above).

Known, not changed here: a seek that lands inside the line already shown, or any seek in
a song without lyrics, sends no frame, so the progress bar on Discord stays where it was
(master does the same).

Looked at and left alone, on purpose:

- Keeping the presence up for a few seconds when the bridge socket drops: with the pause
  hold a socket that is back within 1.5 s now sends nothing at all, and one that stays
  away longer is a pause as far as the app can tell (the socket does not say whether
  Spotify quit or blinked).
- A presence loop that sleeps until the next planned send instead of polling every 50 ms:
  the lateness it removes (about 30 ms) is invisible next to Discord's own delay, and a
  missed wake-up would be a stuck presence.
- Not emitting identical snapshots to the UI twice a second: the saving was not measured
  and the WebView side cannot be verified without driving the live UI.
