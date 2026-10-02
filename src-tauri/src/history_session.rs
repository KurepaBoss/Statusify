//! "Songs this session" and "listened this session" for the Stats overview.
//!
//! Counted from the raw bridge messages with the same rule the engine uses
//! for a play: forward progress while playing, ignoring seeks and stalls; a
//! track counts as a song once 20 s of it were heard. Independent of the
//! history database, so it works with "Remember history" off too.

use serde_json::Value;

const COMMIT_MS: i64 = 20_000;

#[derive(Default, Debug)]
pub struct Session {
    pub songs: i64,
    pub listened_ms: i64,
    current_ms: i64,
    committed: bool,
    last_pos: Option<i64>,
}

impl Session {
    pub fn on_bridge(&mut self, m: &Value) {
        match m.get("type").and_then(|t| t.as_str()).unwrap_or("") {
            "track_change" => {
                self.current_ms = 0;
                self.committed = false;
                self.last_pos = None;
            }
            "position" => {
                let pos = m.get("position_ms").and_then(|v| v.as_f64()).unwrap_or(0.0) as i64;
                let playing = m.get("is_playing").and_then(|v| v.as_bool()).unwrap_or(true);
                if let Some(prev) = self.last_pos {
                    let d = pos - prev;
                    if playing && (0..=5_000).contains(&d) {
                        self.listened_ms += d;
                        self.current_ms += d;
                    }
                }
                self.last_pos = Some(pos);
                if !self.committed && self.current_ms >= COMMIT_MS {
                    self.committed = true;
                    self.songs += 1;
                }
            }
            _ => {}
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn pos(s: &mut Session, ms: i64, playing: bool) {
        s.on_bridge(&json!({"type": "position", "position_ms": ms, "is_playing": playing}));
    }

    #[test]
    fn a_song_counts_after_20_seconds_of_listening() {
        let mut s = Session::default();
        s.on_bridge(&json!({"type": "track_change", "track_uri": "a"}));
        for t in 0..=19 {
            pos(&mut s, t * 1000, true);
        }
        assert_eq!((s.songs, s.listened_ms), (0, 19_000));
        pos(&mut s, 20_000, true);
        pos(&mut s, 21_000, true);
        assert_eq!((s.songs, s.listened_ms), (1, 21_000));
        s.on_bridge(&json!({"type": "track_change", "track_uri": "b"}));
        pos(&mut s, 0, true);
        pos(&mut s, 1000, true);
        assert_eq!(s.songs, 1);
    }

    #[test]
    fn seeks_pauses_and_stalls_do_not_count() {
        let mut s = Session::default();
        s.on_bridge(&json!({"type": "track_change"}));
        pos(&mut s, 0, true);
        pos(&mut s, 60_000, true); // seek forward
        pos(&mut s, 30_000, true); // seek back
        pos(&mut s, 31_000, false); // paused
        assert_eq!((s.songs, s.listened_ms), (0, 0));
    }
}
