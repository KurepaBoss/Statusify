//! history.db — the same SQLite file and schema the Python app uses, so the
//! new app picks up the existing play history and lyric cache unchanged.

use crate::lyrics::{Line, Lyrics};
use rusqlite::{params, Connection, OptionalExtension};
use serde::Serialize;
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
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
    /// The "Remember history" setting (preferences/save_history). When off,
    /// nothing new is recorded or cached; reads keep working.
    enabled: AtomicBool,
}

/// One play for the History list: no lyric bodies, just what the row shows.
/// `badge` data comes from the lyrics the user saw (their pin, else the cache).
#[derive(Clone, Debug, Serialize, PartialEq)]
pub struct Entry {
    pub id: i64,
    pub track_uri: String,
    pub artist: String,
    pub title: String,
    pub album_art: String,
    pub played_at: String,
    pub listened_ms: i64,
    pub mode: String,
    pub synced_n: i64,
    pub plain_n: i64,
}

/// A play with its whole lyric sheet (the History detail view, exports).
#[derive(Clone, Debug, Serialize, PartialEq)]
pub struct FullEntry {
    #[serde(flatten)]
    pub entry: Entry,
    pub synced: Vec<Line>,
    pub plain: Vec<String>,
}

#[derive(Clone, Debug, Serialize, PartialEq)]
pub struct Stats {
    pub plays: i64,
    pub listened_ms: i64,
    pub top_artists: Vec<(String, i64)>,
}

#[derive(Clone, Debug, Serialize, PartialEq)]
pub struct TopTrack {
    pub title: String,
    pub artist: String,
    pub album_art: String,
    pub plays: i64,
    pub listened_ms: i64,
}

#[derive(Clone, Debug, Serialize, PartialEq)]
pub struct TopSong {
    pub title: String,
    pub artist: String,
    pub album_art: String,
    pub plays: i64,
}

/// Everything the monthly Wrapped card shows.
#[derive(Clone, Debug, Serialize, PartialEq)]
pub struct MonthSummary {
    pub year: i32,
    pub month: u32,
    pub plays: i64,
    pub listened_ms: i64,
    pub artists: i64,
    pub top_song: Option<TopSong>,
    pub top_artist: Option<(String, i64)>,
    pub busiest_hour: Option<(i64, i64)>,
    pub longest_streak: i64,
    pub active_days: i64,
}

/// Longest run of consecutive calendar days among "YYYY-MM-DD..." strings
/// (any order, duplicates allowed, unparseable ones ignored).
pub fn longest_streak<S: AsRef<str>>(days: &[S]) -> i64 {
    use chrono::NaiveDate;
    let ds: std::collections::BTreeSet<NaiveDate> = days
        .iter()
        .filter_map(|d| d.as_ref().get(..10).and_then(|d| NaiveDate::parse_from_str(d, "%Y-%m-%d").ok()))
        .collect();
    let mut best = 0;
    for d in &ds {
        if ds.contains(&(*d - chrono::Duration::days(1))) {
            continue; // not the start of a run
        }
        let mut n = 1;
        while ds.contains(&(*d + chrono::Duration::days(n))) {
            n += 1;
        }
        best = best.max(n);
    }
    best
}

/// SQL for the lyrics a play shows: the user's pin if there is one, else the
/// cached copy. Needs `lyrics l` and `lyric_pins pn` joined on the play.
const EFF_MODE: &str = "CASE WHEN pn.track_uri IS NOT NULL THEN pn.mode ELSE COALESCE(l.mode, 'none') END";
const EFF_SYNCED: &str = "CASE WHEN pn.track_uri IS NOT NULL THEN pn.synced ELSE l.synced END";
const EFF_PLAIN: &str = "CASE WHEN pn.track_uri IS NOT NULL THEN pn.plain ELSE l.plain END";

fn entry_sql(tail: &str) -> String {
    format!(
        "SELECT p.id, p.track_uri, p.artist, p.title, p.album_art, p.played_at, p.listened_ms,
                {EFF_MODE},
                CASE WHEN json_valid({EFF_SYNCED}) THEN json_array_length({EFF_SYNCED}) ELSE 0 END,
                CASE WHEN json_valid({EFF_PLAIN}) THEN json_array_length({EFF_PLAIN}) ELSE 0 END
         FROM plays p
         LEFT JOIN lyrics l ON l.track_uri = p.track_uri
         LEFT JOIN lyric_pins pn ON pn.track_uri = p.track_uri
         {tail}"
    )
}

fn entry_row(r: &rusqlite::Row) -> rusqlite::Result<Entry> {
    Ok(Entry {
        id: r.get(0)?,
        track_uri: r.get(1)?,
        artist: r.get(2)?,
        title: r.get(3)?,
        album_art: r.get(4)?,
        played_at: r.get(5)?,
        listened_ms: r.get::<_, Option<i64>>(6)?.unwrap_or(0),
        mode: r.get(7)?,
        synced_n: r.get(8)?,
        plain_n: r.get(9)?,
    })
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
        db.pragma_update(None, "synchronous", "NORMAL")?;
        Ok(Store { db: Mutex::new(db), enabled: AtomicBool::new(true) })
    }

    pub fn set_enabled(&self, on: bool) {
        self.enabled.store(on, Ordering::Relaxed);
    }

    pub fn enabled(&self) -> bool {
        self.enabled.load(Ordering::Relaxed)
    }

    /// Record one play; an Err when history is off, so callers record nothing.
    pub fn record_play(&self, uri: &str, artist: &str, title: &str, art: &str, played_at: &str) -> rusqlite::Result<i64> {
        if !self.enabled() {
            return Err(rusqlite::Error::InvalidQuery);
        }
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

    // ── History / Stats queries ───────────────────────────────────
    // played_at is local ISO-8601 text, so string comparison is time order and
    // substr(played_at, 1, 10) is the local date: every range below is an
    // index range scan on plays_played_at, never a full-table date parse.

    pub fn count(&self) -> i64 {
        self.db.lock().unwrap().query_row("SELECT COUNT(*) FROM plays", [], |r| r.get(0)).unwrap_or(0)
    }

    /// (play count, newest play id): the cheap fingerprint the UI refresh watches.
    pub fn fingerprint(&self) -> (i64, i64) {
        self.db
            .lock()
            .unwrap()
            .query_row("SELECT COUNT(*), COALESCE(MAX(id), 0) FROM plays", [], |r| Ok((r.get(0)?, r.get(1)?)))
            .unwrap_or((0, 0))
    }

    pub fn first_played(&self) -> Option<String> {
        self.db.lock().unwrap().query_row("SELECT MIN(played_at) FROM plays", [], |r| r.get(0)).ok().flatten()
    }

    /// Newest `limit` plays, newest first (id order), for the History list.
    pub fn entries(&self, limit: i64) -> rusqlite::Result<Vec<Entry>> {
        let db = self.db.lock().unwrap();
        let mut st = db.prepare(&entry_sql("ORDER BY p.id DESC LIMIT ?"))?;
        let rows = st.query_map([limit], entry_row)?;
        rows.collect()
    }

    /// Plays whose title, artist or lyrics contain `query` (case-insensitive),
    /// newest first. Covers the whole history, not just what is rendered.
    pub fn search(&self, query: &str, limit: i64) -> rusqlite::Result<Vec<Entry>> {
        let q = format!("%{}%", query.trim().to_lowercase().replace('\\', "\\\\").replace('%', "\\%").replace('_', "\\_"));
        let tail = format!(
            "WHERE lower(p.title) LIKE ?1 ESCAPE '\\' OR lower(p.artist) LIKE ?1 ESCAPE '\\'
                OR lower({EFF_SYNCED}) LIKE ?1 ESCAPE '\\' OR lower({EFF_PLAIN}) LIKE ?1 ESCAPE '\\'
             ORDER BY p.id DESC LIMIT ?2"
        );
        let db = self.db.lock().unwrap();
        let mut st = db.prepare(&entry_sql(&tail))?;
        let rows = st.query_map(params![q, limit], entry_row)?;
        rows.collect()
    }

    /// One play with its lyrics (the user's pin if any, else the cache).
    pub fn entry(&self, id: i64) -> Option<FullEntry> {
        let db = self.db.lock().unwrap();
        let sql = format!(
            "SELECT p.id, p.track_uri, p.artist, p.title, p.album_art, p.played_at, p.listened_ms,
                    {EFF_MODE}, 0, 0, {EFF_SYNCED}, {EFF_PLAIN}
             FROM plays p
             LEFT JOIN lyrics l ON l.track_uri = p.track_uri
             LEFT JOIN lyric_pins pn ON pn.track_uri = p.track_uri
             WHERE p.id = ?"
        );
        let (mut e, synced, plain): (Entry, Option<String>, Option<String>) = db
            .query_row(&sql, [id], |r| Ok((entry_row(r)?, r.get(10)?, r.get(11)?)))
            .optional()
            .ok()
            .flatten()?;
        let synced: Vec<Line> = synced.and_then(|s| serde_json::from_str(&s).ok()).unwrap_or_default();
        let plain: Vec<String> = plain.and_then(|s| serde_json::from_str(&s).ok()).unwrap_or_default();
        e.synced_n = synced.len() as i64;
        e.plain_n = plain.len() as i64;
        Some(FullEntry { entry: e, synced, plain })
    }

    /// Plays at or after `since` ("YYYY-MM-DDTHH:MM:SS"), or all time: count,
    /// listening time and the `top` most played artists.
    pub fn stats(&self, since: Option<&str>, top: i64) -> rusqlite::Result<Stats> {
        let db = self.db.lock().unwrap();
        let (where_, args): (&str, Vec<String>) = match since {
            Some(s) => (" WHERE played_at >= ?", vec![s.to_string()]),
            None => ("", vec![]),
        };
        let (plays, listened_ms) = db.query_row(
            &format!("SELECT COUNT(*), COALESCE(SUM(listened_ms),0) FROM plays{where_}"),
            rusqlite::params_from_iter(args.iter()),
            |r| Ok((r.get(0)?, r.get(1)?)),
        )?;
        let sql = format!(
            "SELECT artist, COUNT(*) c FROM plays{where_}{} artist != ''
             GROUP BY lower(artist) ORDER BY c DESC, artist LIMIT ?",
            if where_.is_empty() { " WHERE" } else { " AND" }
        );
        let mut st = db.prepare(&sql)?;
        let mut a = args.clone();
        a.push(top.to_string());
        let tops = st
            .query_map(rusqlite::params_from_iter(a.iter()), |r| Ok((r.get::<_, String>(0)?, r.get::<_, i64>(1)?)))?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        Ok(Stats { plays, listened_ms, top_artists: tops })
    }

    /// Newest plays first, without lyrics, `limit` at a time from `offset`.
    pub fn recent_plays_page(&self, limit: i64, offset: i64) -> rusqlite::Result<Vec<Play>> {
        let db = self.db.lock().unwrap();
        let mut st = db.prepare(
            "SELECT id, track_uri, artist, title, album_art, played_at, listened_ms
             FROM plays ORDER BY id DESC LIMIT ? OFFSET ?",
        )?;
        let rows = st.query_map([limit, offset], |r| {
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

    /// {"YYYY-MM-DD": plays} for every day on or after `since_date` with a play.
    pub fn plays_per_day(&self, since_date: &str) -> rusqlite::Result<std::collections::BTreeMap<String, i64>> {
        let db = self.db.lock().unwrap();
        let mut st = db.prepare(
            "SELECT substr(played_at, 1, 10) d, COUNT(*) FROM plays WHERE played_at >= ? GROUP BY d",
        )?;
        let rows = st.query_map([since_date], |r| Ok((r.get::<_, String>(0)?, r.get::<_, i64>(1)?)))?;
        rows.collect()
    }

    /// Most played songs, for plays at or after `since` or all time. A song is
    /// its title and artist, case-insensitively: the same song reached from an
    /// album and from a playlist has two URIs but is one song.
    pub fn top_tracks(&self, since: Option<&str>, limit: i64) -> rusqlite::Result<Vec<TopTrack>> {
        let db = self.db.lock().unwrap();
        let mut args: Vec<String> = vec![];
        let mut where_ = String::new();
        if let Some(s) = since {
            where_ = " AND played_at >= ?".into();
            args.push(s.to_string());
        }
        args.push(limit.to_string());
        let mut st = db.prepare(&format!(
            "SELECT title, artist, MAX(album_art), COUNT(*) c, COALESCE(SUM(listened_ms), 0) ms
             FROM plays WHERE title != ''{where_}
             GROUP BY lower(title), lower(artist)
             ORDER BY c DESC, ms DESC, title LIMIT ?"
        ))?;
        let rows = st.query_map(rusqlite::params_from_iter(args.iter()), |r| {
            Ok(TopTrack {
                title: r.get(0)?,
                artist: r.get(1)?,
                album_art: r.get::<_, Option<String>>(2)?.unwrap_or_default(),
                plays: r.get(3)?,
                listened_ms: r.get(4)?,
            })
        })?;
        rows.collect()
    }

    /// The monthly Wrapped numbers for one calendar month.
    pub fn month_summary(&self, year: i32, month: u32) -> rusqlite::Result<MonthSummary> {
        let (ny, nm) = if month >= 12 { (year + 1, 1) } else { (year, month + 1) };
        let rng = [format!("{year:04}-{month:02}-01"), format!("{ny:04}-{nm:02}-01")];
        let db = self.db.lock().unwrap();
        let (plays, listened_ms, artists): (i64, i64, i64) = db.query_row(
            "SELECT COUNT(*), COALESCE(SUM(listened_ms), 0), COUNT(DISTINCT lower(NULLIF(artist, '')))
             FROM plays WHERE played_at >= ?1 AND played_at < ?2",
            params![rng[0], rng[1]],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        )?;
        let top_song = db
            .query_row(
                "SELECT title, artist, MAX(album_art), COUNT(*) c FROM plays
                 WHERE played_at >= ?1 AND played_at < ?2 AND title != ''
                 GROUP BY lower(title), lower(artist)
                 ORDER BY c DESC, SUM(listened_ms) DESC, title LIMIT 1",
                params![rng[0], rng[1]],
                |r| {
                    Ok(TopSong {
                        title: r.get(0)?,
                        artist: r.get(1)?,
                        album_art: r.get::<_, Option<String>>(2)?.unwrap_or_default(),
                        plays: r.get(3)?,
                    })
                },
            )
            .optional()?;
        let top_artist = db
            .query_row(
                "SELECT artist, COUNT(*) c FROM plays
                 WHERE played_at >= ?1 AND played_at < ?2 AND artist != ''
                 GROUP BY lower(artist) ORDER BY c DESC, artist LIMIT 1",
                params![rng[0], rng[1]],
                |r| Ok((r.get::<_, String>(0)?, r.get::<_, i64>(1)?)),
            )
            .optional()?;
        let busiest_hour = db
            .query_row(
                "SELECT CAST(substr(played_at, 12, 2) AS INTEGER) h, COUNT(*) c FROM plays
                 WHERE played_at >= ?1 AND played_at < ?2
                 GROUP BY h ORDER BY c DESC, h LIMIT 1",
                params![rng[0], rng[1]],
                |r| Ok((r.get::<_, i64>(0)?, r.get::<_, i64>(1)?)),
            )
            .optional()?
            .filter(|_| plays > 0);
        let days = {
            let mut st = db.prepare(
                "SELECT DISTINCT substr(played_at, 1, 10) d FROM plays
                 WHERE played_at >= ?1 AND played_at < ?2 ORDER BY d",
            )?;
            let rows = st.query_map(params![rng[0], rng[1]], |r| r.get::<_, String>(0))?;
            rows.collect::<rusqlite::Result<Vec<_>>>()?
        };
        Ok(MonthSummary {
            year,
            month,
            plays,
            listened_ms,
            artists,
            top_song,
            top_artist,
            busiest_hour,
            longest_streak: longest_streak(&days),
            active_days: days.len() as i64,
        })
    }

    // ── Writes the pages need ─────────────────────────────────────

    /// Forget a pinned choice (and the cached copy of it). True if there was one.
    pub fn unpin_lyrics(&self, uri: &str) -> bool {
        if uri.is_empty() {
            return false;
        }
        let db = self.db.lock().unwrap();
        let src: Option<String> =
            db.query_row("SELECT source FROM lyric_pins WHERE track_uri=?", [uri], |r| r.get(0)).optional().ok().flatten();
        let Some(src) = src else { return false };
        let _ = db.execute("DELETE FROM lyric_pins WHERE track_uri=?", [uri]);
        let _ = db.execute("DELETE FROM lyrics WHERE track_uri=? AND source=?", params![uri, src]);
        true
    }

    /// Wipe plays, the lyric cache and pins, then compact the file.
    pub fn clear(&self) -> rusqlite::Result<()> {
        let db = self.db.lock().unwrap();
        db.execute_batch("DELETE FROM plays; DELETE FROM lyrics; DELETE FROM lyric_pins;")?;
        let _ = db.execute_batch("VACUUM");
        Ok(())
    }

    /// One-time import of a legacy history.json; renames it to .migrated so
    /// it is never imported twice. Returns the number of plays imported.
    ///
    /// Legacy entries only have "HH:MM". They are dated to the file's last
    /// write (the only date evidence there is).
    pub fn import_json(&self, path: &Path) -> Result<usize, String> {
        if !path.exists() {
            return Ok(0);
        }
        let text = std::fs::read_to_string(path).map_err(|e| e.to_string())?;
        let entries: serde_json::Value = serde_json::from_str(&text).map_err(|e| e.to_string())?;
        let day = std::fs::metadata(path)
            .and_then(|m| m.modified())
            .map(chrono::DateTime::<chrono::Local>::from)
            .map(|d| d.date_naive())
            .unwrap_or_else(|_| chrono::Local::now().date_naive());
        let mut n = 0;
        for e in entries.as_array().into_iter().flatten() {
            let Some(e) = e.as_object() else { continue };
            let sv = |k: &str| e.get(k).and_then(|v| v.as_str()).unwrap_or("").to_string();
            let played_at = match e.get("played_at").and_then(|v| v.as_str()).filter(|s| !s.is_empty()) {
                Some(p) => p.to_string(),
                None => {
                    let t = sv("time");
                    let mut it = t.split(':').map(|x| x.trim().parse::<u32>());
                    let hm = match (it.next(), it.next()) {
                        (Some(Ok(h)), Some(Ok(m))) => day.and_hms_opt(h, m, 0),
                        _ => None,
                    };
                    hm.unwrap_or_else(|| day.and_hms_opt(0, 0, 0).unwrap()).format("%Y-%m-%dT%H:%M:%S").to_string()
                }
            };
            let uri = sv("track_uri");
            self.record_play_raw(&uri, &sv("artist"), &sv("title"), &sv("album_art"), &played_at)
                .map_err(|e| e.to_string())?;
            let synced: Vec<Line> = serde_json::from_value(e.get("synced").cloned().unwrap_or_default()).unwrap_or_default();
            let plain: Vec<String> = serde_json::from_value(e.get("plain").cloned().unwrap_or_default()).unwrap_or_default();
            if !synced.is_empty() || !plain.is_empty() {
                let mode = e.get("mode").and_then(|v| v.as_str()).unwrap_or("none").to_string();
                let l = Lyrics { mode, synced, plain, source: "history.json".into() };
                let _ = self.save_lyrics_raw(&uri, &l);
            }
            n += 1;
        }
        let mut dest = path.as_os_str().to_owned();
        dest.push(".migrated");
        std::fs::rename(path, dest).map_err(|e| e.to_string())?;
        Ok(n)
    }

    /// record_play without the "history is on" gate (migration).
    fn record_play_raw(&self, uri: &str, artist: &str, title: &str, art: &str, played_at: &str) -> rusqlite::Result<i64> {
        let db = self.db.lock().unwrap();
        db.execute(
            "INSERT INTO plays(track_uri, artist, title, album_art, played_at) VALUES (?,?,?,?,?)",
            params![uri, artist, title, art, played_at],
        )?;
        Ok(db.last_insert_rowid())
    }

    fn save_lyrics_raw(&self, uri: &str, l: &Lyrics) -> rusqlite::Result<()> {
        if uri.is_empty() || l.is_none() {
            return Ok(());
        }
        self.db.lock().unwrap().execute(
            "INSERT OR REPLACE INTO lyrics(track_uri, mode, synced, plain, source, fetched_at) VALUES (?,?,?,?,?,?)",
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

    pub fn save_lyrics(&self, uri: &str, l: &Lyrics) -> rusqlite::Result<()> {
        if uri.is_empty() || l.is_none() || !self.enabled() {
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

    // ── Ported from the Python test-suite (test_history_store / test_stats_tab) ──

    fn play(s: &Store, artist: &str, title: &str, when: &str, ms: i64, art: &str) -> i64 {
        let id = s.record_play(&format!("spotify:track:{artist}-{title}"), artist, title, art, when).unwrap();
        if ms > 0 {
            s.set_listened(id, ms).unwrap();
        }
        id
    }

    fn synced() -> Lyrics {
        Lyrics {
            mode: "synced".into(),
            synced: vec![Line { start_ms: 0, words: "first line".into() }, Line { start_ms: 4000, words: "Second Line".into() }],
            plain: vec![],
            source: "Spicy".into(),
        }
    }

    #[test]
    fn play_is_visible_to_a_fresh_connection_without_close() {
        let d = tmp();
        let s = Store::open(&d).unwrap();
        s.record_play("spotify:track:a", "Artist", "Song", "art", "2026-09-01T10:15:00").unwrap();
        let fresh = Store::open(&d).unwrap();
        let e = fresh.entries(10).unwrap();
        assert_eq!((e[0].artist.as_str(), e[0].title.as_str(), e[0].played_at.as_str()), ("Artist", "Song", "2026-09-01T10:15:00"));
        let _ = std::fs::remove_dir_all(d);
    }

    #[test]
    fn lyrics_are_shared_by_every_play_of_a_track_and_pins_win() {
        let d = tmp();
        let s = Store::open(&d).unwrap();
        s.record_play("u1", "A", "T", "", "2026-09-01T10:00:00").unwrap();
        s.save_lyrics("u1", &synced()).unwrap();
        s.record_play("u1", "A", "T", "", "2026-09-01T11:00:00").unwrap();
        let es = s.entries(10).unwrap();
        assert!(es.iter().all(|e| e.mode == "synced" && e.synced_n == 2));
        assert_eq!(s.entry(es[0].id).unwrap().synced, synced().synced);
        // A pin replaces what the sheet and the badge show.
        let pin = Lyrics { mode: "plain".into(), synced: vec![], plain: vec!["x".into()], source: "LRCLIB · chosen".into() };
        s.set_pin("u1", &pin).unwrap();
        let e = s.entry(es[0].id).unwrap();
        assert_eq!((e.entry.mode.as_str(), e.entry.synced_n, e.entry.plain_n), ("plain", 0, 1));
        assert_eq!(e.plain, vec!["x".to_string()]);
        // Unpinning forgets the pin (and a cached copy from the same source).
        assert!(s.unpin_lyrics("u1"));
        assert!(!s.unpin_lyrics("u1"));
        assert_eq!(s.entry(es[0].id).unwrap().entry.synced_n, 2);
        let _ = std::fs::remove_dir_all(d);
    }

    #[test]
    fn a_failed_fetch_never_erases_cached_lyrics() {
        let d = tmp();
        let s = Store::open(&d).unwrap();
        s.save_lyrics("u1", &synced()).unwrap();
        s.save_lyrics("u1", &Lyrics::none()).unwrap();
        assert_eq!(s.cached("u1").unwrap().mode, "synced");
        assert_eq!(s.cached(""), None);
        let _ = std::fs::remove_dir_all(d);
    }

    #[test]
    fn search_covers_title_artist_and_lyrics_case_insensitively() {
        let d = tmp();
        let s = Store::open(&d).unwrap();
        s.record_play("u1", "Kendrick Lamar", "Money Trees", "", "2026-09-01T10:00:00").unwrap();
        s.record_play("u2", "Tyler", "IFHY", "", "2026-09-01T10:05:00").unwrap();
        s.save_lyrics("u2", &synced()).unwrap();
        let titles = |q: &str| s.search(q, 50).unwrap().into_iter().map(|e| e.title).collect::<Vec<_>>();
        assert_eq!(titles("kendrick"), ["Money Trees"]);
        assert_eq!(titles("TREES"), ["Money Trees"]);
        assert_eq!(titles("second line"), ["IFHY"]);
        assert!(titles("nothing matches").is_empty());
        // % and _ are literal characters, not wildcards.
        assert!(titles("%").is_empty());
        assert!(titles("_").is_empty());
        let _ = std::fs::remove_dir_all(d);
    }

    #[test]
    fn stats_window_and_top_artists() {
        let d = tmp();
        let s = Store::open(&d).unwrap();
        play(&s, "Old", "x", "2020-01-01T00:00:00", 60_000, "");
        for i in 0..3 {
            play(&s, "Tyler", &format!("t{i}"), "2026-09-20T12:00:00", 120_000, "");
        }
        play(&s, "tyler", "lowercase counts as the same artist", "2026-09-20T12:10:00", 0, "");
        play(&s, "Kendrick", "k", "2026-09-20T12:20:00", 0, "");
        let week = s.stats(Some("2026-09-15T00:00:00"), 3).unwrap();
        assert_eq!((week.plays, week.listened_ms, week.top_artists[0].1), (5, 360_000, 4));
        let all = s.stats(None, 3).unwrap();
        assert_eq!((all.plays, all.listened_ms), (6, 420_000));
        let _ = std::fs::remove_dir_all(d);
    }

    #[test]
    fn recent_plays_newest_first_with_paging() {
        let d = tmp();
        let s = Store::open(&d).unwrap();
        for i in 0..5 {
            play(&s, "A", &format!("S{i}"), &format!("2026-09-01T12:0{i}:00"), 1000 * i, "");
        }
        let got = s.recent_plays_page(3, 0).unwrap();
        assert_eq!(got.iter().map(|p| p.title.as_str()).collect::<Vec<_>>(), ["S4", "S3", "S2"]);
        assert_eq!(got[0].listened_ms, 4000);
        let rest = s.recent_plays_page(3, 3).unwrap();
        assert_eq!(rest.iter().map(|p| p.title.as_str()).collect::<Vec<_>>(), ["S1", "S0"]);
        let _ = std::fs::remove_dir_all(d);
    }

    #[test]
    fn plays_per_day_counts_local_dates_from_a_start() {
        let d = tmp();
        let s = Store::open(&d).unwrap();
        for w in ["2026-09-10T23:59:00", "2026-09-10T00:00:00", "2026-09-11T08:00:00", "2026-09-01T08:00:00"] {
            play(&s, "A", "x", w, 0, "");
        }
        let m = s.plays_per_day("2026-09-05").unwrap();
        assert_eq!(m.len(), 2);
        assert_eq!((m["2026-09-10"], m["2026-09-11"]), (2, 1));
        let _ = std::fs::remove_dir_all(d);
    }

    #[test]
    fn top_tracks_merges_case_and_respects_since() {
        let d = tmp();
        let s = Store::open(&d).unwrap();
        for _ in 0..3 {
            play(&s, "Radiohead", "Reckoner", "2026-07-20T12:00:00", 1000, "");
        }
        play(&s, "radiohead", "reckoner", "2026-09-19T12:00:00", 1000, "u");
        play(&s, "Björk", "Jóga", "2026-09-19T12:00:00", 0, "");
        play(&s, "Björk", "Jóga", "2026-09-18T12:00:00", 0, "");
        let all = s.top_tracks(None, 5).unwrap();
        assert_eq!((all[0].plays, all[0].title.to_lowercase()), (4, "reckoner".to_string()));
        assert_eq!((all[0].album_art.as_str(), all[0].listened_ms), ("u", 4000));
        let recent = s.top_tracks(Some("2026-08-21T12:00:00"), 5).unwrap();
        assert_eq!(recent.iter().map(|t| (t.title.as_str(), t.plays)).collect::<Vec<_>>(), [("Jóga", 2), ("reckoner", 1)]);
        let _ = std::fs::remove_dir_all(d);
    }

    #[test]
    fn month_summary_numbers() {
        let d = tmp();
        let s = Store::open(&d).unwrap();
        play(&s, "A", "One", "2026-08-31T23:00:00", 60_000, ""); // previous month
        play(&s, "A", "One", "2026-09-01T21:05:00", 120_000, "");
        play(&s, "A", "One", "2026-09-02T21:30:00", 120_000, "");
        play(&s, "B", "Two", "2026-09-03T09:00:00", 60_000, "");
        play(&s, "b", "Two", "2026-09-03T21:10:00", 60_000, "");
        play(&s, "B", "Two", "2026-09-07T10:00:00", 60_000, "");
        play(&s, "C", "Three", "2026-10-01T00:00:00", 60_000, ""); // next month
        let m = s.month_summary(2026, 9).unwrap();
        assert_eq!((m.plays, m.listened_ms, m.artists), (5, 420_000, 2));
        let song = m.top_song.unwrap();
        assert_eq!((song.title.as_str(), song.plays), ("Two", 3));
        let art = m.top_artist.unwrap();
        assert_eq!((art.0.to_lowercase(), art.1), ("b".to_string(), 3));
        assert_eq!(m.busiest_hour, Some((21, 3)));
        assert_eq!((m.longest_streak, m.active_days), (3, 4)); // 1st, 2nd, 3rd
        let _ = std::fs::remove_dir_all(d);
    }

    #[test]
    fn month_summary_december_and_empty() {
        let d = tmp();
        let s = Store::open(&d).unwrap();
        play(&s, "A", "X", "2025-12-31T23:59:00", 0, "");
        play(&s, "A", "X", "2026-01-01T00:00:00", 0, "");
        assert_eq!(s.month_summary(2025, 12).unwrap().plays, 1);
        let e = s.month_summary(2024, 2).unwrap();
        assert_eq!(e.plays, 0);
        assert!(e.top_song.is_none() && e.top_artist.is_none() && e.busiest_hour.is_none());
        assert_eq!(e.longest_streak, 0);
        assert_eq!(s.first_played().as_deref(), Some("2025-12-31T23:59:00"));
        let _ = std::fs::remove_dir_all(d);
    }

    #[test]
    fn longest_streak_runs() {
        assert_eq!(longest_streak::<&str>(&[]), 0);
        assert_eq!(longest_streak(&["2026-09-01"]), 1);
        assert_eq!(longest_streak(&["2026-09-03", "2026-09-01", "2026-09-02", "2026-09-02", "2026-09-05", "2026-09-06"]), 3);
        assert_eq!(longest_streak(&["2026-02-28", "2026-03-01"]), 2);
        assert_eq!(longest_streak(&["2026-09-01T10:00:00", "2026-09-02T10:00:00", "junk"]), 2);
    }

    #[test]
    fn clear_removes_everything() {
        let d = tmp();
        let s = Store::open(&d).unwrap();
        s.record_play("u1", "A", "T", "", "2026-09-01T10:00:00").unwrap();
        s.save_lyrics("u1", &synced()).unwrap();
        s.set_pin("u1", &synced()).unwrap();
        s.clear().unwrap();
        assert_eq!(s.count(), 0);
        assert!(s.cached("u1").is_none() && s.pinned("u1").is_none());
        let _ = std::fs::remove_dir_all(d);
    }

    #[test]
    fn history_off_records_and_caches_nothing_but_reads_work() {
        let d = tmp();
        let s = Store::open(&d).unwrap();
        s.save_lyrics("u1", &synced()).unwrap();
        s.set_enabled(false);
        assert!(s.record_play("u2", "A", "T", "", "2026-09-01T10:00:00").is_err());
        s.save_lyrics("u2", &synced()).unwrap();
        assert!(s.cached("u1").is_some() && s.cached("u2").is_none());
        assert_eq!(s.count(), 0);
        s.set_enabled(true);
        assert!(s.record_play("u2", "A", "T", "", "2026-09-01T10:00:00").is_ok());
        let _ = std::fs::remove_dir_all(d);
    }

    #[test]
    fn legacy_json_migrates_once_with_dates() {
        let d = tmp();
        let legacy = d.join("history.json");
        std::fs::write(
            &legacy,
            serde_json::json!([
                {"track_uri": "u1", "artist": "A", "title": "T", "album_art": "", "mode": "synced",
                 "synced": [{"startMs": 0, "words": "first line"}], "plain": [], "time": "14:40"},
                {"track_uri": "u2", "artist": "B", "title": "U", "album_art": "", "mode": "none",
                 "synced": [], "plain": [], "time": "garbage"}
            ])
            .to_string(),
        )
        .unwrap();
        let s = Store::open(&d).unwrap();
        s.set_enabled(false); // migration is not a "new play"
        assert_eq!(s.import_json(&legacy).unwrap(), 2);
        assert!(!legacy.exists() && d.join("history.json.migrated").exists());
        let es = s.entries(10).unwrap();
        assert!(es[1].played_at.ends_with("T14:40:00"), "{}", es[1].played_at);
        assert!(es[0].played_at.ends_with("T00:00:00"));
        assert_eq!(es[1].synced_n, 1);
        assert_eq!(s.import_json(&legacy).unwrap(), 0); // never twice
        let _ = std::fs::remove_dir_all(d);
    }
}
