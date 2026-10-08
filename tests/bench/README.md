# Pipeline benchmarks

How fast, and how steadily, does a change in Spotify reach your Discord status?
This folder holds the harness that answers that question and the results the
README's *What's New in v3.0.0* quotes.

The harness lives in `src-tauri/src/bench_bridge.rs` (test builds only). Every
benchmark is marked `#[ignore]`, so the normal `cargo test` never runs them.

## What is measured

The chain is:

```
Spotify  ->  bridge (lyrics-bridge.js)  ->  WebSocket  ->  engine  ->  presence planner  ->  Discord pipe
```

Everything from the WebSocket on is the real code: `bridge::serve`, the engine,
the presence loop with its rate ledger, and the Discord client over a real
Windows named pipe. Only the two ends are fakes:

- **Spotify** is a scripted client that sends the bridge's messages (track
  changes, positions, lyrics, pause, resume, seek) at chosen moments. For the
  part inside Spotify, `bridge_js_probe.mjs` runs the real `lyrics-bridge.js`
  against a stub of the Spicetify API (it needs Node), so the bridge's own
  timers are included.
- **Discord** is a fake server on a private pipe name (`statusify-bench-...`)
  that records every `SET_ACTIVITY` frame with its arrival time and can close
  the connection on cue.

The benchmarks, by the letter used in the code:

| Name | What it does |
| --- | --- |
| `a_track_change` | Song change to the first status update, for lyrics that are already known, that arrive 0.8 s or 2.5 s after the change, or that never come |
| `b_line_jitter_and_waste` | How late each lyric line arrives on Discord, how often a wrong line is up, and how many updates are wasted, on three songs played in real time |
| `c_pause_resume_seek` | Reaction to pause, resume and seek; a burst of 10 seeks in 2.5 s; a burst of skips |
| `d_discord_reconnect` | Discord's pipe closes (for 0.3 s, for 7 s) and comes back |
| `e_bridge_drop` | The bridge's WebSocket drops; a stuck connection that never speaks sits in front of the real one |
| `f_idle_cpu`, `f_unit_costs` | CPU while idle and while playing, and what one presence wake-up (every 50 ms) and one position message cost |
| `js_e2e` | The real `lyrics-bridge.js`: how long a song change, pause, resume or seek in Spotify takes to reach the engine and Discord |
| `js_lyrics_hang` | How long until the bridge gives up when Spotify's lyrics request hangs |
| `h_pause_flapping` | Pause and resume every 0.3 to 3 s for a minute, ending playing or paused: the most updates in any 20 s, and whether the status was ever blank while music played |
| `i_discord_hangup` | Discord hangs up at various moments after connecting, for a minute |
| `j_bridge_flap` | The bridge's socket drops every 0.3 to 6 s, for a minute |
| `k_presence_wakeups` | How many times a second the presence loop wakes up to look at the engine, playing, paused and idle (an exact count), and the process CPU meanwhile |

## Running it

You need Rust (the same toolchain as the build), Node.js 24 for the `js_*`
benchmarks and a bash (on Windows, the one that comes with Git for Windows).
From the repository root:

```bash
tests/bench/run.sh                    # all of them, about 18 minutes
tests/bench/run.sh a_track_change     # one, by the name above
BENCH_JS_TRIALS=24 tests/bench/run.sh js_e2e                  # more trials per kind (default 6)
BENCH_BRIDGE_JS=/path/to/other.js tests/bench/run.sh js_e2e   # measure another bridge file
```

Two more measurements run in Node alone and need no Rust build:

```bash
node tests/bench/bridge_js_traffic.mjs [--seconds 20] [--seeks 12] [--no-update-events]
npx vite --port 1420 &   # serves tests-ts/harness.html
node tests/bench/ui_profile.mjs [--runs 3] [--seconds 20] [--out file.json]
```

`bridge_js_traffic.mjs` runs the real `lyrics-bridge.js` against a stub
Spicetify with real timers and reports what it puts on the socket: messages and
bytes per minute by type, playing and paused, and how long a seek in Spotify
takes to appear as a position message. `--no-update-events` models a Spotify
whose player API never announces state changes, so only the bridge's own
heartbeat can notice a seek. `ui_profile.mjs` drives the Lyrics page in a
headless Chromium (Playwright) for a scripted window of playback with one track
change, and reports the main-thread work from the DevTools trace (style
recalculations, layouts, paints, raster tasks, and their time), the frame
intervals, and what one snapshot publish costs the page. Its absolute frame
times are those of a software renderer; compare runs, do not read them as a
monitor's.

On GitHub, the *Bench* workflow (`.github/workflows/bench.yml`) runs the quick
set on a Windows runner when started by hand or when a commit message contains
`[bench]`, and leaves `bench-results.jsonl` as its artifact.

`run.sh` builds the test binary in its own target directory (`.bench-target`,
ignored by git), so it does not disturb a build you already have. It keeps
optimisation at level 3 but turns link-time optimisation off to avoid a long
link, which makes the unit-cost numbers a little pessimistic; the latency numbers
are bound by timers and I/O and are not affected. Each benchmark prints one
`BENCH_RESULT {json}` line; they are collected in
`tests/bench/results/<timestamp>.jsonl`.

It is safe to run next to a Statusify you are using: no window opens, port 8765
and Discord's own pipe (`discord-ipc-N`) are never touched, no Discord
Application ID is read, and nothing talks to Spotify or Spicetify.

## The files here

| File | What it is |
| --- | --- |
| `baseline.jsonl` | The first build of version 3, before the latency work, run with this harness |
| `baseline-session.jsonl` | A second run of the same baseline, on the day the final numbers were taken, to check that `baseline.jsonl` still held on that PC |
| `after.jsonl` | Version 3.0.0 with the final code. Its first entry (`_meta`) lists the notes and caveats for the run |
| `js_e2e_24_trials.jsonl` | `js_e2e` with 24 trials per kind, before and after, for the seek rows |
| `bridge_js_probe.mjs` | The stub Spicetify that runs the real `lyrics-bridge.js` outside Spotify, for the `js_*` benchmarks. It fires the player API's "update" event on every state change like Spotify does; `--no-update-events` leaves it out |
| `bridge_js_traffic.mjs` | The bridge's traffic per minute and seek latency, in Node alone (see above) |
| `ui_profile.mjs` | The Lyrics page under a headless Chromium: main-thread work and frame intervals (see above) |
| `proposed-bridge-js.patch` | The prototype of the four bridge changes that were measured before being adopted. It is **superseded**: `lyrics-bridge.js` 2.2 contains them and more. Kept for the record |

## The numbers

Medians, from `baseline.jsonl` and `baseline-session.jsonl` ("before") and
`after.jsonl` ("v3.0.0"):

| What | Before | v3.0.0 |
| --- | --- | --- |
| Song change to first status update, lyrics already known (fetched ahead or cached) | 1.57-1.59 s | 0.45 s |
| The same, lyrics arrive within 0.8 s | 1.58-1.59 s | 0.87 s |
| The same, lyrics unknown or late | about 1.57 s | about 1.57 s (unchanged) |
| Pause longer than 1.5 s, then resume, to first update (lyrics known) | 1.57-1.60 s | 0.38 s |
| Pause to status cleared | 0.03-0.05 s | about 1.6 s (the 1.5 s hold); 5-8 s on songs with a lyric line every 4 s or faster |
| 10 seeks in 2.5 s: updates sent / correct line shown after the last seek | 5 / 15.7 s | 2 / 0.6 s |
| Discord's pipe closes for 0.3 s, to first update again | 6.6 s | 1.2 s |
| Discord's pipe closes for 7 s, to first update after it is back | 14.6 s | 1.3 s |
| Bridge socket blip of 0.5 s | status cleared 16 ms later, back after 2.1 s | not touched |
| A stuck TCP peer in front of the real bridge | the real client was never served | served at once |
| Most updates in any 20 s, 60 s of pause/resume flapping (0.3 to 3 s), ending playing or paused | not measured | 5 |
| The same with a Discord that hangs up 0.6 to 2 s after every connect, and with a bridge socket that drops every 0.3 to 6 s | not measured | 5 |

These rows need the updated Spicetify bridge (`lyrics-bridge.js` 2.2.x):

| What | Old bridge | 2.2.x |
| --- | --- | --- |
| Song change reaches the engine, median / 95th percentile | 264 / 461 ms | 1 / 9.5 ms |
| Pause / resume reaches the engine, median | 251 / 209 ms | 0.6 / 0.9 ms |
| Song change to first status update | 1.85 s | 0.46 s |
| A lyrics request that hangs, until the bridge reports "no lyrics" | 63 s | 5 s |
| A seek reaches the engine (found by the bridge's 500 ms poll; unchanged) | 259 ms mean | 248 ms mean |

## How to read them

- **Fakes, not Spotify and Discord.** The benchmarks show what Statusify does
  with the events it is given and how it spends Discord's budget of 5 updates per
  20 seconds. They cannot show how long Spotify takes to announce something or
  how long Discord takes to draw it; those delays are not in any number here.
- **A shared PC.** The numbers were taken on a desktop that was also running
  builds and music (each entry records the machine's CPU load at the time as
  `machine_cpu_pct_during`). Read them to within 10-15 %. Medians of a few
  trials are noisy where a timer's phase matters: the seek rows moved between
  runs with 8 trials, which is why `js_e2e_24_trials.jsonl` exists.
- **A comparison, not an absolute.** "Before" is the first build of version 3;
  the Python app was not measured. The number of trials is in each entry (`n`).
- **Some rows are a price, not a gain.** A pause takes the status down after
  about 1.6 s, not at once, because a quick pause and resume should not spend
  updates; and on songs with a line every 4 s or faster the clear waits for a
  free slot. That trade is deliberate.
- **Not covered.** A seek that lands inside the line already shown, and any seek
  in a song without lyrics, send no update, so Discord's progress bar stays where
  it was.
