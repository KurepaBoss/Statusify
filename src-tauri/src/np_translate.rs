//! Romanisation and translation of lyric lines, ported from
//! statusify_translate.py.
//!
//! Romanisation is attempted only for lines in a non-Latin script. Hangul
//! (Revised Romanization), Cyrillic and Greek are done offline from tables;
//! everything else (Japanese and Chinese included: the optional pykakasi /
//! pypinyin libraries have no equivalent here) takes the transliteration the
//! free Google web endpoint returns next to a translation (dt=rm).
//!
//! Translation is one POST per chunk of lines. Lines are joined with
//! "\n|\n": newlines survive in the translation but are collapsed in the
//! transliteration, while a lone "|" survives both, so the answer can be split
//! back. A chunk whose answer does not split into the right number of pieces
//! is dropped rather than risk a line under the wrong lyric.
//!
//! Results are cached in translations.db (same file and schema as the Python
//! app): keyed by (track uri, target language, sha1 of the lines).

use regex::Regex;
use rusqlite::{params, Connection, OptionalExtension};
use serde_json::{json, Map, Value};
use sha1::{Digest, Sha1};
use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;
use std::sync::LazyLock;

pub const SUBLINE_MODES: [&str; 4] = ["off", "rom", "tr", "both"];

/// (code, name) as the Settings language picker lists them.
pub const LANGUAGES: [(&str, &str); 21] = [
    ("auto", "Auto"), ("en", "English"), ("es", "Spanish"), ("fr", "French"),
    ("de", "German"), ("it", "Italian"), ("pt", "Portuguese"), ("nl", "Dutch"),
    ("pl", "Polish"), ("sr", "Serbian"), ("hr", "Croatian"), ("ru", "Russian"),
    ("uk", "Ukrainian"), ("tr", "Turkish"), ("ar", "Arabic"), ("hi", "Hindi"),
    ("id", "Indonesian"), ("ja", "Japanese"), ("ko", "Korean"),
    ("zh-CN", "Chinese"), ("sv", "Swedish"),
];

// ── Locale ───────────────────────────────────────────────────────

/// 'en_US' -> 'en', 'zh_TW' -> 'zh-TW', 'sr_Latn_RS' -> 'sr'; None -> 'en'.
pub fn locale_to_lang(name: Option<&str>) -> String {
    let Some(name) = name.filter(|n| !n.is_empty()) else { return "en".into() };
    let parts: Vec<&str> = name.split(['_', '-', '.']).collect();
    let base = parts[0].to_lowercase();
    if !(2..=3).contains(&base.len()) || !base.chars().all(|c| c.is_ascii_lowercase()) {
        return "en".into();
    }
    if base == "zh" {
        let tw = parts[1..].iter().any(|p| matches!(p.to_uppercase().as_str(), "TW" | "HK" | "MO" | "HANT"));
        return if tw { "zh-TW".into() } else { "zh-CN".into() };
    }
    if base == "nb" || base == "nn" {
        return "no".into();
    }
    base
}

#[cfg(windows)]
extern "system" {
    fn GetUserDefaultUILanguage() -> u16;
    fn LCIDToLocaleName(locale: u32, name: *mut u16, cch: i32, flags: u32) -> i32;
}

/// The Windows display language as a Google language code ("en" if unknown).
pub fn system_lang() -> String {
    #[cfg(windows)]
    {
        let mut buf = [0u16; 85];
        // SAFETY: plain Win32 calls with a correctly sized buffer.
        let n = unsafe { LCIDToLocaleName(GetUserDefaultUILanguage() as u32, buf.as_mut_ptr(), buf.len() as i32, 0) };
        if n > 1 {
            return locale_to_lang(Some(&String::from_utf16_lossy(&buf[..(n - 1) as usize])));
        }
    }
    locale_to_lang(std::env::var("LANG").ok().as_deref())
}

pub fn resolve_target(setting: &str) -> String {
    if setting.is_empty() || setting == "auto" {
        system_lang()
    } else {
        setting.to_string()
    }
}

fn base(code: &str) -> String {
    code.split('-').next().unwrap_or("").to_lowercase()
}

// ── Script detection ─────────────────────────────────────────────

#[derive(Clone, Copy, PartialEq, Eq, Debug, PartialOrd, Ord)]
pub enum Script {
    Latin,
    Hangul,
    Kana,
    Han,
    Cyrillic,
    Greek,
    Other,
}

fn is_latin_letter(o: u32) -> bool {
    matches!(o, 0x80..=0x2AF | 0x1E00..=0x1EFF | 0x2C60..=0x2C7F | 0xA720..=0xA7FF | 0xFF21..=0xFF3A | 0xFF41..=0xFF5A)
}

/// Coarse script of one character, or None when it is not a letter.
pub fn script(ch: char) -> Option<Script> {
    let o = ch as u32;
    if o < 0x80 {
        return ch.is_alphabetic().then_some(Script::Latin);
    }
    if (0xAC00..=0xD7A3).contains(&o) || (0x1100..=0x11FF).contains(&o) || (0x3130..=0x318F).contains(&o) {
        return Some(Script::Hangul);
    }
    if (0x3040..=0x30FF).contains(&o) || (0x31F0..=0x31FF).contains(&o) || (0xFF66..=0xFF9F).contains(&o) {
        return Some(Script::Kana);
    }
    if (0x4E00..=0x9FFF).contains(&o) || (0x3400..=0x4DBF).contains(&o) || (0xF900..=0xFAFF).contains(&o) || (0x20000..=0x2FA1F).contains(&o) {
        return Some(Script::Han);
    }
    if (0x0400..=0x052F).contains(&o) {
        return ch.is_alphabetic().then_some(Script::Cyrillic);
    }
    if (0x0370..=0x03FF).contains(&o) || (0x1F00..=0x1FFF).contains(&o) {
        return ch.is_alphabetic().then_some(Script::Greek);
    }
    if !ch.is_alphabetic() {
        return None;
    }
    Some(if is_latin_letter(o) { Script::Latin } else { Script::Other })
}

pub fn scripts_in(text: &str) -> BTreeSet<Script> {
    text.chars().filter_map(script).collect()
}

pub fn needs_romanisation(text: &str) -> bool {
    scripts_in(text).iter().any(|s| *s != Script::Latin)
}

// ── Offline romanisation ─────────────────────────────────────────

const L: [&str; 19] = ["g", "kk", "n", "d", "tt", "r", "m", "b", "pp", "s", "ss", "", "j", "jj", "ch", "k", "t", "p", "h"];
const V: [&str; 21] = ["a", "ae", "ya", "yae", "eo", "e", "yeo", "ye", "o", "wa", "wae", "oe", "yo", "u", "wo", "we", "wi", "yu", "eu", "ui", "i"];
/// Final consonant: (spelled before a consonant, (stay, move)) where the pair
/// is what stays and what is carried onto a following vowel-initial syllable.
const T: [(&str, (&str, &str)); 28] = [
    ("", ("", "")), ("k", ("", "g")), ("k", ("", "kk")), ("k", ("k", "s")),
    ("n", ("", "n")), ("n", ("n", "j")), ("n", ("", "n")), ("t", ("", "d")),
    ("l", ("", "r")), ("k", ("l", "g")), ("m", ("l", "m")), ("l", ("l", "b")),
    ("l", ("l", "s")), ("l", ("l", "t")), ("p", ("l", "p")), ("l", ("", "r")),
    ("m", ("", "m")), ("p", ("", "b")), ("p", ("p", "s")), ("t", ("", "s")),
    ("t", ("", "ss")), ("ng", ("ng", "")), ("t", ("", "j")), ("t", ("", "ch")),
    ("k", ("", "k")), ("t", ("", "t")), ("p", ("", "p")), ("t", ("", "")),
];

fn hangul_word(word: &str) -> String {
    let syl: Vec<(usize, usize, usize)> = word
        .chars()
        .map(|c| {
            let s = c as usize - 0xAC00;
            (s / 588, (s % 588) / 28, s % 28)
        })
        .collect();
    let mut out = String::new();
    let mut carry: Option<&str> = None;
    for (i, &(l, v, t)) in syl.iter().enumerate() {
        let nxt = syl.get(i + 1);
        let ini = carry.unwrap_or(L[l]);
        carry = None;
        let mut fin = "";
        if t != 0 {
            let (spelled, (stay, mv)) = T[t];
            match nxt {
                Some(n) if n.0 == 11 => {
                    fin = stay;
                    carry = if mv.is_empty() { None } else { Some(mv) };
                }
                Some(n) => {
                    let nl = n.0;
                    fin = spelled;
                    if nl == 2 || nl == 6 {
                        fin = match fin { "k" => "ng", "t" => "n", "p" => "m", f => f };
                    } else if nl == 5 && (fin == "n" || fin == "l") {
                        fin = "l";
                        carry = Some("l");
                    } else if nl == 5 {
                        carry = Some("n");
                        fin = match fin { "k" => "ng", "p" => "m", "t" => "n", f => f };
                    } else if nl == 18 && matches!(fin, "k" | "t" | "p") {
                        carry = Some(match fin { "k" => "k", "t" => "t", _ => "p" });
                        fin = "";
                    }
                }
                None => fin = spelled,
            }
        }
        out.push_str(ini);
        out.push_str(V[v]);
        out.push_str(fin);
    }
    out
}

static HANGUL_RUN: LazyLock<Regex> = LazyLock::new(|| Regex::new("[\u{AC00}-\u{D7A3}]+").unwrap());

pub fn romanise_hangul(text: &str) -> String {
    HANGUL_RUN.replace_all(text, |c: &regex::Captures| hangul_word(&c[0])).into_owned()
}

fn cyr(ch: char) -> Option<&'static str> {
    Some(match ch {
        'а' => "a", 'б' => "b", 'в' => "v", 'г' => "g", 'д' => "d", 'е' => "e", 'ё' => "yo", 'ж' => "zh",
        'з' => "z", 'и' => "i", 'й' => "y", 'к' => "k", 'л' => "l", 'м' => "m", 'н' => "n", 'о' => "o",
        'п' => "p", 'р' => "r", 'с' => "s", 'т' => "t", 'у' => "u", 'ф' => "f", 'х' => "kh", 'ц' => "ts",
        'ч' => "ch", 'ш' => "sh", 'щ' => "shch", 'ъ' => "", 'ы' => "y", 'ь' => "", 'э' => "e", 'ю' => "yu",
        'я' => "ya",
        // Ukrainian / Belarusian
        'і' => "i", 'ї' => "yi", 'є' => "ye", 'ґ' => "g", 'ў' => "u",
        // Serbian / Macedonian (Gaj's Latin, which Serbian itself uses)
        'ђ' => "đ", 'ј' => "j", 'љ' => "lj", 'њ' => "nj", 'ћ' => "ć", 'џ' => "dž", 'ѓ' => "gj",
        'ќ' => "kj", 'ѕ' => "dz",
        _ => return None,
    })
}

fn grk_di(pair: &str) -> Option<&'static str> {
    Some(match pair {
        "ου" => "ou", "αι" => "ai", "ει" => "ei", "οι" => "oi", "αυ" => "av", "ευ" => "ev",
        "γγ" => "ng", "γκ" => "gk", "μπ" => "b", "ντ" => "d",
        _ => return None,
    })
}

fn grk(ch: char) -> Option<&'static str> {
    Some(match ch {
        'α' => "a", 'β' => "v", 'γ' => "g", 'δ' => "d", 'ε' => "e", 'ζ' => "z", 'η' => "i", 'θ' => "th",
        'ι' => "i", 'κ' => "k", 'λ' => "l", 'μ' => "m", 'ν' => "n", 'ξ' => "x", 'ο' => "o", 'π' => "p",
        'ρ' => "r", 'σ' => "s", 'ς' => "s", 'τ' => "t", 'υ' => "y", 'φ' => "f", 'χ' => "ch", 'ψ' => "ps",
        'ω' => "o",
        _ => return None,
    })
}

/// Python's `src.isupper()` for one char / short string, then capitalise the output.
fn case_like(src: &str, out: &str) -> String {
    let upper = src.chars().any(|c| c.is_uppercase()) && !src.chars().any(|c| c.is_lowercase());
    if out.is_empty() || !upper {
        return out.to_string();
    }
    let mut cs = out.chars();
    let f = cs.next().unwrap();
    f.to_uppercase().collect::<String>() + cs.as_str()
}

fn lower1(ch: char) -> char {
    ch.to_lowercase().next().unwrap_or(ch)
}

pub fn romanise_cyrillic(text: &str) -> String {
    text.chars()
        .map(|ch| match cyr(lower1(ch)) {
            Some(r) => case_like(&ch.to_string(), r),
            None => ch.to_string(),
        })
        .collect()
}

/// Greek with tonos/dialytika dropped (NFD minus combining marks), done by
/// mapping the accented vowels directly (no unicode-normalisation crate).
fn strip_greek_marks(c: char) -> char {
    match c {
        'ά' | 'ἀ' | 'ἁ' | 'ὰ' | 'ᾶ' => 'α', 'έ' | 'ἐ' | 'ἑ' | 'ὲ' => 'ε', 'ή' | 'ἠ' | 'ἡ' | 'ὴ' | 'ῆ' => 'η',
        'ί' | 'ϊ' | 'ΐ' | 'ἰ' | 'ἱ' | 'ὶ' | 'ῖ' => 'ι', 'ό' | 'ὀ' | 'ὁ' | 'ὸ' => 'ο',
        'ύ' | 'ϋ' | 'ΰ' | 'ὐ' | 'ὑ' | 'ὺ' | 'ῦ' => 'υ', 'ώ' | 'ὠ' | 'ὡ' | 'ὼ' | 'ῶ' => 'ω',
        'Ά' => 'Α', 'Έ' => 'Ε', 'Ή' => 'Η', 'Ί' | 'Ϊ' => 'Ι', 'Ό' => 'Ο', 'Ύ' | 'Ϋ' => 'Υ', 'Ώ' => 'Ω',
        c => c,
    }
}

pub fn romanise_greek(text: &str) -> String {
    let t: Vec<char> = text.chars().map(strip_greek_marks).collect();
    let mut out = String::new();
    let mut i = 0;
    while i < t.len() {
        if i + 1 < t.len() {
            let pair: String = [t[i], t[i + 1]].iter().collect();
            let low: String = pair.chars().map(lower1).collect();
            if let Some(r) = grk_di(&low) {
                out.push_str(&case_like(&t[i].to_string(), r));
                i += 2;
                continue;
            }
        }
        let ch = t[i];
        match grk(lower1(ch)) {
            Some(r) => out.push_str(&case_like(&ch.to_string(), r)),
            None => out.push(ch),
        }
        i += 1;
    }
    out
}

/// Romanised text, or None when this line needs the network.
pub fn romanise_offline(text: &str) -> Option<String> {
    let sc = scripts_in(text);
    let non_latin: BTreeSet<Script> = sc.iter().copied().filter(|s| *s != Script::Latin).collect();
    if non_latin.is_empty() {
        return None;
    }
    let mut out = text.to_string();
    if non_latin.contains(&Script::Hangul) {
        out = romanise_hangul(&out);
    }
    if non_latin.contains(&Script::Cyrillic) {
        out = romanise_cyrillic(&out);
    }
    if non_latin.contains(&Script::Greek) {
        out = romanise_greek(&out);
    }
    let rest = non_latin.iter().any(|s| !matches!(s, Script::Hangul | Script::Cyrillic | Script::Greek));
    if !rest {
        return Some(out);
    }
    None // Japanese, Chinese, Arabic, ...: the network
}

// ── Google web endpoint ──────────────────────────────────────────

pub const URL: &str = "https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=";
const URL_TAIL: &str = "&dt=t&dt=rm";
const SEP: &str = "\n|\n";
const CHUNK_CHARS: usize = 3500;
pub const TIMEOUT_S: u64 = 8;

static SPLIT: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\s*[|｜]\s*").unwrap());

/// Google's nested-array answer for n "|"-joined lines ->
/// (translations, romanisations, detected source language).
pub fn parse_response(data: &Value, n: usize) -> (Option<Vec<String>>, Option<Vec<String>>, String) {
    let segs: &[Value] = data.get(0).and_then(|v| v.as_array()).map(|v| v.as_slice()).unwrap_or(&[]);
    let mut tr_text = String::new();
    let mut rom_text = String::new();
    for s in segs {
        let Some(a) = s.as_array() else { continue };
        if let Some(t) = a.first().and_then(|x| x.as_str()) {
            tr_text.push_str(t);
        }
        if a.len() > 3 && a[0].is_null() {
            if let Some(r) = a[3].as_str() {
                rom_text.push_str(r);
            }
        }
    }
    let src = data.get(2).and_then(|v| v.as_str()).unwrap_or("").to_string();
    let split = |t: &str| -> Option<Vec<String>> {
        let t = t.trim();
        if t.is_empty() {
            return None;
        }
        let v: Vec<String> = SPLIT.split(t).map(|p| p.trim().to_string()).collect();
        (v.len() == n).then_some(v)
    };
    (split(&tr_text), split(&rom_text), src)
}

/// Groups of texts, each at most CHUNK_CHARS (3 chars per separator counted).
pub fn chunks(texts: &[String]) -> Vec<Vec<String>> {
    let mut out = Vec::new();
    let (mut cur, mut size): (Vec<String>, usize) = (vec![], 0);
    for t in texts {
        let len = t.chars().count();
        if !cur.is_empty() && size + len + 3 > CHUNK_CHARS {
            out.push(std::mem::take(&mut cur));
            size = 0;
        }
        cur.push(t.clone());
        size += len + 3;
    }
    if !cur.is_empty() {
        out.push(cur);
    }
    out
}

/// Which batch a line goes in. Google detects one source language per request,
/// so lines are grouped by script; kana and han share a group.
pub fn script_group(text: &str) -> String {
    for ch in text.chars() {
        match script(ch) {
            Some(Script::Kana | Script::Han) => return "cjk".into(),
            Some(Script::Hangul) => return "hangul".into(),
            Some(Script::Cyrillic) => return "cyrillic".into(),
            Some(Script::Greek) => return "greek".into(),
            Some(Script::Other) => {
                let o = ch as u32;
                return match o {
                    0x0590..=0x05FF => "hebrew",
                    0x0600..=0x06FF | 0x0750..=0x077F => "arabic",
                    0x0900..=0x097F => "devanagari",
                    0x0E00..=0x0E7F => "thai",
                    _ => "other",
                }
                .into();
            }
            _ => {}
        }
    }
    "latin".into()
}

pub type Post<'a> = &'a dyn Fn(&str, &str) -> Result<Value, String>;

/// {text: (translation, romanisation, detected source language)}; Err on a network failure.
#[allow(clippy::type_complexity)]
pub fn fetch(texts: &[String], tl: &str, post: Post) -> Result<BTreeMap<String, (Option<String>, Option<String>, String)>, String> {
    let mut groups: Vec<(String, Vec<String>)> = Vec::new();
    for t in texts {
        let g = script_group(t);
        match groups.iter_mut().find(|(k, _)| *k == g) {
            Some((_, v)) => v.push(t.clone()),
            None => groups.push((g, vec![t.clone()])),
        }
    }
    let mut out = BTreeMap::new();
    for (_, group) in groups {
        for chunk in chunks(&group) {
            let safe: Vec<String> = chunk.iter().map(|t| t.replace('|', "/")).collect();
            let (trs, roms, src) = parse_response(&post(tl, &safe.join(SEP))?, chunk.len());
            for (i, t) in chunk.iter().enumerate() {
                out.insert(
                    t.clone(),
                    (trs.as_ref().map(|v| v[i].clone()), roms.as_ref().map(|v| v[i].clone()), src.clone()),
                );
            }
        }
    }
    Ok(out)
}

/// After a 429 or 503 nothing is asked of the service until its Retry-After
/// has passed (this long when it names none, never longer than the cap); the
/// fields left out are retried the next time the track plays.
pub const RATE_LIMIT_DEFAULT_HOLD: std::time::Duration = std::time::Duration::from_secs(60);
pub const RATE_LIMIT_MAX_HOLD: std::time::Duration = std::time::Duration::from_secs(600);

/// The production `post`: one blocking HTTPS POST (call from spawn_blocking).
/// `lim` is the service's hold, shared by every call.
pub fn http_post(client: &reqwest::Client, lim: &crate::backoff::Limiter, handle: &tokio::runtime::Handle, tl: &str, text: &str) -> Result<Value, String> {
    if let Some(left) = lim.remaining() {
        return Err(format!("translation service rate limited; {} s to go", left.as_secs() + 1));
    }
    let url = format!("{URL}{}{URL_TAIL}", urlencode(tl));
    handle.block_on(async {
        let r = client
            .post(url)
            .header("User-Agent", "Mozilla/5.0")
            .timeout(std::time::Duration::from_secs(TIMEOUT_S))
            .form(&[("q", text)])
            .send()
            .await
            .map_err(|e| e.to_string())?;
        let code = r.status();
        if code.as_u16() == 429 || code.as_u16() == 503 {
            let hold = crate::backoff::retry_after(r.headers()).unwrap_or(RATE_LIMIT_DEFAULT_HOLD).min(RATE_LIMIT_MAX_HOLD);
            lim.hold(hold);
            return Err(format!("HTTP {code}; translation paused for {} s", hold.as_secs()));
        }
        r.error_for_status().map_err(|e| e.to_string())?.json::<Value>().await.map_err(|e| e.to_string())
    })
}

fn urlencode(s: &str) -> String {
    s.bytes().map(|b| if b.is_ascii_alphanumeric() || b"-_.~".contains(&b) { (b as char).to_string() } else { format!("%{b:02X}") }).collect()
}

// ── compute ──────────────────────────────────────────────────────

#[derive(Clone, Debug, Default, PartialEq)]
pub struct Entry {
    pub rom: Option<String>,
    pub tr: Option<String>,
}

pub type Fields = BTreeSet<String>;

fn set(items: &[&str]) -> Fields {
    items.iter().map(|s| s.to_string()).collect()
}

/// {index: entry} for `lines`. `want` is a subset of {"rom","tr"}. Returns the
/// result and the fields actually completed (a network failure leaves its
/// field out so it is retried the next time the track plays).
pub fn compute(lines: &[String], lang: &str, want: &Fields, post: Post) -> (Vec<Entry>, Fields, Option<String>) {
    let mut result = vec![Entry::default(); lines.len()];
    let mut done = want.clone();
    let mut net_rom: Vec<String> = Vec::new();
    if want.contains("rom") {
        for (i, l) in lines.iter().enumerate() {
            if needs_romanisation(l) {
                match romanise_offline(l) {
                    None => net_rom.push(l.clone()),
                    Some(r) => {
                        if !r.trim().is_empty() && r.trim() != l.trim() {
                            result[i].rom = Some(r.trim().to_string());
                        }
                    }
                }
            }
        }
    }
    let uniq = |it: Vec<String>| {
        let mut seen = BTreeSet::new();
        it.into_iter().filter(|x| seen.insert(x.clone())).collect::<Vec<_>>()
    };
    let texts: Vec<String> = if want.contains("tr") {
        uniq(lines.iter().map(|l| l.trim().to_string()).collect()).into_iter().filter(|l| !l.is_empty() && l.chars().any(|c| c.is_alphabetic())).collect()
    } else if !net_rom.is_empty() {
        uniq(net_rom.iter().map(|l| l.trim().to_string()).collect())
    } else {
        vec![]
    };
    if texts.is_empty() {
        return (result, done, None);
    }
    let got = match fetch(&texts, lang, post) {
        Ok(g) => g,
        Err(e) => {
            if want.contains("tr") {
                done.remove("tr");
            }
            if !net_rom.is_empty() {
                done.remove("rom");
            }
            return (result, done, Some(e));
        }
    };
    for (i, l) in lines.iter().enumerate() {
        let empty = (None, None, String::new());
        let (tr, rom, src) = got.get(l.trim()).unwrap_or(&empty);
        let same_lang = !src.is_empty() && base(src) == base(lang);
        if want.contains("tr") {
            if let Some(t) = tr {
                if !same_lang && t.trim().to_lowercase() != l.trim().to_lowercase() {
                    result[i].tr = Some(t.clone());
                }
            }
        }
        if net_rom.contains(l) {
            if let Some(r) = rom {
                if r.trim() != l.trim() {
                    result[i].rom = Some(r.clone());
                }
            }
        }
    }
    (result, done, None)
}

pub fn wanted(mode: &str) -> Fields {
    match mode {
        "rom" => set(&["rom"]),
        "tr" => set(&["tr"]),
        "both" => set(&["rom", "tr"]),
        _ => Fields::new(),
    }
}

// ── Disk cache (translations.db) ─────────────────────────────────

pub fn lines_hash(lines: &[String]) -> String {
    let mut h = Sha1::new();
    h.update(lines.join("\n").as_bytes());
    h.finalize().iter().map(|b| format!("{b:02x}")).collect()
}

pub struct Cache {
    db: std::sync::Mutex<Connection>,
}

impl Cache {
    pub fn open(dir: &Path) -> rusqlite::Result<Self> {
        Self::from_conn(Connection::open(dir.join("translations.db"))?)
    }

    pub fn from_conn(db: Connection) -> rusqlite::Result<Self> {
        let _ = db.busy_timeout(std::time::Duration::from_secs(5));
        db.execute(
            "CREATE TABLE IF NOT EXISTS translations (uri TEXT, lang TEXT, hash TEXT, fields TEXT, data TEXT, PRIMARY KEY (uri, lang, hash))",
            [],
        )?;
        Ok(Cache { db: std::sync::Mutex::new(db) })
    }

    pub fn get(&self, uri: &str, lang: &str, hash: &str) -> Option<(Fields, Vec<Entry>)> {
        let db = self.db.lock().unwrap();
        let (fields, data): (String, String) = db
            .query_row("SELECT fields, data FROM translations WHERE uri=? AND lang=? AND hash=?", [uri, lang, hash], |r| Ok((r.get(0)?, r.get(1)?)))
            .optional()
            .ok()??;
        let v: Value = serde_json::from_str(&data).ok()?;
        let obj = v.as_object()?;
        let mut entries: Vec<Entry> = Vec::new();
        let n = obj.keys().filter_map(|k| k.parse::<usize>().ok()).max().map_or(0, |m| m + 1);
        entries.resize(n, Entry::default());
        for (k, e) in obj {
            let Ok(i) = k.parse::<usize>() else { continue };
            entries[i] = Entry {
                rom: e.get("rom").and_then(|x| x.as_str()).map(String::from),
                tr: e.get("tr").and_then(|x| x.as_str()).map(String::from),
            };
        }
        Some((fields.split(',').filter(|s| !s.is_empty()).map(String::from).collect(), entries))
    }

    pub fn put(&self, uri: &str, lang: &str, hash: &str, fields: &Fields, data: &[Entry]) {
        let mut m = Map::new();
        for (i, e) in data.iter().enumerate() {
            m.insert(i.to_string(), json!({"rom": e.rom, "tr": e.tr}));
        }
        let fields = fields.iter().cloned().collect::<Vec<_>>().join(",");
        let _ = self.db.lock().unwrap().execute(
            "INSERT OR REPLACE INTO translations VALUES (?,?,?,?,?)",
            params![uri, lang, hash, fields, Value::Object(m).to_string()],
        );
    }
}

/// The full lookup: cache first, then compute what is missing, keeping any
/// field cached earlier that this run did not recompute.
pub fn work(cache: &Cache, uri: &str, lines: &[String], lang: &str, want: &Fields, post: Post) -> (Vec<Entry>, Option<String>) {
    let h = lines_hash(lines);
    let cached = cache.get(uri, lang, &h);
    if let Some((fields, data)) = &cached {
        if want.is_subset(fields) {
            return (data.clone(), None);
        }
    }
    let (prev_fields, prev) = cached.unwrap_or_default();
    let need: Fields = want.union(&prev_fields).cloned().collect();
    let (mut result, mut done, err) = compute(lines, lang, &need, post);
    for f in prev_fields.difference(&done.clone()) {
        for (i, e) in prev.iter().enumerate() {
            if i < result.len() {
                let v = if f == "rom" { &e.rom } else { &e.tr };
                if let Some(v) = v {
                    if f == "rom" { result[i].rom = Some(v.clone()) } else { result[i].tr = Some(v.clone()) }
                }
            }
        }
    }
    done.extend(prev_fields);
    if !done.is_empty() {
        cache.put(uri, lang, &h, &done, &result);
    }
    (result, err)
}

// ── Reading the result ───────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::RefCell;

    fn lines(v: &[&str]) -> Vec<String> {
        v.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn hangul_romanisation_with_sound_changes() {
        assert_eq!(romanise_hangul("안녕하세요"), "annyeonghaseyo");
        assert_eq!(romanise_hangul("사랑해"), "saranghae");
        assert_eq!(romanise_hangul("한국어"), "hangugeo"); // liaison
        assert_eq!(romanise_hangul("합니다"), "hamnida"); // nasalisation ㅂ+ㄴ
        assert_eq!(romanise_hangul("신라"), "silla"); // ㄴ+ㄹ -> ll
        assert_eq!(romanise_hangul("좋아"), "joa");
        assert_eq!(romanise_hangul("hello 너"), "hello neo"); // Latin stays
    }

    #[test]
    fn cyrillic_and_greek() {
        assert_eq!(romanise_cyrillic("Привет, мир"), "Privet, mir");
        assert_eq!(romanise_cyrillic("Љубав"), "Ljubav");
        assert_eq!(romanise_cyrillic("Щука"), "Shchuka");
        assert_eq!(romanise_greek("Καλημέρα"), "Kalimera");
        assert_eq!(romanise_greek("μπαμπάς"), "babas");
        assert_eq!(romanise_greek("Ούζο"), "Ouzo");
    }

    #[test]
    fn script_detection() {
        assert!(!needs_romanisation("Hello, wörld ñ"));
        assert!(needs_romanisation("Привет"));
        assert!(needs_romanisation("café 안녕"));
        assert!(!needs_romanisation("123 ... !!"));
        assert_eq!(script('あ'), Some(Script::Kana));
        assert_eq!(script('愛'), Some(Script::Han));
        assert_eq!(script('ا'), Some(Script::Other));
        assert_eq!(script('5'), None);
        assert_eq!(script_group("こんにちは"), "cjk");
        assert_eq!(script_group("愛している"), "cjk");
        assert_eq!(script_group("안녕"), "hangul");
        assert_eq!(script_group("hello"), "latin");
    }

    #[test]
    fn offline_only_for_tables() {
        assert_eq!(romanise_offline("안녕").as_deref(), Some("annyeong"));
        assert_eq!(romanise_offline("Привет").as_deref(), Some("Privet"));
        assert!(romanise_offline("こんにちは").is_none());
        assert!(romanise_offline("hello").is_none());
        // mixed table script + Japanese needs the network for the whole line
        assert!(romanise_offline("안녕 こんにちは").is_none());
    }

    #[test]
    fn locale_mapping() {
        assert_eq!(locale_to_lang(Some("en_US")), "en");
        assert_eq!(locale_to_lang(Some("zh_TW")), "zh-TW");
        assert_eq!(locale_to_lang(Some("zh-Hans-CN")), "zh-CN");
        assert_eq!(locale_to_lang(Some("sr_Latn_RS")), "sr");
        assert_eq!(locale_to_lang(Some("nb-NO")), "no");
        assert_eq!(locale_to_lang(None), "en");
        assert_eq!(locale_to_lang(Some("C")), "en");
        assert_eq!(resolve_target("de"), "de");
    }

    #[test]
    fn response_parsing_splits_and_rejects_mismatches() {
        let data = json!([
            [["Hello\n|\nWorld", "안녕\n|\n세계", null, null, 1],
             [null, null, "annyeong | segye", "annyeong | segye"]],
            null, "ko"
        ]);
        let (tr, rom, src) = parse_response(&data, 2);
        assert_eq!(tr.unwrap(), vec!["Hello", "World"]);
        assert_eq!(src, "ko");
        // rom is read from entries whose [0] is null and [3] a string
        assert_eq!(rom.unwrap(), vec!["annyeong", "segye"]);
        let (tr3, rom3, _) = parse_response(&data, 3);
        assert!(tr3.is_none() && rom3.is_none());
    }

    #[test]
    fn chunking_respects_the_limit() {
        let texts: Vec<String> = (0..10).map(|_| "x".repeat(1000)).collect();
        let c = chunks(&texts);
        assert!(c.len() >= 3);
        assert_eq!(c.iter().map(|v| v.len()).sum::<usize>(), 10);
        for ch in &c {
            let size: usize = ch.iter().map(|t| t.chars().count() + 3).sum();
            assert!(size <= CHUNK_CHARS + 3 || ch.len() == 1);
        }
        assert!(chunks(&[]).is_empty());
    }

    // A fake endpoint: translates "<x>" to "T(<x>)" and transliterates to "R(<x>)".
    fn fake_post(calls: &RefCell<Vec<String>>) -> impl Fn(&str, &str) -> Result<Value, String> + '_ {
        move |tl, text| {
            calls.borrow_mut().push(format!("{tl}:{text}"));
            let parts: Vec<&str> = text.split("\n|\n").collect();
            let tr = parts.iter().map(|p| format!("T({p})")).collect::<Vec<_>>().join("\n|\n");
            let rom = parts.iter().map(|p| format!("R({p})")).collect::<Vec<_>>().join(" | ");
            Ok(json!([[[tr, text, null, null, 1], [null, null, rom, rom]], null, "ja"]))
        }
    }

    #[test]
    fn compute_groups_by_script_and_fills_fields() {
        let calls = RefCell::new(vec![]);
        let l = lines(&["こんにちは", "안녕", "Hello", "", "こんにちは"]);
        let (res, done, err) = compute(&l, "en", &set(&["rom", "tr"]), &fake_post(&calls));
        assert!(err.is_none());
        assert_eq!(done, set(&["rom", "tr"]));
        // Japanese needs the network for its romanisation; Korean is offline.
        assert_eq!(res[0].rom.as_deref(), Some("R(こんにちは)"));
        assert_eq!(res[1].rom.as_deref(), Some("annyeong"));
        assert_eq!(res[2].rom, None);
        assert_eq!(res[3], Entry::default());
        assert_eq!(res[4], res[0]);
        assert_eq!(res[0].tr.as_deref(), Some("T(こんにちは)"));
        assert_eq!(res[2].tr.as_deref(), Some("T(Hello)"));
        // Detected source "ja" with target "en": kept. A line already in the target language is not.
        let calls2 = RefCell::new(vec![]);
        let same = |tl: &str, text: &str| -> Result<Value, String> {
            calls2.borrow_mut().push(format!("{tl}:{text}"));
            Ok(json!([[[text, text, null, null, 1]], null, "en"]))
        };
        let (res2, _, _) = compute(&lines(&["Hello there"]), "en", &set(&["tr"]), &same);
        assert_eq!(res2[0].tr, None);
        // one request per script group: cjk, hangul(no: offline rom only, but tr wanted), latin
        assert_eq!(calls.borrow().len(), 3);
    }

    #[test]
    fn network_failure_is_not_marked_done() {
        let fail = |_: &str, _: &str| -> Result<Value, String> { Err("offline".into()) };
        let (res, done, err) = compute(&lines(&["Hello", "안녕"]), "es", &set(&["rom", "tr"]), &fail);
        assert!(err.is_some());
        assert!(!done.contains("tr"));
        assert!(done.contains("rom")); // Korean was done offline, nothing needed the network for rom
        assert_eq!(res[1].rom.as_deref(), Some("annyeong"));
        let (_, done2, _) = compute(&lines(&["こんにちは"]), "es", &set(&["rom"]), &fail);
        assert!(!done2.contains("rom"));
    }

    #[test]
    fn pipe_in_lyrics_does_not_break_the_split() {
        let seen = RefCell::new(vec![]);
        let post = fake_post(&seen);
        let (res, _, _) = compute(&lines(&["a | b", "c"]), "en", &set(&["tr"]), &post);
        assert!(seen.borrow()[0].contains("a / b"));
        assert_eq!(res[1].tr.as_deref(), Some("T(c)"));
    }

    #[test]
    fn hash_matches_python_sha1() {
        // hashlib.sha1("a\nb".encode()).hexdigest()
        assert_eq!(lines_hash(&lines(&["a", "b"])), "fcd127ffa1016069006ad91f3f361248f9bdf272");
    }

    #[test]
    fn cache_round_trip_and_work_uses_it() {
        let cache = Cache::from_conn(Connection::open_in_memory().unwrap()).unwrap();
        let l = lines(&["안녕", "Hello"]);
        let calls = RefCell::new(vec![]);
        let (r1, err) = work(&cache, "spotify:track:1", &l, "en", &set(&["rom"]), &fake_post(&calls));
        assert!(err.is_none());
        assert_eq!(r1[0].rom.as_deref(), Some("annyeong"));
        assert!(calls.borrow().is_empty()); // offline romanisation: no request
        // cached: asking again never touches the network
        let boom = |_: &str, _: &str| -> Result<Value, String> { panic!("network used") };
        let (r2, _) = work(&cache, "spotify:track:1", &l, "en", &set(&["rom"]), &boom);
        assert_eq!(r1, r2);
        // asking for more fields computes only the new ones and keeps the old
        let (r3, _) = work(&cache, "spotify:track:1", &l, "en", &set(&["rom", "tr"]), &fake_post(&calls));
        assert_eq!(r3[0].rom.as_deref(), Some("annyeong"));
        assert_eq!(r3[1].tr.as_deref(), Some("T(Hello)"));
        let (f, _) = cache.get("spotify:track:1", "en", &lines_hash(&l)).unwrap();
        assert_eq!(f, set(&["rom", "tr"]));
        // a failed lookup is not cached as done
        let fail = |_: &str, _: &str| -> Result<Value, String> { Err("x".into()) };
        let (_, e) = work(&cache, "spotify:track:2", &l, "en", &set(&["tr"]), &fail);
        assert!(e.is_some());
        assert!(cache.get("spotify:track:2", "en", &lines_hash(&l)).is_none());
    }
}
