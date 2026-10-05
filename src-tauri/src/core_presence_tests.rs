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
    /// Paused at song position `pos` for `dur` ms, ticking every 50 ms.
    fn idle(&mut self, start_ms: u64, s: &mut Snapshot, pos: i64, dur: u64, set: &Settings) -> u64 {
        s.is_playing = false;
        let mut t = start_ms;
        while t < start_ms + dur {
            self.at(t, s, pos, set);
            t += 50;
        }
        t
    }
    /// The most frames (clears included) inside any 20 s window.
    fn max_per_20s(&self) -> usize {
        max_in_window(&self.sends.iter().map(|(t, _)| *t).collect::<Vec<_>>())
    }
}

/// The most of `times` (ms) inside any 20 s window.
fn max_in_window(times: &[u64]) -> usize {
    (0..times.len()).map(|i| times[i..].iter().take_while(|x| **x < times[i] + 20_000).count()).max().unwrap_or(0)
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
    // Paused: the profile stays up for the hold, then comes down once.
    let t1 = sim.idle(t, &mut s, 2000, 1400, &set);
    assert_eq!(sim.sends.len(), 1, "nothing inside the hold");
    sim.idle(t1, &mut s, 2000, 3000, &set);
    assert_eq!(sim.sends.len(), 2);
    assert!(sim.sends[1].1.is_none(), "cleared");
    assert!(sim.sends[1].0 >= t + 1500 && sim.sends[1].0 <= t + 1600, "cleared at {}, paused at {t}", sim.sends[1].0);

    let mut sim = Sim::new();
    let mut s = snap("spotify:track:a", sheet(&[(0, "first")]), 30000);
    let set = Settings { show_paused: true, ..Default::default() };
    let t = sim.play(0, &mut s, 0, 2000, &set);
    sim.idle(t, &mut s, 2000, 3000, &set);
    assert_eq!(sim.sends.len(), 2);
    let a = sim.sends[1].1.as_ref().unwrap();
    assert_eq!(a["state"], "\u{23f8} Paused");
    assert!(a.get("timestamps").is_none());
    assert!(sim.sends[1].0 >= t + 1500 && sim.sends[1].0 <= t + 1600);
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
    let resumed = sim.idle(t, &mut s, 2000, 10_000, &set);
    assert!(sim.sends[1].1.is_none(), "paused: cleared");
    // paused for a while, then the same track plays on
    s.is_playing = true;
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
fn pause_spam_sends_nothing() {
    let mut sim = Sim::new();
    let mut s = snap("spotify:track:a", sheet(&[(0, "first")]), 30000);
    let set = Settings::default();
    let mut t = sim.play(0, &mut s, 0, 2000, &set);
    assert_eq!(sim.sends.len(), 1);
    // play/pause toggled every 200 ms, five times: neither a clear nor a
    // republish, the profile stays as it is (the bar is 1 s behind, which is
    // within BAR_TOLERANCE)
    for _ in 0..5 {
        t = sim.idle(t, &mut s, 2000, 200, &set);
        s.is_playing = true;
        for _ in 0..4 {
            sim.at(t, &mut s, 2000, &set);
            t += 50;
        }
    }
    sim.play(t, &mut s, 2000, 5000, &set);
    assert_eq!(sim.sends.len(), 1, "{:?}", sim.sends.iter().map(|s| s.1.is_some()).collect::<Vec<_>>());
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

// ───────────── pausing, flapping and the rate ledger ─────────────

fn start_of(a: &Value) -> i64 {
    a["timestamps"]["start"].as_i64().unwrap()
}

fn many_lines(n: usize, every_ms: i64) -> Lyrics {
    let words: Vec<String> = (0..n).map(|i| format!("line {i}")).collect();
    let v: Vec<(i64, &str)> = words.iter().enumerate().map(|(i, w)| (i as i64 * every_ms, w.as_str())).collect();
    sheet(&v)
}

#[test]
fn a_quick_pause_and_resume_sends_nothing() {
    // Under PAUSE_HOLD the profile is not touched at all: no clear, no
    // republish, and the UI is not told the profile is empty.
    for pause_ms in [300u64, 1000, 1400] {
        let mut sim = Sim::new();
        let mut s = snap("spotify:track:a", sheet(&[(0, "first"), (30_000, "second")]), 60_000);
        let set = Settings::default();
        let t = sim.play(0, &mut s, 0, 3000, &set);
        assert_eq!(sim.sends.len(), 1);
        let t = sim.idle(t, &mut s, 3050, pause_ms, &set);
        s.is_playing = true;
        sim.play(t, &mut s, 3050, 6000, &set);
        assert_eq!(sim.sends.len(), 1, "a {pause_ms} ms pause sent {:?}", sim.sends);
        assert!(!sim.lines.contains(&String::new()), "a {pause_ms} ms pause told the UI the profile is empty");
    }
}

#[test]
fn short_pauses_that_add_up_put_the_progress_bar_right_once_play_is_steady() {
    let mut sim = Sim::new();
    let mut s = snap("spotify:track:a", sheet(&[(0, "first"), (60_000, "second")]), 120_000);
    let set = Settings::default();
    let mut t = sim.play(0, &mut s, 0, 3000, &set);
    let mut pos = 3050i64;
    let first_start = start_of(sim.sends[0].1.as_ref().unwrap());
    // four 1 s pauses with half a second of play between them
    let mut resumed = 0;
    for _ in 0..4 {
        t = sim.idle(t, &mut s, pos, 1000, &set);
        s.is_playing = true;
        resumed = t;
        t = sim.play(t, &mut s, pos, 450, &set);
        pos += 500;
    }
    assert_eq!(sim.sends.len(), 1, "toggling spends nothing: {:?}", sim.sends.iter().map(|s| s.0).collect::<Vec<_>>());
    // steady play: one frame, once, 2 s after the last resume, with the bar right
    sim.play(t, &mut s, pos, 4000, &set);
    assert_eq!(sim.sends.len(), 2);
    let (at, a) = &sim.sends[1];
    assert!(*at >= resumed + 2000 && *at <= resumed + 2100, "sent at {at}, resumed at {resumed}");
    let a = a.as_ref().unwrap();
    assert_eq!(a["state"], "first");
    assert_eq!(start_of(a) - first_start, 4000, "the bar moved by the four seconds the song stood still");
}

#[test]
fn a_song_that_kept_playing_through_a_blip_does_not_leave_the_bar_behind() {
    // The bridge connection blinks: the app counts it as a pause, but the
    // song went on, so on the way back its position has moved by the time
    // away and the progress bar on Discord is still right.
    let mut sim = Sim::new();
    let mut s = snap("spotify:track:a", sheet(&[(0, "first"), (60_000, "second")]), 120_000);
    let set = Settings::default();
    let mut t = sim.play(0, &mut s, 0, 3000, &set);
    let mut pos = 3050i64;
    for _ in 0..5 {
        t = sim.idle(t, &mut s, pos, 1000, &set);
        pos += 1000;
        s.is_playing = true;
        t = sim.play(t, &mut s, pos, 450, &set);
        pos += 500;
    }
    sim.play(t, &mut s, pos, 6000, &set);
    assert_eq!(sim.sends.len(), 1, "no frame to put the bar right: {:?}", sim.sends.iter().map(|s| s.0).collect::<Vec<_>>());
}

#[test]
fn a_pause_that_leaves_no_slot_for_the_resume_waits() {
    // 4 of 5 slots used: a clear would take the last one, and the resume
    // would find the profile blank, with music playing, and no slot to
    // put it back. So the clear waits until one frees.
    let mut sim = Sim::new();
    let mut s = snap("spotify:track:a", sheet(&[(0, "first")]), 60_000);
    let set = Settings::default();
    let t = sim.play(0, &mut s, 0, 2000, &set);
    assert_eq!(sim.sends.len(), 1);
    let first = sim.sends[0].0;
    for k in 0..3 {
        sim.p.record_external(sim.t0 + Duration::from_millis(t + k));
    }
    let t = sim.idle(t, &mut s, 2050, 17_000, &set);
    assert_eq!(sim.sends.len(), 1, "no clear while only one slot is free");
    let t = sim.idle(t, &mut s, 2050, 6000, &set);
    assert_eq!(sim.sends.len(), 2);
    let (at, a) = &sim.sends[1];
    assert!(a.is_none());
    assert!(*at >= first + 20_000 && *at <= first + 20_100, "cleared at {at}, the frame that held the slot was sent at {first}");
    // ... and now the resume has its slot at once.
    s.is_playing = true;
    sim.play(t, &mut s, 2050, 1000, &set);
    let (at, a) = sim.sends.last().unwrap();
    assert!(a.is_some() && *at <= t + 350, "republished at {at}, resumed at {t}");
}

#[test]
fn a_pause_with_slots_to_spare_clears_after_the_hold() {
    let mut sim = Sim::new();
    let mut s = snap("spotify:track:a", sheet(&[(0, "first")]), 60_000);
    let set = Settings::default();
    let t = sim.play(0, &mut s, 0, 2000, &set);
    sim.p.record_external(sim.t0 + Duration::from_millis(t));
    sim.p.record_external(sim.t0 + Duration::from_millis(t + 1));
    // 3 of 5 used: the clear leaves one for the resume
    sim.idle(t, &mut s, 2050, 2000, &set);
    assert_eq!(sim.sends.len(), 2);
    assert!(sim.sends[1].1.is_none());
}

#[test]
fn a_pause_clear_that_is_still_waiting_for_a_slot_is_dropped_when_playback_resumes() {
    // The ledger is full when the pause passes the hold: the clear is owed.
    // Playback resumes before a slot frees; the clear must not fire later over
    // the frame that replaces the profile.
    let mut sim = Sim::new();
    let mut s = snap("spotify:track:a", sheet(&[(0, "first"), (30_000, "second")]), 60_000);
    let set = Settings::default();
    let t = sim.play(0, &mut s, 0, 2000, &set);
    let first = sim.sends[0].0;
    for k in 0..4 {
        sim.p.record_external(sim.t0 + Duration::from_millis(t + k));
    }
    let t = sim.idle(t, &mut s, 2050, 4000, &set);
    assert_eq!(sim.sends.len(), 1, "nothing goes out while the ledger is full");
    s.is_playing = true;
    sim.play(t, &mut s, 2050, 25_000, &set);
    let sent: Vec<(u64, bool)> = sim.sends.iter().map(|s| (s.0, s.1.is_some())).collect();
    assert!(sim.sends.iter().all(|(_, a)| a.is_some()), "no clear: {sent:?}");
    assert_eq!(sim.sends.len(), 2, "one republish: {sent:?}");
    assert!(sim.sends[1].0 >= first + 20_000 && sim.sends[1].0 <= first + 20_100, "{sent:?}");
}

#[test]
fn a_clear_that_waited_is_not_revived_by_the_next_quick_pause() {
    // A pause passes the hold with one slot left (a clear needs two, so it is
    // owed), playback resumes, and just before the republish a second, quick
    // pause begins: the slot that frees during it must not send the first
    // pause's clear, which would take the profile down inside the hold.
    let mut sim = Sim::new();
    let mut s = snap("spotify:track:a", sheet(&[(0, "first"), (60_000, "second")]), 120_000);
    let set = Settings::default();
    let t = sim.play(0, &mut s, 0, 2000, &set);
    let first = sim.sends[0].0;
    for k in 0..3 {
        sim.p.record_external(sim.t0 + Duration::from_millis(t + k));
    }
    let t = sim.idle(t, &mut s, 2050, 17_700, &set);
    assert_eq!(sim.sends.len(), 1, "one slot left: the clear waits for two");
    s.is_playing = true;
    let t = sim.play(t, &mut s, 2050, 100, &set);
    // the slot frees at first + 20 s, inside this 1 s pause (the hold is 1.5 s)
    assert!(t < first + 20_000 && t + 1000 > first + 20_000, "the test needs the slot to free during the pause: {t}");
    let t = sim.idle(t, &mut s, 2200, 1000, &set);
    s.is_playing = true;
    sim.play(t, &mut s, 2200, 4000, &set);
    let sent: Vec<(u64, bool)> = sim.sends.iter().map(|s| (s.0, s.1.is_some())).collect();
    assert!(sim.sends.iter().all(|(_, a)| a.is_some()), "no clear in the quick pause: {sent:?}");
}

#[test]
fn every_clear_waits_for_a_slot_not_only_the_pause_one() {
    // The RPC switch and a blacklisted track clear too, and a frame over the
    // limit is one Discord drops.
    for why in ["switch", "blacklist"] {
        let mut sim = Sim::new();
        let mut s = snap("spotify:track:a", sheet(&[(0, "first")]), 60_000);
        let on = Settings::default();
        let t = sim.play(0, &mut s, 0, 2000, &on);
        let first = sim.sends[0].0;
        for k in 0..4 {
            sim.p.record_external(sim.t0 + Duration::from_millis(t + k));
        }
        if why == "switch" {
            let off = Settings { enabled: false, ..Default::default() };
            sim.play(t, &mut s, 2000, 25_000, &off);
        } else {
            let mut b = snap("spotify:track:b", sheet(&[(0, "secret")]), 60_000);
            b.track.as_mut().unwrap().blacklisted = true;
            sim.play(t, &mut b, 0, 25_000, &on);
        }
        assert_eq!(sim.sends.len(), 2, "{why}: {:?}", sim.sends.iter().map(|s| s.0).collect::<Vec<_>>());
        let (at, a) = &sim.sends[1];
        assert!(a.is_none());
        assert!(*at >= first + 20_000 && *at <= first + 20_100, "{why}: cleared at {at}, held slot sent at {first}");
    }
}

#[test]
fn a_clear_that_waits_is_dropped_when_the_next_frame_replaces_the_profile() {
    // The switch went off with a full ledger, then on again before the clear
    // found a slot: the clear must not fire later over the republished frame.
    let mut sim = Sim::new();
    let mut s = snap("spotify:track:a", sheet(&[(0, "first"), (40_000, "second")]), 60_000);
    let on = Settings::default();
    let off = Settings { enabled: false, ..Default::default() };
    let t = sim.play(0, &mut s, 0, 2000, &on);
    for k in 0..4 {
        sim.p.record_external(sim.t0 + Duration::from_millis(t + k));
    }
    let t = sim.play(t, &mut s, 2000, 1000, &off);
    // back on: the profile is replaced by the republished frame once a slot
    // is there (the ledger is full for another 17 s)
    sim.play(t, &mut s, 3050, 30_000, &on);
    let seen: Vec<(u64, bool)> = sim.sends.iter().map(|s| (s.0, s.1.is_some())).collect();
    assert!(sim.sends.iter().all(|(_, a)| a.is_some()), "{seen:?}");
    assert!(sim.sends.len() >= 2, "republished: {seen:?}");
}

#[test]
fn a_clear_owed_by_the_switch_is_still_sent_when_it_goes_back_on_while_paused() {
    // The switch goes off with a full ledger (the clear waits for a slot) and
    // back on while playback is paused: no frame is coming that would replace
    // the old one, so the clear must still go out. (Found by the randomised
    // run below.)
    let mut sim = Sim::new();
    let mut s = snap("spotify:track:a", sheet(&[(0, "first"), (40_000, "second")]), 60_000);
    let on = Settings::default();
    let off = Settings { enabled: false, ..Default::default() };
    let t = sim.play(0, &mut s, 0, 2000, &on);
    let first = sim.sends[0].0;
    for k in 0..4 {
        sim.p.record_external(sim.t0 + Duration::from_millis(t + k));
    }
    let t = sim.play(t, &mut s, 2000, 500, &off);
    let t = sim.idle(t, &mut s, 2550, 1000, &off);
    sim.idle(t, &mut s, 2550, 25_000, &on);
    assert_eq!(sim.sends.len(), 2, "{:?}", sim.sends.iter().map(|s| (s.0, s.1.is_some())).collect::<Vec<_>>());
    let (at, a) = &sim.sends[1];
    assert!(a.is_none());
    assert!(*at >= first + 20_000 && *at <= first + 20_100, "cleared at {at}, the frame that held the slot was sent at {first}");
}

#[test]
fn a_disconnect_keeps_the_rate_ledger() {
    // Discord hangs up right after every handshake and the client dials
    // again after 250 ms, 500 ms, 1 s, 2 s, 4 s, 5 s ...: the connection
    // lives long enough for the 0.4 s settle, so each cycle would send a
    // frame if the ledger started over every time.
    let mut sim = Sim::new();
    let mut s = snap("spotify:track:a", many_lines(100, 5000), 600_000);
    let set = Settings::default();
    let (mut t, mut gap) = (0u64, 250u64);
    for _ in 0..16 {
        s.discord_user = Some("kurepa".into());
        let up_until = t + 600;
        while t < up_until {
            sim.at(t, &mut s, t as i64, &set);
            t += 50;
        }
        s.discord_user = None;
        let down_until = t + gap;
        while t < down_until {
            sim.at(t, &mut s, t as i64, &set);
            t += 50;
        }
        gap = (gap * 2).min(5000);
    }
    let times: Vec<u64> = sim.sends.iter().map(|s| s.0).collect();
    assert!(times.len() >= 5, "the test is not vacuous: {times:?}");
    assert!(sim.max_per_20s() <= 5, "frames at {times:?}");
}

#[test]
fn a_reconnect_starts_the_screen_over_but_not_the_ledger() {
    let mut sim = Sim::new();
    let mut s = snap("spotify:track:a", sheet(&[(0, "first")]), 60_000);
    let set = Settings::default();
    let t = sim.play(0, &mut s, 0, 2000, &set);
    assert_eq!(sim.sends.len(), 1);
    s.discord_user = None;
    let t = sim.play(t, &mut s, 2050, 300, &set);
    s.discord_user = Some("kurepa".into());
    sim.play(t, &mut s, 2400, 3000, &set);
    // republished after the reconnect, and counted with the first frame
    assert_eq!(sim.states(), vec!["first", "first"]);
    assert_eq!(sim.p.rl.len(), 2);
}

#[test]
fn resuming_without_known_lyrics_waits_the_long_settle() {
    let mut sim = Sim::new();
    let mut s = snap("spotify:track:a", Lyrics::none(), 30_000);
    let set = Settings::default();
    let t = sim.play(0, &mut s, 0, 2000, &set);
    assert_eq!(sim.states(), vec!["— "], "title only");
    let resumed = sim.idle(t, &mut s, 2050, 10_000, &set);
    s.is_playing = true;
    sim.play(resumed, &mut s, 2050, 3000, &set);
    let (at, a) = sim.sends.last().unwrap();
    assert!(a.is_some());
    assert!(*at >= resumed + 1500 && *at <= resumed + 1600, "sent at {at}, resumed at {resumed}");
}

#[test]
fn a_blacklisted_track_in_between_makes_the_return_a_new_track_not_a_resume() {
    let mut sim = Sim::new();
    let l = sheet(&[(0, "first"), (6000, "second")]);
    let mut a = snap("spotify:track:a", l.clone(), 30_000);
    let set = Settings::default();
    let t = sim.play(0, &mut a, 0, 2000, &set);
    let t = sim.idle(t, &mut a, 2050, 3000, &set);
    let mut b = snap("spotify:track:b", sheet(&[(0, "secret")]), 30_000);
    b.track.as_mut().unwrap().blacklisted = true;
    let t = sim.play(t, &mut b, 0, 1000, &set);
    // back to the track that was paused: another track played in between
    let began = t;
    a.is_playing = true;
    sim.play(began, &mut a, 0, 2000, &set);
    let (at, v) = sim.sends.last().unwrap();
    assert!(v.is_some());
    assert!(*at >= began + 400, "sent at {at}, track began {began}: the 0.3 s resume wait is for the very track that was paused");
}

#[test]
fn the_paused_indicator_never_names_a_blacklisted_track() {
    let mut sim = Sim::new();
    let set = Settings { show_paused: true, ..Default::default() };
    let mut b = snap("spotify:track:b", sheet(&[(0, "secret")]), 30_000);
    b.track.as_mut().unwrap().blacklisted = true;
    let t = sim.play(0, &mut b, 0, 2000, &set);
    sim.idle(t, &mut b, 2050, 4000, &set);
    assert!(sim.sends.is_empty(), "{:?}", sim.sends);
}

#[test]
fn a_loop_that_was_suspended_through_a_pause_treats_it_as_a_long_one() {
    // No tick between the pause and the resume (the machine slept): the
    // profile still shows the old frame; the republish replaces it after the
    // resume wait, with the bar right.
    let mut sim = Sim::new();
    let mut s = snap("spotify:track:a", sheet(&[(0, "first"), (60_000, "second")]), 120_000);
    let set = Settings::default();
    let t = sim.play(0, &mut s, 0, 2000, &set);
    s.is_playing = false;
    sim.at(t, &mut s, 2050, &set);
    s.is_playing = true;
    let resumed = t + 30_000;
    sim.play(resumed, &mut s, 2050, 1000, &set);
    assert_eq!(sim.sends.len(), 2);
    let (at, a) = &sim.sends[1];
    assert!(*at >= resumed + 300 && *at <= resumed + 350, "sent at {at}, resumed at {resumed}");
    assert_eq!(start_of(a.as_ref().unwrap()) - start_of(sim.sends[0].1.as_ref().unwrap()), 30_000);
}

/// What one flapping run did.
struct FlapRun {
    /// When each SET_ACTIVITY went out, clears included.
    frames: Vec<u64>,
    clears: usize,
    /// Time spent playing, more than 650 ms after the last resume, with
    /// nothing on the profile.
    blank_playing_ms: u64,
    /// After the last change of state, how long until the profile was right
    /// (playing: a frame whose bar is within 2 s; paused: cleared).
    correct_after_ms: Option<u64>,
}

const FLAP_FOR: u64 = 60_000;

/// Toggle pause/play every `cadence` ms for a minute (starting `from` ms in),
/// end in the given state, and watch the profile. `reserve`: the slots a
/// pause leaves free; None is the app as shipped (PAUSE_RESERVE).
fn flap_run_with(reserve: Option<usize>, from: u64, cadence: u64, end_playing: bool, lyrics: &Lyrics, set: &Settings) -> FlapRun {
    let mut sim = Sim::new();
    if let Some(r) = reserve {
        sim.p.pause_reserve = r;
    }
    let mut s = snap("spotify:track:a", lyrics.clone(), 600_000);
    let end = from + FLAP_FOR;
    let (mut t, mut pos) = (0u64, 0i64);
    let (mut playing, mut up, mut last_start) = (true, false, None::<i64>);
    let (mut resumed_at, mut last_change, mut next_toggle) = (0u64, 0u64, from);
    let (mut blank, mut right_since) = (0u64, None::<u64>);
    let (mut frames, mut clears) = (vec![], 0);
    while t < end + 40_000 {
        let before = playing;
        if t == end {
            playing = end_playing;
        } else if t >= next_toggle && t < end {
            playing = !playing;
            next_toggle += cadence;
        }
        if playing != before {
            last_change = t;
            if playing {
                resumed_at = t;
            }
        }
        s.is_playing = playing;
        let n = sim.sends.len();
        sim.at(t, &mut s, pos, set);
        for (_, v) in &sim.sends[n..] {
            frames.push(t);
            match v {
                Some(a) => {
                    up = true;
                    last_start = a["timestamps"]["start"].as_i64();
                }
                None => {
                    up = false;
                    clears += 1;
                }
            }
        }
        if playing && !up && t >= from && t > resumed_at + 650 {
            blank += 50;
        }
        // Right: playing with a frame whose bar is within 2 s, or paused and
        // cleared. `right_since` is when it last became so and stayed so.
        let truth = BASE_MS + t as i64 - pos;
        let right = if playing { up && last_start.is_some_and(|st| (st - truth).abs() <= 2000) } else { !up };
        match (right, right_since) {
            (true, None) => right_since = Some(t),
            (false, _) => right_since = None,
            _ => {}
        }
        t += 50;
        if playing {
            pos += 50;
        }
    }
    FlapRun { frames, clears, blank_playing_ms: blank, correct_after_ms: right_since.map(|r| r.saturating_sub(last_change)) }
}

/// The slowest a profile may take to end right after the last change: a
/// clear that has to leave a slot waits for two frames of the window to
/// expire, so at worst the window and the hold.
const FLAP_BOUND_MS: u64 = 20_000 + 1_500;

/// The worst of a cadence/ending over several phases of the song.
#[derive(Default)]
struct FlapRow {
    cadence: u64,
    end_playing: bool,
    max_frames_total: usize,
    max_clears: usize,
    most_in_20s: usize,
    blank_playing_ms: u64,
    right_after_ms: u64,
    never_right: bool,
}

/// A song with a new line every 4 s, so lyric frames compete with the pause
/// frames for the slots; each case runs at several phases of the song (the
/// ledger is in a different state when the last change comes).
fn flap_sweep(reserve: Option<usize>, cadences: &[u64]) -> Vec<FlapRow> {
    let l = many_lines(150, 4000);
    let set = Settings::default();
    let mut rows = vec![];
    for &cadence in cadences {
        for end_playing in [true, false] {
            let mut row = FlapRow { cadence, end_playing, ..Default::default() };
            for from in [3000u64, 4300, 6100, 8900, 12_700] {
                let r = flap_run_with(reserve, from, cadence, end_playing, &l, &set);
                row.max_frames_total = row.max_frames_total.max(r.frames.len());
                row.max_clears = row.max_clears.max(r.clears);
                row.most_in_20s = row.most_in_20s.max(max_in_window(&r.frames));
                row.blank_playing_ms = row.blank_playing_ms.max(r.blank_playing_ms);
                match r.correct_after_ms {
                    Some(a) => row.right_after_ms = row.right_after_ms.max(a),
                    None => row.never_right = true,
                }
            }
            rows.push(row);
        }
    }
    rows
}

fn print_row(r: &FlapRow, reserve: usize) {
    eprintln!(
        "reserve {reserve}  cadence {:4} ms, ends {:7}: frames <= {:2} (clears <= {})  most in 20 s {}  blank while playing {:5} ms  right after the last change <= {}",
        r.cadence,
        if r.end_playing { "playing" } else { "paused" },
        r.max_frames_total,
        r.max_clears,
        r.most_in_20s,
        r.blank_playing_ms,
        if r.never_right { "NEVER".to_string() } else { format!("{} ms", r.right_after_ms) },
    );
}

const FLAP_CADENCES: [u64; 7] = [300, 500, 1000, 1500, 1600, 2000, 3000];

#[test]
fn pause_resume_flapping_stays_inside_the_budget_and_ends_right() {
    // (the app as shipped: whatever Presence::new gives)
    for r in flap_sweep(None, &FLAP_CADENCES) {
        print_row(&r, PAUSE_RESERVE);
        let name = format!("cadence {} ms, ends {}", r.cadence, if r.end_playing { "playing" } else { "paused" });
        assert!(r.most_in_20s <= RATE_CALLS, "{name}: more than 5 frames in 20 s");
        assert_eq!(r.blank_playing_ms, 0, "{name}: the profile was blank while music played");
        assert!(!r.never_right, "{name}: never ended right");
        assert!(r.right_after_ms <= FLAP_BOUND_MS, "{name}: took {} ms to end right", r.right_after_ms);
    }
}

#[test]
fn without_the_reserve_a_flapping_pause_leaves_the_profile_blank_while_music_plays() {
    // The reserve is what makes the test above pass: a clear that takes the
    // last slot leaves the resume without one. (The budget itself holds either
    // way: every frame is counted.)
    let rows = flap_sweep(Some(0), &[1600, 2000, 3000]);
    for r in &rows {
        print_row(r, 0);
        assert!(r.most_in_20s <= RATE_CALLS, "{}: the budget holds without the reserve too", r.cadence);
    }
    assert!(
        rows.iter().any(|r| r.blank_playing_ms >= 1000),
        "taking the reserve away must show up as blank profile time, or the test above proves nothing"
    );
}

/// `cargo test --release --lib report_flapping -- --ignored --nocapture`
#[test]
#[ignore = "report"]
fn report_flapping() {
    for reserve in [PAUSE_RESERVE, 0] {
        for r in flap_sweep(Some(reserve), &FLAP_CADENCES) {
            print_row(&r, reserve);
        }
    }
}

/// How long after a pause the profile comes down, for songs that keep the
/// ledger busy to a different degree: the price of the hold and the reserve.
/// `cargo test --release --lib report_pause_to_clear -- --ignored --nocapture`
#[test]
#[ignore = "report"]
fn report_pause_to_clear() {
    let set = Settings::default();
    for reserve in [PAUSE_RESERVE, 0] {
        for every in [1500i64, 2500, 4000, 8000, 20_000] {
            let l = many_lines(300, every);
            let mut waits = vec![];
            // pause at 60 different phases of the song, after a minute of play
            for k in 0..60u64 {
                let mut sim = Sim::new();
                sim.p.pause_reserve = reserve;
                let mut s = snap("spotify:track:a", l.clone(), 600_000);
                let pause_at = 60_000 + k * 337;
                let t = sim.play(0, &mut s, 0, pause_at, &set);
                let n = sim.sends.len();
                sim.idle(t, &mut s, t as i64, 30_000, &set);
                if let Some((at, _)) = sim.sends[n..].iter().find(|(_, v)| v.is_none()) {
                    waits.push(at - t);
                } else {
                    waits.push(99_999);
                }
            }
            waits.sort();
            eprintln!(
                "reserve {reserve}  a line every {every:5} ms: pause -> clear  p50 {} ms  p95 {} ms  max {} ms",
                waits[waits.len() / 2],
                waits[waits.len() * 95 / 100],
                waits[waits.len() - 1]
            );
        }
    }
}

// ───────────── randomised stress: everything at once ─────────────

/// xorshift64: the same seed gives the same run, a failure names its seed.
struct Rng(u64);

impl Rng {
    fn new(seed: u64) -> Self {
        Rng(seed.wrapping_mul(0x9E37_79B9_7F4A_7C15) | 1)
    }
    fn next(&mut self) -> u64 {
        let mut x = self.0;
        x ^= x << 13;
        x ^= x >> 7;
        x ^= x << 17;
        self.0 = x;
        x
    }
    fn below(&mut self, n: u64) -> u64 {
        self.next() % n
    }
}

/// Song `n`: a title of its own, and lyrics of three kinds (a line every 4 s,
/// which keeps the whole budget busy; a sparse one; none at all).
fn stress_track(n: u64) -> (Snapshot, Lyrics) {
    let lyrics = match n % 3 {
        0 => many_lines(140, 4000),
        1 => many_lines(60, 9000),
        _ => Lyrics::none(),
    };
    let mut s = snap(&format!("spotify:track:s{n}"), lyrics.clone(), 600_000);
    s.track.as_mut().unwrap().title = format!("Song {n}");
    (s, lyrics)
}

/// One run: 2 minutes of random pausing, resuming, skipping, seeking, Discord
/// hang-ups, RPC switching and blacklisting at a random pace, then a final
/// state held for 40 s. Whatever happened, no 20 s window may hold more than
/// 5 frames, and in the end the profile must be right.
fn stress_run(seed: u64) {
    let mut rng = Rng::new(seed);
    let mut sim = Sim::new();
    let mut set = Settings { show_paused: rng.below(4) == 0, ..Default::default() };
    let pace = [250u64, 700, 1500, 3000, 6000][rng.below(5) as usize];
    let mut n = 0u64;
    let (mut s, _) = stress_track(n);
    let mut pos = 0i64;
    let mut late: Option<(u64, Lyrics)> = None;
    let mut reconnect_at: Option<u64> = None;
    let mut last_disconnect = 0u64;
    // A seek that lands in the line already shown (or in a song without
    // lyrics) sends no frame, so the bar stays where it was: that is how
    // master behaves too, and not what this test is about.
    let mut last_seek = 0u64;
    let mut log: Vec<String> = vec![];
    let active_end = 120_000u64;
    let mut next_event = 200 + rng.below(2000);
    let (mut t, mut last_pos_at) = (0u64, (0u64, 0i64));
    while t < active_end + 40_000 {
        if t == active_end {
            // the final state: connected, switched on, not blacklisted
            reconnect_at = None;
            s.discord_user = Some("kurepa".into());
            set.enabled = true;
            s.track.as_mut().unwrap().blacklisted = false;
            s.is_playing = rng.below(2) == 0;
            log.push(format!("{t}: final, playing {}", s.is_playing));
        } else if t >= next_event && t < active_end {
            let ev = rng.below(100);
            if ev < 38 {
                s.is_playing = !s.is_playing;
                log.push(format!("{t}: playing {}", s.is_playing));
            } else if ev < 52 {
                n += 1;
                let (mut fresh, lyrics) = stress_track(n);
                fresh.is_playing = s.is_playing;
                fresh.discord_user = s.discord_user.clone();
                if n % 3 == 0 && rng.below(10) < 4 {
                    // the bridge's answer comes in a moment
                    fresh.lyrics = Lyrics::none();
                    late = Some((t + 200 + rng.below(2800), lyrics));
                } else {
                    late = None;
                }
                s = fresh;
                pos = 0;
                log.push(format!("{t}: track {n}"));
            } else if ev < 64 {
                pos = rng.below(500_000) as i64;
                last_seek = t;
                log.push(format!("{t}: seek to {pos}"));
            } else if ev < 78 {
                if s.discord_user.is_some() {
                    s.discord_user = None;
                    last_disconnect = t;
                    reconnect_at = Some(t + 100 + rng.below(6000));
                    log.push(format!("{t}: discord away"));
                }
            } else if ev < 88 {
                set.enabled = !set.enabled;
                log.push(format!("{t}: rpc enabled {}", set.enabled));
            } else {
                // (show_paused is fixed for a run: flipping it while paused
                // does not rewrite what is already on the profile)
                let tr = s.track.as_mut().unwrap();
                tr.blacklisted = !tr.blacklisted;
                log.push(format!("{t}: blacklisted {}", tr.blacklisted));
            }
            next_event = t + 50 + rng.below(2 * pace);
        }
        if reconnect_at.is_some_and(|r| t >= r) {
            reconnect_at = None;
            s.discord_user = Some("kurepa".into());
        }
        if late.as_ref().is_some_and(|(at, _)| t >= *at) {
            s.lyrics = late.take().unwrap().1;
        }
        sim.at(t, &mut s, pos, &set);
        last_pos_at = (t, pos);
        t += 50;
        if s.is_playing {
            pos += 50;
        }
        if pos > 590_000 {
            pos = 0;
        }
    }
    let what: Vec<String> = sim.sends.iter().map(|(t, v)| format!("{t}:{}", v.as_ref().map_or("clear".to_string(), |a| format!("{}|{}", a["details"].as_str().unwrap_or(""), a["state"].as_str().unwrap_or(""))))).collect();
    let ctx = || format!("seed {seed}, pace {pace}, show_paused {}, sends {what:?}, events: {log:?}", set.show_paused);
    assert!(sim.max_per_20s() <= RATE_CALLS, "more than 5 frames in 20 s: {}", ctx());
    // What is on the profile now: the last frame sent since Discord came back.
    let shown = sim.sends.iter().rev().take_while(|(at, _)| *at >= last_disconnect).map(|(_, v)| v).next().cloned();
    let title = s.track.as_ref().unwrap().title.clone();
    if s.is_playing {
        let a = shown.clone().flatten().unwrap_or_else(|| panic!("playing but the profile is empty: {}", ctx()));
        assert!(a["details"].as_str().unwrap().starts_with(&title), "{title} is playing, the profile says {}: {}", a["details"], ctx());
        let truth = BASE_MS + last_pos_at.0 as i64 - last_pos_at.1;
        let sent_at = sim.sends.last().unwrap().0;
        if sent_at > last_seek {
            assert!((start_of(&a) - truth).abs() <= 2000, "the bar is {} ms off: {}", start_of(&a) - truth, ctx());
        }
    } else {
        // (nothing is fine: a Discord that came back while paused has no
        // pause to show; what must not be there is a playing frame)
        if let Some(Some(a)) = shown {
            assert!(set.show_paused && a["state"] == PAUSED_TEXT, "paused but the profile still shows {}: {}", a["state"], ctx());
        }
    }
}

#[test]
fn random_pausing_skipping_seeking_hangups_and_switching_never_break_the_budget_and_end_right() {
    // STATUSIFY_STRESS_SEEDS=5000 for a longer hunt; a failure names its seed.
    let seeds = std::env::var("STATUSIFY_STRESS_SEEDS").ok().and_then(|v| v.parse().ok()).unwrap_or(150u64);
    let first = std::env::var("STATUSIFY_STRESS_FIRST").ok().and_then(|v| v.parse().ok()).unwrap_or(1u64);
    for seed in first..first + seeds {
        stress_run(seed);
    }
}
