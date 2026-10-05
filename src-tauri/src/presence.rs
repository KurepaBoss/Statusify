//! What the Discord profile shows, and when (port of main.py's rpc_loop).
//!
//! Every publish decision re-plans the next 30 s of song with the beam-search
//! planner (engine::plan, port of statusify_presence_plan.py) under Discord's
//! 5-per-20-s SET_ACTIVITY limit, then acts on the plan's first update. The
//! loop also handles: the RPC switch (clear once on the falling edge), pause
//! (clear, or a "⏸ Paused" presence with show_paused_rpc), the blacklist, the
//! settle time after a track change (rapid skips would burn the budget), the
//! hold after a burst of seeks, the per-track lyric offset, instrumental
//! markers, and counting lines the song moved past before their words reached
//! Discord.
//!
//! Every frame, a clear and the paused indicator included, goes through one
//! ledger and never leaves while it is full: Discord drops what is over 5 per
//! 20 s, and a clear that was dropped leaves a stale lyric on the profile. A
//! pause is acted on only once it has lasted PAUSE_HOLD, so a quick
//! pause/resume sends nothing, and only if it leaves a slot free for the
//! resume, which would otherwise find the profile blank, with music playing,
//! and no slot to put it back. The ledger survives a Discord reconnect.
//!
//! `Presence::tick` is pure (snapshot + clock + settings in, effects out) so
//! it is tested headless; `run_loop` drives it against the engine.

use crate::engine::plan::{self, Kind, Limits, Publish, Unit};
use crate::engine::Engine;
use crate::lyrics::{instrumental_gaps, join_lines, Lyrics};
use crate::state::Snapshot;
use serde_json::{json, Value};
use std::collections::VecDeque;
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::sync::mpsc;

pub const RATE_CALLS: usize = 5;
pub const RATE_WINDOW: Duration = Duration::from_secs(20);
pub const MAX_STATE: usize = 128;
/// A seek, a stall or a big offset change moves playback off the clock a
/// plan was made on; past this much drift, plan again from here.
pub const PLAN_DRIFT_MS: f64 = 1500.0;
/// After a track change nothing is sent for this long (rapid skips) when its
/// lyrics are not known yet, so the bridge's answer usually lands before the
/// first frame, and during a skip burst.
pub const CALIBRATION: Duration = Duration::from_millis(1500);
/// The same wait once the lyrics are known (pinned, prefetched, cached, or
/// they have just arrived) and the change is the first in a while.
pub const KNOWN_SETTLE: Duration = Duration::from_millis(400);
/// Resuming the track that was paused (lyrics known) only debounces
/// pause/play spam; without lyrics it waits CALIBRATION like any first frame.
pub const RESUME_SETTLE: Duration = Duration::from_millis(300);
/// A pause takes the profile down only once it has lasted this long. A quick
/// pause and resume (a mis-press, a Bluetooth hiccup, a bridge blip) leaves
/// the profile alone and sends nothing, instead of a clear and a republish.
pub const PAUSE_HOLD: Duration = Duration::from_millis(1500);
/// Slots a pause must leave free before it clears the profile: the resume
/// that follows can always put the presence back at once. A clear that took
/// the last slot would leave the profile blank, with music playing, until the
/// window freed one (up to 20 s).
pub const PAUSE_RESERVE: usize = 1;
/// A resume with the profile still up leaves its progress bar behind by the
/// time the song stood still. Up to this much is left alone; past it one
/// frame puts the bar right ...
pub const BAR_TOLERANCE: Duration = Duration::from_millis(1500);
/// ... once play has been steady for this long, so toggling does not spend
/// frames on a cosmetic fix.
pub const BAR_SETTLE: Duration = Duration::from_millis(2000);
/// A track change this soon after the previous one is part of a skip burst.
pub const BURST_WINDOW: Duration = Duration::from_millis(3000);
/// A seek this soon after the previous one is a drag, not a click: the
/// destination is published once the position has been still for SEEK_SETTLE.
pub const SEEK_BURST: Duration = Duration::from_millis(2000);
pub const SEEK_SETTLE: Duration = Duration::from_millis(500);
pub const DEFAULT_INSTRUMENTAL: &str = "🎵 ─ ─ ─ ─ ─ ─ ─ ─ ─ 🎵";
pub const LISTEN_LABEL: &str = "Listen on Spotify";
pub const PAUSED_TEXT: &str = "\u{23f8} Paused";

/// What the member list shows after "Listening to": the song (DETAILS) or
/// the Discord application's name (NAME).
pub const STATUS_DISPLAY_NAME: i64 = 0;
pub const STATUS_DISPLAY_DETAILS: i64 = 2;

/// The settings the loop follows, read live from statusify.cfg every tick.
#[derive(Clone, Debug)]
pub struct Settings {
    pub enabled: bool,
    pub show_paused: bool,
    pub status_shows_song: bool,
    pub link_track: bool,
    pub listen_button: bool,
    pub instrumental_text: String,
    pub offset_ms: i64,
}

impl Default for Settings {
    fn default() -> Self {
        Settings {
            enabled: true,
            show_paused: false,
            status_shows_song: true,
            link_track: true,
            listen_button: true,
            instrumental_text: DEFAULT_INSTRUMENTAL.into(),
            offset_ms: 0,
        }
    }
}

impl Settings {
    pub fn from_engine(e: &Engine, snap: &Snapshot) -> Self {
        let mut s = Settings { enabled: e.core.rpc_enabled(), ..Default::default() };
        if let Some(c) = e.config() {
            s.show_paused = c.get_bool("preferences", "show_paused_rpc", false);
            s.status_shows_song = c.get_bool("preferences", "status_shows_song", true);
            s.link_track = c.get_bool("preferences", "link_track", true);
            s.listen_button = c.get_bool("preferences", "listen_button", true);
            let t = c.get_or("preferences", "instrumental_text", "");
            if !t.is_empty() {
                s.instrumental_text = t;
            }
        }
        s.offset_ms = e.offset_ms_for(snap.track.as_ref().map_or("", |t| t.uri.as_str()));
        s
    }
}

pub fn track_url(uri: &str) -> Option<String> {
    uri.strip_prefix("spotify:track:").map(|id| format!("https://open.spotify.com/track/{id}"))
}

fn cut(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

/// Everything one SET_ACTIVITY activity is built from.
pub struct ActivityInput<'a> {
    pub title: &'a str,
    pub artist: &'a str,
    pub album: &'a str,
    pub art: &'a str,
    pub uri: &'a str,
    pub lines: &'a [String],
    /// (position, duration) for the progress bar; None for none.
    pub timing: Option<(i64, i64)>,
}

/// The activity: type 2 (Listening) so it sits beside a game instead of
/// fighting it for the Playing slot.
pub fn activity(a: &ActivityInput, s: &Settings, now_ms: i64) -> Value {
    let label = cut(&format!("{} — {}", a.title, a.artist), 128);
    // Hover text on the cover: the album. Discord rejects the whole activity
    // if a text field is shorter than 2 characters.
    let hover = [a.album.trim(), a.artist, label.as_str()]
        .into_iter()
        .find(|x| x.trim().chars().count() >= 2)
        .unwrap_or(&label)
        .to_string();
    let mut act = json!({
        "type": 2,
        "details": label,
        "status_display_type": if s.status_shows_song { STATUS_DISPLAY_DETAILS } else { STATUS_DISPLAY_NAME },
        "assets": {"large_image": if a.art.is_empty() { "spotify" } else { a.art },
                   "large_text": cut(&hover, 128)},
    });
    let page = track_url(a.uri);
    if let (true, Some(url)) = (s.link_track, &page) {
        act["details_url"] = json!(url);
        act["assets"]["large_url"] = json!(url);
    }
    // Buttons ride in the same payload: free against the rate budget.
    if let (true, Some(url)) = (s.listen_button, &page) {
        act["buttons"] = json!([{"label": cut(LISTEN_LABEL, 32), "url": url}]);
    }
    let f: Vec<&str> = a.lines.iter().map(String::as_str).filter(|l| !l.is_empty()).collect();
    act["state"] = json!(if f.is_empty() { "— ".to_string() } else { cut(&join_lines(&f), MAX_STATE) });
    // Milliseconds: whole seconds let the progress bar drift up to 1 s.
    if let Some((pos, dur)) = a.timing {
        if dur > 0 {
            let start = now_ms - pos;
            act["timestamps"] = json!({"start": start, "end": start + dur});
        }
    }
    act
}

/// The current track's activity carrying `lines`.
pub fn build_activity(snap: &Snapshot, lines: &[String], timed: bool, s: &Settings, now_ms: i64) -> Value {
    let t = snap.track.as_ref().expect("track");
    activity(
        &ActivityInput {
            title: &t.title,
            artist: &t.artist,
            album: &t.album,
            art: &t.album_art,
            uri: &t.uri,
            lines,
            timing: timed.then(|| (snap.estimated_position(now_ms), snap.duration_ms)),
        },
        s,
        now_ms,
    )
}

/// The "Test presence" activity, to verify Discord without a song.
pub fn test_activity(s: &Settings, now_ms: i64) -> Value {
    activity(
        &ActivityInput {
            title: "Statusify test",
            artist: "If you can see this, RPC works",
            album: "",
            art: "",
            uri: "",
            lines: &["✓ Test presence".to_string()],
            timing: None,
        },
        s,
        now_ms,
    )
}

/// What a tick asks the outside world to do.
#[derive(Debug, PartialEq)]
pub enum Effect {
    /// SET_ACTIVITY: Some(activity), or None to clear.
    Send(Option<Value>),
    /// What Discord now shows, for the UI ("" = nothing / title only).
    Line(String),
    /// The song moved past a line whose words never reached Discord.
    Dropped,
    /// The sung line waits for the rate limit this many seconds.
    RateLimited(f64),
    Log(String),
}

type UnitKey = Option<(Kind, i64)>;

/// Why the first frame of a track (or of a resume) waits.
#[derive(Clone, Copy, Debug)]
enum Settle {
    /// A new track: KNOWN_SETTLE once its lyrics are known, else CALIBRATION.
    Track,
    /// A new track soon after another one: skipping, so CALIBRATION.
    Burst,
    /// The paused track plays on: RESUME_SETTLE once its lyrics are known,
    /// else CALIBRATION like any first frame.
    Resume,
}

impl Settle {
    fn wait(self, lyrics_known: bool) -> Duration {
        match self {
            Settle::Track if lyrics_known => KNOWN_SETTLE,
            Settle::Resume if lyrics_known => RESUME_SETTLE,
            Settle::Track | Settle::Burst | Settle::Resume => CALIBRATION,
        }
    }
}

/// A clear (or the paused indicator) that is due but waits for a free slot.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Owed {
    /// Playback paused and stayed paused for PAUSE_HOLD.
    Pause,
    /// The RPC switch went off.
    Off,
    /// A blacklisted track began.
    Blacklist,
}

pub struct Presence {
    fresh: bool,
    /// When the frames of the last RATE_WINDOW were sent: every one, clears
    /// and the paused indicator included. Survives a Discord reconnect.
    rl: VecDeque<Instant>,
    last_uri: Option<String>,
    was_playing: bool,
    /// When the current track (or resume) began, and why it waits.
    settle: Option<(Instant, Settle)>,
    /// When the last new track began (not a resume): burst detection.
    last_change: Option<Instant>,
    /// The track that was playing when playback paused.
    resume_uri: Option<String>,
    /// Playback paused at this instant (and at this song position) and the
    /// profile has not been touched yet: a pause is only acted on once it
    /// has lasted PAUSE_HOLD.
    pause_since: Option<(Instant, f64)>,
    /// A clear (or the paused indicator) that is due and waits for a slot.
    owed: Option<Owed>,
    /// Slots a pause leaves free (PAUSE_RESERVE). A field only so that a test
    /// can show what the reserve is for by taking it away.
    pause_reserve: usize,
    /// While the profile stayed up through short pauses: how long the song
    /// stood still since the last frame, i.e. how far the progress bar on
    /// Discord is ahead of the song. (A song that kept playing, as when only
    /// the bridge connection blinked, did not stand still.)
    bar_behind: Duration,
    /// Put the bar right at this instant if it is still behind then.
    bar_fix_at: Option<Instant>,
    last_jump: Option<Instant>,
    /// Nothing is published before this: a seek burst is still going on.
    hold_until: Option<Instant>,
    rpc_was_enabled: bool,
    /// What Discord shows for this track as texts (plan::TITLE / plan::GAP
    /// stand for the title-only presence and the instrumental marker). Not
    /// the last text *published*: a hook coming back after the marker
    /// replaced it must not be skipped as a duplicate.
    shown: Vec<String>,
    upcoming: Option<Vec<Publish>>,
    plan_src: Option<(Lyrics, i64)>,
    plan_anchor: (f64, Instant),
    units: Vec<Unit>,
    cur_key: UnitKey,
    cur_line: Option<String>,
    cur_seen: bool,
    rl_noted: Option<(Option<String>, UnitKey)>,
    /// Does Discord hold a presence from us? A clear is a SET_ACTIVITY frame
    /// too; clearing nothing would spend a slot for nothing.
    have_presence: bool,
}

impl Presence {
    pub fn new(enabled: bool, now: Instant) -> Self {
        Presence {
            fresh: true,
            rl: VecDeque::new(),
            last_uri: None,
            was_playing: false,
            settle: None,
            last_change: None,
            resume_uri: None,
            pause_since: None,
            owed: None,
            pause_reserve: PAUSE_RESERVE,
            bar_behind: Duration::ZERO,
            bar_fix_at: None,
            last_jump: None,
            hold_until: None,
            rpc_was_enabled: enabled,
            shown: vec![],
            upcoming: None,
            plan_src: None,
            plan_anchor: (0.0, now),
            units: vec![],
            cur_key: None,
            cur_line: None,
            cur_seen: false,
            rl_noted: None,
            have_presence: false,
        }
    }

    /// Discord went away. What it showed went with the connection, so start
    /// over like a fresh loop once it is back; the ledger does not: Discord
    /// counts what was sent, not on which connection, and a Discord that
    /// hangs up right after READY would otherwise hand out 5 fresh frames
    /// per cycle.
    fn restart(&mut self, enabled: bool, now: Instant) {
        let rl = std::mem::take(&mut self.rl);
        *self = Presence::new(enabled, now);
        self.rl = rl;
    }

    /// Forget what was published so the next tick starts clean.
    fn reset_track_state(&mut self) {
        self.last_uri = None;
        self.was_playing = false;
        self.settle = None;
        self.last_jump = None;
        self.hold_until = None;
        self.pause_since = None;
        self.bar_behind = Duration::ZERO;
        self.bar_fix_at = None;
        self.shown.clear();
        self.upcoming = None;
        self.cur_key = None;
    }

    /// Nothing is sent before this: the settle after a track change or a
    /// resume, or the hold during a burst of seeks.
    fn gate_until(&self, lyrics_known: bool) -> Option<Instant> {
        let settle = self.settle.map(|(since, kind)| since + kind.wait(lyrics_known));
        match (settle, self.hold_until) {
            (Some(a), Some(b)) => Some(a.max(b)),
            (a, b) => a.or(b),
        }
    }

    fn prune(&mut self, now: Instant) {
        while self.rl.front().is_some_and(|t| now.duration_since(*t) >= RATE_WINDOW) {
            self.rl.pop_front();
        }
    }

    /// Slots left in the window.
    fn free_slots(&mut self, now: Instant) -> usize {
        self.prune(now);
        RATE_CALLS.saturating_sub(self.rl.len())
    }

    fn avail(&mut self, now: Instant) -> bool {
        self.free_slots(now) > 0
    }

    /// Is there a slot for a frame sent outside the loop (the test presence)?
    pub fn has_slot(&mut self, now: Instant) -> bool {
        self.avail(now)
    }

    /// A presence frame went out.
    fn sent(&mut self, now: Instant) {
        self.rl.push_back(now);
        self.have_presence = true;
        self.owed = None;
        self.bar_behind = Duration::ZERO;
        self.bar_fix_at = None;
    }

    /// Account for a frame sent outside the plan (the test presence) and
    /// re-plan from an unknown screen.
    pub fn record_external(&mut self, now: Instant) {
        self.sent(now);
        self.shown.clear();
        self.upcoming = None;
    }

    /// Send what is owed (a clear, or the paused indicator) as soon as the
    /// ledger has a slot for it. Every frame is counted, clears too: Discord
    /// drops what is over the limit, and a clear that was dropped leaves a
    /// stale lyric on the profile, which is worse than one that waits.
    /// A pause also leaves PAUSE_RESERVE slots free, see there.
    fn settle_owed(&mut self, snap: &Snapshot, now: Instant, now_ms: i64, s: &Settings, out: &mut Vec<Effect>) {
        let Some(why) = self.owed else { return };
        let indicator = why == Owed::Pause && s.show_paused && snap.track.as_ref().is_some_and(|t| !t.blacklisted);
        if !indicator && !self.have_presence {
            // Nothing of ours is up.
            self.owed = None;
            if why == Owed::Pause {
                out.push(Effect::Line(String::new()));
            }
            return;
        }
        let keep = if why == Owed::Pause { self.pause_reserve } else { 0 };
        if self.free_slots(now) <= keep {
            return;
        }
        self.owed = None;
        self.rl.push_back(now);
        if indicator {
            out.push(Effect::Send(Some(build_activity(snap, &[PAUSED_TEXT.to_string()], false, s, now_ms))));
            self.have_presence = true;
            out.push(Effect::Log("RPC paused indicator".into()));
        } else {
            out.push(Effect::Send(None));
            self.have_presence = false;
            out.push(Effect::Log("RPC cleared".into()));
        }
        if why == Owed::Pause {
            out.push(Effect::Line(String::new()));
        }
    }

    /// The same track plays on after a pause shorter than PAUSE_HOLD. The
    /// profile was not touched, so there is nothing to put back; only its
    /// progress bar ran on while the song stood still (`moved`: how far the
    /// song got meanwhile).
    fn resumed_quickly(&mut self, away: Duration, moved: Duration, now: Instant) {
        if self.have_presence {
            self.bar_behind += away.saturating_sub(moved);
            // Past the tolerance one frame puts the bar right, once play has
            // been steady for BAR_SETTLE (a toggling thumb postpones it).
            self.bar_fix_at = (self.bar_behind > BAR_TOLERANCE).then_some(now + BAR_SETTLE);
        } else {
            // Nothing is up yet: the first frame still waits, counted from
            // the resume.
            self.settle = Some((now, Settle::Resume));
            self.upcoming = None;
        }
    }

    fn unit_at(&self, pos: f64) -> Option<&Unit> {
        let i = self.units.partition_point(|u| (u.start as f64) <= pos);
        let u = self.units.get(i.checked_sub(1)?)?;
        (pos < u.end as f64).then_some(u)
    }

    fn cur_in_shown(&self) -> bool {
        self.cur_line.as_ref().is_some_and(|l| self.shown.contains(l))
    }

    pub fn tick(&mut self, snap: &Snapshot, now: Instant, now_ms: i64, s: &Settings) -> Vec<Effect> {
        let mut out = Vec::new();
        if snap.discord_user.is_none() {
            // Not connected: start over like a fresh loop once it reconnects.
            if !self.fresh {
                self.restart(s.enabled, now);
            }
            return out;
        }
        self.fresh = false;
        if !s.enabled {
            if self.rpc_was_enabled {
                self.rpc_was_enabled = false;
                self.reset_track_state();
                self.resume_uri = None;
                self.owed = Some(Owed::Off);
                out.push(Effect::Line(String::new()));
                out.push(Effect::Log("RPC disabled".into()));
            }
            self.settle_owed(snap, now, now_ms, s, &mut out);
            return out;
        }
        if !self.rpc_was_enabled {
            // Re-enabled: republish the current track from scratch. Playing,
            // the next frame replaces whatever is still up; paused, none is
            // coming, so a clear that is still owed stays owed.
            self.reset_track_state();
            self.rpc_was_enabled = true;
            if snap.is_playing {
                self.owed = None;
            }
            out.push(Effect::Log("RPC enabled".into()));
        }
        if !snap.is_playing {
            if self.was_playing {
                // The pause. Nothing is sent yet: a quick pause and resume
                // (a mis-press, a Bluetooth hiccup, a bridge blip) must not
                // cost the two frames a clear and a republish would.
                self.was_playing = false;
                self.resume_uri = self.last_uri.clone();
                self.pause_since = Some((now, (snap.estimated_position(now_ms) + s.offset_ms) as f64));
            }
            if self.pause_since.is_some_and(|(t, _)| now.saturating_duration_since(t) >= PAUSE_HOLD) {
                self.pause_since = None;
                self.reset_track_state();
                self.owed = Some(Owed::Pause);
            }
            self.settle_owed(snap, now, now_ms, s, &mut out);
            return out;
        }
        self.was_playing = true;
        let Some(track) = snap.track.as_ref().filter(|t| !t.title.is_empty()) else { return out };
        // A pause, if there was one, is over; so is its claim on resume_uri.
        let paused = self.pause_since.take();
        let away = paused.map(|(t, _)| now.saturating_duration_since(t));
        let resume_uri = self.resume_uri.take();
        match away {
            // PAUSE_HOLD passed without a tick (the loop was suspended): as
            // if it had been acted on.
            Some(d) if d >= PAUSE_HOLD => self.reset_track_state(),
            Some(d) if self.last_uri.as_deref() == Some(track.uri.as_str()) => {
                let at_pause = paused.map_or(0.0, |(_, p)| p);
                let moved = ((snap.estimated_position(now_ms) + s.offset_ms) as f64 - at_pause).max(0.0);
                self.resumed_quickly(d, Duration::from_millis(moved as u64), now);
            }
            _ => {}
        }
        // Blacklist: clear what we published for this track, then stay silent.
        if track.blacklisted {
            if self.last_uri.as_deref() != Some(track.uri.as_str()) {
                self.last_uri = Some(track.uri.clone());
                self.shown.clear();
                self.upcoming = None;
                self.owed = Some(Owed::Blacklist);
                out.push(Effect::Line("— blacklisted —".into()));
            }
            self.settle_owed(snap, now, now_ms, s, &mut out);
            return out;
        }
        // What was owed (a clear) is moot: the frame this track sends
        // replaces whatever is up.
        self.owed = None;
        if self.last_uri.as_deref() != Some(track.uri.as_str()) {
            let resumed = resume_uri.is_some_and(|u| u == track.uri);
            self.last_uri = Some(track.uri.clone());
            self.shown.clear();
            self.upcoming = None;
            self.cur_key = None;
            self.last_jump = None;
            self.hold_until = None;
            self.bar_behind = Duration::ZERO;
            self.bar_fix_at = None;
            let kind = if resumed {
                Settle::Resume
            } else {
                let burst = self.last_change.is_some_and(|t| now.saturating_duration_since(t) < BURST_WINDOW);
                self.last_change = Some(now);
                if burst { Settle::Burst } else { Settle::Track }
            };
            self.settle = Some((now, kind));
        }
        let known = !snap.lyrics.is_none();
        if self.bar_fix_at.is_some_and(|t| now >= t) {
            // The pauses left the bar behind: say where the song is now (the
            // planner sends one frame for the sung line, when a slot allows).
            self.bar_fix_at = None;
            self.shown.clear();
            self.upcoming = None;
        }

        let pos = (snap.estimated_position(now_ms) + s.offset_ms) as f64;
        let since_anchor = now.saturating_duration_since(self.plan_anchor.1).as_secs_f64() * 1000.0;
        let jumped = self.plan_src.is_some() && (pos - self.plan_anchor.0 - since_anchor).abs() > PLAN_DRIFT_MS;
        if jumped {
            self.cur_key = None; // a seek: lines jumped over were not dropped
            let drag = self.last_jump.is_some_and(|t| now.saturating_duration_since(t) < SEEK_BURST);
            self.last_jump = Some(now);
            if drag {
                self.hold_until = Some(now + SEEK_SETTLE);
            }
        }
        let src_changed = self
            .plan_src
            .as_ref()
            .is_none_or(|(l, d)| *d != snap.duration_ms || *l != snap.lyrics);
        if self.upcoming.is_none() || jumped || src_changed || pos >= self.plan_anchor.0 + plan::PLAN_HORIZON_MS / 2.0 {
            // Re-plan: after every send, when the lyrics change, after a
            // seek, and before the horizon runs out.
            self.prune(now);
            if src_changed {
                let gaps = if snap.lyrics.mode == "synced" { instrumental_gaps(&snap.lyrics.synced, snap.duration_ms) } else { vec![] };
                self.units = plan::build_units(&snap.lyrics, snap.duration_ms, &gaps);
                self.plan_src = Some((snap.lyrics.clone(), snap.duration_ms));
            }
            let hist: Vec<f64> = self.rl.iter().map(|x| pos - now.duration_since(*x).as_secs_f64() * 1000.0).collect();
            let wait = self.gate_until(known).map_or(0.0, |c| c.saturating_duration_since(now).as_secs_f64() * 1000.0);
            let lim = Limits { calls: RATE_CALLS, window_ms: RATE_WINDOW.as_secs_f64() * 1000.0, max_state: MAX_STATE, ..Default::default() };
            self.upcoming = Some(plan::plan(&self.units, pos, &hist, &self.shown, pos + wait, lim));
            self.plan_anchor = (pos, now);
        }

        // Count a line as dropped when the song moves past it unseen.
        let u = self.unit_at(pos).cloned();
        let key: UnitKey = u.as_ref().map(|u| (u.kind, u.start));
        if key != self.cur_key {
            if self.cur_key.is_some() && !self.cur_seen {
                if let Some(l) = &self.cur_line {
                    out.push(Effect::Dropped);
                    out.push(Effect::Log(format!("Dropped  ·  {}", cut(l, 40))));
                }
            }
            self.cur_key = key;
            self.cur_seen = false;
            self.cur_line = u.as_ref().filter(|u| u.kind == Kind::Line).map(|u| u.text.clone());
        }
        if self.cur_in_shown() {
            self.cur_seen = true;
        }

        let Some(ev) = self.upcoming.as_ref().and_then(|v| v.first()).cloned() else { return out };
        if pos >= ev.end as f64 {
            self.upcoming = None; // overtaken (a stall, a late tick): re-plan
            return out;
        }
        if pos < ev.t {
            // The sung line is not on Discord and the budget says wait.
            let calibrated = self.gate_until(known).is_none_or(|c| now >= c);
            let noted = Some((self.last_uri.clone(), key));
            if self.cur_line.is_some() && !self.cur_seen && calibrated && self.rl_noted != noted {
                self.rl_noted = noted;
                let w = (ev.t - pos) / 1000.0;
                out.push(Effect::Log(format!("Rate limited  ·  {w:.1}s")));
                out.push(Effect::RateLimited(w));
            }
            return out;
        }
        if !self.avail(now) {
            return out; // a hair early by the ledger's clock
        }
        match ev.kind {
            Kind::Line => {
                out.push(Effect::Send(Some(build_activity(snap, &ev.lines, true, s, now_ms))));
                let refs: Vec<&str> = ev.lines.iter().map(String::as_str).collect();
                let display = join_lines(&refs);
                out.push(Effect::Log(format!("RPC ({}L)  ·  {}", ev.lines.len(), cut(&display, 55))));
                out.push(Effect::Line(display));
            }
            Kind::Gap => {
                out.push(Effect::Send(Some(build_activity(snap, &[s.instrumental_text.clone()], true, s, now_ms))));
                out.push(Effect::Log(format!("RPC instrumental  (gap {:.1}s)", (ev.end - ev.start) as f64 / 1000.0)));
                out.push(Effect::Line(s.instrumental_text.clone()));
            }
            _ => {
                out.push(Effect::Send(Some(build_activity(snap, &[], true, s, now_ms))));
                out.push(Effect::Log(format!("RPC title-only  ·  {} — {}", track.artist, track.title)));
                out.push(Effect::Line(String::new()));
            }
        }
        self.sent(now);
        self.shown = plan::shown_for(&ev);
        if self.cur_in_shown() {
            self.cur_seen = true;
        }
        self.upcoming = None; // re-plan from what is on screen now
        out
    }
}

/// Drive the presence against the engine forever, sending to Discord.
pub async fn run_loop(engine: Arc<Engine>, tx: mpsc::UnboundedSender<crate::discord::Update>) {
    engine.core.with(|c| c.presence_running = true);
    let mut p = Presence::new(engine.core.rpc_enabled(), Instant::now());
    let mut test_waiting = false;
    loop {
        tokio::time::sleep(Duration::from_millis(50)).await;
        let snap = engine.snapshot();
        let now = Instant::now();
        let now_ms = crate::state::now_ms();
        let s = Settings::from_engine(&engine, &snap);
        if engine.core.with(|c| std::mem::take(&mut c.test_request)) {
            if snap.discord_user.is_some() {
                if p.has_slot(now) {
                    let _ = tx.send(Some(test_activity(&s, now_ms)));
                    p.record_external(now);
                    test_waiting = false;
                    crate::log("Test presence sent — check your Discord profile");
                } else {
                    // Discord would drop it: send it as soon as a slot frees.
                    engine.core.with(|c| c.test_request = true);
                    if !test_waiting {
                        test_waiting = true;
                        crate::log("Test presence waits for a free slot (Discord allows 5 updates per 20 s)");
                    }
                }
            } else {
                crate::log("Test presence: not connected to Discord");
            }
        }
        for e in p.tick(&snap, now, now_ms, &s) {
            match e {
                Effect::Send(u) => {
                    let _ = tx.send(u);
                }
                Effect::Line(l) => engine.core.with(|c| c.discord_line = l),
                Effect::Dropped => engine.core.with(|c| c.dropped_lines += 1),
                Effect::RateLimited(w) => engine.core.with(|c| c.rate_limited = Some((w, now_ms))),
                Effect::Log(m) => crate::log(&m),
            }
        }
    }
}

#[cfg(test)]
#[path = "core_presence_tests.rs"]
mod tests;
