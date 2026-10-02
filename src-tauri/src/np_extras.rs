//! Now Playing extras that need no UI: player state with optimistic holds,
//! the Up Next queue, lyric search results, syllable timing and the lyrics
//! the bridge sent for a pinned track. Ported from statusify_np_extras.py and
//! the player/queue parts of main.py / statusify_bridge.py.

use crate::lyrics::{clean_title, Line, Lyrics};
use serde_json::{json, Value};
use std::time::{Duration, Instant};

pub const PIN_SOURCE: &str = "LRCLIB · chosen";
pub const QUEUE_MAX: usize = 10;
pub const OPTIMISTIC_HOLD: Duration = Duration::from_millis(2500);
pub const SEARCH_LIMIT: usize = 20;

/// Config-safe option name for a track URI (its last ':' segment).
pub fn offset_key(uri: &str) -> String {
    uri.rsplit(':').next().unwrap_or("").to_string()
}

// ── Player state ─────────────────────────────────────────────────

/// A value the user just set, held against the bridge's reports for a moment:
/// Spotify applies a change after the bridge has already sent its follow-up
/// player_state, which still carries the old value.
#[derive(Clone, Debug)]
struct Held {
    value: f64,
    until: Instant,
}

#[derive(Clone, Debug)]
pub struct Player {
    pub volume: f64,
    pub shuffle: bool,
    pub repeat: u8,
    pub liked: bool,
    held: [Option<Held>; 4], // volume, shuffle, repeat, liked
}

impl Default for Player {
    fn default() -> Self {
        Player { volume: 1.0, shuffle: false, repeat: 0, liked: false, held: [None, None, None, None] }
    }
}

const F_VOLUME: usize = 0;
const F_SHUFFLE: usize = 1;
const F_REPEAT: usize = 2;
const F_LIKED: usize = 3;

impl Player {
    pub fn to_json(&self) -> Value {
        json!({"volume": self.volume, "shuffle": self.shuffle, "repeat": self.repeat, "liked": self.liked})
    }

    fn hold(&mut self, f: usize, v: f64, now: Instant) {
        self.held[f] = Some(Held { value: v, until: now + OPTIMISTIC_HOLD });
    }

    /// The value to show for field `f` given what the bridge reported.
    fn merge(&mut self, f: usize, reported: f64, now: Instant) -> f64 {
        let Some(h) = self.held[f].clone() else { return reported };
        let same = if f == F_VOLUME { (reported - h.value).abs() < 0.006 } else { reported == h.value };
        if same || now > h.until {
            self.held[f] = None;
            return reported;
        }
        h.value
    }

    /// A `player_state` message; missing or invalid fields keep their value.
    pub fn apply_report(&mut self, m: &Value, now: Instant) {
        let vol = m.get("volume").and_then(|v| v.as_f64()).filter(|v| !v.is_nan()).map(|v| v.clamp(0.0, 1.0));
        let shuffle = m.get("shuffle").and_then(|v| v.as_bool());
        let repeat = m.get("repeat").and_then(|v| v.as_i64()).filter(|r| (0..=2).contains(r));
        let liked = m.get("liked").and_then(|v| v.as_bool());
        if let Some(v) = vol {
            self.volume = self.merge(F_VOLUME, v, now);
        }
        if let Some(v) = shuffle {
            self.shuffle = self.merge(F_SHUFFLE, v as u8 as f64, now) != 0.0;
        }
        if let Some(v) = repeat {
            self.repeat = self.merge(F_REPEAT, v as f64, now) as u8;
        }
        if let Some(v) = liked {
            self.liked = self.merge(F_LIKED, v as u8 as f64, now) != 0.0;
        }
    }

    /// shuffle / repeat / like flip optimistically; the bridge's reply corrects it.
    pub fn optimistic(&mut self, action: &str, now: Instant) -> bool {
        match action {
            "shuffle" => {
                self.shuffle = !self.shuffle;
                self.hold(F_SHUFFLE, self.shuffle as u8 as f64, now);
            }
            "repeat" => {
                self.repeat = (self.repeat + 1) % 3;
                self.hold(F_REPEAT, self.repeat as f64, now);
            }
            "like" => {
                self.liked = !self.liked;
                self.hold(F_LIKED, self.liked as u8 as f64, now);
            }
            _ => return false,
        }
        true
    }

    pub fn set_volume(&mut self, v: f64, now: Instant) {
        self.volume = v.clamp(0.0, 1.0);
        self.hold(F_VOLUME, self.volume, now);
    }
}

// ── Queue ────────────────────────────────────────────────────────

fn num(v: Option<&Value>) -> i64 {
    v.and_then(|x| x.as_f64()).map(|f| f as i64).unwrap_or(0).max(0)
}

fn text(v: Option<&Value>) -> String {
    match v {
        Some(Value::String(s)) => s.clone(),
        _ => String::new(),
    }
}

/// The bridge's `queue.tracks` as a clean list of up to QUEUE_MAX objects
/// {uri, uid, title, artist, album_art, duration_ms}. Junk entries dropped.
pub fn sanitise_queue(tracks: Option<&Value>) -> Vec<Value> {
    let Some(arr) = tracks.and_then(|t| t.as_array()) else { return vec![] };
    let mut out = Vec::new();
    for t in arr {
        let Some(o) = t.as_object() else { continue };
        let uri = text(o.get("uri"));
        if uri.is_empty() {
            continue;
        }
        out.push(json!({
            "uri": uri, "uid": text(o.get("uid")), "title": text(o.get("title")),
            "artist": text(o.get("artist")), "album_art": text(o.get("album_art")),
            "duration_ms": num(o.get("duration_ms")),
        }));
        if out.len() >= QUEUE_MAX {
            break;
        }
    }
    out
}

// ── Lyric search ─────────────────────────────────────────────────

/// Usable LRCLIB results for the search panel: no instrumentals, nothing
/// without lyrics; the ones matching the song's length first, synced before
/// plain. Each is {track, artist, album, duration, synced, raw}.
pub fn clean_results(results: &Value, duration_ms: i64, limit: usize) -> Vec<Value> {
    let mut out: Vec<(Value, f64, bool)> = Vec::new();
    for r in results.as_array().map(|a| a.as_slice()).unwrap_or(&[]) {
        let Some(o) = r.as_object() else { continue };
        if o.get("instrumental").and_then(|v| v.as_bool()).unwrap_or(false) {
            continue;
        }
        let filled = |k: &str| o.get(k).and_then(|v| v.as_str()).is_some_and(|s| !s.trim().is_empty());
        let (synced, plain) = (filled("syncedLyrics"), filled("plainLyrics"));
        if !(synced || plain) {
            continue;
        }
        let dur = o.get("duration").and_then(|v| v.as_f64()).unwrap_or(0.0);
        let name = text(o.get("trackName").or_else(|| o.get("name")));
        out.push((
            json!({"track": name, "artist": text(o.get("artistName")), "album": text(o.get("albumName")),
                   "duration": dur, "synced": synced, "raw": r}),
            dur,
            synced,
        ));
    }
    if duration_ms > 0 {
        let want = duration_ms as f64 / 1000.0;
        let near = |d: f64| if d != 0.0 && (d - want).abs() <= 3.0 { 0 } else { 1 };
        out.sort_by_key(|(_, d, s)| (near(*d), if *s { 0 } else { 1 })); // stable
    }
    out.into_iter().take(limit).map(|(v, _, _)| v).collect()
}

/// Where the free-text search goes; the field search when the box still holds
/// the prefilled "artist title".
pub fn search_url(base: &str, text: &str, artist: &str, title: &str) -> Option<(String, Vec<(&'static str, String)>)> {
    let text = text.trim();
    if text.is_empty() {
        return None;
    }
    let default = format!("{artist} {title}").trim().to_string();
    if text == default && !title.is_empty() {
        return Some((base.to_string(), vec![("track_name", clean_title(title)), ("artist_name", artist.to_string())]));
    }
    Some((base.to_string(), vec![("q", text.to_string())]))
}

pub async fn search(client: &reqwest::Client, base: &str, text: &str, artist: &str, title: &str) -> Result<Value, String> {
    let Some((url, q)) = search_url(base, text, artist, title) else { return Ok(json!([])) };
    let get = |q: Vec<(&'static str, String)>| {
        let req = client.get(&url).query(&q).timeout(Duration::from_secs(8));
        async move {
            let r = req.send().await.map_err(|e| e.to_string())?;
            if !r.status().is_success() {
                return Err(format!("HTTP {}", r.status()));
            }
            r.json::<Value>().await.map_err(|e| e.to_string())
        }
    };
    let by_field = q[0].0 == "track_name";
    let res = get(q).await;
    // Same fallback as the Python app: when the field search finds nothing,
    // the box's own text is searched freely.
    if by_field {
        if let Ok(v) = &res {
            if v.as_array().is_some_and(|a| !a.is_empty()) {
                return res;
            }
        }
        return get(vec![("q", text.trim().to_string())]).await;
    }
    res
}

// ── Lyrics from raw bridge messages ──────────────────────────────

/// A `lyrics` / `lyrics_prefetch` message as Lyrics (for the pin stash).
pub fn lyrics_from_raw(m: &Value, default_src: &str) -> Option<Lyrics> {
    let mode = m.get("mode").or(m.get("lyrics_mode")).and_then(|x| x.as_str()).unwrap_or("none");
    if mode != "synced" && mode != "plain" {
        return None;
    }
    let synced: Vec<Line> = serde_json::from_value(m.get("synced").cloned().unwrap_or_default()).unwrap_or_default();
    let plain: Vec<String> = serde_json::from_value(m.get("plain").cloned().unwrap_or_default()).unwrap_or_default();
    if synced.is_empty() && plain.is_empty() {
        return None;
    }
    let src = m.get("source").and_then(|x| x.as_str()).unwrap_or(default_src);
    Some(Lyrics { mode: mode.into(), synced, plain, source: src.into() })
}

/// Word/syllable timing the engine's Line type does not carry. Returns one
/// {endMs?, syl?} per synced line (null where a line has neither), or None
/// when nothing is timed or the raw lines do not match the sheet.
pub fn timing_from_raw(raw_synced: &Value, sheet: &[Line]) -> Option<Value> {
    let arr = raw_synced.as_array()?;
    if arr.len() != sheet.len() || arr.is_empty() {
        return None;
    }
    let mut any = false;
    let mut out = Vec::with_capacity(arr.len());
    for (r, l) in arr.iter().zip(sheet) {
        let words = r.get("words").and_then(|w| w.as_str()).unwrap_or("");
        let start = r.get("startMs").and_then(|s| s.as_f64()).map(|f| f as i64);
        if words.trim() != l.words.trim() || start != Some(l.start_ms) {
            return None;
        }
        let mut e = serde_json::Map::new();
        if let Some(end) = r.get("endMs").filter(|v| v.is_number()) {
            e.insert("endMs".into(), end.clone());
        }
        if let Some(syl) = r.get("syl").filter(|v| v.as_array().is_some_and(|a| !a.is_empty())) {
            e.insert("syl".into(), syl.clone());
        }
        if e.is_empty() {
            out.push(Value::Null);
        } else {
            any = true;
            out.push(Value::Object(e));
        }
    }
    any.then(|| Value::Array(out))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn offset_key_is_the_last_segment() {
        assert_eq!(offset_key("spotify:track:4uLU6hMCjMI75M1A2tKUQC"), "4uLU6hMCjMI75M1A2tKUQC");
        assert_eq!(offset_key(""), "");
    }

    #[test]
    fn player_report_keeps_missing_fields_and_clamps() {
        let now = Instant::now();
        let mut p = Player::default();
        p.apply_report(&json!({"volume": 0.4, "shuffle": true, "repeat": 2, "liked": true}), now);
        assert_eq!((p.volume, p.shuffle, p.repeat, p.liked), (0.4, true, 2, true));
        p.apply_report(&json!({"volume": 7.0}), now);
        assert_eq!((p.volume, p.shuffle, p.repeat), (1.0, true, 2));
        p.apply_report(&json!({"repeat": 9, "shuffle": "yes", "liked": 1}), now);
        assert_eq!((p.repeat, p.shuffle, p.liked), (2, true, true)); // invalid values ignored
    }

    #[test]
    fn optimistic_value_survives_a_stale_report_until_confirmed() {
        let t0 = Instant::now();
        let mut p = Player::default();
        p.apply_report(&json!({"volume": 1.0, "shuffle": false, "repeat": 0, "liked": false}), t0);
        assert!(p.optimistic("shuffle", t0));
        assert!(p.shuffle);
        // Spotify's follow-up report still says the old value: held.
        p.apply_report(&json!({"shuffle": false}), t0 + Duration::from_millis(400));
        assert!(p.shuffle);
        // A report that agrees releases the hold.
        p.apply_report(&json!({"shuffle": true}), t0 + Duration::from_millis(800));
        p.apply_report(&json!({"shuffle": false}), t0 + Duration::from_millis(900));
        assert!(!p.shuffle);
        // After the hold expires the bridge wins.
        p.optimistic("like", t0);
        assert!(p.liked);
        p.apply_report(&json!({"liked": false}), t0 + OPTIMISTIC_HOLD + Duration::from_millis(1));
        assert!(!p.liked);
        // repeat cycles 0 -> 1 -> 2 -> 0; volume holds within 0.006
        for want in [1, 2, 0] {
            p.optimistic("repeat", t0);
            assert_eq!(p.repeat, want);
        }
        p.set_volume(0.65, t0);
        p.apply_report(&json!({"volume": 0.9}), t0 + Duration::from_millis(100));
        assert_eq!(p.volume, 0.65);
        p.apply_report(&json!({"volume": 0.652}), t0 + Duration::from_millis(200));
        assert_eq!(p.volume, 0.652);
        assert!(!p.optimistic("toggle", t0));
        p.set_volume(3.0, t0);
        assert_eq!(p.volume, 1.0);
    }

    #[test]
    fn queue_is_cleaned_and_capped() {
        let tracks: Vec<Value> = (0..14).map(|i| json!({"uri": format!("spotify:track:{i}"), "title": "T", "duration_ms": 1234.7})).collect();
        let mut v = tracks;
        v.insert(0, json!("junk"));
        v.insert(1, json!({"title": "no uri"}));
        v.insert(2, json!({"uri": ""}));
        let q = sanitise_queue(Some(&json!(v)));
        assert_eq!(q.len(), QUEUE_MAX);
        assert_eq!(q[0]["uri"], "spotify:track:0");
        assert_eq!(q[0]["duration_ms"], 1234);
        assert_eq!(q[0]["uid"], "");
        assert!(sanitise_queue(Some(&json!("x"))).is_empty());
        assert!(sanitise_queue(None).is_empty());
    }

    fn lr(name: &str, artist: &str, dur: f64, synced: &str, plain: &str, inst: bool) -> Value {
        json!({"trackName": name, "artistName": artist, "albumName": "Al", "duration": dur,
               "syncedLyrics": synced, "plainLyrics": plain, "instrumental": inst})
    }

    #[test]
    fn results_filtered_and_ordered_by_length_then_synced() {
        let r = json!([
            lr("far synced", "A", 300.0, "[00:01.00]x", "", false),
            lr("near plain", "A", 200.5, "", "words", false),
            lr("near synced", "A", 201.0, "[00:01.00]x", "words", false),
            lr("instrumental", "A", 200.0, "[00:01.00]x", "", true),
            lr("empty", "A", 200.0, "  ", "", false),
            json!("junk"),
        ]);
        let c = clean_results(&r, 200_000, 20);
        let names: Vec<&str> = c.iter().map(|v| v["track"].as_str().unwrap()).collect();
        assert_eq!(names, ["near synced", "near plain", "far synced"]);
        assert_eq!(c[0]["synced"], true);
        assert_eq!(c[1]["synced"], false);
        assert_eq!(c[0]["raw"]["trackName"], "near synced");
        assert_eq!(clean_results(&r, 0, 2).len(), 2);
        assert_eq!(clean_results(&r, 0, 20)[0]["track"], "far synced"); // untouched order
    }

    #[test]
    fn prefilled_query_searches_by_field_else_free_text() {
        let (_, q) = search_url("u", "Artist Song (feat. X)", "Artist", "Song (feat. X)").unwrap();
        assert_eq!(q[0], ("track_name", "Song".to_string()));
        assert_eq!(q[1], ("artist_name", "Artist".to_string()));
        let (_, q) = search_url("u", "other words", "Artist", "Song").unwrap();
        assert_eq!(q, vec![("q", "other words".to_string())]);
        assert!(search_url("u", "   ", "Artist", "Song").is_none());
        let (_, q) = search_url("u", "Artist", "Artist", "").unwrap();
        assert_eq!(q[0].0, "q");
    }

    #[test]
    fn syllable_timing_is_matched_to_the_sheet() {
        let sheet = vec![Line { start_ms: 1000, words: "hello world".into() }, Line { start_ms: 5000, words: "bye".into() }];
        let raw = json!([
            {"startMs": 1000, "words": "hello world", "endMs": 3000, "syl": [[1000, 2000, "hello"], [2000, 3000, "world"]]},
            {"startMs": 5000, "words": "bye"}
        ]);
        let t = timing_from_raw(&raw, &sheet).unwrap();
        assert_eq!(t[0]["endMs"], 3000);
        assert_eq!(t[0]["syl"][1][2], "world");
        assert!(t[1].is_null());
        // a different sheet (e.g. LRCLIB chosen later) must not get foreign timing
        let other = vec![Line { start_ms: 1000, words: "hello world".into() }];
        assert!(timing_from_raw(&raw, &other).is_none());
        let wrong = vec![Line { start_ms: 1000, words: "hello there".into() }, Line { start_ms: 5000, words: "bye".into() }];
        assert!(timing_from_raw(&raw, &wrong).is_none());
        // nothing timed -> None
        let plain = json!([{"startMs": 1000, "words": "hello world"}, {"startMs": 5000, "words": "bye"}]);
        assert!(timing_from_raw(&plain, &sheet).is_none());
    }

    #[test]
    fn raw_lyrics_for_the_stash() {
        let m = json!({"mode": "synced", "source": "Spicy", "synced": [{"startMs": 1, "words": "a"}], "plain": []});
        let l = lyrics_from_raw(&m, "x").unwrap();
        assert_eq!((l.mode.as_str(), l.source.as_str(), l.synced.len()), ("synced", "Spicy", 1));
        assert!(lyrics_from_raw(&json!({"mode": "none"}), "x").is_none());
        assert!(lyrics_from_raw(&json!({"mode": "plain", "plain": []}), "x").is_none());
        assert_eq!(lyrics_from_raw(&json!({"mode": "plain", "plain": ["a"]}), "Spotify").unwrap().source, "Spotify");
    }
}
