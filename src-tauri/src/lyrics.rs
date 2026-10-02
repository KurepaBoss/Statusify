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
}
