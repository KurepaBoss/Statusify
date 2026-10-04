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
fn known_lyrics_wait_a_short_settle_then_publish_the_sung_line() {
    let mut sim = Sim::new();
    let mut s = snap("spotify:track:a", sheet(&[(0, "first"), (6000, "second"), (12000, "third")]), 30000);
    let set = Settings::default();
    sim.play(0, &mut s, 0, 350, &set);
    assert!(sim.sends.is_empty(), "nothing inside the 0.4 s settle time");
    sim.play(400, &mut s, 400, 12000, &set);
    let (t, a) = &sim.sends[0];
    assert!(*t >= 400 && *t <= 450, "sent at {t}");
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

#[test]
fn unknown_lyrics_keep_the_long_settle_and_publish_as_soon_as_they_arrive() {
    // Nothing known: the first frame still waits 1.5 s, and is title-only
    // when no lyrics ever come.
    let mut sim = Sim::new();
    let mut s = snap("spotify:track:a", Lyrics::none(), 30000);
    let set = Settings::default();
    sim.play(0, &mut s, 0, 1400, &set);
    assert!(sim.sends.is_empty());
    sim.play(1450, &mut s, 1450, 500, &set);
    assert!(sim.sends[0].0 >= 1500 && sim.sends[0].0 <= 1550);
    assert_eq!(sim.states(), vec!["— "]);

    // The bridge answers inside the settle window: the first frame carries
    // the sung line, as soon as the lyrics are there, not at 1.5 s.
    let mut sim = Sim::new();
    let mut s = snap("spotify:track:a", Lyrics::none(), 30000);
    let t = sim.play(0, &mut s, 0, 750, &set);
    assert!(sim.sends.is_empty());
    s.lyrics = sheet(&[(0, "first"), (6000, "second")]);
    sim.play(t, &mut s, t as i64, 1500, &set);
    assert_eq!(sim.states(), vec!["first"]);
    assert!(sim.sends[0].0 >= 800 && sim.sends[0].0 <= 900, "sent at {}", sim.sends[0].0);
}

/// `ms` of `uri` playing from its start, ticked every 50 ms from `t`.
fn track_for(sim: &mut Sim, t: &mut u64, uri: &str, ms: u64, l: &Lyrics, set: &Settings) {
    let mut s = snap(uri, l.clone(), 30000);
    let mut p = 0;
    while p < ms {
        sim.at(*t, &mut s, p as i64, set);
        *t += 50;
        p += 50;
    }
}

#[test]
fn a_skip_burst_publishes_only_the_track_it_ends_on() {
    let l = sheet(&[(0, "first"), (6000, "second")]);
    let set = Settings::default();
    // Skipping faster than the settle: nothing goes out until the last
    // track has played 1.5 s (the burst keeps the long wait even though
    // every track's lyrics are known).
    let mut sim = Sim::new();
    let mut t = 0;
    for k in 0..4 {
        track_for(&mut sim, &mut t, &format!("spotify:track:{k}"), 350, &l, &set);
    }
    let mut last = snap("spotify:track:4", l.clone(), 30000);
    sim.play(t, &mut last, 0, 3000, &set);
    assert_eq!(sim.sends.len(), 1);
    let (at, a) = &sim.sends[0];
    assert!(*at >= 1400 + 1500 - 50 && *at <= 1400 + 1500 + 100, "sent at {at}");
    assert_eq!(a.as_ref().unwrap()["details_url"], "https://open.spotify.com/track/4");
}

#[test]
fn a_burst_costs_at_most_the_first_frame_and_a_quiet_change_is_fast_again() {
    let l = sheet(&[(0, "first"), (6000, "second")]);
    let set = Settings::default();
    let mut sim = Sim::new();
    let mut t = 0;
    // The first change after a quiet spell has the short wait, so a skip
    // 0.7 s later has already cost one frame; the rest of the burst has not.
    for k in 0..3 {
        track_for(&mut sim, &mut t, &format!("spotify:track:{k}"), 700, &l, &set);
    }
    let mut last = snap("spotify:track:3", l.clone(), 30000);
    let t_end = sim.play(t, &mut last, 0, 2500, &set);
    assert_eq!(sim.sends.len(), 2, "{:?}", sim.sends.iter().map(|s| s.0).collect::<Vec<_>>());
    assert!(sim.sends[0].0 <= 450);
    assert!(sim.sends[1].0 >= 3 * 700 + 1450);
    // Long after the burst a new track is quick again.
    let t5 = t_end + 5000;
    let mut t = t5;
    track_for(&mut sim, &mut t, "spotify:track:late", 1000, &l, &set);
    let (at, _) = sim.sends.last().unwrap();
    assert!(*at >= t5 + 400 && *at <= t5 + 450, "sent at {at}, track began {t5}");
}

#[test]
fn resuming_the_same_track_waits_only_the_debounce() {
    let mut sim = Sim::new();
    let mut s = snap("spotify:track:a", sheet(&[(0, "first"), (6000, "second")]), 30000);
    let set = Settings::default();
    let t = sim.play(0, &mut s, 0, 2000, &set);
    assert_eq!(sim.sends.len(), 1);
    s.is_playing = false;
    sim.at(t, &mut s, 2000, &set);
    assert!(sim.sends[1].1.is_none(), "paused: cleared");
    // paused for a while, then the same track plays on
    s.is_playing = true;
    let resumed = t + 10_000;
    sim.play(resumed, &mut s, 2000, 1000, &set);
    let (at, a) = &sim.sends[2];
    assert!(*at >= resumed + 300 && *at <= resumed + 350, "sent at {at}, resumed at {resumed}");
    assert_eq!(a.as_ref().unwrap()["state"], "first");
    assert_eq!(sim.sends.len(), 3);
}

#[test]
fn another_track_after_a_pause_is_a_new_track_not_a_resume() {
    let mut sim = Sim::new();
    let mut a = snap("spotify:track:a", Lyrics::none(), 30000);
    let set = Settings::default();
    let t = sim.play(0, &mut a, 0, 2000, &set);
    a.is_playing = false;
    sim.at(t, &mut a, 2000, &set);
    // a different track starts from the pause, lyrics not known yet
    let mut b = snap("spotify:track:b", Lyrics::none(), 30000);
    let began = t + 10_000;
    sim.play(began, &mut b, 0, 2500, &set);
    let (at, _) = sim.sends.last().unwrap();
    assert!(*at >= began + 1500, "sent at {at}, track began {began}");
}

#[test]
fn pause_spam_publishes_nothing_until_playback_settles() {
    let mut sim = Sim::new();
    let mut s = snap("spotify:track:a", sheet(&[(0, "first")]), 30000);
    let set = Settings::default();
    let mut t = sim.play(0, &mut s, 0, 2000, &set);
    assert_eq!(sim.sends.len(), 1);
    // play/pause toggled every 200 ms, five times
    for _ in 0..5 {
        s.is_playing = false;
        for _ in 0..4 {
            sim.at(t, &mut s, 2000, &set);
            t += 50;
        }
        s.is_playing = true;
        for _ in 0..4 {
            sim.at(t, &mut s, 2000, &set);
            t += 50;
        }
    }
    assert_eq!(sim.sends.len(), 2, "one clear, nothing else: {:?}", sim.sends.iter().map(|s| s.1.is_some()).collect::<Vec<_>>());
    sim.play(t, &mut s, 2000, 600, &set);
    assert_eq!(sim.sends.len(), 3);
}

fn lines_every_5s() -> Lyrics {
    let words: Vec<String> = (0..12).map(|i| format!("l{i}")).collect();
    let v: Vec<(i64, &str)> = words.iter().enumerate().map(|(i, w)| (i as i64 * 5000, w.as_str())).collect();
    sheet(&v)
}

#[test]
fn a_single_seek_is_published_at_once() {
    let mut sim = Sim::new();
    let mut s = snap("spotify:track:a", lines_every_5s(), 60000);
    let set = Settings::default();
    let t = sim.play(0, &mut s, 0, 2000, &set);
    sim.at(t, &mut s, 31_000, &set);
    let (at, a) = sim.sends.last().unwrap();
    assert_eq!(*at, t);
    assert_eq!(a.as_ref().unwrap()["state"], "l6");
}

#[test]
fn a_seek_burst_publishes_only_where_it_ends() {
    let mut sim = Sim::new();
    let mut s = snap("spotify:track:a", lines_every_5s(), 60000);
    let set = Settings::default();
    let t = sim.play(0, &mut s, 0, 2000, &set);
    // dragging the bar: five seeks 300 ms apart
    let mut t = t;
    let mut last = 0;
    for pos in [31_000, 51_000, 11_000, 36_000, 21_000] {
        sim.at(t, &mut s, pos, &set);
        last = t;
        t += 300;
    }
    // ... and the bar is let go: playback goes on from there
    t = last + 50;
    let mut p = 21_050;
    while t < last + 1500 {
        sim.at(t, &mut s, p, &set);
        t += 50;
        p += 50;
    }
    assert_eq!(sim.states(), vec!["l0", "l6", "l4"], "the first seek goes out at once, the rest only the destination");
    let (at, _) = sim.sends.last().unwrap();
    assert!(*at >= last + 500 && *at <= last + 600, "destination sent at {at}, last seek at {last}");
    assert_eq!(sim.dropped, 0, "lines jumped over are not dropped lines");
    assert_eq!(sim.rl, 0, "and the hold is not a rate-limit wait");
}
