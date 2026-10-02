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
            s.push_str(&format!("{k} = {}\n", v.replace('\n', "\n\t")));
        }
        s.push('\n');
    }
    s
}

impl Config {
    pub fn open(dir: &Path) -> Self {
        let path = dir.join("statusify.cfg");
        let text = std::fs::read_to_string(&path).unwrap_or_default();
        Config { path, data: Mutex::new(parse(&text)) }
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
        let tmp = self.path.with_extension("cfg.tmp");
        if std::fs::write(&tmp, text).is_ok() {
            let _ = std::fs::rename(&tmp, &self.path);
        }
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
        let tmp = self.path.with_extension("cfg.tmp");
        if std::fs::write(&tmp, text).is_ok() {
            let _ = std::fs::rename(&tmp, &self.path);
        }
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
}
