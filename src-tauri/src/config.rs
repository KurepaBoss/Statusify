//! statusify.cfg — the same INI file Python's configparser writes, so both
//! apps share settings. Keys are case-insensitive (stored lowercase), values
//! are strings; typed getters parse them the way the Python app did.
//!
//! Every set() rewrites the file (temp file + rename), so a crash never leaves
//! it half-written. Sections and keys keep their order.

use std::path::{Path, PathBuf};
use std::sync::Mutex;

type Section = (String, Vec<(String, String)>);

pub struct Config {
    path: PathBuf,
    data: Mutex<Vec<Section>>,
    /// The file existed but could not be read (sharing violation, permissions).
    /// Like Python's "Config unreadable - using defaults", the app runs on
    /// defaults, but nothing is ever written back over the unread file.
    read_failed: bool,
}

/// Windows-1252 bytes 0x80..=0x9F as characters. The five holes (81 8D 8F 90 9D)
/// map to the same code point, so the round trip stays lossless.
const CP1252_HIGH: [char; 32] = [
    '\u{20AC}', '\u{81}', '\u{201A}', '\u{192}', '\u{201E}', '\u{2026}', '\u{2020}', '\u{2021}', '\u{2C6}', '\u{2030}', '\u{160}', '\u{2039}', '\u{152}', '\u{8D}',
    '\u{17D}', '\u{8F}', '\u{90}', '\u{2018}', '\u{2019}', '\u{201C}', '\u{201D}', '\u{2022}', '\u{2013}', '\u{2014}', '\u{2DC}', '\u{2122}', '\u{161}', '\u{203A}',
    '\u{153}', '\u{9D}', '\u{17E}', '\u{178}',
];

/// Bytes read as Windows-1252 (Python's fallback when a file is not UTF-8).
pub fn decode_cp1252(bytes: &[u8]) -> String {
    bytes.iter().map(|&b| if (0x80..0xA0).contains(&b) { CP1252_HIGH[(b - 0x80) as usize] } else { b as char }).collect()
}

fn encode_cp1252(text: &str) -> Option<Vec<u8>> {
    text.chars()
        .map(|c| match c as u32 {
            0..=0x7F | 0xA0..=0xFF => Some(c as u32 as u8),
            _ => CP1252_HIGH.iter().position(|&h| h == c).map(|i| 0x80 + i as u8),
        })
        .collect()
}

/// Undo UTF-8-read-as-cp1252 corruption, however many layers deep (port of
/// statusify_config.unmojibake). Only accepted when the round trip is exact
/// and the result is shorter, so genuine accented text is left alone.
pub fn unmojibake(text: &str) -> String {
    let mut text = text.to_string();
    for _ in 0..6 {
        let Some(bytes) = encode_cp1252(&text) else { break };
        let Ok(fixed) = String::from_utf8(bytes) else { break };
        if fixed == text || fixed.chars().count() >= text.chars().count() {
            break;
        }
        text = fixed;
    }
    text
}

/// Decode a config file: UTF-8 (BOM tolerated), else Windows-1252.
fn decode_file(bytes: &[u8]) -> String {
    let bytes = bytes.strip_prefix(&[0xEF, 0xBB, 0xBF]).unwrap_or(bytes);
    match std::str::from_utf8(bytes) {
        Ok(s) => s.to_string(),
        Err(_) => decode_cp1252(bytes),
    }
}

/// configparser's BasicInterpolation: "%%" in the file is a literal "%".
fn unescape_pct(v: &str) -> String {
    v.replace("%%", "%")
}

fn escape_pct(v: &str) -> String {
    v.replace('%', "%%")
}

fn parse(text: &str) -> Vec<Section> {
    let mut out: Vec<Section> = Vec::new();
    for raw in text.lines() {
        let line = raw.trim_end();
        let t = line.trim_start();
        if t.is_empty() || t.starts_with('#') || t.starts_with(';') {
            continue;
        }
        if t.starts_with('[') && t.ends_with(']') {
            out.push((t[1..t.len() - 1].trim().to_string(), Vec::new()));
            continue;
        }
        // configparser continuation lines (indented) extend the last value.
        if line.starts_with([' ', '\t']) {
            if let Some((_, kv)) = out.last_mut() {
                if let Some((_, v)) = kv.last_mut() {
                    v.push('\n');
                    v.push_str(t);
                    continue;
                }
            }
        }
        let Some(i) = t.find(['=', ':']) else { continue };
        let (k, v) = (t[..i].trim().to_lowercase(), t[i + 1..].trim().to_string());
        if out.is_empty() {
            out.push((String::new(), Vec::new()));
        }
        out.last_mut().unwrap().1.push((k, v));
    }
    out
}

fn render(data: &[Section]) -> String {
    let mut s = String::new();
    for (name, kv) in data {
        s.push_str(&format!("[{name}]\n"));
        for (k, v) in kv {
            s.push_str(&format!("{k} = {}\n", escape_pct(v).replace('\n', "\n\t")));
        }
        s.push('\n');
    }
    s
}

impl Config {
    pub fn open(dir: &Path) -> Self {
        let path = dir.join("statusify.cfg");
        let mut bytes = Vec::new();
        let mut read_failed = false;
        // A sharing violation is usually over in a few milliseconds.
        for attempt in 0..4 {
            match std::fs::read(&path) {
                Ok(b) => {
                    bytes = b;
                    read_failed = false;
                    break;
                }
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => break,
                Err(e) => {
                    read_failed = true;
                    if attempt == 3 {
                        crate::log(&format!("Config unreadable ({e}) - using defaults; the file will not be overwritten"));
                    } else {
                        std::thread::sleep(std::time::Duration::from_millis(60));
                    }
                }
            }
        }
        let mut data = parse(&decode_file(&bytes));
        // Repair double-encoded text values, then drop interpolation escapes.
        let mut repaired = false;
        for (_, kv) in data.iter_mut() {
            for (_, v) in kv.iter_mut() {
                let fixed = unmojibake(v);
                if fixed != *v {
                    *v = fixed;
                    repaired = true;
                }
                *v = unescape_pct(v);
            }
        }
        let cfg = Config { path, data: Mutex::new(data), read_failed };
        if repaired {
            crate::log("Config: repaired garbled (double-encoded) text values");
            cfg.save();
        }
        cfg
    }

    /// Write the file (temp + rename) unless it was never read successfully.
    fn save(&self) {
        let text = render(&self.data.lock().unwrap());
        self.write_text(&text);
    }

    fn write_text(&self, text: &str) {
        if self.read_failed {
            return;
        }
        let tmp = self.path.with_extension("cfg.tmp");
        if std::fs::write(&tmp, text).is_ok() {
            let _ = std::fs::rename(&tmp, &self.path);
        }
    }

    pub fn get(&self, section: &str, key: &str) -> Option<String> {
        let key = key.to_lowercase();
        let d = self.data.lock().unwrap();
        d.iter().find(|(n, _)| n == section)?.1.iter().find(|(k, _)| *k == key).map(|(_, v)| v.clone())
    }

    pub fn get_or(&self, section: &str, key: &str, default: &str) -> String {
        self.get(section, key).unwrap_or_else(|| default.to_string())
    }

    /// "true"/"1"/"yes"/"on" (case-insensitive), as configparser's getboolean.
    pub fn get_bool(&self, section: &str, key: &str, default: bool) -> bool {
        match self.get(section, key).map(|v| v.trim().to_lowercase()) {
            Some(v) if ["true", "1", "yes", "on"].contains(&v.as_str()) => true,
            Some(v) if ["false", "0", "no", "off"].contains(&v.as_str()) => false,
            _ => default,
        }
    }

    pub fn get_i64(&self, section: &str, key: &str, default: i64) -> i64 {
        self.get(section, key).and_then(|v| v.trim().parse::<f64>().ok()).map_or(default, |f| f as i64)
    }

    pub fn get_f64(&self, section: &str, key: &str, default: f64) -> f64 {
        self.get(section, key).and_then(|v| v.trim().parse().ok()).unwrap_or(default)
    }

    /// Whole section as (key, value) pairs.
    pub fn section(&self, section: &str) -> Vec<(String, String)> {
        let d = self.data.lock().unwrap();
        d.iter().find(|(n, _)| n == section).map(|(_, kv)| kv.clone()).unwrap_or_default()
    }

    pub fn set(&self, section: &str, key: &str, value: &str) {
        let key = key.to_lowercase();
        let text = {
            let mut d = self.data.lock().unwrap();
            let idx = match d.iter().position(|(n, _)| n == section) {
                Some(i) => i,
                None => {
                    d.push((section.to_string(), Vec::new()));
                    d.len() - 1
                }
            };
            let kv = &mut d[idx].1;
            match kv.iter_mut().find(|(k, _)| *k == key) {
                Some(e) => e.1 = value.to_string(),
                None => kv.push((key, value.to_string())),
            }
            render(&d)
        };
        self.write_text(&text);
    }

    pub fn remove(&self, section: &str, key: &str) {
        let key = key.to_lowercase();
        let text = {
            let mut d = self.data.lock().unwrap();
            if let Some((_, kv)) = d.iter_mut().find(|(n, _)| n == section) {
                kv.retain(|(k, _)| *k != key);
            }
            render(&d)
        };
        self.write_text(&text);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reads_python_file_and_round_trips() {
        let d = std::env::temp_dir().join(format!("statusify-cfg-{}", crate::state::now_ms()));
        std::fs::create_dir_all(&d).unwrap();
        std::fs::write(d.join("statusify.cfg"), "[profiles]\n\n[window]\ngeometry = 520x720+700+151\n\n[preferences]\nDark_Mode = True\nblacklist = a\n\tb\n").unwrap();
        let c = Config::open(&d);
        assert_eq!(c.get("window", "geometry").as_deref(), Some("520x720+700+151"));
        assert!(c.get_bool("preferences", "dark_mode", false));
        assert_eq!(c.get("preferences", "blacklist").as_deref(), Some("a\nb"));
        assert_eq!(c.get_i64("preferences", "lyric_delay_ms", -40), -40);
        c.set("preferences", "lyric_delay_ms", "120");
        c.set("new", "k", "v");
        let c2 = Config::open(&d);
        assert_eq!(c2.get_i64("preferences", "lyric_delay_ms", 0), 120);
        assert_eq!(c2.get("new", "k").as_deref(), Some("v"));
        assert_eq!(c2.get("preferences", "blacklist").as_deref(), Some("a\nb"));
        let _ = std::fs::remove_dir_all(d);
    }

    fn tmpdir(tag: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("statusify-cfg-{tag}-{}", crate::state::now_ms()));
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    #[test]
    fn legacy_cp1252_file_is_read_not_blanked() {
        let d = tmpdir("cp1252");
        // 0xE9 and a curly apostrophe (0x92) are not valid UTF-8.
        let mut bytes = b"[preferences]\ninstrumental_text = caf".to_vec();
        bytes.extend([0xE9, b' ', 0x92, b'\n']);
        bytes.extend(b"dark_mode = false\n");
        std::fs::write(d.join("statusify.cfg"), bytes).unwrap();
        let c = Config::open(&d);
        assert_eq!(c.get("preferences", "instrumental_text").as_deref(), Some("caf\u{e9} \u{2019}"));
        assert!(!c.get_bool("preferences", "dark_mode", true));
        c.set("preferences", "x", "1");
        // Saved back as UTF-8 with every other key kept.
        let c2 = Config::open(&d);
        assert!(!c2.get_bool("preferences", "dark_mode", true));
        assert_eq!(c2.get("preferences", "instrumental_text").as_deref(), Some("caf\u{e9} \u{2019}"));
        let _ = std::fs::remove_dir_all(d);
    }

    #[test]
    fn unreadable_file_is_never_overwritten() {
        // A directory where the file should be: the read fails with something other than NotFound.
        let d = tmpdir("unreadable");
        std::fs::create_dir_all(d.join("statusify.cfg")).unwrap();
        let c = Config::open(&d);
        assert!(c.read_failed);
        c.set("preferences", "k", "v");
        assert_eq!(c.get("preferences", "k").as_deref(), Some("v"), "in-memory value still works");
        assert!(d.join("statusify.cfg").is_dir(), "nothing written over it");
        assert!(!d.join("statusify.cfg.tmp").exists());
        let _ = std::fs::remove_dir_all(d);
    }

    #[test]
    fn missing_file_starts_empty_and_is_writable() {
        let d = tmpdir("missing");
        let c = Config::open(&d);
        assert!(!c.read_failed);
        c.set("preferences", "k", "v");
        assert_eq!(Config::open(&d).get("preferences", "k").as_deref(), Some("v"));
        let _ = std::fs::remove_dir_all(d);
    }

    #[test]
    fn mojibake_is_repaired_on_load_and_saved() {
        let d = tmpdir("mojibake");
        // The note emoji double-encoded: its UTF-8 bytes read as cp1252, twice.
        let note = "\u{1F3B5}";
        let once = decode_cp1252(note.as_bytes());
        assert_ne!(once, note);
        let twice = decode_cp1252(once.as_bytes());
        std::fs::write(d.join("statusify.cfg"), format!("[preferences]\ninstrumental_text = {twice}\nname = Beyonc\u{e9}\n")).unwrap();
        let c = Config::open(&d);
        assert_eq!(c.get("preferences", "instrumental_text").as_deref(), Some(note));
        // Genuine accents survive.
        assert_eq!(c.get("preferences", "name").as_deref(), Some("Beyonc\u{e9}"));
        let on_disk = std::fs::read_to_string(d.join("statusify.cfg")).unwrap();
        assert!(on_disk.contains(&format!("instrumental_text = {note}")), "{on_disk}");
        let _ = std::fs::remove_dir_all(d);
    }

    #[test]
    fn percent_is_escaped_like_configparser() {
        let d = tmpdir("pct");
        std::fs::write(d.join("statusify.cfg"), "[preferences]\nblacklist = 100%% pure\n").unwrap();
        let c = Config::open(&d);
        assert_eq!(c.get("preferences", "blacklist").as_deref(), Some("100% pure"));
        c.set("preferences", "instrumental_text", "50% off");
        let raw = std::fs::read_to_string(d.join("statusify.cfg")).unwrap();
        assert!(raw.contains("instrumental_text = 50%% off"), "{raw}");
        assert!(raw.contains("blacklist = 100%% pure"), "{raw}");
        let c2 = Config::open(&d);
        assert_eq!(c2.get("preferences", "instrumental_text").as_deref(), Some("50% off"));
        let _ = std::fs::remove_dir_all(d);
    }

    #[test]
    fn utf8_bom_is_tolerated() {
        let d = tmpdir("bom");
        let mut b = vec![0xEF, 0xBB, 0xBF];
        b.extend(b"[window]\ngeometry = 1x1+0+0\n");
        std::fs::write(d.join("statusify.cfg"), b).unwrap();
        assert_eq!(Config::open(&d).get("window", "geometry").as_deref(), Some("1x1+0+0"));
        let _ = std::fs::remove_dir_all(d);
    }
}
