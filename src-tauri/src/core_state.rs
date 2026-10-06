//! State the core area shares between the engine, the presence loop and the
//! `core` feature: the RPC switch, what Discord shows, dropped lines, the
//! sleep timer (port of statusify_sleep.py) and the bridge health check
//! (port of statusify_bridge.BridgeHealth).

use serde_json::Value;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use std::time::{Duration, Instant};

pub struct Shared {
    rpc_enabled: AtomicBool,
    data: Mutex<CoreData>,
}

impl Default for Shared {
    fn default() -> Self {
        Shared { rpc_enabled: AtomicBool::new(true), data: Mutex::new(CoreData::default()) }
    }
}

impl Shared {
    pub fn rpc_enabled(&self) -> bool {
        self.rpc_enabled.load(Ordering::Relaxed)
    }
    pub fn set_rpc_enabled(&self, on: bool) {
        self.rpc_enabled.store(on, Ordering::Relaxed);
    }
    pub fn with<T>(&self, f: impl FnOnce(&mut CoreData) -> T) -> T {
        f(&mut self.data.lock().unwrap())
    }
}

#[derive(Default)]
pub struct CoreData {
    /// Lines the song moved past without their words reaching Discord (this song).
    pub dropped_lines: u32,
    /// What the presence loop last put on Discord ("" for title-only/cleared).
    pub discord_line: String,
    /// The last rate-limit wait: (seconds, epoch ms it was reported).
    pub rate_limited: Option<(f64, i64)>,
    /// A "send test presence" request for the presence loop.
    pub test_request: bool,
    /// The presence loop is running (a DISCORD_APP_ID is configured).
    pub presence_running: bool,
    /// The newest release when it is newer than this build.
    pub update: Option<Value>,
    /// The bridge-health / bridge-version warning, "" when none.
    pub bridge_warning: String,
    /// The bridge running inside Spotify differs from the one we ship.
    pub bridge_outdated: bool,
    pub sleep: SleepTimer,
}

// ── Sleep timer ───────────────────────────────────────────────────

/// Pause this long before the song's end so Spotify doesn't start the next
/// one. The track-change check catches it if the ping comes in late.
pub const EOS_LEAD_S: f64 = 0.6;
pub const CUSTOM_MIN: u32 = 1;
pub const CUSTOM_MAX: u32 = 600;

/// A typed custom duration: whole minutes CUSTOM_MIN..=CUSTOM_MAX, with an
/// optional "m"/"min"/"minutes" suffix.
pub fn parse_minutes(text: &str) -> Option<u32> {
    let mut s = text.trim().to_lowercase();
    for suf in ["minutes", "min", "m"] {
        if let Some(x) = s.strip_suffix(suf) {
            s = x.trim().to_string();
            break;
        }
    }
    if s.is_empty() || !s.chars().all(|c| c.is_ascii_digit()) {
        return None;
    }
    let m: u32 = s.parse().ok()?;
    (CUSTOM_MIN..=CUSTOM_MAX).contains(&m).then_some(m)
}

/// What the end-of-song mode needs to know about playback.
#[derive(Clone, Debug, Default)]
pub struct PlayInfo {
    pub uri: String,
    pub duration_ms: i64,
    pub position_ms: i64,
    pub is_playing: bool,
}

#[derive(Clone, Debug, Default, PartialEq)]
enum Mode {
    #[default]
    Off,
    Minutes { minutes: f64, deadline: Instant },
    Eos { uri: String },
}

/// Pause Spotify after N minutes, or when the current song ends. Nothing is
/// persisted: a restart forgets the timer.
#[derive(Clone, Debug, Default)]
pub struct SleepTimer {
    mode: Mode,
}

pub enum SleepSpec {
    Off,
    Minutes(f64),
    EndOfSong,
}

impl SleepSpec {
    /// minutes (number or numeric text, "45m" too), "eos", or null/"off"/0.
    pub fn from_json(v: &Value) -> Result<Self, String> {
        match v {
            Value::Null => Ok(SleepSpec::Off),
            Value::Number(n) => Ok(match n.as_f64().unwrap_or(0.0) {
                m if m > 0.0 => SleepSpec::Minutes(m),
                _ => SleepSpec::Off,
            }),
            Value::String(s) => {
                let t = s.trim().to_lowercase();
                match t.as_str() {
                    "" | "off" | "0" => Ok(SleepSpec::Off),
                    "eos" => Ok(SleepSpec::EndOfSong),
                    _ => parse_minutes(&t)
                        .map(|m| SleepSpec::Minutes(m as f64))
                        .ok_or_else(|| format!("not a sleep duration: {s} (1-600 minutes)")),
                }
            }
            _ => Err("sleep value must be minutes, \"eos\" or null".into()),
        }
    }
}

/// "1:05" / "1:02:03".
pub fn fmt(seconds: f64) -> String {
    let s = seconds.max(0.0).round() as i64;
    let (h, rem) = (s / 3600, s % 3600);
    let (m, s) = (rem / 60, rem % 60);
    if h > 0 { format!("{h}:{m:02}:{s:02}") } else { format!("{m}:{s:02}") }
}

impl SleepTimer {
    pub fn set(&mut self, spec: SleepSpec, now: Instant, play: Option<&PlayInfo>) {
        self.mode = match spec {
            SleepSpec::Off => Mode::Off,
            SleepSpec::EndOfSong => Mode::Eos { uri: play.map(|p| p.uri.clone()).unwrap_or_default() },
            SleepSpec::Minutes(m) if m > 0.0 => {
                Mode::Minutes { minutes: m, deadline: now + Duration::from_secs_f64(m * 60.0) }
            }
            SleepSpec::Minutes(_) => Mode::Off,
        };
    }

    pub fn active(&self) -> bool {
        self.mode != Mode::Off
    }

    /// "off", "eos" or the minutes as text ("15", or a custom "45").
    pub fn value(&self) -> String {
        match &self.mode {
            Mode::Off => "off".into(),
            Mode::Eos { .. } => "eos".into(),
            Mode::Minutes { minutes, .. } => {
                if minutes.fract() == 0.0 { format!("{}", *minutes as i64) } else { format!("{minutes}") }
            }
        }
    }

    /// Seconds left; None when off or (end of song) unknown.
    pub fn remaining(&self, now: Instant, play: Option<&PlayInfo>) -> Option<f64> {
        match &self.mode {
            Mode::Off => None,
            Mode::Minutes { deadline, .. } => Some(deadline.saturating_duration_since(now).as_secs_f64()),
            Mode::Eos { .. } => {
                let p = play?;
                if p.duration_ms <= 0 {
                    return None;
                }
                Some(((p.duration_ms - p.position_ms) as f64 / 1000.0).max(0.0))
            }
        }
    }

    pub fn due(&self, now: Instant, play: Option<&PlayInfo>) -> bool {
        match &self.mode {
            Mode::Off => false,
            Mode::Minutes { deadline, .. } => now >= *deadline,
            Mode::Eos { uri } => {
                let Some(p) = play else { return false };
                if !uri.is_empty() && !p.uri.is_empty() && p.uri != *uri {
                    return true; // the next song already started
                }
                if !p.is_playing {
                    return false;
                }
                self.remaining(now, play).is_some_and(|l| l <= EOS_LEAD_S)
            }
        }
    }

    /// Switch off if due. True means it fired: the caller pauses Spotify.
    pub fn tick(&mut self, now: Instant, play: Option<&PlayInfo>) -> bool {
        if !self.active() || !self.due(now, play) {
            return false;
        }
        self.mode = Mode::Off;
        true
    }

    pub fn label(&self, now: Instant, play: Option<&PlayInfo>) -> String {
        match &self.mode {
            Mode::Off => String::new(),
            Mode::Minutes { .. } => format!("Sleep in {}", fmt(self.remaining(now, play).unwrap_or(0.0))),
            Mode::Eos { .. } => match self.remaining(now, play) {
                Some(l) => format!("Sleep at end of song · {}", fmt(l)),
                None => "Sleep at end of song".into(),
            },
        }
    }
}

// ── Bridge health ─────────────────────────────────────────────────
// A Spicetify/Spotify update wipes injected extensions: Spotify runs fine,
// nothing ever connects, lyrics never appear, no error anywhere. Detect
// "Spotify is running but no bridge has connected for a while".

pub const HEALTH_MSG: &str = "Spotify is running but the lyrics bridge isn't connected — click to repair";
pub const HEALTH_GRACE: Duration = Duration::from_secs(45);

#[derive(Debug, PartialEq, Eq, Clone, Copy)]
pub enum Verdict {
    Flag,
    Clear,
}

/// evaluate() says Flag (show the warning) or Clear (remove it) once each.
pub struct BridgeHealth {
    pub grace: Duration,
    pub connected: bool,
    pub flagged: bool,
    since: Instant,
}

impl BridgeHealth {
    pub fn new(now: Instant) -> Self {
        BridgeHealth { grace: HEALTH_GRACE, connected: false, flagged: false, since: now }
    }
    pub fn on_connect(&mut self) -> Option<Verdict> {
        self.connected = true;
        if self.flagged {
            self.flagged = false;
            return Some(Verdict::Clear);
        }
        None
    }
    pub fn on_disconnect(&mut self, now: Instant) {
        if self.connected {
            self.connected = false;
            self.since = now;
        }
    }
    /// A repair is under way (it restarts Spotify): stand down for `extra`
    /// on top of the usual grace.
    pub fn snooze(&mut self, now: Instant, extra: Duration) {
        self.flagged = false;
        self.since = now + extra;
    }
    pub fn needs_check(&self) -> bool {
        !self.connected
    }
    pub fn evaluate(&mut self, spotify_running: bool, now: Instant) -> Option<Verdict> {
        if self.connected {
            return self.on_connect();
        }
        if !spotify_running {
            // Grace restarts from Spotify's launch, not ours.
            self.since = now;
            if self.flagged {
                self.flagged = false;
                return Some(Verdict::Clear);
            }
            return None;
        }
        if !self.flagged && now >= self.since && now - self.since >= self.grace {
            self.flagged = true;
            return Some(Verdict::Flag);
        }
        None
    }
}

// ── Bridge message helpers ────────────────────────────────────────

/// A "beats" message's grid (sorted whole ms) and tempo; anything malformed
/// gives no beats and tempo 0, as main.py does.
pub fn parse_beats(m: &Value) -> (Vec<i64>, f64) {
    let beats = match m.get("beats") {
        None | Some(Value::Null) => Some(vec![]),
        Some(Value::Array(a)) => a.iter().map(|b| b.as_f64().map(|f| f as i64)).collect::<Option<Vec<i64>>>(),
        Some(_) => None,
    };
    let tempo = match m.get("tempo") {
        None | Some(Value::Null) => Some(0.0),
        Some(v) => v.as_f64().or_else(|| v.as_str().and_then(|s| s.trim().parse().ok())),
    };
    match (beats, tempo) {
        (Some(mut b), Some(t)) => {
            b.sort_unstable();
            (b, t)
        }
        _ => (vec![], 0.0),
    }
}

/// Cache a "no lyrics" verdict for `uri` (statusify_history.save_lyrics with
/// mode "none"): written only when no real lyrics are cached, since a failed
/// fetch today must not erase what an earlier fetch found.
pub fn save_none_lyrics(st: &crate::db::Store, uri: &str, source: &str) -> rusqlite::Result<()> {
    if uri.is_empty() {
        return Ok(());
    }
    st.with_conn(|db| {
        let mode: Option<String> = db
            .query_row("SELECT mode FROM lyrics WHERE track_uri=?", [uri], |r| r.get(0))
            .map(Some)
            .or_else(|e| if e == rusqlite::Error::QueryReturnedNoRows { Ok(None) } else { Err(e) })?;
        if mode.is_some_and(|m| m != "none") {
            return Ok(());
        }
        db.execute(
            "INSERT OR REPLACE INTO lyrics(track_uri, mode, synced, plain, source, fetched_at) VALUES (?,?,?,?,?,?)",
            rusqlite::params![uri, "none", "[]", "[]", source, crate::db::now_iso()],
        )?;
        Ok(())
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn beats_are_parsed_like_python() {
        assert_eq!(parse_beats(&json!({"beats": [500.7, 100, 300], "tempo": 120})), (vec![100, 300, 500], 120.0));
        assert_eq!(parse_beats(&json!({"beats": null, "tempo": "98.5"})), (vec![], 98.5));
        assert_eq!(parse_beats(&json!({"beats": [1, "x"], "tempo": 120})), (vec![], 0.0));
        assert_eq!(parse_beats(&json!({"beats": [1], "tempo": "fast"})), (vec![], 0.0));
    }

    #[test]
    fn none_verdict_is_cached_but_never_over_real_lyrics() {
        let dir = std::env::temp_dir().join(format!("statusify-none-{}", crate::state::now_ms()));
        std::fs::create_dir_all(&dir).unwrap();
        let st = crate::db::Store::open(&dir).unwrap();
        save_none_lyrics(&st, "u1", "fallback").unwrap();
        let mode = |u: &str| st.with_conn(|db| db.query_row("SELECT mode FROM lyrics WHERE track_uri=?", [u], |r| r.get::<_, String>(0)).ok());
        assert_eq!(mode("u1").as_deref(), Some("none"));
        assert!(st.cached("u1").is_none()); // a "none" row is not served as lyrics
        let real = crate::lyrics::Lyrics { mode: "plain".into(), synced: vec![], plain: vec!["x".into()], source: "LRCLIB".into() };
        st.save_lyrics("u2", &real).unwrap();
        save_none_lyrics(&st, "u2", "fallback").unwrap();
        assert_eq!(mode("u2").as_deref(), Some("plain"));
        drop(st);
        let _ = std::fs::remove_dir_all(dir);
    }

    fn play(uri: &str, pos: i64, playing: bool) -> PlayInfo {
        PlayInfo { uri: uri.into(), duration_ms: 200_000, position_ms: pos, is_playing: playing }
    }

    #[test]
    fn custom_minutes_parse() {
        assert_eq!(parse_minutes("45"), Some(45));
        assert_eq!(parse_minutes(" 45 min"), Some(45));
        assert_eq!(parse_minutes("10m"), Some(10));
        assert_eq!(parse_minutes("0"), None);
        assert_eq!(parse_minutes("601"), None);
        assert_eq!(parse_minutes("abc"), None);
    }

    #[test]
    fn minutes_timer_counts_down_and_fires_once() {
        let t0 = Instant::now();
        let mut s = SleepTimer::default();
        s.set(SleepSpec::from_json(&json!(15)).unwrap(), t0, None);
        assert_eq!(s.value(), "15");
        assert_eq!(s.label(t0, None), "Sleep in 15:00");
        assert!(!s.tick(t0 + Duration::from_secs(899), None));
        assert!(s.tick(t0 + Duration::from_secs(900), None));
        assert!(!s.active());
        assert_eq!(s.label(t0, None), "");
        assert_eq!(s.value(), "off");
    }

    #[test]
    fn end_of_song_fires_near_the_end_or_on_skip() {
        let t0 = Instant::now();
        let mut s = SleepTimer::default();
        s.set(SleepSpec::from_json(&json!("eos")).unwrap(), t0, Some(&play("a", 0, true)));
        assert_eq!(s.label(t0, Some(&play("a", 140_000, true))), "Sleep at end of song · 1:00");
        assert!(!s.tick(t0, Some(&play("a", 199_000, true))));
        assert!(!s.tick(t0, Some(&play("a", 199_500, false)))); // paused: wait
        assert!(s.tick(t0, Some(&play("a", 199_500, true))));
        s.set(SleepSpec::EndOfSong, t0, Some(&play("a", 0, true)));
        assert!(s.tick(t0, Some(&play("b", 0, true))));
    }

    #[test]
    fn sleep_spec_parsing() {
        assert!(matches!(SleepSpec::from_json(&json!(null)).unwrap(), SleepSpec::Off));
        assert!(matches!(SleepSpec::from_json(&json!("off")).unwrap(), SleepSpec::Off));
        assert!(matches!(SleepSpec::from_json(&json!(0)).unwrap(), SleepSpec::Off));
        assert!(matches!(SleepSpec::from_json(&json!("30")).unwrap(), SleepSpec::Minutes(m) if m == 30.0));
        assert!(SleepSpec::from_json(&json!("soon")).is_err());
        assert_eq!(fmt(3723.0), "1:02:03");
    }

    #[test]
    fn bridge_health_flags_after_grace_and_clears() {
        let t0 = Instant::now();
        let mut h = BridgeHealth::new(t0);
        assert_eq!(h.evaluate(true, t0 + Duration::from_secs(10)), None);
        assert_eq!(h.evaluate(true, t0 + Duration::from_secs(45)), Some(Verdict::Flag));
        assert_eq!(h.evaluate(true, t0 + Duration::from_secs(60)), None);
        assert_eq!(h.evaluate(false, t0 + Duration::from_secs(61)), Some(Verdict::Clear));
        // Spotify launches again: a fresh grace period
        assert_eq!(h.evaluate(true, t0 + Duration::from_secs(100)), None);
        assert_eq!(h.evaluate(true, t0 + Duration::from_secs(106)), Some(Verdict::Flag));
        assert_eq!(h.on_connect(), Some(Verdict::Clear));
        assert!(!h.needs_check());
        h.on_disconnect(t0 + Duration::from_secs(200));
        h.snooze(t0 + Duration::from_secs(200), Duration::from_secs(120));
        assert_eq!(h.evaluate(true, t0 + Duration::from_secs(300)), None);
        assert_eq!(h.evaluate(true, t0 + Duration::from_secs(365)), Some(Verdict::Flag));
    }
}
