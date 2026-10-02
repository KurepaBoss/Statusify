//! Settings page backend (owner: shell agent).
//!
//! The page writes plain statusify.cfg keys, the same ones the Python app
//! used, through one validated entry point (`set`). Every write ends in
//! `engine.config_changed()` so features re-read their settings live.
//!
//! Actions: get_all, set {key, value}, set_many {values}, set_collapsed
//! {id, collapsed}, read_log {lines}, fonts, languages.

use super::{Ctx, Feature};
use serde_json::{json, Map, Value};
use std::io::{Read, Seek, SeekFrom};
use std::path::Path;
use std::sync::Arc;

#[derive(Default)]
pub struct Settings;

#[derive(Clone, Copy)]
pub enum Kind {
    Bool,
    Int(i64, i64),
    Choice(&'static [&'static str]),
    /// Free text; empty falls back to the default.
    Text,
    /// "#rrggbb"
    Color,
    /// Several lines, stored with a literal backslash-n like Python did.
    Lines,
}

pub struct Def {
    pub key: &'static str,
    pub section: &'static str,
    pub kind: Kind,
    pub default: &'static str,
}

const fn d(section: &'static str, key: &'static str, kind: Kind, default: &'static str) -> Def {
    Def { key, section, kind, default }
}

pub const INSTRUMENTAL_DEFAULT: &str = "🎵 ─ ─ ─ ─ ─ ─ ─ ─ ─ 🎵";
pub const DEFAULT_FONT: &str = "Segoe UI";

/// (code, display name) — the same list as statusify_translate.LANGUAGES.
pub const LANGUAGES: [(&str, &str); 21] = [
    ("auto", "Auto"), ("en", "English"), ("es", "Spanish"), ("fr", "French"), ("de", "German"), ("it", "Italian"),
    ("pt", "Portuguese"), ("nl", "Dutch"), ("pl", "Polish"), ("sr", "Serbian"), ("hr", "Croatian"), ("ru", "Russian"),
    ("uk", "Ukrainian"), ("tr", "Turkish"), ("ar", "Arabic"), ("hi", "Hindi"), ("id", "Indonesian"), ("ja", "Japanese"),
    ("ko", "Korean"), ("zh-CN", "Chinese"), ("sv", "Swedish"),
];

pub static DEFS: &[Def] = &[
    // Lyrics
    d("preferences", "lrclib_fallback", Kind::Bool, "true"),
    d("preferences", "lyric_subline", Kind::Choice(&["off", "rom", "tr", "both"]), "off"),
    d("preferences", "translate_to", Kind::Text, "auto"),
    d("preferences", "lyric_font", Kind::Text, DEFAULT_FONT),
    d("preferences", "lyric_font_boost", Kind::Int(-2, 10), "0"),
    d("preferences", "beat_react", Kind::Bool, "true"),
    d("preferences", "overlay_enabled", Kind::Bool, "false"),
    d("preferences", "overlay_next_line", Kind::Bool, "true"),
    d("preferences", "overlay_size", Kind::Int(16, 72), "30"),
    d("window", "overlay_locked", Kind::Bool, "true"),
    // Appearance
    d("preferences", "dark_mode", Kind::Bool, "true"),
    d("preferences", "album_tint", Kind::Bool, "true"),
    d("preferences", "accent_color", Kind::Color, "#1db954"),
    d("preferences", "animations", Kind::Bool, "true"),
    d("preferences", "render_quality", Kind::Choice(&["auto", "high", "low"]), "auto"),
    // Window
    d("preferences", "always_on_top", Kind::Bool, "false"),
    d("preferences", "close_to_tray", Kind::Bool, "false"),
    d("preferences", "start_minimized", Kind::Bool, "false"),
    // Discord
    d("preferences", "show_paused_rpc", Kind::Bool, "false"),
    d("preferences", "status_shows_song", Kind::Bool, "true"),
    d("preferences", "link_track", Kind::Bool, "true"),
    d("preferences", "listen_button", Kind::Bool, "true"),
    d("preferences", "instrumental_text", Kind::Text, INSTRUMENTAL_DEFAULT),
    // History and privacy
    d("preferences", "save_history", Kind::Bool, "true"),
    d("preferences", "blacklist", Kind::Lines, ""),
];

pub fn def(key: &str) -> Option<&'static Def> {
    DEFS.iter().find(|x| x.key == key)
}

fn is_hex_colour(t: &str) -> bool {
    t.len() == 7 && t.starts_with('#') && t[1..].chars().all(|c| c.is_ascii_hexdigit())
}

/// Check `value` against the setting's type and return the string to store.
pub fn normalize(def: &Def, value: &Value) -> Result<String, String> {
    let text = match value {
        Value::String(s) => s.clone(),
        Value::Bool(b) => b.to_string(),
        Value::Number(n) => n.to_string(),
        Value::Null => String::new(),
        _ => return Err(format!("{}: unsupported value", def.key)),
    };
    match def.kind {
        Kind::Bool => match text.trim().to_lowercase().as_str() {
            "true" | "1" | "yes" | "on" => Ok("true".into()),
            "false" | "0" | "no" | "off" => Ok("false".into()),
            _ => Err(format!("{}: expected true or false", def.key)),
        },
        Kind::Int(lo, hi) => {
            let n = text.trim().parse::<f64>().map_err(|_| format!("{}: expected a number", def.key))?;
            Ok((n.round() as i64).clamp(lo, hi).to_string())
        }
        Kind::Choice(opts) => {
            let t = text.trim().to_lowercase();
            if opts.contains(&t.as_str()) {
                Ok(t)
            } else {
                Err(format!("{}: must be one of {}", def.key, opts.join(", ")))
            }
        }
        Kind::Text => {
            let t = text.trim().to_string();
            if def.key == "translate_to" && !t.is_empty() && !LANGUAGES.iter().any(|(c, _)| *c == t) {
                return Err(format!("translate_to: unknown language '{t}'"));
            }
            if def.key == "lyric_font" {
                return Ok(validate_font(&t, &fonts_cached()));
            }
            Ok(if t.is_empty() { def.default.to_string() } else { t })
        }
        Kind::Color => {
            let t = text.trim().to_lowercase();
            if is_hex_colour(&t) {
                Ok(t)
            } else {
                Err(format!("{}: expected a colour like #1db954", def.key))
            }
        }
        // Python: raw.replace("\n", "\\n") on the stripped text.
        Kind::Lines => Ok(text.replace("\r\n", "\n").trim().replace('\n', "\\n")),
    }
}

/// The line Python logged when a setting flipped, if it logged one.
pub fn toggle_log(key: &str, on: bool) -> Option<String> {
    let word = if on { "enabled" } else { "disabled" };
    Some(match key {
        "animations" => format!("Smooth animations {word}"),
        "show_paused_rpc" => format!("Paused RPC {word}"),
        "save_history" => format!("Session history {word}"),
        "always_on_top" => format!("Always on top {word}"),
        _ => return None,
    })
}

/// Store one validated setting, with the side effects Python's toggles had:
/// the log line, and unlocking a hidden overlay switches it on.
fn apply(ctx: &Arc<Ctx>, def: &Def, stored: &str) {
    let was = ctx.config.get_bool(def.section, def.key, def.default == "true");
    if def.key == "overlay_locked" {
        crate::shell_window::set_overlay_locked(ctx, stored == "true");
        return;
    }
    ctx.config.set(def.section, def.key, stored);
    if matches!(def.kind, Kind::Bool) {
        let now = stored == "true";
        if now != was {
            if let Some(line) = toggle_log(def.key, now) {
                crate::log(&line);
            }
        }
    }
}

/// The value as the page wants it (typed, defaults applied).
fn read(ctx: &Ctx, def: &Def) -> Value {
    let raw = ctx.config.get(def.section, def.key);
    match def.kind {
        Kind::Bool => json!(ctx.config.get_bool(def.section, def.key, def.default == "true")),
        Kind::Int(lo, hi) => {
            let dflt: i64 = def.default.parse().unwrap_or(0);
            json!(ctx.config.get_i64(def.section, def.key, dflt).clamp(lo, hi))
        }
        Kind::Choice(opts) => {
            let v = raw.map(|v| v.trim().to_lowercase()).unwrap_or_default();
            json!(if opts.contains(&v.as_str()) { v } else { def.default.to_string() })
        }
        Kind::Text => {
            let v = raw.map(|v| v.trim().to_string()).unwrap_or_default();
            if def.key == "lyric_font" {
                return json!(validate_font(&v, &fonts_cached()));
            }
            json!(if v.is_empty() { def.default.to_string() } else { v })
        }
        Kind::Color => {
            let v = raw.map(|v| v.trim().to_lowercase()).unwrap_or_default();
            json!(if is_hex_colour(&v) { v } else { def.default.to_string() })
        }
        // Shown one term per line; the file holds literal "\n" separators.
        Kind::Lines => json!(raw.unwrap_or_default().replace("\\n", "\n")),
    }
}

/// Every setting, typed with defaults applied (also what "settings-changed" carries).
pub fn all(ctx: &Ctx) -> Value {
    let mut m = Map::new();
    for def in DEFS {
        m.insert(def.key.to_string(), read(ctx, def));
    }
    m.insert("collapsed".into(), json!(collapsed(&ctx.config)));
    Value::Object(m)
}

/// Comma-separated list of collapsed section ids. A missing key means the
/// log is collapsed (Python's default); an empty value means none are.
pub fn collapsed(cfg: &crate::config::Config) -> Vec<String> {
    cfg.get("ui", "collapsed_sections")
        .unwrap_or_else(|| "log".into())
        .split(',')
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .collect()
}

/// installed_fonts(), remembered for a minute: the settings are read on every
/// change and the Fonts folder holds hundreds of files.
fn fonts_cached() -> Vec<String> {
    static CACHE: std::sync::Mutex<Option<(std::time::Instant, Vec<String>)>> = std::sync::Mutex::new(None);
    let mut c = CACHE.lock().unwrap();
    if let Some((at, v)) = c.as_ref() {
        if at.elapsed() < std::time::Duration::from_secs(60) {
            return v.clone();
        }
    }
    let v = installed_fonts();
    *c = Some((std::time::Instant::now(), v.clone()));
    v
}

/// Installed lyric fonts, the default first (statusify_textrender.available_families).
pub fn installed_fonts() -> Vec<String> {
    let mut dirs = vec![std::path::PathBuf::from(std::env::var_os("WINDIR").unwrap_or_else(|| r"C:\Windows".into())).join("Fonts")];
    if let Some(l) = std::env::var_os("LOCALAPPDATA") {
        dirs.push(std::path::PathBuf::from(l).join("Microsoft").join("Windows").join("Fonts"));
    }
    let mut names: Vec<String> = Vec::new();
    for dir in dirs {
        if let Ok(rd) = std::fs::read_dir(dir) {
            names.extend(rd.flatten().map(|e| e.file_name().to_string_lossy().to_lowercase()));
        }
    }
    fonts_from_files(&names)
}

pub fn fonts_from_files(files_lower: &[String]) -> Vec<String> {
    let font_ext = |f: &str| f.ends_with(".ttf") || f.ends_with(".otf") || f.ends_with(".ttc");
    let has = |pred: &dyn Fn(&str) -> bool| files_lower.iter().any(|f| font_ext(f) && pred(f));
    let mut out = vec![DEFAULT_FONT.to_string()];
    // "Segoe UI Variable" is left out: indistinguishable from the default at lyric sizes.
    if has(&|f| f == "bahnschrift.ttf") {
        out.push("Bahnschrift".into());
    }
    if has(&|f| f == "georgia.ttf") {
        out.push("Georgia".into());
    }
    if has(&|f| f == "consola.ttf") {
        out.push("Consolas".into());
    }
    // The same file patterns as statusify_textrender.FAMILIES["..."]["regular"].
    if has(&|f| glob_match("lexend-regular.*", f) || glob_match("lexend*wght*.ttf", f)) {
        out.push("Lexend".into());
    }
    if has(&|f| glob_match("opendyslexic-regular.*", f) || glob_match("opendyslexic*regular.*", f)) {
        out.push("OpenDyslexic".into());
    }
    out
}

/// Shell-style match where `*` is any run of characters (both sides lowercase).
pub fn glob_match(pattern: &str, name: &str) -> bool {
    let parts: Vec<&str> = pattern.split('*').collect();
    let mut rest = name;
    for (i, part) in parts.iter().enumerate() {
        if i == 0 {
            match rest.strip_prefix(part) {
                Some(r) => rest = r,
                None => return false,
            }
        } else if i == parts.len() - 1 {
            return rest.ends_with(part) && rest.len() >= part.len();
        } else {
            match rest.find(part) {
                Some(p) => rest = &rest[p + part.len()..],
                None => return false,
            }
        }
    }
    rest.is_empty()
}

/// Python's lyric_font(): a name that is not an installed family is the default.
pub fn validate_font(name: &str, installed: &[String]) -> String {
    let n = name.trim();
    installed.iter().find(|f| f.as_str() == n).cloned().unwrap_or_else(|| DEFAULT_FONT.to_string())
}

/// Last `n` lines of the log file as {ts, msg, tag}. Tags follow the Python
/// log view: g = RPC news, y = warnings/errors, m = everything else.
pub fn read_log(path: &Path, n: usize) -> Vec<Value> {
    let Ok(mut f) = std::fs::File::open(path) else { return vec![] };
    let len = f.metadata().map(|m| m.len()).unwrap_or(0);
    let take = len.min(96 * 1024);
    let mut buf = Vec::new();
    if f.seek(SeekFrom::Start(len - take)).is_err() || f.read_to_end(&mut buf).is_err() {
        return vec![];
    }
    let text = String::from_utf8_lossy(&buf);
    let mut lines: Vec<&str> = text.lines().collect();
    if take < len && !lines.is_empty() {
        lines.remove(0); // probably cut mid-line
    }
    let start = lines.len().saturating_sub(n);
    lines[start..].iter().filter(|l| !l.trim().is_empty()).map(|l| log_line(l)).collect()
}

pub fn log_line(line: &str) -> Value {
    // "2026-10-03 12:34:56  message"
    let b = line.as_bytes();
    let stamped = line.len() >= 19 && b[4] == b'-' && b[7] == b'-' && b[13] == b':' && line.is_char_boundary(19);
    let (ts, msg) = if stamped { (line[11..19].to_string(), line[19..].trim_start().to_string()) } else { (String::new(), line.to_string()) };
    let low = msg.to_lowercase();
    let tag = if msg.contains("RPC") && !["error", "rate"].iter().any(|x| low.contains(x)) {
        "g"
    } else if ["rate", "drop", "error", "warn"].iter().any(|x| low.contains(x)) {
        "y"
    } else {
        "m"
    };
    json!({"ts": ts, "msg": msg, "tag": tag})
}

impl Feature for Settings {
    fn name(&self) -> &'static str {
        "settings"
    }

    fn call(&self, ctx: &Arc<Ctx>, action: &str, args: Value) -> Result<Value, String> {
        match action {
            "get_all" => Ok(all(ctx)),
            "set" => {
                let key = args.get("key").and_then(|k| k.as_str()).ok_or("settings.set: missing key")?;
                let def = def(key).ok_or_else(|| format!("settings: unknown setting {key}"))?;
                let stored = normalize(def, args.get("value").unwrap_or(&Value::Null))?;
                apply(ctx, def, &stored);
                ctx.engine.config_changed();
                Ok(read(ctx, def))
            }
            "set_many" => {
                let vals = args.get("values").and_then(|v| v.as_object()).ok_or("settings.set_many: missing values")?;
                // Validate everything first so a bad value changes nothing.
                let mut todo = Vec::new();
                for (k, v) in vals {
                    let def = def(k).ok_or_else(|| format!("settings: unknown setting {k}"))?;
                    todo.push((def, normalize(def, v)?));
                }
                for (def, s) in &todo {
                    apply(ctx, def, s);
                }
                ctx.engine.config_changed();
                Ok(json!(true))
            }
            "set_collapsed" => {
                let id = args.get("id").and_then(|k| k.as_str()).ok_or("missing id")?.trim().to_string();
                let on = args.get("collapsed").and_then(|v| v.as_bool()).unwrap_or(true);
                let mut list = collapsed(&ctx.config);
                list.retain(|x| *x != id);
                if on {
                    list.push(id);
                }
                ctx.config.set("ui", "collapsed_sections", &list.join(","));
                // Like every other write: other windows' copies of the settings catch up.
                ctx.engine.config_changed();
                Ok(json!(list))
            }
            "read_log" => {
                let n = args.get("lines").and_then(|v| v.as_u64()).unwrap_or(200).clamp(1, 1000) as usize;
                Ok(json!(read_log(&ctx.data_dir.join("statusify-rs.log"), n)))
            }
            "fonts" => Ok(json!(installed_fonts())),
            "languages" => Ok(json!(LANGUAGES.iter().map(|(c, n)| json!({"code": c, "name": n})).collect::<Vec<_>>())),
            _ => Err(format!("settings: unknown action {action}")),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn n(key: &str, v: Value) -> Result<String, String> {
        normalize(def(key).unwrap(), &v)
    }

    #[test]
    fn every_default_is_valid_for_its_own_kind() {
        for d in DEFS {
            let r = normalize(d, &json!(d.default));
            assert!(r.is_ok(), "{}: {:?}", d.key, r);
        }
        let mut keys: Vec<_> = DEFS.iter().map(|d| (d.section, d.key)).collect();
        keys.sort();
        keys.dedup();
        assert_eq!(keys.len(), DEFS.len(), "duplicate setting");
    }

    #[test]
    fn bools_and_ints() {
        assert_eq!(n("dark_mode", json!(true)).unwrap(), "true");
        assert_eq!(n("dark_mode", json!("Off")).unwrap(), "false");
        assert!(n("dark_mode", json!("maybe")).is_err());
        assert_eq!(n("lyric_font_boost", json!(99)).unwrap(), "10");
        assert_eq!(n("lyric_font_boost", json!(-7)).unwrap(), "-2");
        assert_eq!(n("overlay_size", json!(2)).unwrap(), "16");
        assert_eq!(n("overlay_size", json!("44")).unwrap(), "44");
        assert!(n("overlay_size", json!("big")).is_err());
    }

    #[test]
    fn choices_colours_text() {
        assert_eq!(n("render_quality", json!("HIGH")).unwrap(), "high");
        assert!(n("render_quality", json!("ultra")).is_err());
        assert_eq!(n("lyric_subline", json!("both")).unwrap(), "both");
        assert_eq!(n("accent_color", json!("#1DB954")).unwrap(), "#1db954");
        assert!(n("accent_color", json!("green")).is_err());
        assert!(n("accent_color", json!("#12345")).is_err());
        assert_eq!(n("instrumental_text", json!("   ")).unwrap(), INSTRUMENTAL_DEFAULT);
        assert_eq!(n("instrumental_text", json!(" ♪ ")).unwrap(), "♪");
        assert_eq!(n("translate_to", json!("zh-CN")).unwrap(), "zh-CN");
        assert!(n("translate_to", json!("xx")).is_err());
        assert_eq!(n("translate_to", json!("")).unwrap(), "auto");
    }

    #[test]
    fn blacklist_uses_python_escaping() {
        assert_eq!(n("blacklist", json!("  one\ntwo\r\nthree \n")).unwrap(), "one\\ntwo\\nthree");
        assert_eq!(n("blacklist", json!("")).unwrap(), "");
    }

    #[test]
    fn fonts_follow_installed_files() {
        let files: Vec<String> = ["segoeui.ttf", "georgia.ttf", "lexend-regular.ttf", "readme.txt", "opendyslexic.txt"].iter().map(|s| s.to_string()).collect();
        assert_eq!(fonts_from_files(&files), vec!["Segoe UI", "Georgia", "Lexend"]);
        assert_eq!(fonts_from_files(&[]), vec!["Segoe UI"]);
    }

    #[test]
    fn lexend_and_opendyslexic_need_the_exact_files_python_looks_for() {
        let f = |names: &[&str]| fonts_from_files(&names.iter().map(|s| s.to_string()).collect::<Vec<_>>());
        // Lexend-Bold alone is not the regular face; Lexend Deca is a different family.
        assert_eq!(f(&["lexend-bold.ttf"]), vec!["Segoe UI"]);
        assert_eq!(f(&["lexenddeca-regular.ttf"]), vec!["Segoe UI"]);
        assert_eq!(f(&["lexend-regular.otf"]), vec!["Segoe UI", "Lexend"]);
        assert_eq!(f(&["lexend[wght].ttf"]), vec!["Segoe UI", "Lexend"]);
        assert_eq!(f(&["lexend-variable-wght.ttf"]), vec!["Segoe UI", "Lexend"]);
        assert_eq!(f(&["lexend[wght].otf"]), vec!["Segoe UI"], "the variable pattern is .ttf only");
        assert_eq!(f(&["opendyslexic-regular.otf"]), vec!["Segoe UI", "OpenDyslexic"]);
        assert_eq!(f(&["opendyslexicalta-regular.otf"]), vec!["Segoe UI", "OpenDyslexic"]);
        assert_eq!(f(&["opendyslexic-bold.otf"]), vec!["Segoe UI"]);
    }

    #[test]
    fn glob_star_matches_runs() {
        assert!(glob_match("a*c", "abc"));
        assert!(glob_match("a*c", "ac"));
        assert!(!glob_match("a*c", "ab"));
        assert!(glob_match("*", ""));
        assert!(glob_match("x.ttf", "x.ttf"));
        assert!(!glob_match("x.ttf", "x.ttfa"));
        assert!(glob_match("l*w*.ttf", "lwx.ttf"));
        assert!(!glob_match("l*w*.ttf", "l.ttf"));
    }

    #[test]
    fn unknown_lyric_font_falls_back_to_the_default() {
        let installed: Vec<String> = ["Segoe UI", "Georgia"].iter().map(|s| s.to_string()).collect();
        assert_eq!(validate_font("Georgia", &installed), "Georgia");
        assert_eq!(validate_font(" Georgia ", &installed), "Georgia");
        assert_eq!(validate_font("Comic Neue", &installed), "Segoe UI");
        assert_eq!(validate_font("", &installed), "Segoe UI");
        assert_eq!(validate_font("georgia", &installed), "Segoe UI", "family names are exact, as in Python");
        // And through the real entry point: garbage never gets stored.
        assert_eq!(n("lyric_font", json!("Definitely Not A Font")).unwrap(), "Segoe UI");
        assert_eq!(n("lyric_font", json!("")).unwrap(), "Segoe UI");
    }

    #[test]
    fn toggle_log_lines_match_python() {
        assert_eq!(toggle_log("animations", true).as_deref(), Some("Smooth animations enabled"));
        assert_eq!(toggle_log("animations", false).as_deref(), Some("Smooth animations disabled"));
        assert_eq!(toggle_log("show_paused_rpc", true).as_deref(), Some("Paused RPC enabled"));
        assert_eq!(toggle_log("save_history", false).as_deref(), Some("Session history disabled"));
        assert_eq!(toggle_log("always_on_top", true).as_deref(), Some("Always on top enabled"));
        assert_eq!(toggle_log("dark_mode", true), None);
    }

    #[test]
    fn percent_signs_survive_the_text_settings() {
        // Stored verbatim; config.rs escapes them as %% in the file (see its tests).
        assert_eq!(n("instrumental_text", json!("100% ♪")).unwrap(), "100% ♪");
        assert_eq!(n("blacklist", json!("50% off\nfoo")).unwrap(), "50% off\\nfoo");
    }

    #[test]
    fn log_lines_are_split_and_tagged() {
        let v = log_line("2026-10-03 12:34:56  RPC handshake OK  ·  kurepa");
        assert_eq!(v["ts"], "12:34:56");
        assert_eq!(v["tag"], "g");
        assert_eq!(log_line("2026-10-03 12:34:56  RPC error: boom")["tag"], "y");
        assert_eq!(log_line("2026-10-03 12:34:56  Dropped a line (rate limit)")["tag"], "y");
        assert_eq!(log_line("2026-10-03 12:34:56  Lyrics (bridge)  ·  synced")["tag"], "m");
        let v = log_line("no timestamp here");
        assert_eq!(v["ts"], "");
        assert_eq!(v["msg"], "no timestamp here");
        assert_eq!(log_line("short")["msg"], "short");
    }

    #[test]
    fn reads_the_tail_of_a_log() {
        let dir = std::env::temp_dir().join(format!("statusify-settings-log-{}", crate::state::now_ms()));
        std::fs::create_dir_all(&dir).unwrap();
        let p = dir.join("statusify-rs.log");
        let text: String = (0..50).map(|i| format!("2026-10-03 10:00:{:02}  line {i}\n", i % 60)).collect();
        std::fs::write(&p, text).unwrap();
        let out = read_log(&p, 5);
        assert_eq!(out.len(), 5);
        assert_eq!(out[4]["msg"], "line 49");
        assert!(read_log(&dir.join("nope.log"), 5).is_empty());
        let _ = std::fs::remove_dir_all(dir);
    }
}
