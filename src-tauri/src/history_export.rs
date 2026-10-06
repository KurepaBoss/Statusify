//! Lyric export (.lrc / .txt) and the Wrapped PNG's file naming, ported from
//! main.py's _export_lyrics / _lrc_timestamp.

use crate::db::FullEntry;
use std::path::{Path, PathBuf};

/// [mm:ss.xx]: negative offsets clamp to zero, hundredths always two digits.
pub fn lrc_timestamp(ms: i64) -> String {
    let ms = ms.max(0);
    let (minutes, rem) = (ms / 60_000, ms % 60_000);
    let (seconds, millis) = (rem / 1000, rem % 1000);
    format!("[{minutes:02}:{seconds:02}.{:02}]", millis / 10)
}

/// "Artist - Title" with the characters Windows rejects removed, <= 120 chars.
pub fn safe_name(artist: &str, title: &str) -> String {
    let artist = if artist.trim().is_empty() { "Unknown" } else { artist.trim() };
    let title = if title.trim().is_empty() { "Unknown" } else { title.trim() };
    let s: String = format!("{artist} - {title}").chars().filter(|c| !"\\/:*?\"<>|".contains(*c)).collect();
    s.trim().chars().take(120).collect()
}

/// (extension, file text). "lrc" needs synced lyrics, otherwise it is a .txt:
/// a header, a blank line, then the lyrics (synced words, or the plain lines).
pub fn render(e: &FullEntry, fmt: &str) -> (&'static str, String) {
    let artist = if e.entry.artist.trim().is_empty() { "Unknown" } else { e.entry.artist.trim() };
    let title = if e.entry.title.trim().is_empty() { "Unknown" } else { e.entry.title.trim() };
    let mut lines: Vec<String> = Vec::new();
    let ext = if fmt == "lrc" && !e.synced.is_empty() {
        lines.push(format!("[ar:{artist}]"));
        lines.push(format!("[ti:{title}]"));
        lines.push("[re:Statusify]".into());
        for ln in &e.synced {
            lines.push(format!("{}{}", lrc_timestamp(ln.start_ms), ln.words));
        }
        "lrc"
    } else {
        lines.push(format!("{artist} — {title}"));
        lines.push(String::new());
        if e.synced.is_empty() {
            lines.extend(e.plain.iter().cloned());
        } else {
            lines.extend(e.synced.iter().map(|l| l.words.clone()));
        }
        "txt"
    };
    (ext, lines.join("\n") + "\n")
}

/// Write the export into `<data_dir>/exports/`; returns the file written.
pub fn export(data_dir: &Path, e: &FullEntry, fmt: &str) -> Result<PathBuf, String> {
    let dir = data_dir.join("exports");
    std::fs::create_dir_all(&dir).map_err(|err| err.to_string())?;
    let (ext, text) = render(e, fmt);
    let path = dir.join(format!("{}.{ext}", safe_name(&e.entry.artist, &e.entry.title)));
    std::fs::write(&path, text).map_err(|err| err.to_string())?;
    Ok(path)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::Entry;
    use crate::lyrics::Line;

    fn entry(synced: Vec<(i64, &str)>, plain: Vec<&str>) -> FullEntry {
        FullEntry {
            entry: Entry {
                id: 1,
                track_uri: "spotify:track:1".into(),
                artist: "AC/DC".into(),
                title: "Who Made Who?".into(),
                album_art: String::new(),
                played_at: "2026-09-01T10:00:00".into(),
                listened_ms: 0,
                mode: "synced".into(),
                synced_n: synced.len() as i64,
                plain_n: plain.len() as i64,
            },
            synced: synced.into_iter().map(|(s, w)| Line { start_ms: s, words: w.into() }).collect(),
            plain: plain.into_iter().map(String::from).collect(),
        }
    }

    #[test]
    fn lrc_timestamps() {
        assert_eq!(lrc_timestamp(0), "[00:00.00]");
        assert_eq!(lrc_timestamp(450), "[00:00.45]");
        assert_eq!(lrc_timestamp(12_340), "[00:12.34]");
        assert_eq!(lrc_timestamp(61_000), "[01:01.00]");
        assert_eq!(lrc_timestamp(605_000), "[10:05.00]");
        assert_eq!(lrc_timestamp(-500), "[00:00.00]");
        assert_eq!(lrc_timestamp(50), "[00:00.05]");
        assert_eq!(lrc_timestamp(3_599_999), "[59:59.99]");
    }

    #[test]
    fn safe_names() {
        let n = safe_name("AC/DC", "Who Made Who?");
        assert!(!n.contains(['\\', '/', ':', '*', '?', '"', '<', '>', '|']));
        assert_eq!(safe_name("Rick Astley", "Never Gonna Give You Up"), "Rick Astley - Never Gonna Give You Up");
        assert_eq!(safe_name(&"a".repeat(200), &"b".repeat(200)).chars().count(), 120);
        assert!(safe_name("Björk", "Jóga").contains("Björk"));
        assert_eq!(safe_name("", ""), "Unknown - Unknown");
    }

    #[test]
    fn lrc_has_header_and_timestamps() {
        let (ext, text) = render(&entry(vec![(0, "first"), (4000, "second")], vec![]), "lrc");
        assert_eq!(ext, "lrc");
        assert_eq!(text, "[ar:AC/DC]\n[ti:Who Made Who?]\n[re:Statusify]\n[00:00.00]first\n[00:04.00]second\n");
    }

    #[test]
    fn txt_uses_words_and_lrc_falls_back_without_synced() {
        let (ext, text) = render(&entry(vec![(0, "first"), (4000, "second")], vec![]), "txt");
        assert_eq!((ext, text.as_str()), ("txt", "AC/DC — Who Made Who?\n\nfirst\nsecond\n"));
        let (ext, text) = render(&entry(vec![], vec!["a", "b"]), "lrc");
        assert_eq!((ext, text.as_str()), ("txt", "AC/DC — Who Made Who?\n\na\nb\n"));
    }

    #[test]
    fn export_writes_into_exports_folder() {
        let d = std::env::temp_dir().join(format!("statusify-export-{}", crate::state::now_ms()));
        let p = export(&d, &entry(vec![(0, "x")], vec![]), "lrc").unwrap();
        assert!(p.ends_with("exports/ACDC - Who Made Who.lrc") || p.ends_with("exports\\ACDC - Who Made Who.lrc"));
        assert!(std::fs::read_to_string(&p).unwrap().contains("[00:00.00]x"));
        let _ = std::fs::remove_dir_all(d);
    }
}
