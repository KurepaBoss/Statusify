//! Benchmark harness for the Spotify -> bridge -> engine -> presence -> Discord
//! pipeline. Test builds only; every benchmark is `#[ignore]`, so a plain
//! `cargo test` stays fast. Run them with tests/bench/run.sh (release build,
//! `--ignored --nocapture --test-threads=1`).
//!
//! Everything is real except the two ends:
//!   fake Spotify/bridge client  ->  REAL bridge::serve (WebSocket)
//!     -> REAL Engine -> REAL presence::run_loop (planner, rate ledger)
//!     -> REAL discord::run_on (overlapped named pipe) -> fake Discord pipe
//! The fake Discord listens on a private pipe name (`statusify-bench-...`),
//! never on `discord-ipc-N`, and the harness never starts Tauri, so it cannot
//! touch the real Discord profile, the live Statusify instance (port 8765,
//! single-instance mutex) or Spotify. Ports are OS-assigned (bind(0)).
//!
//! The one thing in-process fakes cannot give is the Spicetify side: the
//! bridge JS polls on a 500 ms timer. `tests/bench/bridge_js_probe.mjs` runs
//! the REAL lyrics-bridge.js against a stub Spicetify and `bench_js_e2e`
//! measures that hop with the real server + engine + presence behind it.
//!
//! Output: one `BENCH_RESULT {json}` line per benchmark on stdout (and appended
//! to $STATUSIFY_BENCH_OUT when set). Numbers are measured, never estimated.

#![allow(clippy::too_many_arguments, dead_code)]

use crate::bridge;
use crate::db::Store;
use crate::discord;
use crate::engine::{Engine, Event};
use crate::presence;
use serde_json::{json, Value};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::windows::named_pipe::ServerOptions;
use tokio::sync::mpsc;
use tokio::task::JoinHandle;
use tokio_tungstenite::tungstenite::Message;

use futures_util::{SinkExt, StreamExt};

// ───────────────────────── process / machine CPU (Windows FFI) ─────────────────────────

#[repr(C)]
#[derive(Default, Clone, Copy)]
struct FileTime {
    lo: u32,
    hi: u32,
}
impl FileTime {
    fn ms(self) -> f64 {
        (((self.hi as u64) << 32) | self.lo as u64) as f64 / 10_000.0
    }
}
#[link(name = "ntdll")]
extern "system" {
    fn NtQueryTimerResolution(min: *mut u32, max: *mut u32, cur: *mut u32) -> i32;
}
extern "system" {
    fn GetCurrentProcess() -> isize;
    fn GetProcessTimes(h: isize, c: *mut FileTime, e: *mut FileTime, k: *mut FileTime, u: *mut FileTime) -> i32;
    fn GetSystemTimes(idle: *mut FileTime, kernel: *mut FileTime, user: *mut FileTime) -> i32;
}

/// CPU time this process has used so far (user + kernel), ms.
fn process_cpu_ms() -> f64 {
    let (mut c, mut e, mut k, mut u) = (FileTime::default(), FileTime::default(), FileTime::default(), FileTime::default());
    unsafe { GetProcessTimes(GetCurrentProcess(), &mut c, &mut e, &mut k, &mut u) };
    k.ms() + u.ms()
}

/// (busy ms, total ms) summed over all logical CPUs since boot.
fn machine_times() -> (f64, f64) {
    let (mut i, mut k, mut u) = (FileTime::default(), FileTime::default(), FileTime::default());
    unsafe { GetSystemTimes(&mut i, &mut k, &mut u) };
    // kernel time includes idle time.
    let total = k.ms() + u.ms();
    (total - i.ms(), total)
}

/// Samples machine-wide CPU use over a benchmark so the report says how noisy
/// the box was (other builds and the user's own music run alongside).
struct LoadProbe(f64, f64);
impl LoadProbe {
    fn start() -> Self {
        let (b, t) = machine_times();
        LoadProbe(b, t)
    }
    fn pct(&self) -> f64 {
        let (b, t) = machine_times();
        let d = t - self.1;
        if d <= 0.0 {
            0.0
        } else {
            ((b - self.0) / d * 1000.0).round() / 10.0
        }
    }
}

// ───────────────────────── small utilities ─────────────────────────

fn ms(d: Duration) -> f64 {
    d.as_secs_f64() * 1000.0
}

fn r1(x: f64) -> f64 {
    (x * 10.0).round() / 10.0
}

fn stats(v: &[f64]) -> Value {
    if v.is_empty() {
        return json!({"n": 0});
    }
    let mut s = v.to_vec();
    s.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let n = s.len();
    let p = |q: f64| s[(((n as f64) * q).ceil() as usize).clamp(1, n) - 1];
    json!({"n": n, "min": r1(s[0]), "mean": r1(s.iter().sum::<f64>() / n as f64), "p50": r1(p(0.5)), "p95": r1(p(0.95)), "max": r1(s[n - 1])})
}

fn report(name: &str, data: Value) {
    let line = json!({"name": name, "data": data}).to_string();
    println!("BENCH_RESULT {line}");
    if let Some(p) = std::env::var_os("STATUSIFY_BENCH_OUT") {
        use std::io::Write;
        if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(p) {
            let _ = writeln!(f, "{line}");
        }
    }
}

async fn sleep(ms: u64) {
    tokio::time::sleep(Duration::from_millis(ms)).await;
}

/// Poll `f` every 5 ms until it yields Some, or give up after `limit`.
async fn wait_for<T>(limit: Duration, mut f: impl FnMut() -> Option<T>) -> Option<T> {
    let end = Instant::now() + limit;
    loop {
        if let Some(v) = f() {
            return Some(v);
        }
        if Instant::now() >= end {
            return None;
        }
        sleep(5).await;
    }
}

// ───────────────────────── lyric sheets ─────────────────────────

#[derive(Clone)]
pub struct Sheet {
    pub duration_ms: i64,
    pub lines: Vec<(i64, String)>,
}

impl Sheet {
    fn token(i: usize) -> String {
        format!("L{i:03}")
    }
    /// Synthetic words: a unique token ("L012") padded with filler to `len`
    /// characters, so a presence frame can be traced back to its line while
    /// the rate planner sees the real line lengths.
    fn from_timing(duration_ms: i64, starts: &[i64], lens: &[usize]) -> Sheet {
        let lines = starts
            .iter()
            .zip(lens)
            .enumerate()
            .map(|(i, (&s, &len))| {
                let mut w = Self::token(i);
                while w.chars().count() < len.max(4) {
                    w.push_str(if w.ends_with(' ') { "la" } else { " " });
                }
                w.truncate(len.max(4));
                (s, w.trim_end().to_string())
            })
            .collect();
        Sheet { duration_ms, lines }
    }
    /// `n` lines `spacing_ms` apart (budget-friendly: nothing is rate-limited).
    fn evenly(n: usize, spacing_ms: i64, start_ms: i64) -> Sheet {
        let starts: Vec<i64> = (0..n as i64).map(|i| start_ms + i * spacing_ms).collect();
        let lens = vec![30; n];
        Sheet::from_timing(start_ms + n as i64 * spacing_ms + 10_000, &starts, &lens)
    }
    /// The repo's own rate-limit stress: 1.2 s lines of 70+ characters.
    fn dense() -> Sheet {
        let starts: Vec<i64> = (0..60).map(|i| i * 1200).collect();
        Sheet::from_timing(80_000, &starts, &vec![75; 60])
    }
    fn fixture(which: &str) -> Sheet {
        let fx: Value = serde_json::from_str(include_str!("bench_fixtures.json")).unwrap();
        let f = &fx[which];
        let starts: Vec<i64> = f["starts_ms"].as_array().unwrap().iter().map(|x| x.as_i64().unwrap()).collect();
        let lens: Vec<usize> = f["lens"].as_array().unwrap().iter().map(|x| x.as_u64().unwrap() as usize).collect();
        Sheet::from_timing(f["duration_ms"].as_i64().unwrap(), &starts, &lens)
    }
    fn synced_json(&self) -> Value {
        Value::Array(self.lines.iter().map(|(s, w)| json!({"startMs": s, "words": w})).collect())
    }
    /// Index of the line a frame's state shows first, by token.
    fn lines_in(&self, state: &str) -> Vec<usize> {
        (0..self.lines.len()).filter(|&i| state.contains(&Self::token(i))).collect()
    }
}

// ───────────────────────── fake Spotify + bridge client ─────────────────────────

#[derive(Clone)]
pub struct SimTrack {
    uri: String,
    artist: String,
    title: String,
    sheet: Option<Arc<Sheet>>,
    duration_ms: i64,
}

impl SimTrack {
    fn new(n: usize, sheet: Option<Sheet>, duration_ms: i64) -> SimTrack {
        SimTrack {
            uri: format!("spotify:track:bench{n:04}"),
            artist: "Bench Artist".into(),
            title: format!("Bench Track {n}"),
            duration_ms: sheet.as_ref().map_or(duration_ms, |s| s.duration_ms.max(duration_ms)),
            sheet: sheet.map(Arc::new),
        }
    }
    fn track_change(&self) -> Value {
        json!({"type": "track_change", "artist": self.artist, "title": self.title, "track_uri": self.uri,
               "album_art": "https://i.scdn.co/image/bench", "duration_ms": self.duration_ms, "album": "Bench Album"})
    }
    fn lyrics(&self, ty: &str) -> Value {
        match &self.sheet {
            Some(s) => json!({"type": ty, "track_uri": self.uri, "source": "Spicy", "mode": "synced", "synced": s.synced_json(), "plain": []}),
            None => json!({"type": ty, "track_uri": self.uri, "source": "none", "mode": "none", "synced": [], "plain": []}),
        }
    }
}

/// How lyrics reach the engine for a new track.
#[derive(Clone, Copy, Debug)]
pub enum LyricsPlan {
    /// The bridge's own fetch answers after this long (Spicy is ~0.3-1.5 s).
    After(u64),
    /// Prefetched for the queue earlier, bridge fetch answers after this long.
    PrefetchedThen(u64),
}

struct SimSpotify {
    track: SimTrack,
    playing: bool,
    pos0: f64,
    wall0: Instant,
    plan: LyricsPlan,
    /// When the track began (truth), for lateness maths.
    track_started: Instant,
}

impl SimSpotify {
    fn pos(&self, now: Instant) -> f64 {
        if self.playing {
            self.pos0 + ms(now.duration_since(self.wall0))
        } else {
            self.pos0
        }
    }
    fn set_pos(&mut self, p: f64, now: Instant) {
        self.pos0 = p;
        self.wall0 = now;
    }
}

#[derive(Clone, Copy, PartialEq, Debug)]
pub enum Mode {
    /// Event-driven bridge: a change is pushed the moment it happens.
    Immediate,
    /// The shipped bridge: a 500 ms setInterval notices changes.
    Poll500,
}

#[derive(Clone, Copy)]
pub struct BridgeOpts {
    mode: Mode,
    /// The JS reconnect timer after a drop.
    reconnect_ms: u64,
    /// How long the bridge's lyric fetch takes after (re)connect.
    fetch_ms: u64,
    /// Position message cadence (shipped: 500 ms).
    tick_ms: u64,
}

impl Default for BridgeOpts {
    fn default() -> Self {
        BridgeOpts { mode: Mode::Immediate, reconnect_ms: 3000, fetch_ms: 600, tick_ms: 500 }
    }
}

enum Cmd {
    /// A truth change just happened: (Immediate mode) push it now.
    Poke(Poke),
    Prefetch(SimTrack),
    DropWs,
    Stop,
}

#[derive(Clone, Copy)]
enum Poke {
    Track,
    Pause,
    Resume,
    Seek,
}

struct FakeBridge {
    cmds: mpsc::UnboundedSender<Cmd>,
    /// (when, message type) for every message the fake sent.
    sent: Arc<Mutex<Vec<(Instant, String)>>>,
    spotify: Arc<Mutex<SimSpotify>>,
    handle: JoinHandle<()>,
}

impl FakeBridge {
    fn spawn(port: u16, spotify: Arc<Mutex<SimSpotify>>, opts: BridgeOpts) -> FakeBridge {
        let (cmds, rx) = mpsc::unbounded_channel();
        let sent = Arc::new(Mutex::new(Vec::new()));
        let handle = tokio::spawn(bridge_client(port, spotify.clone(), rx, opts, sent.clone()));
        FakeBridge { cmds, sent, spotify, handle }
    }
}

async fn bridge_client(port: u16, sp: Arc<Mutex<SimSpotify>>, mut cmds: mpsc::UnboundedReceiver<Cmd>, o: BridgeOpts, sent: Arc<Mutex<Vec<(Instant, String)>>>) {
    let mut first = true;
    'conn: loop {
        if !first {
            sleep(o.reconnect_ms).await;
        }
        first = false;
        let Ok((ws, _)) = tokio_tungstenite::connect_async(format!("ws://127.0.0.1:{port}")).await else {
            sleep(200).await;
            continue;
        };
        let (mut wtx, mut wrx) = ws.split();
        let (out, mut out_rx) = mpsc::unbounded_channel::<Value>();
        let sent2 = sent.clone();
        let writer = tokio::spawn(async move {
            while let Some(v) = out_rx.recv().await {
                sent2.lock().unwrap().push((Instant::now(), v["type"].as_str().unwrap_or("?").to_string()));
                if wtx.send(Message::text(v.to_string())).await.is_err() {
                    break;
                }
            }
            let _ = wtx.close().await;
        });

        let position = |sp: &SimSpotify| {
            json!({"type": "position", "position_ms": sp.pos(Instant::now()) as i64, "duration_ms": sp.track.duration_ms, "is_playing": sp.playing})
        };
        // Track + lyrics, as sendTrackAndLyrics: track_change at once, the
        // lyrics after the bridge's own fetch.
        let send_track = |sp: &SimSpotify, out: &mpsc::UnboundedSender<Value>, fetch: u64| {
            let _ = out.send(sp.track.track_change());
            let l = sp.track.lyrics("lyrics");
            let out = out.clone();
            let delay = match sp.plan {
                LyricsPlan::After(d) | LyricsPlan::PrefetchedThen(d) => d,
            };
            let _ = fetch;
            tokio::spawn(async move {
                sleep(delay).await;
                let _ = out.send(l);
            });
        };

        // onopen: hello, then the current track.
        let mut last_uri = {
            let g = sp.lock().unwrap();
            let _ = out.send(json!({"type": "hello", "version": "bench"}));
            send_track(&g, &out, o.fetch_ms);
            let _ = out.send(position(&g));
            g.track.uri.clone()
        };
        let mut was_playing = true;
        let mut last_replay = Instant::now();
        let mut tick = tokio::time::interval(Duration::from_millis(o.tick_ms));
        tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        loop {
            tokio::select! {
                m = wrx.next() => match m {
                    Some(Ok(Message::Text(t))) => {
                        if t.contains("request_state") && last_replay.elapsed() > Duration::from_millis(1500) {
                            last_replay = Instant::now();
                            let g = sp.lock().unwrap();
                            last_uri = g.track.uri.clone();
                            send_track(&g, &out, o.fetch_ms);
                            let _ = out.send(position(&g));
                        }
                    }
                    Some(Ok(_)) => {}
                    _ => { writer.abort(); continue 'conn; }
                },
                _ = tick.tick() => {
                    let g = sp.lock().unwrap();
                    if !g.playing {
                        if was_playing { let _ = out.send(json!({"type": "paused"})); }
                        was_playing = false;
                        let _ = out.send(position(&g));
                    } else {
                        was_playing = true;
                        if g.track.uri != last_uri {
                            last_uri = g.track.uri.clone();
                            send_track(&g, &out, o.fetch_ms);
                        }
                        let _ = out.send(position(&g));
                    }
                },
                c = cmds.recv() => match c {
                    None | Some(Cmd::Stop) => { writer.abort(); return; }
                    Some(Cmd::DropWs) => { writer.abort(); drop(wrx); continue 'conn; }
                    Some(Cmd::Prefetch(t)) => { let _ = out.send(t.lyrics("lyrics_prefetch")); }
                    Some(Cmd::Poke(p)) => {
                        if o.mode == Mode::Immediate {
                            let g = sp.lock().unwrap();
                            match p {
                                Poke::Track => { last_uri = g.track.uri.clone(); was_playing = true; send_track(&g, &out, o.fetch_ms); let _ = out.send(position(&g)); }
                                Poke::Pause => { was_playing = false; let _ = out.send(json!({"type": "paused"})); let _ = out.send(position(&g)); }
                                Poke::Resume => { was_playing = true; let _ = out.send(position(&g)); }
                                Poke::Seek => { let _ = out.send(position(&g)); }
                            }
                        }
                        // Poll500: the next tick notices by itself.
                    }
                },
            }
        }
    }
}

// ───────────────────────── fake Discord ─────────────────────────

pub struct Rx {
    at: Instant,
    /// args.activity: null = a clear.
    act: Value,
}

impl Rx {
    fn is_clear(&self) -> bool {
        self.act.is_null()
    }
    fn state(&self) -> &str {
        self.act["state"].as_str().unwrap_or("")
    }
    fn details(&self) -> &str {
        self.act["details"].as_str().unwrap_or("")
    }
    fn start_ms(&self) -> Option<i64> {
        self.act["timestamps"]["start"].as_i64()
    }
}

enum DCmd {
    Close,
    Open,
}

struct FakeDiscord {
    frames: Arc<Mutex<Vec<Rx>>>,
    ready_at: Arc<Mutex<Vec<Instant>>>,
    ctl: mpsc::UnboundedSender<DCmd>,
    handle: JoinHandle<()>,
}

async fn read_frame<R: AsyncReadExt + Unpin>(r: &mut R) -> std::io::Result<Value> {
    let mut h = [0u8; 8];
    r.read_exact(&mut h).await?;
    let n = u32::from_le_bytes(h[4..8].try_into().unwrap()) as usize;
    let mut body = vec![0u8; n];
    r.read_exact(&mut body).await?;
    serde_json::from_slice(&body).map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidData, e))
}

impl FakeDiscord {
    fn spawn(prefix: String, start_open: bool) -> FakeDiscord {
        let frames = Arc::new(Mutex::new(Vec::new()));
        let ready_at = Arc::new(Mutex::new(Vec::new()));
        let (ctl, mut rx) = mpsc::unbounded_channel::<DCmd>();
        let (f, r) = (frames.clone(), ready_at.clone());
        let name = format!("{prefix}0");
        // The first instance exists before this returns: a client that starts
        // at once must find the pipe, as it finds a running Discord.
        let mut first = start_open.then(|| ServerOptions::new().create(&name).unwrap());
        let handle = tokio::spawn(async move {
            let mut open = start_open;
            loop {
                if !open {
                    match rx.recv().await {
                        Some(DCmd::Open) => open = true,
                        Some(DCmd::Close) => {}
                        None => return,
                    }
                    continue;
                }
                let server = match first.take() {
                    Some(sv) => sv,
                    None => ServerOptions::new().create(&name).unwrap(),
                };
                let connected = tokio::select! {
                    r = server.connect() => r.is_ok(),
                    c = rx.recv() => match c {
                        Some(DCmd::Close) => { open = false; false }
                        Some(DCmd::Open) => false,
                        None => return,
                    },
                };
                if !connected {
                    continue;
                }
                let (mut rd, mut wr) = tokio::io::split(server);
                if read_frame(&mut rd).await.is_err() {
                    continue;
                }
                let ok = wr.write_all(&discord::frame(1, &json!({"evt": "READY", "data": {"user": {"username": "bench"}}}))).await;
                if ok.is_err() {
                    continue;
                }
                r.lock().unwrap().push(Instant::now());
                loop {
                    tokio::select! {
                        fr = read_frame(&mut rd) => match fr {
                            Ok(v) => {
                                if v["cmd"] == "SET_ACTIVITY" {
                                    f.lock().unwrap().push(Rx { at: Instant::now(), act: v["args"]["activity"].clone() });
                                }
                            }
                            Err(_) => break,
                        },
                        c = rx.recv() => match c {
                            Some(DCmd::Close) => { open = false; break; }
                            Some(DCmd::Open) => {}
                            None => return,
                        },
                    }
                }
            }
        });
        FakeDiscord { frames, ready_at, ctl, handle }
    }
    fn count(&self) -> usize {
        self.frames.lock().unwrap().len()
    }
}

// ───────────────────────── the pipeline under test ─────────────────────────

#[derive(Clone)]
pub struct Opts {
    bridge: BridgeOpts,
    store: bool,
    /// Serialise the snapshot on every change, as the Tauri `emit` does.
    serialize: bool,
    discord_open: bool,
    start_bridge_client: bool,
}

impl Default for Opts {
    fn default() -> Self {
        Opts { bridge: BridgeOpts::default(), store: true, serialize: false, discord_open: true, start_bridge_client: true }
    }
}

/// What the engine recorded arriving from the bridge socket.
struct Arrival {
    at: Instant,
    typ: String,
    uri: String,
    pos: i64,
    playing: Option<bool>,
}

struct Pipeline {
    engine: Arc<Engine>,
    port: u16,
    link: &'static discord::Link,
    discord: FakeDiscord,
    bridge: Option<FakeBridge>,
    spotify: Arc<Mutex<SimSpotify>>,
    arrivals: Arc<Mutex<Vec<Arrival>>>,
    emit_calls: Arc<AtomicU64>,
    emit_bytes: Arc<AtomicU64>,
    tasks: Vec<JoinHandle<()>>,
    dir: PathBuf,
}

static N: AtomicU64 = AtomicU64::new(0);

impl Pipeline {
    async fn start(first: SimTrack, plan: LyricsPlan, o: Opts) -> Pipeline {
        let id = N.fetch_add(1, Ordering::SeqCst);
        let tag = format!("{}-{}", std::process::id(), id);
        let dir = std::env::temp_dir().join(format!("statusify-bench-{tag}"));
        std::fs::create_dir_all(&dir).unwrap();
        let store = o.store.then(|| Store::open(&dir).unwrap());
        let calls = Arc::new(AtomicU64::new(0));
        let bytes = Arc::new(AtomicU64::new(0));
        let (c2, b2, ser) = (calls.clone(), bytes.clone(), o.serialize);
        let mut engine = Engine::new(store, move |s| {
            if ser {
                let j = serde_json::to_string(s).unwrap();
                b2.fetch_add(j.len() as u64, Ordering::Relaxed);
            }
            c2.fetch_add(1, Ordering::Relaxed);
        });
        // The harness measures the pipeline, not lrclib.net: no network.
        Arc::get_mut(&mut engine).unwrap().lrclib_enabled = AtomicBool::new(false);

        let mut tasks = Vec::new();
        // Raw bridge arrivals, stamped as the engine sees them.
        let arrivals = Arc::new(Mutex::new(Vec::new()));
        {
            let mut rx = engine.subscribe();
            let a = arrivals.clone();
            tasks.push(tokio::spawn(async move {
                loop {
                    match rx.recv().await {
                        Ok(Event::Bridge(v)) => a.lock().unwrap().push(Arrival {
                            at: Instant::now(),
                            typ: v["type"].as_str().unwrap_or("").to_string(),
                            uri: v["track_uri"].as_str().unwrap_or("").to_string(),
                            pos: v["position_ms"].as_f64().map_or(-1, |f| f as i64),
                            playing: v["is_playing"].as_bool(),
                        }),
                        Ok(_) => {}
                        Err(tokio::sync::broadcast::error::RecvError::Lagged(_)) => {}
                        Err(_) => break,
                    }
                }
            }));
        }

        // Discord: a private pipe, a private Link, exactly lib.rs::start_discord's wiring.
        let prefix = format!(r"\\.\pipe\statusify-bench-{tag}-");
        let discord = FakeDiscord::spawn(prefix.clone(), o.discord_open);
        let link: &'static discord::Link = Box::leak(Box::new(discord::Link::new(discord::RETRY, discord::PIPE_TIMEOUT)));
        let (tx, rx) = mpsc::unbounded_channel();
        let e = engine.clone();
        tasks.push(tokio::spawn(discord::run_on(link, Box::leak(prefix.into_boxed_str()), "123".into(), rx, move |st| match st {
            discord::Status::Connected(u) => e.update(|s| s.discord_user = Some(u)),
            discord::Status::Disconnected => e.update(|s| s.discord_user = None),
        })));
        tasks.push(tokio::spawn(presence::run_loop(engine.clone(), tx)));

        // The real bridge server on an OS-assigned port.
        let listener = bridge::bind(0).await.unwrap();
        let port = listener.local_addr().unwrap().port();
        tasks.push(tokio::spawn(bridge::serve(listener, engine.clone(), bridge::Outbox::default())));

        let now = Instant::now();
        let spotify = Arc::new(Mutex::new(SimSpotify { track: first, playing: true, pos0: 0.0, wall0: now, plan, track_started: now }));
        let bridge = o.start_bridge_client.then(|| FakeBridge::spawn(port, spotify.clone(), o.bridge));
        Pipeline { engine, port, link, discord, bridge, spotify, arrivals, emit_calls: calls, emit_bytes: bytes, tasks, dir }
    }

    fn b(&self) -> &FakeBridge {
        self.bridge.as_ref().unwrap()
    }

    // ── Spotify-side actions: the truth changes now; the bridge reports per its mode ──
    fn pause(&self) -> Instant {
        let t = Instant::now();
        {
            let mut g = self.spotify.lock().unwrap();
            let p = g.pos(t);
            g.set_pos(p, t);
            g.playing = false;
        }
        let _ = self.b().cmds.send(Cmd::Poke(Poke::Pause));
        t
    }
    fn resume(&self) -> Instant {
        let t = Instant::now();
        {
            let mut g = self.spotify.lock().unwrap();
            let p = g.pos(t);
            g.set_pos(p, t);
            g.playing = true;
        }
        let _ = self.b().cmds.send(Cmd::Poke(Poke::Resume));
        t
    }
    fn seek(&self, to_ms: i64) -> Instant {
        let t = Instant::now();
        self.spotify.lock().unwrap().set_pos(to_ms as f64, t);
        let _ = self.b().cmds.send(Cmd::Poke(Poke::Seek));
        t
    }
    fn change_track(&self, tr: SimTrack, plan: LyricsPlan) -> Instant {
        let t = Instant::now();
        {
            let mut g = self.spotify.lock().unwrap();
            g.track = tr;
            g.plan = plan;
            g.pos0 = 0.0;
            g.wall0 = t;
            g.track_started = t;
            g.playing = true;
        }
        let _ = self.b().cmds.send(Cmd::Poke(Poke::Track));
        t
    }
    fn prefetch(&self, tr: &SimTrack) {
        let _ = self.b().cmds.send(Cmd::Prefetch(tr.clone()));
    }

    // ── observation ──
    fn frames_since(&self, t: Instant) -> Vec<(Instant, Value)> {
        self.discord.frames.lock().unwrap().iter().filter(|f| f.at >= t).map(|f| (f.at, f.act.clone())).collect()
    }
    async fn first_frame_after(&self, t: Instant, limit: Duration, pred: impl Fn(&Rx) -> bool) -> Option<Instant> {
        wait_for(limit, || self.discord.frames.lock().unwrap().iter().find(|f| f.at >= t && pred(f)).map(|f| f.at)).await
    }
    fn arrival_after(&self, t: Instant, pred: impl Fn(&Arrival) -> bool) -> Option<Instant> {
        self.arrivals.lock().unwrap().iter().find(|a| a.at >= t && pred(a)).map(|a| a.at)
    }
    async fn wait_connected(&self) {
        wait_for(Duration::from_secs(10), || self.engine.snapshot().discord_user.is_some().then_some(())).await.expect("fake Discord never connected");
    }
    /// Wait for the first presence frame, i.e. the pipeline is live and steady.
    async fn wait_first_frame(&self) {
        wait_for(Duration::from_secs(15), || (self.discord.count() > 0).then_some(())).await.expect("no first SET_ACTIVITY");
    }

    async fn stop(self) {
        if let Some(b) = &self.bridge {
            let _ = b.cmds.send(Cmd::Stop);
            b.handle.abort();
        }
        for t in &self.tasks {
            t.abort();
        }
        self.discord.handle.abort();
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

fn track_with(n: usize, sheet: Option<Sheet>) -> SimTrack {
    SimTrack::new(n, sheet, 240_000)
}

// ───────────────────────── frame analysis ─────────────────────────

/// Sliding-window rate audit over frame arrival times (clears count: they are
/// SET_ACTIVITY frames too).
fn rate_audit(times: &[Instant]) -> Value {
    let w = Duration::from_secs(20);
    let mut max_in_window = 0;
    let mut violating = 0;
    for (j, t) in times.iter().enumerate() {
        let n = times[..=j].iter().filter(|x| t.duration_since(**x) < w).count();
        max_in_window = max_in_window.max(n);
        if n > presence::RATE_CALLS {
            violating += 1;
        }
    }
    let tightest = (presence::RATE_CALLS..times.len()).map(|j| ms(times[j].duration_since(times[j - presence::RATE_CALLS]))).fold(f64::INFINITY, f64::min);
    json!({"frames": times.len(), "max_frames_in_any_20s": max_in_window, "frames_over_budget": violating,
           "tightest_6_frame_span_ms": if tightest.is_finite() { json!(r1(tightest)) } else { Value::Null }})
}

/// How well the presence followed a song played from `t0` at 1x without
/// pausing: per-line first-shown lateness, wrong-line time, waste.
fn follow_audit(sheet: &Sheet, t0: Instant, frames: &[(Instant, Value)], dropped_counter: u32) -> Value {
    let n = sheet.lines.len();
    let w = |ms_: i64| t0 + Duration::from_millis(ms_.max(0) as u64);
    let nonclear: Vec<&(Instant, Value)> = frames.iter().filter(|(_, a)| !a.is_null()).collect();
    // when each frame is "current": from its arrival to the next frame's
    let mut shown_intervals: Vec<Vec<(Instant, Instant)>> = vec![vec![]; n];
    let end_of_song = w(sheet.duration_ms);
    for (k, (at, act)) in frames.iter().enumerate() {
        if act.is_null() {
            continue;
        }
        let until = frames.get(k + 1).map_or(end_of_song, |f| f.0);
        for i in sheet.lines_in(act["state"].as_str().unwrap_or("")) {
            shown_intervals[i].push((*at, until));
        }
    }
    let mut late = vec![];
    let mut early = 0;
    let mut never = 0;
    let (mut sung, mut off) = (0.0f64, 0.0f64);
    let mut by_line = vec![];
    for i in 0..n {
        let s = w(sheet.lines[i].0);
        let e = if i + 1 < n { w(sheet.lines[i + 1].0) } else { w(sheet.lines[i].0 + 5000) };
        let dur = ms(e.duration_since(s));
        sung += dur;
        let shown: f64 = shown_intervals[i]
            .iter()
            .map(|(a, b)| {
                let (a, b) = ((*a).max(s), (*b).min(e));
                if b > a { ms(b.duration_since(a)) } else { 0.0 }
            })
            .sum();
        off += (dur - shown).max(0.0);
        match shown_intervals[i].first() {
            None => never += 1,
            Some((a, _)) => {
                if *a < s {
                    early += 1;
                    late.push(0.0);
                } else {
                    late.push(ms(a.duration_since(s)));
                }
            }
        }
        by_line.push(dur - shown);
    }
    // waste: a frame identical to the one before it, or one that lands after
    // every line it carries has ended.
    let mut dup = 0;
    let mut stale = 0;
    for k in 0..nonclear.len() {
        if k > 0 && nonclear[k].1["state"] == nonclear[k - 1].1["state"] && nonclear[k].1["details"] == nonclear[k - 1].1["details"] {
            dup += 1;
        }
        let lines = sheet.lines_in(nonclear[k].1["state"].as_str().unwrap_or(""));
        if let Some(&last) = lines.last() {
            let end = if last + 1 < n { w(sheet.lines[last + 1].0) } else { w(sheet.lines[last].0 + 5000) };
            if nonclear[k].0 >= end {
                stale += 1;
            }
        }
    }
    let clears = frames.iter().filter(|(_, a)| a.is_null()).count();
    let times: Vec<Instant> = frames.iter().map(|f| f.0).collect();
    json!({
        "lines": n,
        "line_first_shown_lateness_ms": stats(&late),
        "lines_shown_before_their_start": early,
        "lines_never_shown": never,
        "wrong_line_time_pct": r1(off / sung * 100.0),
        "engine_dropped_lines": dropped_counter,
        "frames_total": frames.len(),
        "frames_clear": clears,
        "frames_duplicate_of_previous": dup,
        "frames_stale_on_arrival": stale,
        "rate": rate_audit(&times),
    })
}

// ───────────────────────── tests ─────────────────────────

fn mt() -> tokio::runtime::Runtime {
    tokio::runtime::Builder::new_multi_thread().worker_threads(4).enable_all().build().unwrap()
}

/// Always-on smoke test: the wiring works and a track change reaches the fake
/// Discord. Keeps the harness from rotting under `cargo test`.
#[test]
fn smoke_pipeline_delivers_a_frame() {
    mt().block_on(async {
        let p = Pipeline::start(track_with(1, Some(Sheet::evenly(6, 3000, 0))), LyricsPlan::After(100), Opts::default()).await;
        let t0 = Instant::now();
        let got = p.first_frame_after(t0, Duration::from_secs(8), |f| !f.is_clear()).await;
        assert!(got.is_some(), "no SET_ACTIVITY reached the fake Discord");
        assert!(p.discord.frames.lock().unwrap()[0].details().contains("Bench Track 1"));
        p.stop().await;
    });
}

/// (a) track change -> first SET_ACTIVITY, downstream of the socket only
/// (Immediate bridge). Four lyric situations.
#[test]
#[ignore = "benchmark"]
fn bench_a_track_change() {
    mt().block_on(async {
        let load = LoadProbe::start();
        let mut out = serde_json::Map::new();
        // The settle gate depends on whether another change happened in the last
        // 3 s (a skip burst), so the change comes either ~4 s after the previous
        // one (quiet: the usual way a track ends or is skipped) or ~2 s after it.
        let cases: [(&str, LyricsPlan, bool, u64); 5] = [
            ("prefetched_bridge_answers_600ms", LyricsPlan::PrefetchedThen(600), true, 3500),
            ("fresh_bridge_answers_800ms", LyricsPlan::After(800), true, 3500),
            ("fresh_bridge_answers_2500ms", LyricsPlan::After(2500), true, 3500),
            ("no_lyrics_anywhere", LyricsPlan::After(100), false, 3500),
            ("prefetched_bridge_answers_600ms_2s_after_previous_change", LyricsPlan::PrefetchedThen(600), true, 1500),
        ];
        for (name, plan, has_lyrics, quiet_ms) in cases {
            let mk = || has_lyrics.then(|| Sheet::evenly(20, 6000, 0));
            let (mut ws_hop, mut total, mut first_with_lyric, mut first_is_title) = (vec![], vec![], vec![], 0);
            let mut frames_in_4s = vec![];
            let reps = 8usize;
            for rep in 0..reps {
                let p = Pipeline::start(track_with(1, mk()), LyricsPlan::After(100), Opts::default()).await;
                p.wait_first_frame().await;
                sleep(quiet_ms).await;
                let next = track_with(2 + rep, mk());
                if matches!(plan, LyricsPlan::PrefetchedThen(_)) {
                    p.prefetch(&next);
                    sleep(200).await;
                }
                let t = p.change_track(next.clone(), plan);
                let title = next.title.clone();
                let hit = p.first_frame_after(t, Duration::from_secs(8), |f| !f.is_clear() && f.details().contains(&title)).await.expect("no frame for the new track");
                total.push(ms(hit.duration_since(t)));
                let ws = p.arrival_after(t, |a| a.typ == "track_change" && a.uri == next.uri).unwrap();
                ws_hop.push(ms(ws.duration_since(t)));
                let frames = p.frames_since(t);
                if frames[0].1["state"].as_str().unwrap_or("").starts_with("— ") {
                    first_is_title += 1;
                } else {
                    first_with_lyric.push(ms(frames[0].0.duration_since(t)));
                }
                sleep(4000u64.saturating_sub(ms(t.elapsed()) as u64)).await;
                frames_in_4s.push(p.frames_since(t).len() as f64);
                p.stop().await;
            }
            out.insert(
                name.into(),
                json!({"track_change_to_first_SET_ACTIVITY_ms": stats(&total), "event_to_engine_arrival_ms": stats(&ws_hop),
                       "reps": reps, "frames_sent_in_first_4s": stats(&frames_in_4s), "first_frame_was_title_only": first_is_title, "first_frame_carried_a_lyric_line_ms": stats(&first_with_lyric)}),
            );
        }
        out.insert("settle_ms".into(), json!({"unknown_lyrics_or_burst": presence::CALIBRATION.as_millis(), "known_lyrics": presence::KNOWN_SETTLE.as_millis()}));
        out.insert("machine_cpu_pct_during".into(), json!(load.pct()));
        report("a_track_change_downstream", Value::Object(out));
    });
}

/// (b)+(g) line-start -> delivery jitter, wrong-line time and wasted/violating
/// frames, on three songs played in real time in parallel pipelines.
#[test]
#[ignore = "benchmark"]
fn bench_b_line_jitter_and_waste() {
    mt().block_on(async {
        let load = LoadProbe::start();
        let songs: Vec<(&str, Sheet)> = vec![
            // pure pipeline jitter: 5 s lines never touch the rate limit
            ("sparse_5s_lines_unconstrained", Sheet::evenly(24, 5000, 2000)),
            // a real fast song from history.db (timings only)
            ("fast_real_65_lines_39_per_min", Sheet::fixture("fast")),
            // the repo's own stress: 1.2 s lines of 75 chars
            ("dense_stress_1200ms_75chars", Sheet::dense()),
        ];
        let mut handles = vec![];
        for (name, sheet) in songs {
            handles.push(tokio::spawn(async move {
                let p = Pipeline::start(track_with(1, Some(sheet.clone())), LyricsPlan::After(300), Opts::default()).await;
                let t0 = p.spotify.lock().unwrap().track_started;
                sleep(sheet.duration_ms as u64 + 1500).await;
                let frames = p.frames_since(t0);
                let dropped = p.engine.core.with(|c| c.dropped_lines);
                let rec = follow_audit(&sheet, t0, &frames, dropped);
                p.stop().await;
                (name, rec)
            }));
        }
        let mut out = serde_json::Map::new();
        for h in handles {
            let (n, r) = h.await.unwrap();
            out.insert(n.into(), r);
        }
        out.insert("machine_cpu_pct_during".into(), json!(load.pct()));
        report("b_g_follow_and_waste", Value::Object(out));
    });
}

/// (c) reaction to pause / resume / seek, plus seek-spam and skip-spam
/// coalescing. Immediate bridge: downstream hops only.
#[test]
#[ignore = "benchmark"]
fn bench_c_pause_resume_seek() {
    mt().block_on(async {
        let load = LoadProbe::start();
        let sheet = Sheet::evenly(12, 30_000, 0);
        let mut out = serde_json::Map::new();

        // pause / resume cycles
        {
            let p = Pipeline::start(track_with(1, Some(sheet.clone())), LyricsPlan::After(100), Opts::default()).await;
            p.wait_first_frame().await;
            sleep(2000).await;
            let (mut pause, mut resume, mut resume_first_is_title) = (vec![], vec![], 0);
            for _ in 0..6 {
                let t = p.pause();
                let hit = p.first_frame_after(t, Duration::from_secs(5), |f| f.is_clear()).await;
                pause.push(hit.map_or(f64::NAN, |h| ms(h.duration_since(t))));
                sleep(3000).await;
                let t = p.resume();
                let hit = p.first_frame_after(t, Duration::from_secs(8), |f| !f.is_clear()).await;
                resume.push(hit.map_or(f64::NAN, |h| ms(h.duration_since(t))));
                if let Some(f) = p.frames_since(t).first() {
                    if f.1["state"].as_str().unwrap_or("").starts_with("— ") {
                        resume_first_is_title += 1;
                    }
                }
                sleep(5000).await;
            }
            out.insert("pause_to_clear_ms".into(), stats(&pause.iter().copied().filter(|x| x.is_finite()).collect::<Vec<_>>()));
            out.insert("resume_to_first_SET_ACTIVITY_ms".into(), stats(&resume.iter().copied().filter(|x| x.is_finite()).collect::<Vec<_>>()));
            out.insert("resume_first_frame_title_only".into(), json!(resume_first_is_title));
            p.stop().await;
        }

        // seeks to a different line each time
        {
            let p = Pipeline::start(track_with(1, Some(sheet.clone())), LyricsPlan::After(100), Opts::default()).await;
            p.wait_first_frame().await;
            sleep(2000).await;
            let mut v = vec![];
            for k in 0..8usize {
                let line = (k * 5 + 3) % 12;
                let tok = Sheet::token(line);
                let t = p.seek(sheet.lines[line].0 + 4000);
                let hit = p.first_frame_after(t, Duration::from_secs(25), |f| f.state().contains(&tok)).await;
                v.push(hit.map_or(f64::NAN, |h| ms(h.duration_since(t))));
                sleep(5500).await;
            }
            out.insert("seek_to_new_line_frame_ms".into(), stats(&v.iter().copied().filter(|x| x.is_finite()).collect::<Vec<_>>()));
            out.insert("seek_timeouts".into(), json!(v.iter().filter(|x| !x.is_finite()).count()));
            p.stop().await;
        }

        // seek spam: drag the bar across 10 lines in 2.5 s, then stop
        {
            let p = Pipeline::start(track_with(1, Some(sheet.clone())), LyricsPlan::After(100), Opts::default()).await;
            p.wait_first_frame().await;
            sleep(2000).await;
            let before = p.discord.count();
            let t = Instant::now();
            let mut t_last = t;
            for k in 0..10usize {
                t_last = p.seek(sheet.lines[(k + 1) % 12].0 + 3000);
                sleep(250).await;
            }
            let tok = Sheet::token(10);
            let hit = p.first_frame_after(t, Duration::from_secs(25), |f| f.state().contains(&tok)).await;
            sleep(1000).await;
            let sent = p.discord.count() - before;
            out.insert(
                "seek_spam_10_seeks_in_2.5s".into(),
                json!({"frames_sent": sent, "final_line_shown_after_last_seek_ms": hit.map(|h| r1(ms(h.duration_since(t_last)))),
                       "rate": rate_audit(&p.frames_since(t).iter().map(|f| f.0).collect::<Vec<_>>())}),
            );
            p.stop().await;
        }

        // skip spam: 8 track changes 400 ms apart
        {
            let p = Pipeline::start(track_with(1, Some(Sheet::evenly(6, 6000, 0))), LyricsPlan::After(100), Opts::default()).await;
            p.wait_first_frame().await;
            sleep(2000).await;
            let before = p.discord.count();
            let mut last = Instant::now();
            for k in 0..8usize {
                last = p.change_track(track_with(10 + k, Some(Sheet::evenly(6, 6000, 0))), LyricsPlan::After(300));
                sleep(400).await;
            }
            let title = p.spotify.lock().unwrap().track.title.clone();
            let hit = p.first_frame_after(last, Duration::from_secs(8), |f| f.details().contains(&title)).await;
            sleep(2500).await;
            out.insert(
                "skip_spam_8_changes_400ms_apart".into(),
                json!({"frames_sent": p.discord.count() - before, "last_change_to_final_track_frame_ms": hit.map(|h| r1(ms(h.duration_since(last))))}),
            );
            p.stop().await;
        }
        out.insert("machine_cpu_pct_during".into(), json!(load.pct()));
        report("c_pause_resume_seek_downstream", Value::Object(out));
    });
}

/// (d) the Discord pipe closes and reopens.
#[test]
#[ignore = "benchmark"]
fn bench_d_discord_reconnect() {
    mt().block_on(async {
        let load = LoadProbe::start();
        let mut handles = vec![];
        for (name, reopen_ms) in [("pipe_back_after_300ms", 300u64), ("pipe_back_after_7s", 7000u64)] {
            handles.push(tokio::spawn(async move {
                let p = Pipeline::start(track_with(1, Some(Sheet::evenly(40, 5000, 0))), LyricsPlan::After(100), Opts::default()).await;
                p.wait_first_frame().await;
                sleep(3000).await;
                let ready_before = p.discord.ready_at.lock().unwrap().len();
                let t = Instant::now();
                let _ = p.discord.ctl.send(DCmd::Close);
                sleep(reopen_ms).await;
                let t_open = Instant::now();
                let _ = p.discord.ctl.send(DCmd::Open);
                let ready = wait_for(Duration::from_secs(60), || p.discord.ready_at.lock().unwrap().get(ready_before).copied()).await;
                let frame = p.first_frame_after(t, Duration::from_secs(60), |f| !f.is_clear()).await;
                let r = json!({
                    "close_to_reconnected_ms": ready.map(|r| r1(ms(r.duration_since(t)))),
                    "pipe_reopened_to_reconnected_ms": ready.map(|r| r1(ms(r.duration_since(t_open)))),
                    "close_to_first_frame_ms": frame.map(|f| r1(ms(f.duration_since(t)))),
                    "pipe_reopened_to_first_frame_ms": frame.map(|f| r1(ms(f.duration_since(t_open)))),
                });
                p.stop().await;
                (name, r)
            }));
        }
        let mut out = serde_json::Map::new();
        for h in handles {
            let (n, r) = h.await.unwrap();
            out.insert(n.into(), r);
        }
        out.insert("constants_ms".into(), json!({"retry_first": discord::RETRY.first.as_millis(), "retry_cap_pipe_missing": discord::RETRY.cap_missing.as_millis(), "retry_cap_other": discord::RETRY.cap_other.as_millis(), "pipe_timeout": discord::PIPE_TIMEOUT.as_millis(), "settle_unknown_lyrics": presence::CALIBRATION.as_millis(), "settle_known_lyrics": presence::KNOWN_SETTLE.as_millis(), "settle_resume": presence::RESUME_SETTLE.as_millis()}));
        out.insert("machine_cpu_pct_during".into(), json!(load.pct()));
        report("d_discord_pipe_recovery", Value::Object(out));
    });
}

/// (e) the bridge WebSocket drops; plus the starvation a second / half-open
/// connection suffers because bridge::serve handles one socket at a time.
#[test]
#[ignore = "benchmark"]
fn bench_e_bridge_drop() {
    mt().block_on(async {
        let load = LoadProbe::start();
        let mut out = serde_json::Map::new();
        for (name, reconnect_ms) in [("js_reconnect_timer_3000ms", 3000u64), ("reconnect_timer_500ms", 500u64)] {
            let mut o = Opts::default();
            o.bridge.reconnect_ms = reconnect_ms;
            let p = Pipeline::start(track_with(1, Some(Sheet::evenly(40, 5000, 0))), LyricsPlan::After(100), o).await;
            p.wait_first_frame().await;
            sleep(4000).await;
            let n0 = p.discord.count();
            let hellos0 = p.arrivals.lock().unwrap().iter().filter(|a| a.typ == "hello").count();
            let t = Instant::now();
            let _ = p.b().cmds.send(Cmd::DropWs);
            let clear = p.first_frame_after(t, Duration::from_secs(5), |f| f.is_clear()).await;
            let back = wait_for(Duration::from_secs(15), || {
                let a = p.arrivals.lock().unwrap();
                a.iter().filter(|x| x.typ == "hello").nth(hellos0).map(|x| x.at)
            })
            .await;
            let frame = match clear {
                Some(c) => p.first_frame_after(c, Duration::from_secs(20), |f| !f.is_clear()).await,
                None => None,
            };
            sleep(2000).await;
            let blank = match (clear, frame) {
                (Some(c), Some(f)) => Some(r1(ms(f.duration_since(c)))),
                _ => None,
            };
            let lyric_back = frame.map(|f| {
                let g = p.frames_since(f);
                g.first().map(|x| !x.1["state"].as_str().unwrap_or("").starts_with("— ")).unwrap_or(false)
            });
            out.insert(
                name.into(),
                json!({
                    "drop_to_presence_cleared_ms": clear.map(|c| r1(ms(c.duration_since(t)))),
                    "drop_to_bridge_reconnected_ms": back.map(|b| r1(ms(b.duration_since(t)))),
                    "drop_to_presence_back_ms": frame.map(|f| r1(ms(f.duration_since(t)))),
                    "presence_blank_for_ms": blank,
                    "first_frame_back_carried_a_lyric": lyric_back,
                    "frames_spent_by_the_blip": p.discord.count() - n0,
                }),
            );
            p.stop().await;
        }

        // second connection while the first is open (a reload, or a half-open
        // zombie): bridge::serve accepts one socket at a time.
        {
            let l = bridge::bind(0).await.unwrap();
            let port = l.local_addr().unwrap().port();
            let engine = Engine::new(None, |_| {});
            tokio::spawn(bridge::serve(l, engine.clone(), bridge::Outbox::default()));
            let (mut a, _) = tokio_tungstenite::connect_async(format!("ws://127.0.0.1:{port}")).await.unwrap();
            let _ = a.next().await; // request_state; then A goes silent: a half-open peer
            let t = Instant::now();
            let b = tokio::time::timeout(Duration::from_secs(6), tokio_tungstenite::connect_async(format!("ws://127.0.0.1:{port}"))).await;
            let starved = b.is_err();
            let waited = ms(t.elapsed());
            drop(a);
            let t2 = Instant::now();
            let b2 = tokio::time::timeout(Duration::from_secs(6), tokio_tungstenite::connect_async(format!("ws://127.0.0.1:{port}"))).await;
            let after = b2.is_ok().then(|| r1(ms(t2.elapsed())));
            // and: a connected-but-silent bridge is never noticed
            let still_connected_after_silence = engine.snapshot().bridge_connected;
            out.insert(
                "second_connection_while_first_is_silent".into(),
                json!({"second_client_served_within_6s": !starved, "waited_ms": r1(waited),
                       "served_ms_after_first_dropped": after, "engine_still_reports_bridge_connected_after_silence": still_connected_after_silence}),
            );
        }
        // a TCP connection that never completes the WebSocket handshake (a stuck
        // local process): accept_async has no timeout, so everyone queues behind it.
        {
            let l = bridge::bind(0).await.unwrap();
            let port = l.local_addr().unwrap().port();
            let engine = Engine::new(None, |_| {});
            tokio::spawn(bridge::serve(l, engine.clone(), bridge::Outbox::default()));
            let idle = tokio::net::TcpStream::connect(("127.0.0.1", port)).await.unwrap();
            sleep(100).await;
            let b = tokio::time::timeout(Duration::from_secs(6), tokio_tungstenite::connect_async(format!("ws://127.0.0.1:{port}"))).await;
            out.insert("real_client_while_an_idle_tcp_peer_holds_the_accept_loop".into(), json!({"served_within_6s": b.is_ok()}));
            drop(idle);
        }
        out.insert("machine_cpu_pct_during".into(), json!(load.pct()));
        report("e_bridge_ws_drop", Value::Object(out));
    });
}

/// (f) CPU while idle and playing, and the unit costs behind it.
#[test]
#[ignore = "benchmark"]
fn bench_f_idle_cpu() {
    mt().block_on(async {
        let mut out = serde_json::Map::new();
        let variants: [(&str, bool, bool, bool); 4] = [
            ("playing_2Hz_positions_noop_emit", true, false, true),
            ("playing_2Hz_positions_serialising_emit", true, true, true),
            ("paused_2Hz_positions", false, false, true),
            ("no_track_no_bridge_client_discord_connected", false, false, false),
        ];
        for (name, playing, serialize, client) in variants {
            let mut o = Opts::default();
            o.serialize = serialize;
            o.start_bridge_client = client;
            let p = Pipeline::start(track_with(1, Some(Sheet::fixture("fast"))), LyricsPlan::After(100), o).await;
            p.wait_connected().await;
            if client {
                sleep(4000).await;
                if !playing {
                    p.pause();
                    sleep(3000).await;
                }
            } else {
                sleep(2000).await;
            }
            let (cpu0, wall0, calls0, bytes0, load) = (process_cpu_ms(), Instant::now(), p.emit_calls.load(Ordering::Relaxed), p.emit_bytes.load(Ordering::Relaxed), LoadProbe::start());
            sleep(30_000).await;
            let wall = ms(wall0.elapsed());
            let cpu = process_cpu_ms() - cpu0;
            let calls = p.emit_calls.load(Ordering::Relaxed) - calls0;
            let bytes = p.emit_bytes.load(Ordering::Relaxed) - bytes0;
            out.insert(
                name.into(),
                json!({"window_s": r1(wall / 1000.0), "process_cpu_ms": r1(cpu), "cpu_pct_of_one_core": r1(cpu / wall * 100.0),
                       "cpu_ms_per_second": r1(cpu / wall * 1000.0), "cpu_clock_resolution_note": "GetProcessTimes ticks at the scheduler quantum (about 15.6 ms), so a 30 s window is good to +-15.6 ms", "on_change_calls_per_s": r1(calls as f64 / (wall / 1000.0)),
                       "snapshot_json_bytes_per_call": if calls > 0 && serialize { json!(bytes / calls) } else { Value::Null },
                       "machine_cpu_pct_during": load.pct()}),
            );
            p.stop().await;
        }
        report("f_idle_cpu", Value::Object(out));
    });
}

/// (f, unit costs) what one 50 ms presence wakeup and one position message cost.
#[test]
#[ignore = "benchmark"]
fn bench_f_unit_costs() {
    use crate::engine::plan;
    use crate::lyrics::{instrumental_gaps, Lyrics};
    let mut out = serde_json::Map::new();
    let sheet = Sheet::fixture("fast");
    let lyrics = Lyrics {
        mode: "synced".into(),
        synced: sheet.lines.iter().map(|(s, w)| crate::lyrics::Line { start_ms: *s, words: w.clone() }).collect(),
        plain: vec![],
        source: "Spicy".into(),
    };
    let mut e = Engine::new(None, |_| {});
    Arc::get_mut(&mut e).unwrap().lrclib_enabled = AtomicBool::new(false);
    e.handle(&json!({"type": "track_change", "track_uri": "spotify:track:x", "artist": "A", "title": "T", "duration_ms": sheet.duration_ms}));
    e.set_lyrics(lyrics.clone());
    // a plausible extras payload (palette, queue, translation are bigger in the real app)
    e.set_extra("core", json!({"line": "x", "next_line": "y", "rpc_enabled": true, "discord_line": "z"}));
    e.update(|s| s.discord_user = Some("bench".into()));
    let cdir = std::env::temp_dir().join(format!("statusify-bench-cfg-{}", std::process::id()));
    std::fs::create_dir_all(&cdir).unwrap();
    e.set_config(Arc::new(crate::config::Config::open(&cdir)));

    let time = |n: u32, mut f: Box<dyn FnMut()>| {
        let t = Instant::now();
        for _ in 0..n {
            f();
        }
        ms(t.elapsed()) * 1000.0 / n as f64
    };
    let e2 = e.clone();
    out.insert("engine_snapshot_clone_us".into(), json!(r1(time(100_000, Box::new(move || { std::hint::black_box(e2.snapshot()); })))));
    let snap = e.snapshot();
    out.insert(
        "snapshot_to_json_us".into(),
        json!(r1(time(20_000, { let s = snap.clone(); Box::new(move || { std::hint::black_box(serde_json::to_string(&s).unwrap()); }) }))),
    );
    out.insert("snapshot_json_bytes".into(), json!(serde_json::to_string(&snap).unwrap().len()));
    out.insert("lyrics_deep_compare_us".into(), json!(r1(time(200_000, { let (a, b) = (lyrics.clone(), lyrics.clone()); Box::new(move || { std::hint::black_box(a == b); }) }))));

    // one steady-state presence tick
    let t0 = Instant::now();
    let mut p = presence::Presence::new(true, t0);
    let set = presence::Settings::default();
    let mut s = snap.clone();
    s.position_ms = 30_000;
    s.position_at_ms = crate::state::now_ms();
    let now_ms = s.position_at_ms;
    p.tick(&s, t0, now_ms, &set);
    let mut k = 0u64;
    let tick_us = time(50_000, Box::new(move || {
        k += 1;
        std::hint::black_box(p.tick(&s, t0 + Duration::from_millis(k % 10), now_ms + (k % 10) as i64, &set));
    }));
    out.insert("presence_tick_steady_us".into(), json!(r1(tick_us)));
    {
        let (e3, s3) = (e.clone(), snap.clone());
        out.insert("presence_settings_from_engine_us".into(), json!(r1(time(100_000, Box::new(move || { std::hint::black_box(presence::Settings::from_engine(&e3, &s3)); })))));
    }
    out.insert("presence_wakeups_per_s_nominal".into(), json!(1000 / 50));
    {
        let (mut mn, mut mx, mut cur) = (0u32, 0u32, 0u32);
        unsafe { NtQueryTimerResolution(&mut mn, &mut mx, &mut cur) };
        out.insert("system_timer_resolution_ms_now".into(), json!({"current": cur as f64 / 10_000.0, "finest": mx as f64 / 10_000.0, "coarsest": mn as f64 / 10_000.0}));
        let rt = mt();
        let v: Vec<f64> = rt.block_on(async {
            let mut v = vec![];
            for _ in 0..200 {
                let t = Instant::now();
                tokio::time::sleep(Duration::from_millis(50)).await;
                v.push(ms(t.elapsed()));
            }
            v
        });
        out.insert("tokio_sleep_50ms_actual_ms".into(), stats(&v));
    }
    out.insert("log_call_us_to_stderr".into(), json!(r1(time(5_000, Box::new(|| crate::log("bench log line  ·  Now playing  ·  A — T"))))));

    // planner cost: one replan (runs after every send and at half horizon)
    for (name, sh) in [("fast", Sheet::fixture("fast")), ("dense", Sheet::dense()), ("typical", Sheet::fixture("typical"))] {
        let l = Lyrics {
            mode: "synced".into(),
            synced: sh.lines.iter().map(|(s, w)| crate::lyrics::Line { start_ms: *s, words: w.clone() }).collect(),
            plain: vec![],
            source: "x".into(),
        };
        let gaps = instrumental_gaps(&l.synced, sh.duration_ms);
        let units = plan::build_units(&l, sh.duration_ms, &gaps);
        let mut times = vec![];
        let lim = plan::Limits::default();
        for k in 0..150usize {
            let pos = (k as f64 * sh.duration_ms as f64 / 150.0).floor();
            // a ledger with 4 recent frames makes the search work hardest
            let hist = [pos - 15000.0, pos - 11000.0, pos - 7000.0, pos - 2000.0];
            let t = Instant::now();
            std::hint::black_box(plan::plan(&units, pos, &hist, &[], pos, lim));
            times.push(ms(t.elapsed()));
        }
        out.insert(format!("planner_replan_ms_{name}"), stats(&times));
    }
    let _ = std::fs::remove_dir_all(&cdir);
    out.insert("note".into(), json!("single-thread micro timings on this box; build profile per run.sh"));
    report("f_unit_costs", Value::Object(out));
}

/// (JS hop) the REAL lyrics-bridge.js against a stub Spicetify, with the real
/// server, engine and presence behind it. Rust drives the stub over stdin at
/// random phases of the bridge's 500 ms timer. Needs node.
#[test]
#[ignore = "benchmark"]
fn bench_js_e2e() {
    mt().block_on(async {
        let (bridge_js, probe) = js_paths();
        let load = LoadProbe::start();
        let p = Pipeline::start(track_with(1, None), LyricsPlan::After(100), Opts { start_bridge_client: false, ..Default::default() }).await;
        p.wait_connected().await;
        let n: usize = std::env::var("BENCH_JS_TRIALS").ok().and_then(|x| x.parse().ok()).unwrap_or(6);
        let mut child = KillOnDrop(
            std::process::Command::new("node")
                .arg(&probe)
                .args(["--port", &p.port.to_string(), "--bridge", &bridge_js, "--scenario", "e2e"])
                .stdin(std::process::Stdio::piped())
                .stdout(std::process::Stdio::null())
                .stderr(std::process::Stdio::inherit())
                .spawn()
                .expect("node"),
        );
        let mut stdin = child.0.stdin.take().unwrap();
        // the stub's first track (n=0) plays; wait until it is on the profile
        wait_for(Duration::from_secs(30), || (p.discord.count() > 0).then_some(())).await.expect("bridge JS never produced a presence frame");
        sleep(3000).await;
        let mut seed = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos() as u64 | 1;
        let mut phase = move || {
            seed ^= seed << 13;
            seed ^= seed >> 7;
            seed ^= seed << 17;
            seed % 500
        };
        let (mut songchange, mut pause, mut resume, mut seek) = (vec![], vec![], vec![], vec![]);
        let (mut sc_ws, mut p_ws, mut r_ws, mut s_ws) = (vec![], vec![], vec![], vec![]);
        for i in 0..n {
            // 1) song change
            sleep(phase()).await;
            let k = i + 1;
            let (uri, title) = (format!("spotify:track:bench{k:04}"), format!("Bench Track {k}"));
            let t = Instant::now();
            send_line(&mut stdin, &format!("songchange {k}"));
            let f = p.first_frame_after(t, Duration::from_secs(10), |f| !f.is_clear() && f.details().contains(&title)).await;
            if let Some(w) = p.arrival_after(t, |a| a.typ == "track_change" && a.uri == uri) {
                sc_ws.push(ms(w.duration_since(t)));
            }
            if let Some(f) = f {
                songchange.push(ms(f.duration_since(t)));
            }
            sleep(6000).await;
            // 2) pause
            sleep(phase()).await;
            let t = Instant::now();
            send_line(&mut stdin, "pause");
            let f = p.first_frame_after(t, Duration::from_secs(6), |f| f.is_clear()).await;
            if let Some(w) = p.arrival_after(t, |a| a.typ == "paused") {
                p_ws.push(ms(w.duration_since(t)));
            }
            if let Some(f) = f {
                pause.push(ms(f.duration_since(t)));
            }
            sleep(4000).await;
            // 3) resume
            sleep(phase()).await;
            let t = Instant::now();
            send_line(&mut stdin, "resume");
            let f = p.first_frame_after(t, Duration::from_secs(10), |f| !f.is_clear()).await;
            if let Some(w) = p.arrival_after(t, |a| a.typ == "position" && a.playing == Some(true)) {
                r_ws.push(ms(w.duration_since(t)));
            }
            if let Some(f) = f {
                resume.push(ms(f.duration_since(t)));
            }
            sleep(6000).await;
            // 4) seek to a different line
            sleep(phase()).await;
            let line = (i * 5 + 3) % 12;
            let target = line as i64 * 30_000 + 4000;
            let tok = Sheet::token(line);
            let t = Instant::now();
            send_line(&mut stdin, &format!("seek {target}"));
            let f = p.first_frame_after(t, Duration::from_secs(25), |f| f.state().contains(&tok)).await;
            if let Some(w) = p.arrival_after(t, |a| a.typ == "position" && a.pos >= target - 50 && a.pos < target + 3000) {
                s_ws.push(ms(w.duration_since(t)));
            }
            if let Some(f) = f {
                seek.push(ms(f.duration_since(t)));
            }
            sleep(8000).await;
        }
        drop(child);
        let mut out = serde_json::Map::new();
        out.insert("bridge_js".into(), json!(bridge_js));
        out.insert("trials_per_kind".into(), json!(n));
        out.insert("songchange_event_to_engine_ms".into(), stats(&sc_ws));
        out.insert("songchange_event_to_first_SET_ACTIVITY_ms".into(), stats(&songchange));
        out.insert("pause_event_to_engine_ms".into(), stats(&p_ws));
        out.insert("pause_event_to_clear_ms".into(), stats(&pause));
        out.insert("resume_event_to_engine_ms".into(), stats(&r_ws));
        out.insert("resume_event_to_first_SET_ACTIVITY_ms".into(), stats(&resume));
        out.insert("seek_event_to_engine_ms".into(), stats(&s_ws));
        out.insert("seek_event_to_new_line_frame_ms".into(), stats(&seek));
        out.insert("machine_cpu_pct_during".into(), json!(load.pct()));
        report("js_e2e_real_bridge_js", Value::Object(out));
        p.stop().await;
    });
}

/// (known problem) how long until the bridge's "none" verdict when Spotify's
/// color-lyrics request hangs. Real bridge JS, stubbed Spotify. ~70 s.
#[test]
#[ignore = "benchmark"]
fn bench_js_lyrics_hang() {
    mt().block_on(async {
        let (bridge_js, probe) = js_paths();
        let p = Pipeline::start(track_with(1, None), LyricsPlan::After(100), Opts { start_bridge_client: false, ..Default::default() }).await;
        p.wait_connected().await;
        let hang: u64 = std::env::var("BENCH_HANG_MS").ok().and_then(|x| x.parse().ok()).unwrap_or(30_000);
        let t_start = Instant::now();
        let child = KillOnDrop(
            std::process::Command::new("node")
                .arg(&probe)
                .args(["--port", &p.port.to_string(), "--bridge", &bridge_js, "--scenario", "lyrics-hang", "--hang-ms", &hang.to_string()])
                .stdin(std::process::Stdio::null())
                .stdout(std::process::Stdio::null())
                .stderr(std::process::Stdio::inherit())
                .spawn()
                .expect("node"),
        );
        let tc = wait_for(Duration::from_secs(60), || p.arrival_after(t_start, |a| a.typ == "track_change")).await;
        let tc = tc.expect("no track_change from the bridge");
        let none = wait_for(Duration::from_secs(200), || p.arrival_after(tc, |a| a.typ == "lyrics")).await;
        drop(child);
        report(
            "js_lyrics_hang_to_none_verdict",
            json!({"bridge_js": bridge_js, "stub_cosmos_hang_ms": hang, "track_change_to_lyrics_message_ms": none.map(|n| r1(ms(n.duration_since(tc))))}),
        );
        p.stop().await;
    });
}

fn js_paths() -> (String, String) {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let bridge = std::env::var("BENCH_BRIDGE_JS").unwrap_or_else(|_| root.join("resources/lyrics-bridge.js").to_string_lossy().into_owned());
    let probe = root.join("../tests/bench/bridge_js_probe.mjs").to_string_lossy().into_owned();
    (bridge, probe)
}

/// The node probe dies with the test, whatever happens.
struct KillOnDrop(std::process::Child);
impl Drop for KillOnDrop {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

fn send_line(stdin: &mut std::process::ChildStdin, line: &str) {
    use std::io::Write;
    stdin.write_all(line.as_bytes()).unwrap();
    stdin.write_all(b"\n").unwrap();
    stdin.flush().unwrap();
}
