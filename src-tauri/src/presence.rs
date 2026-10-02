//! What the Discord profile shows, and a 5-per-20-s send budget.
//!
//! Skeleton: greedy (publish the sung line as soon as the budget allows).
//! The Python app's look-ahead planner (statusify_presence_plan.py) is a
//! later port.

use crate::lyrics::{current_index, join_lines};
use crate::state::Snapshot;
use serde_json::{json, Value};
use std::collections::VecDeque;
use std::time::{Duration, Instant};

pub const RATE_CALLS: usize = 5;
pub const RATE_WINDOW: Duration = Duration::from_secs(20);
const MAX_STATE: usize = 128;

pub fn track_url(uri: &str) -> Option<String> {
    uri.strip_prefix("spotify:track:").map(|id| format!("https://open.spotify.com/track/{id}"))
}

fn cut(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

/// The SET_ACTIVITY activity: type 2 (Listening) so it sits beside a game
/// instead of fighting it for the Playing slot.
pub fn build_activity(s: &Snapshot, lines: &[&str], now_ms: i64) -> Value {
    let t = s.track.as_ref().expect("track");
    let label = cut(&format!("{} — {}", t.title, t.artist), 128);
    // Discord rejects text fields shorter than 2 characters.
    let hover = [t.album.as_str(), t.artist.as_str(), label.as_str()]
        .into_iter()
        .find(|x| x.trim().chars().count() >= 2)
        .unwrap_or(&label)
        .to_string();
    let mut act = json!({
        "type": 2,
        "details": label,
        "status_display_type": 2,
        "assets": {"large_image": if t.album_art.is_empty() { "spotify" } else { &t.album_art },
                   "large_text": cut(&hover, 128)},
    });
    if let Some(url) = track_url(&t.uri) {
        act["details_url"] = json!(url);
        act["assets"]["large_url"] = json!(url);
        act["buttons"] = json!([{"label": "Listen on Spotify", "url": url}]);
    }
    let f: Vec<&str> = lines.iter().copied().filter(|l| !l.is_empty()).collect();
    act["state"] = json!(if f.is_empty() { "— ".to_string() } else { cut(&join_lines(&f), MAX_STATE) });
    if s.duration_ms > 0 {
        let start = now_ms - s.estimated_position(now_ms);
        act["timestamps"] = json!({"start": start, "end": start + s.duration_ms});
    }
    act
}

/// What the profile should say right now, as a comparable key + lines.
/// None = clear the presence.
pub fn desired(s: &Snapshot, now_ms: i64) -> Option<(String, Vec<String>)> {
    let t = s.track.as_ref()?;
    if !s.is_playing || t.blacklisted {
        return None;
    }
    if s.lyrics.mode == "synced" {
        let pos = s.estimated_position(now_ms);
        if let Some(i) = current_index(&s.lyrics.synced, pos) {
            let w = s.lyrics.synced[i].words.trim();
            if !w.is_empty() {
                return Some((format!("{}#{i}", t.uri), vec![w.to_string()]));
            }
            // A blank line: keep whatever is showing (handled by caller).
            return Some((String::from("\u{0}blank"), vec![]));
        }
    }
    Some((format!("{}#title", t.uri), vec![]))
}

pub struct Budget {
    sent: VecDeque<Instant>,
}

impl Budget {
    pub fn new() -> Self {
        Budget { sent: VecDeque::new() }
    }
    pub fn try_take(&mut self, now: Instant) -> bool {
        while self.sent.front().is_some_and(|t| now.duration_since(*t) >= RATE_WINDOW) {
            self.sent.pop_front();
        }
        if self.sent.len() < RATE_CALLS {
            self.sent.push_back(now);
            true
        } else {
            false
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::lyrics::{parse_lrc, Lyrics};
    use crate::state::Track;

    fn snap() -> Snapshot {
        let mut s = Snapshot::default();
        s.track = Some(Track {
            uri: "spotify:track:abc".into(),
            artist: "Lil Peep".into(),
            title: "GODS".into(),
            album: "X".into(),
            album_art: "https://i.scdn.co/image/1".into(),
            blacklisted: false,
        });
        s.duration_ms = 200_000;
        s.position_ms = 1_500;
        s.position_at_ms = 10_000;
        s.is_playing = true;
        s.lyrics = Lyrics {
            mode: "synced".into(),
            synced: parse_lrc("[00:01.00]first\n[00:03.00]\n[00:05.00]second"),
            plain: vec![],
            source: "Spicy".into(),
        };
        s
    }

    #[test]
    fn activity_matches_python_shape() {
        let s = snap();
        let a = build_activity(&s, &["first"], 10_000);
        assert_eq!(a["type"], 2);
        assert_eq!(a["details"], "GODS — Lil Peep");
        assert_eq!(a["state"], "first");
        // one-char album falls back to the artist for the hover text
        assert_eq!(a["assets"]["large_text"], "Lil Peep");
        assert_eq!(a["details_url"], "https://open.spotify.com/track/abc");
        assert_eq!(a["timestamps"]["start"], 10_000 - 1_500);
        assert_eq!(build_activity(&s, &[], 10_000)["state"], "— ");
    }

    #[test]
    fn desired_follows_position_and_pause() {
        let mut s = snap();
        assert_eq!(desired(&s, 10_000).unwrap().1, vec!["first"]);
        assert_eq!(desired(&s, 12_000).unwrap().0, "\u{0}blank"); // 3.5 s: blank line
        assert_eq!(desired(&s, 14_000).unwrap().1, vec!["second"]);
        s.is_playing = false;
        assert!(desired(&s, 14_000).is_none());
        s.is_playing = true;
        s.lyrics = Lyrics::none();
        assert_eq!(desired(&s, 14_000).unwrap().0, "spotify:track:abc#title");
    }

    #[test]
    fn budget_is_five_per_twenty_seconds() {
        let mut b = Budget::new();
        let t0 = Instant::now();
        for _ in 0..5 {
            assert!(b.try_take(t0));
        }
        assert!(!b.try_take(t0 + Duration::from_secs(19)));
        assert!(b.try_take(t0 + Duration::from_secs(20)));
    }
}
