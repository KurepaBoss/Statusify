//! Word timing that survives a restart (owner: nowplaying agent).
//!
//! The engine's `Line` keeps only `startMs` and `words`, so lyrics loaded from
//! history.db come back without the bridge's `syl` / `endMs`. Two places can
//! still hold them:
//!   * `np_timing.db` next to history.db, written here whenever a bridge
//!     message carried timing (the raw `synced` array, pruned to the newest
//!     `KEEP` tracks), and
//!   * history.db itself, whose rows the Python app wrote with the extra keys
//!     (opened read-only, never written).
//! Either is matched back to the sheet line by line by
//! `np_extras::timing_from_raw`, so stale or different lyrics are ignored.

use rusqlite::{params, Connection, OpenFlags, OptionalExtension};
use serde_json::Value;
use std::path::{Path, PathBuf};

const KEEP: i64 = 400;

pub struct TimingStore {
    path: PathBuf,
    history: PathBuf,
}

impl TimingStore {
    pub fn new(data_dir: &Path) -> Self {
        TimingStore { path: data_dir.join("np_timing.db"), history: data_dir.join("history.db") }
    }

    fn open(&self) -> Option<Connection> {
        let c = Connection::open(&self.path).ok()?;
        c.busy_timeout(std::time::Duration::from_millis(500)).ok()?;
        c.execute_batch(
            "CREATE TABLE IF NOT EXISTS timing(track_uri TEXT PRIMARY KEY, synced TEXT NOT NULL, saved_at INTEGER NOT NULL);",
        )
        .ok()?;
        Some(c)
    }

    /// Remember the raw synced lines (with their timing) of a track.
    pub fn save(&self, uri: &str, raw_synced: &Value) {
        if uri.is_empty() || !raw_synced.is_array() {
            return;
        }
        let Some(c) = self.open() else { return };
        let json = raw_synced.to_string();
        let same: Option<String> = c.query_row("SELECT synced FROM timing WHERE track_uri=?", [uri], |r| r.get(0)).optional().ok().flatten();
        if same.as_deref() == Some(json.as_str()) {
            return;
        }
        let now = crate::state::now_ms();
        let _ = c.execute("INSERT OR REPLACE INTO timing(track_uri, synced, saved_at) VALUES(?,?,?)", params![uri, json, now]);
        let _ = c.execute(
            "DELETE FROM timing WHERE track_uri NOT IN (SELECT track_uri FROM timing ORDER BY saved_at DESC LIMIT ?)",
            [KEEP],
        );
    }

    pub fn load(&self, uri: &str) -> Option<Value> {
        if !self.path.exists() {
            return None;
        }
        let c = self.open()?;
        let s: String = c.query_row("SELECT synced FROM timing WHERE track_uri=?", [uri], |r| r.get(0)).optional().ok().flatten()?;
        serde_json::from_str(&s).ok()
    }

    /// The `synced` column the Python app wrote (it keeps `syl` / `endMs`).
    pub fn from_history(&self, uri: &str) -> Option<Value> {
        if !self.history.exists() {
            return None;
        }
        let c = Connection::open_with_flags(&self.history, OpenFlags::SQLITE_OPEN_READ_ONLY).ok()?;
        c.busy_timeout(std::time::Duration::from_millis(500)).ok()?;
        for table in ["lyric_pins", "lyrics"] {
            let s: Option<String> =
                c.query_row(&format!("SELECT synced FROM {table} WHERE track_uri=?"), [uri], |r| r.get(0)).optional().ok().flatten();
            if let Some(v) = s.and_then(|s| serde_json::from_str::<Value>(&s).ok()) {
                if v.as_array().is_some_and(|a| a.iter().any(|l| l.get("syl").is_some() || l.get("endMs").is_some())) {
                    return Some(v);
                }
            }
        }
        None
    }

    /// Everything known for this track, best source first.
    pub fn lookup(&self, uri: &str) -> Vec<Value> {
        [self.load(uri), self.from_history(uri)].into_iter().flatten().collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn tmp(tag: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("sfy-npt-{tag}-{}", crate::state::now_ms()));
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    #[test]
    fn saved_timing_comes_back_and_prunes() {
        let d = tmp("save");
        let s = TimingStore::new(&d);
        assert!(s.load("u").is_none());
        let raw = json!([{"startMs": 1000, "words": "hi", "endMs": 2000, "syl": [[1000, 1500, "h"]]}]);
        s.save("u", &raw);
        s.save("u", &raw); // idempotent
        assert_eq!(s.load("u"), Some(raw));
        assert!(s.load("other").is_none());
        for i in 0..(KEEP + 20) {
            s.save(&format!("t{i}"), &json!([{"startMs": i, "words": "x", "endMs": i + 1}]));
        }
        let n: i64 = s.open().unwrap().query_row("SELECT COUNT(*) FROM timing", [], |r| r.get(0)).unwrap();
        assert!(n <= KEEP);
        std::fs::remove_dir_all(d).ok();
    }

    #[test]
    fn python_rows_in_history_db_are_read_only_sources() {
        let d = tmp("hist");
        let c = Connection::open(d.join("history.db")).unwrap();
        c.execute_batch("CREATE TABLE lyrics(track_uri TEXT PRIMARY KEY, mode TEXT, synced TEXT, plain TEXT, source TEXT, fetched_at TEXT);").unwrap();
        c.execute(
            "INSERT INTO lyrics VALUES('u','synced',?,'[]','s','t')",
            [json!([{"startMs": 5, "words": "x", "endMs": 90}]).to_string()],
        )
        .unwrap();
        c.execute("INSERT INTO lyrics VALUES('plain','synced','[{\"startMs\":1,\"words\":\"y\"}]','[]','s','t')", []).unwrap();
        drop(c);
        let s = TimingStore::new(&d);
        assert!(s.from_history("u").is_some());
        assert!(s.from_history("plain").is_none(), "no timing keys, nothing to add");
        assert!(s.from_history("nope").is_none());
        assert_eq!(s.lookup("u").len(), 1);
        assert!(!d.join("np_timing.db").exists(), "lookup must not create files");
        std::fs::remove_dir_all(d).ok();
    }
}
