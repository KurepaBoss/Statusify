"""Listening history and lyric cache, stored in SQLite.

Replaces history.json, which had three problems:

  * It was only written by the Quit button. Killing the process, logging off,
    shutting Windows down, a crash, or the one-click updater all skipped it,
    so weeks of listening silently vanished (history.json went 18 days
    without a write while Statusify was in daily use).
  * Every save rewrote the whole file, and every entry carried its full lyric
    sheet — about 7 KB per track, so ~3.5 MB rewritten per save at the cap.
  * Entries stored only "HH:MM", so nothing could be asked about *when*.

SQLite commits each play as it happens (atomically, so a kill mid-write can't
corrupt anything), stores a full timestamp, and keeps each track's lyrics once
in their own table — which doubles as a local lyrics cache: a track you have
heard before never needs a network fetch again.

No Tk, no Windows APIs: importable and testable anywhere.
"""
import datetime
import json
import os
import sqlite3
import threading

_SCHEMA = """
CREATE TABLE IF NOT EXISTS plays (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    track_uri   TEXT NOT NULL DEFAULT '',
    artist      TEXT NOT NULL DEFAULT '',
    title       TEXT NOT NULL DEFAULT '',
    album_art   TEXT NOT NULL DEFAULT '',
    played_at   TEXT NOT NULL,              -- local ISO-8601, seconds
    listened_ms INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS plays_played_at ON plays(played_at);
CREATE TABLE IF NOT EXISTS lyrics (
    track_uri  TEXT PRIMARY KEY,
    mode       TEXT NOT NULL,               -- synced | plain | none
    synced     TEXT NOT NULL DEFAULT '[]',  -- JSON [{startMs, words}]
    plain      TEXT NOT NULL DEFAULT '[]',  -- JSON [str]
    source     TEXT NOT NULL DEFAULT '',
    fetched_at TEXT NOT NULL
);
"""


def _now_iso():
    return datetime.datetime.now().replace(microsecond=0).isoformat()


def display_time(played_at, now=None):
    """"14:40" for today, "Sep 21 · 14:40" this year, else "2025-09-21 · 14:40"."""
    try:
        t = datetime.datetime.fromisoformat(played_at)
    except (TypeError, ValueError):
        return played_at or ""
    now = now or datetime.datetime.now()
    if t.date() == now.date():
        return t.strftime("%H:%M")
    if t.year == now.year:
        return f"{t.strftime('%b')} {t.day} · {t.strftime('%H:%M')}"
    return t.strftime("%Y-%m-%d · %H:%M")


def longest_streak(days):
    """Longest run of consecutive calendar days in `days` ("YYYY-MM-DD"
    strings or dates, any order, duplicates allowed)."""
    ds = set()
    for d in days:
        try:
            ds.add(d if isinstance(d, datetime.date) else datetime.date.fromisoformat(str(d)[:10]))
        except ValueError:
            pass
    best = 0
    for d in ds:
        if d - datetime.timedelta(days=1) in ds:
            continue            # not the start of a run
        n = 1
        while d + datetime.timedelta(days=n) in ds:
            n += 1
        best = max(best, n)
    return best


class HistoryStore:
    """Thread-safe: the asyncio backend writes, the Tk thread reads."""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=5)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            if path != ":memory:":
                # WAL: readers never block the writer, and a commit is one
                # append to the log rather than a rewrite of the database.
                self._db.execute("PRAGMA journal_mode=WAL")
                self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.executescript(_SCHEMA)
            self._db.commit()

    def close(self):
        with self._lock:
            try:
                self._db.close()
            except sqlite3.Error:
                pass

    # ── Writes ────────────────────────────────────────────────────
    def record_play(self, track_uri, artist, title, album_art="", played_at=None):
        """Insert one play and return its id."""
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO plays(track_uri, artist, title, album_art, played_at)"
                " VALUES (?,?,?,?,?)",
                (track_uri or "", artist or "", title or "", album_art or "",
                 played_at or _now_iso()))
            self._db.commit()
            return cur.lastrowid

    def set_listened(self, play_id, listened_ms):
        if not play_id:
            return
        with self._lock:
            self._db.execute("UPDATE plays SET listened_ms=? WHERE id=?",
                             (max(0, int(listened_ms)), play_id))
            self._db.commit()

    def save_lyrics(self, track_uri, mode, synced, plain, source=""):
        """Cache a track's lyrics. A "none" result never overwrites real lyrics:
        a failed fetch today must not erase what an earlier fetch found."""
        if not track_uri:
            return
        with self._lock:
            if mode == "none":
                row = self._db.execute("SELECT mode FROM lyrics WHERE track_uri=?",
                                       (track_uri,)).fetchone()
                if row and row["mode"] != "none":
                    return
            self._db.execute(
                "INSERT OR REPLACE INTO lyrics(track_uri, mode, synced, plain, source, fetched_at)"
                " VALUES (?,?,?,?,?,?)",
                (track_uri, mode, json.dumps(synced or [], ensure_ascii=False),
                 json.dumps(plain or [], ensure_ascii=False), source or "", _now_iso()))
            self._db.commit()

    def clear(self):
        with self._lock:
            self._db.execute("DELETE FROM plays")
            self._db.execute("DELETE FROM lyrics")
            self._db.commit()
            try:
                self._db.execute("VACUUM")
            except sqlite3.Error:
                pass

    # ── Reads ─────────────────────────────────────────────────────
    def get_lyrics(self, track_uri):
        """(mode, synced, plain, source) for a cached track, or None."""
        if not track_uri:
            return None
        with self._lock:
            row = self._db.execute(
                "SELECT mode, synced, plain, source FROM lyrics WHERE track_uri=?",
                (track_uri,)).fetchone()
        if not row or row["mode"] == "none":
            return None
        return row["mode"], json.loads(row["synced"]), json.loads(row["plain"]), row["source"]

    def recent(self, limit=500):
        """Newest `limit` plays, oldest first, as history-entry dicts."""
        with self._lock:
            rows = self._db.execute(
                "SELECT p.id, p.track_uri, p.artist, p.title, p.album_art, p.played_at,"
                "       l.mode, l.synced, l.plain"
                " FROM plays p LEFT JOIN lyrics l ON l.track_uri = p.track_uri"
                " ORDER BY p.id DESC LIMIT ?", (int(limit),)).fetchall()
        return [self._entry(r) for r in reversed(rows)]

    def search(self, query, limit=200):
        """Plays whose title, artist or lyrics contain `query` (case-insensitive),
        newest first. Covers the whole history, not just what is rendered."""
        q = f"%{query.strip().lower()}%"
        with self._lock:
            rows = self._db.execute(
                "SELECT p.id, p.track_uri, p.artist, p.title, p.album_art, p.played_at,"
                "       l.mode, l.synced, l.plain"
                " FROM plays p LEFT JOIN lyrics l ON l.track_uri = p.track_uri"
                " WHERE lower(p.title) LIKE ? OR lower(p.artist) LIKE ?"
                "    OR lower(l.synced) LIKE ? OR lower(l.plain) LIKE ?"
                " ORDER BY p.id DESC LIMIT ?", (q, q, q, q, int(limit))).fetchall()
        return [self._entry(r) for r in rows]

    def stats(self, since=None, top=3):
        """{"plays", "listened_ms", "top_artists": [(artist, plays)]} for plays at
        or after `since` (a datetime), or for all time."""
        where, args = "", ()
        if since is not None:
            where, args = " WHERE played_at >= ?", (since.replace(microsecond=0).isoformat(),)
        with self._lock:
            n, ms = self._db.execute(
                f"SELECT COUNT(*), COALESCE(SUM(listened_ms),0) FROM plays{where}", args).fetchone()
            tops = self._db.execute(
                f"SELECT artist, COUNT(*) c FROM plays{where}"
                f"{' AND' if where else ' WHERE'} artist != ''"
                " GROUP BY lower(artist) ORDER BY c DESC, artist LIMIT ?",
                args + (int(top),)).fetchall()
        return {"plays": n, "listened_ms": ms,
                "top_artists": [(r[0], r[1]) for r in tops]}

    def count(self):
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM plays").fetchone()[0]

    # ── Stats page queries ────────────────────────────────────────
    # played_at is local ISO-8601 text, so string comparison is time order and
    # substr(played_at, 1, 10) is the local date: every range below is an
    # index range scan on plays_played_at, never a full-table date parse.

    def recent_plays(self, limit=30, offset=0):
        """Newest plays first, without lyrics: [{id, track_uri, artist, title,
        album_art, played_at, listened_ms}]."""
        with self._lock:
            rows = self._db.execute(
                "SELECT id, track_uri, artist, title, album_art, played_at, listened_ms"
                " FROM plays ORDER BY id DESC LIMIT ? OFFSET ?",
                (int(limit), int(offset))).fetchall()
        return [dict(r) for r in rows]

    def plays_per_day(self, since_date):
        """{"YYYY-MM-DD": plays} for every day on or after `since_date`
        (a date) that has at least one play."""
        with self._lock:
            rows = self._db.execute(
                "SELECT substr(played_at, 1, 10) d, COUNT(*) FROM plays"
                " WHERE played_at >= ? GROUP BY d",
                (since_date.isoformat(),)).fetchall()
        return {r[0]: r[1] for r in rows}

    def top_tracks(self, since=None, limit=10):
        """Most played songs as [{title, artist, album_art, plays, listened_ms}],
        for plays at or after `since` (a datetime) or for all time. A song is
        its title and artist, case-insensitively: the same song reached from
        an album and from a playlist has two URIs but is one song."""
        where, args = "", ()
        if since is not None:
            where, args = " AND played_at >= ?", (since.replace(microsecond=0).isoformat(),)
        with self._lock:
            rows = self._db.execute(
                "SELECT title, artist, MAX(album_art) art, COUNT(*) c,"
                "       COALESCE(SUM(listened_ms), 0) ms"
                f" FROM plays WHERE title != ''{where}"
                " GROUP BY lower(title), lower(artist)"
                " ORDER BY c DESC, ms DESC, title LIMIT ?", args + (int(limit),)).fetchall()
        return [{"title": r[0], "artist": r[1], "album_art": r[2] or "",
                 "plays": r[3], "listened_ms": r[4]} for r in rows]

    def first_played(self):
        """played_at of the oldest play, or None."""
        with self._lock:
            row = self._db.execute("SELECT MIN(played_at) FROM plays").fetchone()
        return row[0] if row else None

    def month_summary(self, year, month):
        """Everything the monthly Wrapped card shows, for one calendar month:
        {"year", "month", "plays", "listened_ms", "artists" (distinct),
         "top_song": {title, artist, album_art, plays} | None,
         "top_artist": (name, plays) | None,
         "busiest_hour": (hour, plays) | None,
         "longest_streak": consecutive days with a play (within the month),
         "active_days"}."""
        start = datetime.date(year, month, 1)
        end = datetime.date(year + (month == 12), month % 12 + 1, 1)
        rng = (start.isoformat(), end.isoformat())
        with self._lock:
            n, ms, artists = self._db.execute(
                "SELECT COUNT(*), COALESCE(SUM(listened_ms), 0),"
                "       COUNT(DISTINCT lower(NULLIF(artist, '')))"
                " FROM plays WHERE played_at >= ? AND played_at < ?", rng).fetchone()
            song = self._db.execute(
                "SELECT title, artist, MAX(album_art), COUNT(*) c FROM plays"
                " WHERE played_at >= ? AND played_at < ? AND title != ''"
                " GROUP BY lower(title), lower(artist)"
                " ORDER BY c DESC, SUM(listened_ms) DESC, title LIMIT 1", rng).fetchone()
            art = self._db.execute(
                "SELECT artist, COUNT(*) c FROM plays"
                " WHERE played_at >= ? AND played_at < ? AND artist != ''"
                " GROUP BY lower(artist) ORDER BY c DESC, artist LIMIT 1", rng).fetchone()
            hour = self._db.execute(
                "SELECT CAST(substr(played_at, 12, 2) AS INTEGER) h, COUNT(*) c FROM plays"
                " WHERE played_at >= ? AND played_at < ?"
                " GROUP BY h ORDER BY c DESC, h LIMIT 1", rng).fetchone()
            days = [r[0] for r in self._db.execute(
                "SELECT DISTINCT substr(played_at, 1, 10) d FROM plays"
                " WHERE played_at >= ? AND played_at < ? ORDER BY d", rng)]
        return {
            "year": year, "month": month, "plays": n, "listened_ms": ms,
            "artists": artists,
            "top_song": ({"title": song[0], "artist": song[1], "album_art": song[2] or "",
                          "plays": song[3]} if song else None),
            "top_artist": (art[0], art[1]) if art else None,
            "busiest_hour": (hour[0], hour[1]) if hour and n else None,
            "longest_streak": longest_streak(days),
            "active_days": len(days),
        }

    @staticmethod
    def _entry(r):
        mode = r["mode"] or "none"
        return {
            "id": r["id"], "track_uri": r["track_uri"], "artist": r["artist"],
            "title": r["title"], "album_art": r["album_art"],
            "played_at": r["played_at"], "time": display_time(r["played_at"]),
            "mode": mode,
            "synced": json.loads(r["synced"]) if r["synced"] else [],
            "plain": json.loads(r["plain"]) if r["plain"] else [],
        }

    # ── Migration ─────────────────────────────────────────────────
    def import_json(self, json_path):
        """One-time import of a legacy history.json. Returns the number of plays
        imported; renames the file to .migrated so it is never imported twice.

        Legacy entries only have "HH:MM". They are dated to the file's last
        write (the only date evidence there is), which is exact for the final
        session and at worst a few days early for older ones."""
        if not os.path.exists(json_path):
            return 0
        with open(json_path, "r", encoding="utf-8") as f:
            entries = json.load(f)
        day = datetime.datetime.fromtimestamp(os.path.getmtime(json_path)).date()
        n = 0
        for e in entries if isinstance(entries, list) else []:
            if not isinstance(e, dict):
                continue
            played_at = e.get("played_at")
            if not played_at:
                try:
                    hh, mm = (int(x) for x in str(e.get("time", "")).split(":")[:2])
                    played_at = datetime.datetime.combine(
                        day, datetime.time(hh, mm)).isoformat()
                except (TypeError, ValueError):
                    played_at = datetime.datetime.combine(day, datetime.time()).isoformat()
            self.record_play(e.get("track_uri", ""), e.get("artist", ""),
                             e.get("title", ""), e.get("album_art", ""), played_at)
            if e.get("synced") or e.get("plain"):
                self.save_lyrics(e.get("track_uri", ""), e.get("mode", "none"),
                                 e.get("synced"), e.get("plain"), "history.json")
            n += 1
        os.replace(json_path, json_path + ".migrated")
        return n
