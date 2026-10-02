//! Lyric sheets: LRC parsing, LRCLIB result picking, line lookup.
//! Ported from statusify_lyrics.py; same data shapes as history.db stores.

use regex::Regex;
use serde::{Deserialize, Serialize};
use std::sync::LazyLock;

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct Line {
    pub start_ms: i64,
    #[serde(default)]
    pub words: String,
}

#[derive(Clone, Debug, Serialize, PartialEq)]
pub struct Lyrics {
    pub mode: String, // synced | plain | none
    pub synced: Vec<Line>,
    pub plain: Vec<String>,
    pub source: String,
}

impl Default for Lyrics {
    fn default() -> Self {
        Self::none()
    }
}

impl Lyrics {
    pub fn none() -> Self {
        Lyrics { mode: "none".into(), synced: vec![], plain: vec![], source: String::new() }
    }
    pub fn is_none(&self) -> bool {
        self.mode == "none"
    }
    pub fn line_count(&self) -> usize {
        if self.synced.is_empty() { self.plain.len() } else { self.synced.len() }
    }
}

/// LRCLIB only trusts results within this many seconds of the track length.
pub const LRCLIB_DURATION_TOLERANCE_S: f64 = 3.0;

static LRC_TAG: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\[(\d+):(\d+)(?:[.:](\d+))?\]").unwrap());
static FEAT: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?i)\s*[\(\[](feat\.?|ft\.?|with)\s[^\)\]]*[\)\]]").unwrap());
static SUFFIX: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?i)\s+-\s+.*(remaster|version|edit|mix|live|mono|stereo).*$").unwrap()
});

/// LRC text -> lines sorted by time. "[00:10.00][01:20.00]chorus" yields one
/// line per tag.
pub fn parse_lrc(text: &str) -> Vec<Line> {
    let mut out = Vec::new();
    for raw in text.lines() {
        let tags: Vec<_> = LRC_TAG.captures_iter(raw).collect();
        let Some(first) = tags.first() else { continue };
        if first.get(0).unwrap().start() != 0 {
            continue;
        }
        let words = raw[tags.last().unwrap().get(0).unwrap().end()..].trim().to_string();
        for c in &tags {
            let frac = c.get(3).map_or("0", |m| m.as_str());
            let mut f = frac.to_string();
            while f.len() < 3 {
                f.push('0');
            }
            let ms: i64 = f[..3].parse().unwrap_or(0);
            let min: i64 = c[1].parse().unwrap_or(0);
            let sec: i64 = c[2].parse().unwrap_or(0);
            out.push(Line { start_ms: (min * 60 + sec) * 1000 + ms, words: words.clone() });
        }
    }
    out.sort_by_key(|l| l.start_ms);
    out
}

/// Drop decorations LRCLIB titles usually lack: "(feat. X)", " - Remastered".
pub fn clean_title(title: &str) -> String {
    let t = FEAT.replace_all(title, "");
    SUFFIX.replace_all(&t, "").trim().to_string()
}

/// Several lyric lines as one presence status: "a, b" or "a. b".
pub fn join_lines(lines: &[&str]) -> String {
    let mut out = String::new();
    for (i, ln) in lines.iter().enumerate() {
        if i > 0 {
            let sep = if out.ends_with(['.', '!', '?', ';', ',']) { " " } else { ", " };
            out.push_str(sep);
        }
        out.push_str(ln);
    }
    out
}

/// Best LRCLIB search result, or None. Synced beats plain; a closer length
/// breaks ties; results too far from the track's length are not trusted.
pub fn pick_lrclib(results: &serde_json::Value, duration_ms: i64) -> Option<Lyrics> {
    let mut best: Option<((u8, f64), Lyrics)> = None;
    for r in results.as_array()? {
        if !r.is_object() || r.get("instrumental").and_then(|v| v.as_bool()).unwrap_or(false) {
            continue;
        }
        let Some(dur_s) = r.get("duration").and_then(|v| v.as_f64()) else { continue };
        let diff = if duration_ms > 0 { (dur_s - duration_ms as f64 / 1000.0).abs() } else { 0.0 };
        if duration_ms > 0 && diff > LRCLIB_DURATION_TOLERANCE_S {
            continue;
        }
        let synced = parse_lrc(r.get("syncedLyrics").and_then(|v| v.as_str()).unwrap_or(""));
        let plain: Vec<String> = r
            .get("plainLyrics")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .lines()
            .map(str::to_string)
            .collect();
        if synced.is_empty() && !plain.iter().any(|l| !l.trim().is_empty()) {
            continue;
        }
        let rank = (if synced.is_empty() { 1 } else { 0 }, diff);
        let lyr = if synced.is_empty() {
            Lyrics { mode: "plain".into(), synced: vec![], plain, source: "LRCLIB".into() }
        } else {
            Lyrics { mode: "synced".into(), synced, plain: vec![], source: "LRCLIB".into() }
        };
        if best.as_ref().map_or(true, |(b, _)| rank < *b) {
            best = Some((rank, lyr));
        }
    }
    best.map(|(_, l)| l)
}

/// Index of the line being sung at `pos_ms`, or None before the first line.
pub fn current_index(synced: &[Line], pos_ms: i64) -> Option<usize> {
    let n = synced.partition_point(|l| l.start_ms <= pos_ms);
    n.checked_sub(1)
}


/// Config-safe [offsets] option name for a track URI: the base62 id after the
/// last ':'. ':' is a configparser delimiter, so the raw URI can never
/// round-trip through statusify.cfg (Python 3.13 refuses to write it; older
/// versions parse it back as the option "spotify").
pub fn offset_key(uri: &str) -> String {
    uri.rsplit(':').next().unwrap_or("").to_string()
}

/// A stored per-track offset against the global delay: "" / None means no
/// override; a value that will not parse falls back to the global, so a
/// hand-edited statusify.cfg cannot break playback.
pub fn resolve_offset_ms(raw: Option<&str>, global_ms: i64) -> i64 {
    match raw {
        None | Some("") => global_ms,
        Some(r) => r.trim().parse::<i64>().unwrap_or(global_ms),
    }
}

/// (current, next) line for `pos_ms`, which must already include the lyric
/// offset. Synced: the last line whose start has been reached, and the next
/// line with different words. Plain: linear interpolation over the track.
pub fn select_line(l: &Lyrics, pos_ms: i64, duration_ms: i64) -> (String, String) {
    if l.mode == "synced" && !l.synced.is_empty() {
        let Some(idx) = current_index(&l.synced, pos_ms) else { return (String::new(), String::new()) };
        let cur = l.synced[idx].words.clone();
        let nxt = l.synced[idx + 1..].iter().find(|e| e.words != cur).map(|e| e.words.clone()).unwrap_or_default();
        return (cur, nxt);
    }
    if l.mode == "plain" && !l.plain.is_empty() && duration_ms > 0 {
        let ratio = (pos_ms as f64 / duration_ms as f64).clamp(0.0, 1.0);
        let n = l.plain.len();
        let i = ((ratio * n as f64) as usize).min(n - 1);
        return (l.plain[i].clone(), l.plain[(i + 1).min(n - 1)].clone());
    }
    (String::new(), String::new())
}

/// An instrumental stretch: the presence shows the instrumental marker.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct Gap {
    pub start_ms: i64,
    pub end_ms: i64,
    pub gap_ms: i64,
    /// -2 intro, -3 outro, else the index of the line before the gap.
    pub key: i64,
}

/// Instrumental gaps in a synced sheet (port of _calc_instrumental_gaps).
/// A gap is >= 2x the song's median line length and >= 8 s; a mid-song gap
/// also needs 4 s of real silence after the line is sung, and the marker
/// goes up 3 s into the line so it can still be read.
pub fn instrumental_gaps(synced: &[Line], duration_ms: i64) -> Vec<Gap> {
    if synced.is_empty() || duration_ms <= 0 {
        return vec![];
    }
    let n = synced.len();
    let mut all: Vec<i64> = (0..n - 1).map(|i| synced[i + 1].start_ms - synced[i].start_ms).collect();
    if synced[0].start_ms > 0 {
        all.push(synced[0].start_ms);
    }
    all.push(duration_ms - synced[n - 1].start_ms);
    all.sort();
    let mid = all.len() / 2;
    let median = if all.len() % 2 == 0 { (all[mid - 1] + all[mid]) as f64 / 2.0 } else { all[mid] as f64 };
    const MULT: f64 = 2.0;
    const ABS: i64 = 8000;
    let mut gaps = Vec::new();
    let intro = synced[0].start_ms;
    if intro >= ABS && intro as f64 >= median * MULT {
        gaps.push(Gap { start_ms: 0, end_ms: intro, gap_ms: intro, key: -2 });
    }
    for i in 0..n - 1 {
        let (cur, nxt) = (synced[i].start_ms, synced[i + 1].start_ms);
        let g = nxt - cur;
        if g < ABS || (g as f64) < median * MULT {
            continue;
        }
        let sung_end = cur + (median as i64).min(g - 1000);
        if nxt - sung_end < 4000 {
            continue;
        }
        let start = cur + 3000;
        if start >= nxt {
            continue;
        }
        gaps.push(Gap { start_ms: start, end_ms: nxt, gap_ms: g, key: i as i64 });
    }
    let last = synced[n - 1].start_ms;
    let outro = duration_ms - last;
    if outro >= ABS && outro as f64 >= median * MULT {
        let start = last + (median as i64).min(outro - 1000);
        if start < duration_ms {
            gaps.push(Gap { start_ms: start, end_ms: duration_ms, gap_ms: outro, key: -3 });
        }
    }
    gaps.sort_by_key(|g| g.start_ms);
    gaps
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn lrc_parses_tags_fractions_and_repeats() {
        let l = parse_lrc("[00:01.5]a\n[00:02.25][01:00.00] chorus \nnot a line\n x[00:03.00]b");
        assert_eq!(
            l,
            vec![
                Line { start_ms: 1500, words: "a".into() },
                Line { start_ms: 2250, words: "chorus".into() },
                Line { start_ms: 60000, words: "chorus".into() },
            ]
        );
    }

    #[test]
    fn titles_are_cleaned() {
        assert_eq!(clean_title("Song (feat. Someone)"), "Song");
        assert_eq!(clean_title("Song - Remastered 2011"), "Song");
        assert_eq!(clean_title("Song [Remix]"), "Song [Remix]");
    }

    #[test]
    fn lines_join_like_python() {
        assert_eq!(join_lines(&["hi", "there"]), "hi, there");
        assert_eq!(join_lines(&["hi.", "there"]), "hi. there");
    }

    #[test]
    fn lrclib_prefers_synced_within_tolerance() {
        let r = json!([
            {"duration": 300, "syncedLyrics": "[00:01.00]wrong song"},
            {"duration": 101, "plainLyrics": "plain one"},
            {"duration": 102, "syncedLyrics": "[00:01.00]right"},
            {"duration": 100, "instrumental": true},
        ]);
        let got = pick_lrclib(&r, 100_000).unwrap();
        assert_eq!(got.mode, "synced");
        assert_eq!(got.synced[0].words, "right");
        assert!(pick_lrclib(&json!([{"duration": 200, "plainLyrics": "x"}]), 100_000).is_none());
    }

    #[test]
    fn current_line_lookup() {
        let s = parse_lrc("[00:01.00]a\n[00:05.00]b");
        assert_eq!(current_index(&s, 500), None);
        assert_eq!(current_index(&s, 1000), Some(0));
        assert_eq!(current_index(&s, 9000), Some(1));
    }

    #[test]
    fn offsets_resolve_like_python() {
        assert_eq!(offset_key("spotify:track:4uLU6hMCjMI75M1A2tKUQC"), "4uLU6hMCjMI75M1A2tKUQC");
        assert_eq!(offset_key(""), "");
        assert_eq!(resolve_offset_ms(None, -40), -40);
        assert_eq!(resolve_offset_ms(Some(""), -40), -40);
        assert_eq!(resolve_offset_ms(Some("250"), -40), 250);
        assert_eq!(resolve_offset_ms(Some("abc"), -40), -40);
    }

    #[test]
    fn select_line_synced_and_plain() {
        let l = Lyrics { mode: "synced".into(), synced: parse_lrc("[00:01.00]a
[00:02.00]a
[00:03.00]b"), plain: vec![], source: String::new() };
        assert_eq!(select_line(&l, 500, 10_000), (String::new(), String::new()));
        assert_eq!(select_line(&l, 1500, 10_000), ("a".into(), "b".into()));
        let p = Lyrics { mode: "plain".into(), synced: vec![], plain: vec!["x".into(), "y".into()], source: String::new() };
        assert_eq!(select_line(&p, 0, 10_000), ("x".into(), "y".into()));
        assert_eq!(select_line(&p, 9_999, 10_000), ("y".into(), "y".into()));
    }

    #[test]
    fn instrumental_gaps_intro_mid_outro() {
        let mk = |v: &[i64]| v.iter().map(|&s| Line { start_ms: s, words: "w".into() }).collect::<Vec<_>>();
        // lines every 3 s, a 20 s break after line at 21 s, intro 12 s, outro 30 s
        let mut starts: Vec<i64> = (0..4).map(|i| 12_000 + i * 3_000).collect();
        starts.push(41_000);
        starts.extend((1..5).map(|i| 41_000 + i * 3_000));
        let s = mk(&starts);
        let g = instrumental_gaps(&s, 83_000);
        assert_eq!(g[0], Gap { start_ms: 0, end_ms: 12_000, gap_ms: 12_000, key: -2 });
        assert_eq!(g[1], Gap { start_ms: 24_000, end_ms: 41_000, gap_ms: 20_000, key: 3 });
        assert_eq!(g[2], Gap { start_ms: 56_000, end_ms: 83_000, gap_ms: 30_000, key: -3 });
        assert!(instrumental_gaps(&mk(&[0, 3000, 6000]), 9000).is_empty());
        assert!(instrumental_gaps(&[], 9000).is_empty());
    }
}
