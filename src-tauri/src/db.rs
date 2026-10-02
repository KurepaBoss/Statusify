//! history.db — the same SQLite file and schema the Python app uses, so the
//! new app picks up the existing play history and lyric cache unchanged.

use crate::lyrics::{Line, Lyrics};
use rusqlite::{params, Connection, OptionalExtension};
use serde::Serialize;
use std::path::Path;
use std::sync::Mutex;

const SCHEMA: &str = "
CREATE TABLE IF NOT EXISTS plays (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    track_uri   TEXT NOT NULL DEFAULT '',
    artist      TEXT NOT NULL DEFAULT '',
    title       TEXT NOT NULL DEFAULT '',
    album_art   TEXT NOT NULL DEFAULT '',
    played_at   TEXT NOT NULL,
    listened_ms INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS plays_played_at ON plays(played_at);
CREATE TABLE IF NOT EXISTS lyrics (
    track_uri  TEXT PRIMARY KEY,
    mode       TEXT NOT NULL,
    synced     TEXT NOT NULL DEFAULT '[]',
    plain      TEXT NOT NULL DEFAULT '[]',
    source     TEXT NOT NULL DEFAULT '',
    fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lyric_pins (
    track_uri  TEXT PRIMARY KEY,
    mode       TEXT NOT NULL,
    synced     TEXT NOT NULL DEFAULT '[]',
    plain      TEXT NOT NULL DEFAULT '[]',
    source     TEXT NOT NULL DEFAULT '',
    pinned_at  TEXT NOT NULL
);";

#[derive(Clone, Debug, Serialize, PartialEq)]
pub struct Play {
    pub id: i64,
    pub track_uri: String,
    pub artist: String,
    pub title: String,
    pub album_art: String,
    pub played_at: String,
    pub listened_ms: i64,
}

pub struct Store {
    db: Mutex<Connection>,
}

pub fn now_iso() -> String {
    chrono::Local::now().format("%Y-%m-%dT%H:%M:%S").to_string()
}

impl Store {
    pub fn open(dir: &Path) -> rusqlite::Result<Self> {
        let db = Connection::open(dir.join("history.db"))?;
        db.busy_timeout(std::time::Duration::from_secs(5))?;
        db.pragma_update(None, "journal_mode", "WAL")?;
        db.execute_batch(SCHEMA)?;
        Ok(Store { db: Mutex::new(db) })
    }

    pub fn record_play(&self, uri: &str, artist: &str, title: &str, art: &str, played_at: &str) -> rusqlite::Result<i64> {
        let db = self.db.lock().unwrap();
        db.execute(
            "INSERT INTO plays(track_uri, artist, title, album_art, played_at) VALUES (?,?,?,?,?)",
            params![uri, artist, title, art, played_at],
        )?;
        Ok(db.last_insert_rowid())
    }

    pub fn set_listened(&self, id: i64, ms: i64) -> rusqlite::Result<()> {
        self.db.lock().unwrap().execute("UPDATE plays SET listened_ms=? WHERE id=?", params![ms, id])?;
        Ok(())
    }

    pub fn recent_plays(&self, limit: i64) -> rusqlite::Result<Vec<Play>> {
        let db = self.db.lock().unwrap();
        let mut st = db.prepare(
            "SELECT id, track_uri, artist, title, album_art, played_at, listened_ms
             FROM plays ORDER BY played_at DESC, id DESC LIMIT ?",
        )?;
        let rows = st.query_map([limit], |r| {
            Ok(Play {
                id: r.get(0)?,
                track_uri: r.get(1)?,
                artist: r.get(2)?,
                title: r.get(3)?,
                album_art: r.get(4)?,
                played_at: r.get(5)?,
                listened_ms: r.get(6)?,
            })
        })?;
        rows.collect()
    }

    fn read(&self, table: &str, uri: &str) -> Option<Lyrics> {
        let db = self.db.lock().unwrap();
        let sql = format!("SELECT mode, synced, plain, source FROM {table} WHERE track_uri=?");
        let row: Option<(String, String, String, String)> = db
            .query_row(&sql, [uri], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)))
            .optional()
            .ok()
            .flatten();
        let (mode, synced, plain, source) = row?;
        let synced: Vec<Line> = serde_json::from_str(&synced).unwrap_or_default();
        let plain: Vec<String> = serde_json::from_str(&plain).unwrap_or_default();
        if mode == "none" || (synced.is_empty() && plain.is_empty()) {
            return None;
        }
        Some(Lyrics { mode, synced, plain, source })
    }

    /// Lyrics the user picked by hand for this track; they beat every source.
    pub fn pinned(&self, uri: &str) -> Option<Lyrics> {
        self.read("lyric_pins", uri)
    }

    pub fn cached(&self, uri: &str) -> Option<Lyrics> {
        self.read("lyrics", uri)
    }

    pub fn set_pin(&self, uri: &str, l: &Lyrics) -> rusqlite::Result<()> {
        self.db.lock().unwrap().execute(
            "INSERT OR REPLACE INTO lyric_pins(track_uri, mode, synced, plain, source, pinned_at)
             VALUES (?,?,?,?,?,?)",
            params![
                uri,
                l.mode,
                serde_json::to_string(&l.synced).unwrap(),
                serde_json::to_string(&l.plain).unwrap(),
                l.source,
                now_iso()
            ],
        )?;
        Ok(())
    }

    pub fn clear_pin(&self, uri: &str) -> rusqlite::Result<()> {
        self.db.lock().unwrap().execute("DELETE FROM lyric_pins WHERE track_uri=?", [uri])?;
        Ok(())
    }

    /// Raw connection for feature modules with their own queries.
    pub fn with_conn<T>(&self, f: impl FnOnce(&Connection) -> T) -> T {
        f(&self.db.lock().unwrap())
    }

    pub fn save_lyrics(&self, uri: &str, l: &Lyrics) -> rusqlite::Result<()> {
        if uri.is_empty() || l.is_none() {
            return Ok(());
        }
        self.db.lock().unwrap().execute(
            "INSERT OR REPLACE INTO lyrics(track_uri, mode, synced, plain, source, fetched_at)
             VALUES (?,?,?,?,?,?)",
            params![
                uri,
                l.mode,
                serde_json::to_string(&l.synced).unwrap(),
                serde_json::to_string(&l.plain).unwrap(),
                l.source,
                now_iso()
            ],
        )?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmp() -> std::path::PathBuf {
        let d = std::env::temp_dir().join(format!("statusify-rs-test-{}", rand_suffix()));
        std::fs::create_dir_all(&d).unwrap();
        d
    }
    fn rand_suffix() -> u128 {
        std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos()
    }

    #[test]
    fn plays_and_lyrics_round_trip() {
        let d = tmp();
        let s = Store::open(&d).unwrap();
        let id = s.record_play("spotify:track:1", "A", "T", "", "2026-10-02T23:00:00").unwrap();
        s.set_listened(id, 42_000).unwrap();
        let p = s.recent_plays(10).unwrap();
        assert_eq!(p.len(), 1);
        assert_eq!(p[0].listened_ms, 42_000);

        let l = Lyrics {
            mode: "synced".into(),
            synced: vec![Line { start_ms: 1000, words: "hi".into() }],
            plain: vec![],
            source: "LRCLIB".into(),
        };
        s.save_lyrics("spotify:track:1", &l).unwrap();
        assert_eq!(s.cached("spotify:track:1"), Some(l));
        assert_eq!(s.cached("spotify:track:2"), None);
        assert_eq!(s.pinned("spotify:track:1"), None);
        let _ = std::fs::remove_dir_all(d);
    }

    #[test]
    fn reads_python_written_rows() {
        // The Python app writes synced as [{"startMs":..,"words":..}].
        let d = tmp();
        let s = Store::open(&d).unwrap();
        s.db.lock().unwrap().execute(
            "INSERT INTO lyric_pins VALUES ('u','synced','[{\"startMs\": 5, \"words\": \"x\"}]','[]','LRCLIB · chosen','t')",
            [],
        ).unwrap();
        let p = s.pinned("u").unwrap();
        assert_eq!(p.synced[0], Line { start_ms: 5, words: "x".into() });
        let _ = std::fs::remove_dir_all(d);
    }
}
