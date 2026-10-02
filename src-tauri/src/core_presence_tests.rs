use super::*;
use crate::lyrics::{Line, Lyrics};
use crate::state::Track;

const BASE_MS: i64 = 1_000_000;

fn sheet(lines: &[(i64, &str)]) -> Lyrics {
    Lyrics {
        mode: "synced".into(),
        synced: lines.iter().map(|&(s, w)| Line { start_ms: s, words: w.into() }).collect(),
        plain: vec![],
        source: "Spicy".into(),
    }
}

fn snap(uri: &str, l: Lyrics, dur: i64) -> Snapshot {
    let mut s = Snapshot::default();
    s.track = Some(Track {
        uri: uri.into(),
        artist: "Lil Peep".into(),
        title: "GODS".into(),
        album: "X".into(),
        album_art: "https://i.scdn.co/image/1".into(),
        blacklisted: false,
    });
    s.duration_ms = dur;
    s.is_playing = true;
    s.lyrics = l;
    s.discord_user = Some("kurepa".into());
    s
}

/// Song position `pos` at elapsed time `ms` since t0.
struct Sim {
    p: Presence,
    t0: Instant,
    sends: Vec<(u64, Option<Value>)>,
    lines: Vec<String>,
    dropped: u32,
    rl: u32,
}

impl Sim {
    fn new() -> Self {
        let t0 = Instant::now();
        Sim { p: Presence::new(true, t0), t0, sends: vec![], lines: vec![], dropped: 0, rl: 0 }
    }
    fn at(&mut self, ms: u64, s: &mut Snapshot, pos: i64, set: &Settings) -> Vec<Effect> {
        let now_ms = BASE_MS + ms as i64;
        s.position_ms = pos;
        s.position_at_ms = now_ms;
        let eff = self.p.tick(s, self.t0 + Duration::from_millis(ms), now_ms, set);
        for e in &eff {
            match e {
                Effect::Send(v) => self.sends.push((ms, v.clone())),
                Effect::Line(l) => self.lines.push(l.clone()),
                Effect::Dropped => self.dropped += 1,
                Effect::RateLimited(_) => self.rl += 1,
                Effect::Log(_) => {}
            }
        }
        eff
    }
    /// Play from song position `from` for `dur` ms, ticking every 50 ms.
    fn play(&mut self, start_ms: u64, s: &mut Snapshot, from: i64, dur: u64, set: &Settings) -> u64 {
        let mut t = start_ms;
        while t <= start_ms + dur {
            self.at(t, s, from + (t - start_ms) as i64, set);
            t += 50;
        }
        t
    }
    fn states(&self) -> Vec<String> {
        self.sends.iter().filter_map(|(_, v)| v.as_ref().map(|a| a["state"].as_str().unwrap().to_string())).collect()
    }
}

#[test]
fn waits_out_the_settle_time_then_publishes_the_sung_line() {
    let mut sim = Sim::new();
    let mut s = snap("spotify:track:a", sheet(&[(0, "first"), (6000, "second"), (12000, "third")]), 30000);
    let set = Settings::default();
    sim.play(0, &mut s, 0, 1400, &set);
    assert!(sim.sends.is_empty(), "nothing inside the 1.5 s settle time");
    sim.play(1450, &mut s, 1450, 12000, &set);
    let (t, a) = &sim.sends[0];
    assert!(*t >= 1500 && *t <= 1550);
    let a = a.as_ref().unwrap();
    assert_eq!(a["state"], "first");
    assert_eq!(a["type"], 2);
    assert_eq!(a["status_display_type"], 2);
    assert_eq!(a["details_url"], "https://open.spotify.com/track/a");
    assert_eq!(a["buttons"][0]["label"], "Listen on Spotify");
    assert!(sim.states().contains(&"second".to_string()));
    assert!(sim.states().contains(&"third".to_string()));
    assert_eq!(sim.lines.last().unwrap(), "third");
}

#[test]
fn never_more_than_five_frames_per_twenty_seconds() {
    // A dense verse: 1.2 s lines of 70+ characters (cannot be packed much).
    let words: Vec<String> = (0..60).map(|i| format!("line {i:02} {}", "la ".repeat(22))).collect();
    let lines: Vec<(i64, &str)> = words.iter().enumerate().map(|(i, w)| (i as i64 * 1200, w.as_str())).collect();
    let mut sim = Sim::new();
    let mut s = snap("spotify:track:dense", sheet(&lines), 80000);
    sim.play(0, &mut s, 0, 75000, &Settings::default());
    let times: Vec<u64> = sim.sends.iter().map(|(t, _)| *t).collect();
    assert!(times.len() >= 15);
    for (i, t) in times.iter().enumerate() {
        let in_window = times[i..].iter().take_while(|x| **x < t + 20000).count();
        assert!(in_window <= 5, "{in_window} frames within 20 s of {t}");
    }
    assert!(sim.dropped > 0, "a dense verse drops lines and says so");
    assert!(sim.rl > 0, "and reports rate-limit waits");
}

#[test]
fn pause_clears_once_and_paused_indicator_is_optional() {
    let mut sim = Sim::new();
    let mut s = snap("spotify:track:a", sheet(&[(0, "first")]), 30000);
    let set = Settings::default();
    let t = sim.play(0, &mut s, 0, 2000, &set);
    assert_eq!(sim.sends.len(), 1);
    s.is_playing = false;
    sim.at(t, &mut s, 2000, &set);
    sim.at(t + 50, &mut s, 2000, &set);
    assert_eq!(sim.sends.len(), 2);
    assert!(sim.sends[1].1.is_none(), "cleared");

    let mut sim = Sim::new();
    let mut s = snap("spotify:track:a", sheet(&[(0, "first")]), 30000);
    let set = Settings { show_paused: true, ..Default::default() };
    let t = sim.play(0, &mut s, 0, 2000, &set);
    s.is_playing = false;
    sim.at(t, &mut s, 2000, &set);
    let a = sim.sends[1].1.as_ref().unwrap();
    assert_eq!(a["state"], "\u{23f8} Paused");
    assert!(a.get("timestamps").is_none());
}

#[test]
fn rpc_switch_clears_on_the_falling_edge_and_republishes_on_return() {
    let mut sim = Sim::new();
    let mut s = snap("spotify:track:a", sheet(&[(0, "first")]), 30000);
    let on = Settings::default();
    let off = Settings { enabled: false, ..Default::default() };
    let t = sim.play(0, &mut s, 0, 2000, &on);
    let t = sim.play(t, &mut s, 2000, 500, &off);
    assert_eq!(sim.sends.len(), 2);
    assert!(sim.sends[1].1.is_none());
    sim.play(t, &mut s, 2500, 3000, &on);
    assert_eq!(sim.sends.len(), 3);
    assert_eq!(sim.sends[2].1.as_ref().unwrap()["state"], "first");
}

#[test]
fn blacklisted_tracks_are_cleared_and_stay_silent() {
    let mut sim = Sim::new();
    let mut s = snap("spotify:track:a", sheet(&[(0, "first")]), 30000);
    let set = Settings::default();
    sim.play(0, &mut s, 0, 2000, &set);
    let mut b = snap("spotify:track:b", sheet(&[(0, "secret")]), 30000);
    b.track.as_mut().unwrap().blacklisted = true;
    sim.play(2050, &mut b, 0, 10000, &set);
    assert_eq!(sim.sends.len(), 2);
    assert!(sim.sends[1].1.is_none());
    assert!(sim.lines.contains(&"— blacklisted —".to_string()));
}

#[test]
fn instrumental_gap_shows_the_marker_and_the_hook_returns() {
    // lines every 3 s, then a 25 s break, then the same hook again
    let mut l: Vec<(i64, &str)> = (0..6).map(|i| (i * 3000, if i % 2 == 0 { "hook" } else { "verse" })).collect();
    l.push((40000, "hook"));
    l.push((43000, "end"));
    let mut sim = Sim::new();
    let mut s = snap("spotify:track:g", sheet(&l), 50000);
    let set = Settings { instrumental_text: "~ music ~".into(), ..Default::default() };
    sim.play(0, &mut s, 0, 45000, &set);
    let st = sim.states();
    let gap = st.iter().position(|x| x == "~ music ~").expect("marker shown");
    assert!(st[gap + 1..].contains(&"hook".to_string()), "the hook after the break is republished: {st:?}");
}

#[test]
fn per_track_offset_moves_the_lyrics() {
    let mut sim = Sim::new();
    let mut s = snap("spotify:track:a", sheet(&[(0, "first"), (8000, "second")]), 30000);
    let set = Settings { offset_ms: 2000, ..Default::default() };
    sim.play(0, &mut s, 0, 7000, &set);
    let i = sim.states().iter().position(|x| x == "second").expect("second line sent");
    assert!(sim.sends.iter().filter(|(_, v)| v.is_some()).nth(i).unwrap().0 <= 6100);
}

#[test]
fn activity_follows_the_rpc_toggles() {
    let s = snap("spotify:track:a", Lyrics::none(), 30000);
    let set = Settings { status_shows_song: false, link_track: false, listen_button: false, ..Default::default() };
    let a = build_activity(&s, &["x".into()], true, &set, BASE_MS);
    assert_eq!(a["status_display_type"], 0);
    assert!(a.get("details_url").is_none());
    assert!(a["assets"].get("large_url").is_none());
    assert!(a.get("buttons").is_none());
    let set = Settings { listen_button: true, link_track: false, ..Default::default() };
    let a = build_activity(&s, &[], true, &set, BASE_MS);
    assert!(a.get("details_url").is_none());
    assert_eq!(a["buttons"][0]["url"], "https://open.spotify.com/track/a");
    assert_eq!(a["state"], "— ");
    // one-char album falls back to the artist for the hover text
    assert_eq!(a["assets"]["large_text"], "Lil Peep");
    let t = test_activity(&Settings::default(), BASE_MS);
    assert_eq!(t["state"], "✓ Test presence");
    assert!(t.get("buttons").is_none());
}

#[test]
fn a_disconnect_starts_over() {
    let mut sim = Sim::new();
    let mut s = snap("spotify:track:a", sheet(&[(0, "first")]), 30000);
    let set = Settings::default();
    let t = sim.play(0, &mut s, 0, 2000, &set);
    s.discord_user = None;
    let t = sim.play(t, &mut s, 2000, 500, &set);
    s.discord_user = Some("kurepa".into());
    sim.play(t, &mut s, 2500, 2500, &set);
    // republished after the reconnect (after the settle time)
    assert_eq!(sim.states(), vec!["first", "first"]);
}

#[test]
fn no_lyrics_publishes_title_only() {
    let mut sim = Sim::new();
    let mut s = snap("spotify:track:a", Lyrics::none(), 30000);
    sim.play(0, &mut s, 0, 3000, &Settings::default());
    assert_eq!(sim.states(), vec!["— "]);
}
