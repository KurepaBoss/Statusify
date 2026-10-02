//! The app's live state, as the UI sees it.

use crate::lyrics::Lyrics;
use serde::Serialize;

#[derive(Clone, Debug, Serialize, Default, PartialEq)]
pub struct Track {
    pub uri: String,
    pub artist: String,
    pub title: String,
    pub album: String,
    pub album_art: String,
    pub blacklisted: bool,
}

#[derive(Clone, Debug, Serialize, Default)]
pub struct Snapshot {
    pub track: Option<Track>,
    pub position_ms: i64,
    /// Wall-clock (epoch ms) when position_ms was reported, so the UI and the
    /// presence loop can extrapolate between bridge updates.
    pub position_at_ms: i64,
    pub duration_ms: i64,
    pub is_playing: bool,
    pub lyrics: Lyrics,
    pub bridge_connected: bool,
    pub discord_user: Option<String>,
    pub note: String,
}

impl Snapshot {
    pub fn estimated_position(&self, now_ms: i64) -> i64 {
        let mut p = self.position_ms;
        if self.is_playing {
            p += (now_ms - self.position_at_ms).max(0);
        }
        if self.duration_ms > 0 {
            p = p.min(self.duration_ms);
        }
        p
    }
}

pub fn now_ms() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_millis() as i64
}
