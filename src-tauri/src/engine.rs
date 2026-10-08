//! The core: reacts to bridge messages, chooses lyrics, records plays.
//! Knows nothing about Tauri, so it is tested headless.
//!
//! Lyric priority for a track: the user's pin > prefetched > cached > the
//! bridge's live fetch > LRCLIB. LRCLIB starts LRCLIB_EARLY after a track
//! starts with nothing to show, not only after the bridge's "none" verdict —
//! when Spotify's lyrics endpoint hangs the bridge takes ~60 s to give up.

// The core area's modules live beside the engine (lib.rs is shared).
#[path = "core_maint.rs"]
pub mod maint;
#[path = "core_plan.rs"]
pub mod plan;
#[path = "core_state.rs"]
pub mod core_state;

use crate::config::Config;
use crate::db::{now_iso, Store};
use crate::lrclib;
use crate::lyrics::{offset_key, resolve_offset_ms, Line, Lyrics};
use crate::state::{now_ms, Snapshot, Track};
use serde_json::Value;
use std::collections::{HashSet, VecDeque};
use std::sync::atomic::{AtomicBool, AtomicI64, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::Duration;
use tokio::sync::broadcast;

pub const PLAY_COMMIT_MS: i64 = 20_000;
/// Lyric offsets are clamped to this (both ways) by the UI steppers.
pub const OFFSET_LIMIT_MS: i64 = 5_000;
pub const LRCLIB_EARLY: Duration = Duration::from_millis(2500);
/// Lyrics prefetched for upcoming tracks kept at once (main.py PrefetchCache(5)).
const PREFETCH_CAP: usize = 5;
/// A position this close to the end, then this close to the start of the
/// same track while playing, is the song starting over (repeat one): a new play.
const RESTART_EDGE_MS: i64 = 3_000;

/// What kind of change `Engine::new`'s callback is told about. The UI gets
/// the whole snapshot for a `Full` change; a `Position` change (a bridge
/// position message: twice a second while playing) only moves the clock, so
/// `Snapshot::position_json` (four fields) is all that needs to cross to it.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Change {
    Full,
    Position,
}

/// What feature modules can react to (Engine::subscribe).
#[derive(Clone, Debug)]
pub enum Event {
    /// Every raw message from the Spicetify bridge, before the engine acts
    /// on it (queue, player_state, beats, ... are only seen this way).
    Bridge(#[allow(dead_code)] Value),
    TrackChanged,
    LyricsChanged,
    Paused,
    Resumed,
    /// statusify.cfg was changed (Engine::config_changed); re-read settings.
    ConfigChanged,
    /// Something the presence loop should look at now that is not in the
    /// events above: Discord connected or went away, the RPC switch, a test
    /// presence request (Engine::wake).
    Wake,
}

/// The play in progress. Listening time is wall-clock time while playing
/// (main.py _get_listen_time), not position progress: the bridge may ping
/// only once a minute when Spotify's timers are throttled, and a seek is not
/// listening.
#[derive(Default)]
struct Play {
    uri: String,
    started_at: String,
    /// Listening time banked before the current playing stretch.
    banked_ms: i64,
    /// Engine::mono_ms when the current playing stretch began.
    since: Option<i64>,
    saved_ms: i64,
    id: Option<i64>,
}

impl Play {
    fn listened(&self, now: i64) -> i64 {
        self.banked_ms + self.since.map_or(0, |t| (now - t).max(0))
    }
    fn pause(&mut self, now: i64) {
        self.banked_ms = self.listened(now);
        self.since = None;
    }
    fn resume(&mut self, now: i64) {
        if self.since.is_none() {
            self.since = Some(now);
        }
    }
}

#[derive(Default)]
struct Inner {
    snap: Snapshot,
    play: Play,
    last_pos_ms: Option<i64>,
    prefetch: VecDeque<(String, Lyrics)>,
    lrclib_tried: HashSet<String>,
    pinned_uri: Option<String>,
    /// The current track's beat grid (ms, sorted) and tempo from the bridge.
    beats: Vec<i64>,
    tempo: f64,
}

pub struct Engine {
    inner: Mutex<Inner>,
    pub store: Option<Store>,
    http: reqwest::Client,
    pub lrclib_url: String,
    pub lrclib_enabled: AtomicBool,
    pub lrclib_early: Duration,
    /// LRCLIB retry backoff step (3 s, 6 s); shortened in tests.
    pub lrclib_backoff: Duration,
    on_change: Box<dyn Fn(&Snapshot, Change) + Send + Sync>,
    events: broadcast::Sender<Event>,
    /// statusify.cfg, once the core feature hands it over (Engine::set_config).
    config: OnceLock<Arc<Config>>,
    /// State shared by the presence loop and the core feature.
    pub core: core_state::Shared,
    /// Monotonic clock for listening time (tests move it with `advance`).
    epoch: std::time::Instant,
    skew_ms: AtomicI64,
}

/// The blacklist: newline-separated, case-insensitive substrings matched
/// against "artist title". Python stores the newlines as a literal "\n".
pub fn parse_blacklist(raw: &str) -> Vec<String> {
    raw.replace("\\n", "\n").lines().map(|l| l.trim().to_lowercase()).filter(|l| !l.is_empty()).collect()
}

pub fn is_blacklisted(terms: &[String], artist: &str, title: &str) -> bool {
    if terms.is_empty() {
        return false;
    }
    let hay = format!("{artist} {title}").to_lowercase();
    terms.iter().any(|t| hay.contains(t.as_str()))
}

fn s(v: &Value, k: &str) -> String {
    v.get(k).and_then(|x| x.as_str()).unwrap_or("").to_string()
}
fn i(v: &Value, k: &str) -> Option<i64> {
    v.get(k).and_then(|x| x.as_f64()).map(|f| f as i64)
}
fn lyrics_from(v: &Value, default_src: &str) -> Lyrics {
    let mode = v.get("mode").or(v.get("lyrics_mode")).and_then(|x| x.as_str()).unwrap_or("none");
    let synced: Vec<Line> = serde_json::from_value(v.get("synced").cloned().unwrap_or_default()).unwrap_or_default();
    let plain: Vec<String> = serde_json::from_value(v.get("plain").cloned().unwrap_or_default()).unwrap_or_default();
    let src = v.get("source").and_then(|x| x.as_str()).unwrap_or(default_src);
    Lyrics { mode: mode.into(), synced, plain, source: src.into() }
}

impl Engine {
    pub fn new(store: Option<Store>, on_change: impl Fn(&Snapshot, Change) + Send + Sync + 'static) -> Arc<Self> {
        Arc::new(Engine {
            inner: Mutex::new(Inner::default()),
            store,
            http: lrclib::client(),
            lrclib_url: lrclib::URL.into(),
            lrclib_enabled: AtomicBool::new(true),
            lrclib_early: LRCLIB_EARLY,
            lrclib_backoff: Duration::from_secs(3),
            on_change: Box::new(on_change),
            events: broadcast::channel(256).0,
            config: OnceLock::new(),
            core: core_state::Shared::default(),
            epoch: std::time::Instant::now(),
            skew_ms: AtomicI64::new(0),
        })
    }

    /// Monotonic ms for listening time. Tests run on a frozen clock that
    /// only `advance` moves, so their arithmetic is exact.
    fn mono_ms(&self) -> i64 {
        let real = if cfg!(test) { 0 } else { self.epoch.elapsed().as_millis() as i64 };
        real + self.skew_ms.load(Ordering::Relaxed)
    }

    /// Move the listening-time clock forward (tests).
    #[cfg(test)]
    pub fn advance(&self, ms: i64) {
        self.skew_ms.fetch_add(ms, Ordering::Relaxed);
    }

    /// The current track's beat grid (ms) and tempo, as the bridge sent them.
    #[allow(dead_code)] // for the nowplaying area (beat-synced visuals)
    pub fn beats(&self) -> (Vec<i64>, f64) {
        let g = self.inner.lock().unwrap();
        (g.beats.clone(), g.tempo)
    }

    /// Hand over statusify.cfg; settings are read from it live from now on.
    pub fn set_config(&self, c: Arc<Config>) {
        let _ = self.config.set(c);
        self.refresh_config();
    }

    pub fn config(&self) -> Option<&Arc<Config>> {
        self.config.get()
    }

    /// Wake the presence loop (Event::Wake) for a change it cannot see in
    /// the snapshot: the RPC switch, a test presence request.
    pub fn wake(&self) {
        self.emit(Event::Wake);
    }

    /// Call after writing statusify.cfg so every feature re-reads it.
    pub fn config_changed(&self) {
        self.refresh_config();
        self.emit(Event::ConfigChanged);
    }

    /// Settings the engine keeps as state: the LRCLIB switch and whether the
    /// current track is blacklisted (an edited blacklist applies at once).
    fn refresh_config(&self) {
        let Some(c) = self.config() else { return };
        self.lrclib_enabled.store(c.get_bool("preferences", "lrclib_fallback", true), Ordering::Relaxed);
        let terms = self.blacklist();
        let mut g = self.inner.lock().unwrap();
        let Some(t) = g.snap.track.as_mut() else { return };
        let b = is_blacklisted(&terms, &t.artist, &t.title);
        if b != t.blacklisted {
            t.blacklisted = b;
            crate::log(&format!(
                "{}  ·  {} — {}",
                if b { "Blacklisted — RPC suppressed" } else { "No longer blacklisted" },
                t.artist,
                t.title
            ));
            drop(g);
            self.changed();
        }
    }

    pub fn blacklist(&self) -> Vec<String> {
        self.config().map(|c| parse_blacklist(&c.get_or("preferences", "blacklist", ""))).unwrap_or_default()
    }

    /// The global lyric delay (preferences.lyric_delay_ms).
    pub fn global_delay_ms(&self) -> i64 {
        self.config().map_or(0, |c| c.get_i64("preferences", "lyric_delay_ms", 0))
    }

    /// The per-track offset for `uri` ([offsets] section), else the global.
    pub fn offset_ms_for(&self, uri: &str) -> i64 {
        let global = self.global_delay_ms();
        if uri.is_empty() {
            return global;
        }
        let raw = self.config().and_then(|c| c.get("offsets", &offset_key(uri)));
        resolve_offset_ms(raw.as_deref(), global)
    }

    /// The effective lyric offset for the current track.
    pub fn offset_ms(&self) -> i64 {
        let uri = self.inner.lock().unwrap().snap.track.as_ref().map(|t| t.uri.clone()).unwrap_or_default();
        self.offset_ms_for(&uri)
    }

    /// preferences.save_history: off means no plays recorded, no lyric cache.
    pub fn history_enabled(&self) -> bool {
        self.config().is_none_or(|c| c.get_bool("preferences", "save_history", true))
    }

    fn store(&self) -> Option<&Store> {
        self.store.as_ref().filter(|_| self.history_enabled())
    }

    pub fn subscribe(&self) -> broadcast::Receiver<Event> {
        self.events.subscribe()
    }

    fn emit(&self, e: Event) {
        let _ = self.events.send(e);
    }

    /// Feature data for the UI: lands in snapshot.extras[key].
    pub fn set_extra(&self, key: &str, value: Value) {
        self.update(|s| {
            s.extras.insert(key.to_string(), value);
        });
    }

    #[allow(dead_code)] // contract API for the nowplaying feature
    /// Lyrics from somewhere other than the bridge (search, translation
    /// source switch, ...) for the current track.
    pub fn set_lyrics(&self, l: Lyrics) {
        let mut g = self.inner.lock().unwrap();
        self.apply(&mut g, l);
        drop(g);
        self.changed();
    }

    #[allow(dead_code)] // contract API for the nowplaying feature
    /// The user's choice for the current track: stored in lyric_pins and
    /// preferred over every source from now on.
    pub fn pin_lyrics(&self, l: Lyrics) {
        let mut g = self.inner.lock().unwrap();
        let Some(uri) = g.snap.track.as_ref().map(|t| t.uri.clone()) else { return };
        if let Some(st) = &self.store {
            let _ = st.set_pin(&uri, &l);
        }
        g.pinned_uri = Some(uri);
        self.apply(&mut g, l);
        drop(g);
        self.changed();
    }

    #[allow(dead_code)] // contract API for the nowplaying feature
    pub fn clear_pin(&self) {
        let mut g = self.inner.lock().unwrap();
        if let (Some(st), Some(t)) = (&self.store, &g.snap.track) {
            let _ = st.clear_pin(&t.uri);
        }
        g.pinned_uri = None;
    }

    #[allow(dead_code)] // contract API for the nowplaying feature
    pub fn is_pinned(&self) -> bool {
        let g = self.inner.lock().unwrap();
        g.snap.track.as_ref().is_some_and(|t| g.pinned_uri.as_deref() == Some(t.uri.as_str()))
    }

    pub fn snapshot(&self) -> Snapshot {
        self.inner.lock().unwrap().snap.clone()
    }

    /// The clock part of the snapshot (Snapshot::position_json), without
    /// cloning the rest.
    pub fn position_json(&self) -> Value {
        self.inner.lock().unwrap().snap.position_json()
    }

    fn changed(&self) {
        self.changed_with(Change::Full);
    }

    fn changed_with(&self, c: Change) {
        let s = self.snapshot();
        (self.on_change)(&s, c);
    }

    pub fn update(&self, f: impl FnOnce(&mut Snapshot)) {
        let mut paused = false;
        let discord;
        {
            let mut g = self.inner.lock().unwrap();
            let was = g.snap.bridge_connected;
            let user = g.snap.discord_user.clone();
            f(&mut g.snap);
            discord = user != g.snap.discord_user;
            // The bridge went away: Spotify is not playing for us any more.
            if was && !g.snap.bridge_connected && g.snap.is_playing {
                g.snap.is_playing = false;
                let now = self.mono_ms();
                g.play.pause(now);
                self.write_listened(&mut g);
                paused = true;
            }
        }
        if paused {
            self.emit(Event::Paused);
        }
        if discord {
            self.emit(Event::Wake);
        }
        self.changed();
    }

    fn apply(&self, g: &mut Inner, l: Lyrics) {
        crate::log(&format!("Lyrics ({})  ·  {}  ·  {} lines", l.source, l.mode, l.line_count()));
        if let (Some(st), Some(t)) = (self.store(), &g.snap.track) {
            if l.source != "cache" && !l.source.contains("chosen") {
                let _ = st.save_lyrics(&t.uri, &l);
            }
        }
        g.snap.lyrics = l;
        self.emit(Event::LyricsChanged);
    }

    /// One message from the Spicetify bridge.
    pub fn handle(self: &Arc<Self>, msg: &Value) {
        self.emit(Event::Bridge(msg.clone()));
        let t = msg.get("type").and_then(|x| x.as_str()).unwrap_or("");
        match t {
            "track_change" => self.on_track(msg),
            "position" => self.on_position(msg),
            "paused" => {
                // Older bridges repeat "paused" every 500 ms: act on the
                // transition only.
                let mut g = self.inner.lock().unwrap();
                if g.snap.is_playing {
                    self.maybe_commit(&mut g);
                    g.snap.is_playing = false;
                    let now = self.mono_ms();
                    g.play.pause(now);
                    self.write_listened(&mut g);
                    drop(g);
                    self.emit(Event::Paused);
                    self.changed();
                }
            }
            "hello" => crate::log(&format!(
                "Lyrics bridge v{} connected",
                msg.get("version").and_then(|v| v.as_str()).unwrap_or("?")
            )),
            "lyrics" => self.on_lyrics(msg),
            "lyrics_prefetch" => self.on_prefetch(msg),
            "beats" => self.on_beats(msg),
            "lyrics_debug" => {
                let m = s(msg, "message");
                if !m.is_empty() {
                    crate::log(&format!("[Bridge] {m}"));
                }
            }
            _ => {}
        }
    }

    /// Write the listening time of the play in progress.
    fn write_listened(&self, g: &mut Inner) {
        if let (Some(id), Some(st)) = (g.play.id, self.store()) {
            let ms = g.play.listened(self.mono_ms());
            if let Err(e) = st.set_listened(id, ms) {
                crate::log(&format!("Could not save listening time: {e}"));
            }
            g.play.saved_ms = ms;
        }
    }

    /// The app is closing: bank the play in progress. It is committed if it
    /// has been listened to long enough but no position update has come since
    /// (they arrive every 500 ms, but quitting does not wait for one), and its
    /// listening time is written, so the up-to-5 seconds the periodic save has
    /// not reached are not lost. Called from every quit path (lib.rs shutdown);
    /// safe to call twice, and a no-op while "Remember history" is off.
    pub fn finish_play(&self) {
        let mut g = self.inner.lock().unwrap();
        self.maybe_commit(&mut g);
        self.write_listened(&mut g);
    }

    /// Begin a candidate play of the current track (nothing is written yet).
    fn start_play(&self, g: &mut Inner, uri: &str) {
        let since = g.snap.is_playing.then(|| self.mono_ms());
        g.play = Play { uri: uri.to_string(), started_at: now_iso(), since, ..Default::default() };
    }

    /// A track becomes a play (history row) only after PLAY_COMMIT_MS of
    /// real listening: opening on a paused song or skipping past one must
    /// not count.
    fn maybe_commit(&self, g: &mut Inner) {
        if g.play.id.is_some() || g.play.listened(self.mono_ms()) < PLAY_COMMIT_MS {
            return;
        }
        if let (Some(st), Some(t)) = (self.store(), &g.snap.track) {
            if t.uri == g.play.uri && !t.uri.is_empty() {
                match st.record_play(&t.uri, &t.artist, &t.title, &t.album_art, &g.play.started_at) {
                    Ok(id) => g.play.id = Some(id),
                    Err(e) => crate::log(&format!("Could not record play: {e}")),
                }
            }
        }
    }

    fn on_track(self: &Arc<Self>, m: &Value) {
        let uri = s(m, "track_uri");
        {
            let mut g = self.inner.lock().unwrap();
            // The bridge repeats track_change for the song already playing on
            // every reconnect and request_state: that is not a new song. Keep
            // the interpolated position (a jump to 0 mid-song would make the
            // presence re-plan from the intro) and the play in progress.
            let same = !uri.is_empty() && g.snap.track.as_ref().is_some_and(|t| t.uri == uri);
            self.maybe_commit(&mut g); // the previous track, if it earned it
            if !same {
                self.write_listened(&mut g);
            }
            let (artist, title) = (s(m, "artist"), s(m, "title"));
            let blacklisted = is_blacklisted(&self.blacklist(), &artist, &title);
            let track = Track { uri: uri.clone(), artist, title, album: s(m, "album"), album_art: s(m, "album_art"), blacklisted };
            if blacklisted {
                crate::log(&format!("Blacklisted — RPC suppressed  ·  {} — {}", track.artist, track.title));
            } else {
                crate::log(&format!("Now playing  ·  {} — {}", track.artist, track.title));
            }
            // The per-song dropped-line counter starts over.
            self.core.with(|c| c.dropped_lines = 0);
            g.snap.track = Some(track);
            g.snap.duration_ms = i(m, "duration_ms").unwrap_or(0);
            if !same {
                g.snap.position_ms = 0;
                g.snap.position_at_ms = now_ms();
                g.last_pos_ms = None;
            }
            g.snap.is_playing = true;
            g.snap.lyrics = Lyrics::none();
            g.beats.clear();
            g.tempo = 0.0;
            if same {
                let now = self.mono_ms();
                g.play.resume(now);
            } else {
                self.start_play(&mut g, &uri);
            }
            g.pinned_uri = None;

            let pin = self.store.as_ref().and_then(|st| st.pinned(&uri));
            if let Some(p) = pin {
                g.pinned_uri = Some(uri.clone());
                self.apply(&mut g, p);
            } else if let Some(k) = g.prefetch.iter().position(|(u, _)| *u == uri) {
                let (_, mut l) = g.prefetch.remove(k).unwrap();
                l.source = format!("{} · preloaded", l.source);
                self.apply(&mut g, l);
            } else if let Some(mut c) = self.store.as_ref().and_then(|st| st.cached(&uri)) {
                c.source = "cache".into();
                self.apply(&mut g, c);
            }
        }
        self.emit(Event::TrackChanged);
        self.changed();
        if self.snapshot().lyrics.is_none() {
            self.schedule_early_lrclib(uri);
        }
    }

    fn on_position(&self, m: &Value) {
        let mut g = self.inner.lock().unwrap();
        let pos = i(m, "position_ms").unwrap_or(0);
        let playing = m.get("is_playing").and_then(|x| x.as_bool()).unwrap_or(true);
        let now = self.mono_ms();
        // The same song starting over (repeat one) is a new play.
        let dur = i(m, "duration_ms").unwrap_or(g.snap.duration_ms);
        let est = g.snap.estimated_position(now_ms());
        if playing && g.snap.is_playing && dur > RESTART_EDGE_MS * 2 && est >= dur - RESTART_EDGE_MS && pos < RESTART_EDGE_MS {
            if let Some(uri) = g.snap.track.as_ref().map(|t| t.uri.clone()) {
                self.maybe_commit(&mut g);
                self.write_listened(&mut g);
                self.start_play(&mut g, &uri);
                crate::log("Song started over  ·  counting a new play");
            }
        }
        g.last_pos_ms = Some(pos);
        g.snap.position_ms = pos;
        g.snap.position_at_ms = now_ms();
        if let Some(d) = i(m, "duration_ms") {
            g.snap.duration_ms = d;
        }
        let resumed = playing && !g.snap.is_playing;
        let paused = !playing && g.snap.is_playing;
        if paused {
            self.maybe_commit(&mut g);
            g.play.pause(now);
            self.write_listened(&mut g);
        }
        g.snap.is_playing = playing;
        if resumed {
            g.play.resume(now);
        }
        if playing {
            self.maybe_commit(&mut g);
        }
        // Keep listened_ms current, so a play cut short by quitting still counts.
        if let (Some(id), Some(st)) = (g.play.id, self.store()) {
            let ms = g.play.listened(now);
            if ms - g.play.saved_ms >= 5_000 {
                let _ = st.set_listened(id, ms);
                g.play.saved_ms = ms;
            }
        }
        drop(g);
        if resumed {
            self.emit(Event::Resumed);
        }
        if paused {
            self.emit(Event::Paused);
        }
        // Only the clock moved (is_playing and duration_ms ride with it).
        self.changed_with(Change::Position);
    }

    fn on_lyrics(self: &Arc<Self>, m: &Value) {
        let synced = m.get("synced").and_then(|v| v.as_array()).is_some_and(|a| !a.is_empty());
        let mode = m.get("mode").or(m.get("lyrics_mode")).and_then(|x| x.as_str()).unwrap_or("none");
        let l = lyrics_from(m, if mode == "synced" && synced { "Spicy" } else { "fallback" });
        let uri = s(m, "track_uri");
        let mut fetch = None;
        {
            let mut g = self.inner.lock().unwrap();
            let cur = g.snap.track.as_ref().map(|t| t.uri.clone()).unwrap_or_default();
            let showing = !g.snap.lyrics.is_none();
            if l.is_none() && showing && (uri == cur || uri.is_empty()) {
                // Already showing cached/LRCLIB lyrics; a failed fetch keeps them.
                crate::log(&format!("Lyrics ({})  ·  none  ·  keeping current lyrics", l.source));
                return;
            }
            // Only this track's lyrics. A missing uri (bridges that predate
            // the field) is accepted only while nothing is showing, so a late
            // answer cannot replace lyrics already up.
            let ours = (!uri.is_empty() && uri == cur) || (uri.is_empty() && !showing);
            if !ours || g.pinned_uri.as_deref() == Some(cur.as_str()) {
                return; // a late answer for the previous song, or the user's pick stands
            }
            if l.is_none() {
                // The bridge's verdict: no lyrics. Cached as "none" (never over
                // real lyrics) for the History tab, then LRCLIB gets a try.
                crate::log(&format!("Lyrics ({})  ·  none  ·  0 lines", l.source));
                if let Some(st) = self.store() {
                    if let Err(e) = core_state::save_none_lyrics(st, &cur, &l.source) {
                        crate::log(&format!("Could not cache lyrics: {e}"));
                    }
                }
                fetch = Some(cur);
            } else {
                self.apply(&mut g, l);
            }
        }
        self.changed();
        if let Some(u) = fetch {
            self.fetch_lrclib(u);
        }
    }

    fn on_prefetch(&self, m: &Value) {
        let l = lyrics_from(m, "Spicy");
        let uri = s(m, "track_uri");
        if uri.is_empty() || l.is_none() {
            return;
        }
        let mut g = self.inner.lock().unwrap();
        let cur = g.snap.track.as_ref().map(|t| t.uri.clone()).unwrap_or_default();
        if uri == cur {
            if g.snap.lyrics.is_none() {
                let mut l = l;
                l.source = format!("{} · preloaded", l.source);
                self.apply(&mut g, l);
                drop(g);
                self.changed();
            }
            return;
        }
        g.prefetch.retain(|(u, _)| *u != uri);
        g.prefetch.push_back((uri, l));
        while g.prefetch.len() > PREFETCH_CAP {
            g.prefetch.pop_front();
        }
    }

    fn on_beats(&self, m: &Value) {
        let uri = s(m, "track_uri");
        let mut g = self.inner.lock().unwrap();
        let cur = g.snap.track.as_ref().map(|t| t.uri.clone()).unwrap_or_default();
        if !uri.is_empty() && uri != cur {
            return;
        }
        let (beats, tempo) = core_state::parse_beats(m);
        g.beats = beats;
        g.tempo = tempo;
    }

    fn still_waiting(&self, uri: &str) -> bool {
        let g = self.inner.lock().unwrap();
        g.snap.track.as_ref().is_some_and(|t| t.uri == uri) && g.snap.lyrics.is_none()
    }

    pub fn schedule_early_lrclib(self: &Arc<Self>, uri: String) {
        if !self.lrclib_enabled.load(Ordering::Relaxed) || uri.is_empty() {
            return;
        }
        let me = self.clone();
        tokio::spawn(async move {
            tokio::time::sleep(me.lrclib_early).await;
            if me.still_waiting(&uri) {
                me.fetch_lrclib(uri);
            }
        });
    }

    /// One LRCLIB lookup per track; a failed lookup (network, HTTP error,
    /// bad JSON) is forgotten so a later trigger (the bridge's "none") may
    /// retry it. Only a real answer with no usable match is final.
    pub fn fetch_lrclib(self: &Arc<Self>, uri: String) {
        if !self.lrclib_enabled.load(Ordering::Relaxed) || uri.is_empty() {
            return;
        }
        let (artist, title, dur) = {
            let mut g = self.inner.lock().unwrap();
            if !g.lrclib_tried.insert(uri.clone()) {
                return;
            }
            let Some(t) = &g.snap.track else { return };
            (t.artist.clone(), t.title.clone(), g.snap.duration_ms)
        };
        let me = self.clone();
        tokio::spawn(async move {
            match lrclib::fetch_with_backoff(&me.http, &me.lrclib_url, &artist, &title, dur, me.lrclib_backoff).await {
                Ok(l) => {
                    if me.still_waiting(&uri) {
                        let mut g = me.inner.lock().unwrap();
                        me.apply(&mut g, l);
                        drop(g);
                        me.changed();
                    }
                }
                Err(lrclib::Error::NoMatch) => crate::log(&format!("LRCLIB: no match for {artist} — {title}")),
                Err(lrclib::Error::Failed(e)) => {
                    crate::log(&format!("LRCLIB lookup failed: {e}"));
                    me.inner.lock().unwrap().lrclib_tried.remove(&uri);
                }
            }
        });
    }

    pub fn recent_plays(&self, n: i64) -> Vec<crate::db::Play> {
        self.store.as_ref().and_then(|s| s.recent_plays(n).ok()).unwrap_or_default()
    }

    #[cfg(test)]
    pub fn tried(&self, uri: &str) -> bool {
        self.inner.lock().unwrap().lrclib_tried.contains(uri)
    }
}

#[allow(dead_code)]
fn _assert_send(e: Arc<Engine>) -> impl Send { e }

#[cfg(test)]
mod tests;
