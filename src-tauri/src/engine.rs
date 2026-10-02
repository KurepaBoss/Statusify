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
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::Duration;
use tokio::sync::broadcast;

pub const PLAY_COMMIT_MS: i64 = 20_000;
/// Lyric offsets are clamped to this (both ways) by the UI steppers.
pub const OFFSET_LIMIT_MS: i64 = 5_000;
pub const LRCLIB_EARLY: Duration = Duration::from_millis(2500);
const PREFETCH_CAP: usize = 20;

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
}

#[derive(Default)]
struct Play {
    uri: String,
    started_at: String,
    listened_ms: i64,
    saved_ms: i64,
    id: Option<i64>,
}

#[derive(Default)]
struct Inner {
    snap: Snapshot,
    play: Play,
    last_pos_ms: Option<i64>,
    prefetch: VecDeque<(String, Lyrics)>,
    lrclib_tried: HashSet<String>,
    pinned_uri: Option<String>,
}

pub struct Engine {
    inner: Mutex<Inner>,
    pub store: Option<Store>,
    http: reqwest::Client,
    pub lrclib_url: String,
    pub lrclib_enabled: AtomicBool,
    pub lrclib_early: Duration,
    on_change: Box<dyn Fn(&Snapshot) + Send + Sync>,
    events: broadcast::Sender<Event>,
    /// statusify.cfg, once the core feature hands it over (Engine::set_config).
    config: OnceLock<Arc<Config>>,
    /// State shared by the presence loop and the core feature.
    pub core: core_state::Shared,
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
    pub fn new(store: Option<Store>, on_change: impl Fn(&Snapshot) + Send + Sync + 'static) -> Arc<Self> {
        Arc::new(Engine {
            inner: Mutex::new(Inner::default()),
            store,
            http: lrclib::client(),
            lrclib_url: lrclib::URL.into(),
            lrclib_enabled: AtomicBool::new(true),
            lrclib_early: LRCLIB_EARLY,
            on_change: Box::new(on_change),
            events: broadcast::channel(256).0,
            config: OnceLock::new(),
            core: core_state::Shared::default(),
        })
    }

    /// Hand over statusify.cfg; settings are read from it live from now on.
    pub fn set_config(&self, c: Arc<Config>) {
        let _ = self.config.set(c);
        self.refresh_config();
    }

    pub fn config(&self) -> Option<&Arc<Config>> {
        self.config.get()
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

    fn changed(&self) {
        let s = self.snapshot();
        (self.on_change)(&s);
    }

    pub fn update(&self, f: impl FnOnce(&mut Snapshot)) {
        let mut paused = false;
        {
            let mut g = self.inner.lock().unwrap();
            let was = g.snap.bridge_connected;
            f(&mut g.snap);
            // The bridge went away: Spotify is not playing for us any more.
            if was && !g.snap.bridge_connected && g.snap.is_playing {
                g.snap.is_playing = false;
                self.finish_play(&mut g);
                paused = true;
            }
        }
        if paused {
            self.emit(Event::Paused);
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
                    g.snap.is_playing = false;
                    self.maybe_commit(&mut g);
                    self.finish_play(&mut g);
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
    fn finish_play(&self, g: &mut Inner) {
        if let (Some(id), Some(st)) = (g.play.id, self.store()) {
            let _ = st.set_listened(id, g.play.listened_ms);
            g.play.saved_ms = g.play.listened_ms;
        }
    }

    /// A track becomes a play (history row) only after PLAY_COMMIT_MS of
    /// real listening: opening on a paused song or skipping past one must
    /// not count.
    fn maybe_commit(&self, g: &mut Inner) {
        if g.play.id.is_some() || g.play.listened_ms < PLAY_COMMIT_MS {
            return;
        }
        if let (Some(st), Some(t)) = (self.store(), &g.snap.track) {
            if t.uri == g.play.uri && !t.uri.is_empty() {
                g.play.id = st.record_play(&t.uri, &t.artist, &t.title, &t.album_art, &g.play.started_at).ok();
            }
        }
    }

    fn on_track(self: &Arc<Self>, m: &Value) {
        let uri = s(m, "track_uri");
        {
            let mut g = self.inner.lock().unwrap();
            self.maybe_commit(&mut g); // the previous track, if it earned it
            self.finish_play(&mut g);
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
            g.snap.position_ms = 0;
            g.snap.position_at_ms = now_ms();
            g.snap.is_playing = true;
            g.snap.lyrics = Lyrics::none();
            g.last_pos_ms = None;
            g.play = Play { uri: uri.clone(), started_at: now_iso(), ..Default::default() };
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
        // Listening time: forward progress while playing, ignoring seeks.
        if let Some(prev) = g.last_pos_ms {
            let d = pos - prev;
            if playing && (0..=5_000).contains(&d) {
                g.play.listened_ms += d;
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
        g.snap.is_playing = playing;
        self.maybe_commit(&mut g);
        if paused {
            self.finish_play(&mut g);
        }
        // Keep listened_ms current, so a play cut short by quitting still counts.
        if let (Some(id), Some(st)) = (g.play.id, self.store()) {
            if g.play.listened_ms - g.play.saved_ms >= 5_000 {
                let _ = st.set_listened(id, g.play.listened_ms);
                g.play.saved_ms = g.play.listened_ms;
            }
        }
        drop(g);
        if resumed {
            self.emit(Event::Resumed);
        }
        if paused {
            self.emit(Event::Paused);
        }
        self.changed();
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
            let ours = uri == cur || uri.is_empty();
            if !ours || g.pinned_uri.as_deref() == Some(cur.as_str()) {
                return; // a late answer for the previous song, or the user's pick stands
            }
            if l.is_none() {
                if g.snap.lyrics.is_none() {
                    crate::log("Lyrics: bridge found none");
                    fetch = Some(cur);
                } else {
                    crate::log("Lyrics (none)  ·  keeping current lyrics");
                }
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

    /// One LRCLIB lookup per track; a network failure is forgotten so a
    /// later trigger (the bridge's "none") may retry it.
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
            match lrclib::fetch(&me.http, &me.lrclib_url, &artist, &title, dur).await {
                Ok(l) => {
                    if me.still_waiting(&uri) {
                        let mut g = me.inner.lock().unwrap();
                        me.apply(&mut g, l);
                        drop(g);
                        me.changed();
                    }
                }
                Err(lrclib::Error::NoMatch) => crate::log(&format!("LRCLIB: no match for {artist} — {title}")),
                Err(lrclib::Error::Retryable(e)) => {
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
