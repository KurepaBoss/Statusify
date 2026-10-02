//! Now Playing extras (owner: nowplaying agent): player state, Up Next queue,
//! lyric search / pin, cover colours and art, syllable timing.
//!
//! Pushes (snapshot.extras):
//!   "player"    {volume, shuffle, repeat, liked}
//!   "queue"     [{uri, uid, title, artist, album_art, duration_ms}]
//!   "palette"   {uri, art, accent, tint, tokens, colors, base, blobs, light}  (cover colours)
//!   "beats"     {uri, tempo, beats: [ms]} | null
//!   "np_timing" {uri, n, start0, startN, lines: [{endMs?, syl?} | null] | null}
//!   "np"        {prefs the lyric page reads, pinned, song_offset}
//!
//! Actions: get_state, player_cmd, set_volume, queue_pick, lyric_search,
//! pin_result, unpin, art, save_image, set_pref, nudge_global_delay,
//! toggle_topmost.

#[path = "../np_art.rs"]
pub mod np_art;
#[path = "../np_colors.rs"]
pub mod np_colors;
#[path = "../np_extras.rs"]
pub mod np_extras;
#[path = "../np_translate.rs"]
pub mod np_translate;

use super::{Ctx, Feature};
use crate::config::Config;
use crate::engine::{Engine, Event};
use crate::lyrics::{pick_lrclib, Lyrics};
use np_art::ArtCache;
use np_extras::{Player, PIN_SOURCE};
use serde_json::{json, Map, Value};
use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::Instant;

const RAW_CAP: usize = 30;
const COVER_PX: u32 = 128;

/// Preferences the lyric page may set (preferences section of statusify.cfg).
pub const SETTABLE: [&str; 8] = [
    "lyric_font", "lyric_font_boost", "beat_react", "lyric_subline", "translate_to", "always_on_top",
    "lyric_delay_ms", "animations",
];

pub struct State {
    player: Mutex<Player>,
    art: ArtCache,
    http: reqwest::Client,
    results: Mutex<Vec<Value>>,
    search_gen: AtomicU64,
    cover_gen: AtomicU64,
    /// Lyrics shown before the user pinned a search result, per track.
    stash: Mutex<HashMap<String, Lyrics>>,
    /// Raw `lyrics` / `lyrics_prefetch` messages (syllable timing, and the
    /// bridge's own lyrics for "use Spotify's lyrics again"), oldest first.
    raw: Mutex<Vec<(String, Value)>>,
    last: Mutex<HashMap<String, Value>>,
}

impl State {
    pub fn new(data_dir: &std::path::Path) -> Arc<Self> {
        Arc::new(State {
            player: Mutex::new(Player::default()),
            art: ArtCache::new(data_dir),
            http: crate::lrclib::client(),
            results: Mutex::new(vec![]),
            search_gen: AtomicU64::new(0),
            cover_gen: AtomicU64::new(0),
            stash: Mutex::new(HashMap::new()),
            raw: Mutex::new(vec![]),
            last: Mutex::new(HashMap::new()),
        })
    }

    /// set_extra, skipping a value identical to the last one pushed under `key`
    /// (every set_extra emits a whole snapshot).
    fn push(&self, engine: &Engine, key: &str, v: Value) {
        {
            let mut l = self.last.lock().unwrap();
            if l.get(key) == Some(&v) {
                return;
            }
            l.insert(key.to_string(), v.clone());
        }
        engine.set_extra(key, v);
    }

    fn cur_uri(engine: &Engine) -> String {
        engine.snapshot().track.map(|t| t.uri).unwrap_or_default()
    }

    // ── Bridge messages ──────────────────────────────────────────
    pub fn on_bridge(self: &Arc<Self>, engine: &Arc<Engine>, m: &Value) {
        let uri = m.get("track_uri").and_then(|u| u.as_str()).unwrap_or("").to_string();
        match m.get("type").and_then(|t| t.as_str()).unwrap_or("") {
            "player_state" => {
                let j = {
                    let mut p = self.player.lock().unwrap();
                    p.apply_report(m, Instant::now());
                    p.to_json()
                };
                self.push(engine, "player", j);
            }
            "queue" => self.push(engine, "queue", Value::Array(np_extras::sanitise_queue(m.get("tracks")))),
            "beats" => {
                let cur = Self::cur_uri(engine);
                if uri.is_empty() || uri == cur {
                    let mut beats: Vec<i64> = m
                        .get("beats")
                        .and_then(|b| b.as_array())
                        .map(|a| a.iter().filter_map(|x| x.as_f64()).map(|f| f as i64).collect())
                        .unwrap_or_default();
                    beats.sort_unstable();
                    let tempo = m.get("tempo").and_then(|t| t.as_f64()).unwrap_or(0.0);
                    self.push(engine, "beats", json!({"uri": cur, "tempo": tempo, "beats": beats}));
                }
            }
            "lyrics" | "lyrics_prefetch" => {
                if uri.is_empty() {
                    return;
                }
                let mut raw = self.raw.lock().unwrap();
                raw.retain(|(u, _)| *u != uri);
                if np_extras::lyrics_from_raw(m, "Spicy").is_some() {
                    raw.push((uri, m.clone()));
                }
                while raw.len() > RAW_CAP {
                    raw.remove(0);
                }
            }
            _ => {}
        }
    }

    // ── Track / lyrics changes ───────────────────────────────────
    pub fn on_track(self: &Arc<Self>, engine: &Arc<Engine>) {
        self.push(engine, "beats", Value::Null);
        let (uri, art) = engine.snapshot().track.map(|t| (t.uri, t.album_art)).unwrap_or_default();
        self.sync_timing(engine);
        // Cover colours, off the hot path.
        let gen = self.cover_gen.fetch_add(1, Ordering::SeqCst) + 1;
        let me = self.clone();
        let eng = engine.clone();
        tokio::spawn(async move {
            let colors = if art.is_empty() { None } else { me.art.fetch(&art, COVER_PX).await.map(|i| np_art::colors_of(&i)) };
            if me.cover_gen.load(Ordering::SeqCst) != gen {
                return; // skipped on since: a newer cover owns the page
            }
            me.push(&eng, "palette", palette_json(&uri, &art, colors.as_ref()));
        });
    }

    pub fn on_lyrics(self: &Arc<Self>, engine: &Arc<Engine>) {
        self.sync_timing(engine);
    }

    /// Syllable timing for the sheet on screen, matched to it line by line.
    fn sync_timing(&self, engine: &Engine) {
        let s = engine.snapshot();
        let Some(t) = &s.track else { return self.push(engine, "np_timing", Value::Null) };
        let lines = if s.lyrics.mode == "synced" {
            let raw = self.raw.lock().unwrap();
            raw.iter()
                .rev()
                .find(|(u, _)| *u == t.uri)
                .and_then(|(_, m)| np_extras::timing_from_raw(m.get("synced")?, &s.lyrics.synced))
        } else {
            None
        };
        let n = s.lyrics.synced.len();
        let v = match lines {
            Some(l) => json!({"uri": t.uri, "n": n, "start0": s.lyrics.synced.first().map_or(0, |l| l.start_ms),
                              "startN": s.lyrics.synced.last().map_or(0, |l| l.start_ms), "lines": l}),
            None => json!({"uri": t.uri, "n": n, "lines": null}),
        };
        self.push(engine, "np_timing", v);
    }

    // ── Pins ─────────────────────────────────────────────────────
    pub fn pin_result(&self, engine: &Arc<Engine>, index: usize) -> Result<Value, String> {
        let raw = self.results.lock().unwrap().get(index).and_then(|r| r.get("raw").cloned()).ok_or("no such result")?;
        let Some(mut picked) = pick_lrclib(&json!([raw]), 0) else {
            return Ok(json!({"ok": false, "error": "That result has no usable lyrics"}));
        };
        let s = engine.snapshot();
        let uri = s.track.as_ref().map(|t| t.uri.clone()).ok_or("nothing is playing")?;
        // What was showing before, so unpinning can go back to it.
        if !engine.is_pinned() && (s.lyrics.mode == "synced" || s.lyrics.mode == "plain") {
            self.stash.lock().unwrap().insert(uri, s.lyrics.clone());
        }
        picked.source = PIN_SOURCE.into();
        let mode = picked.mode.clone();
        engine.pin_lyrics(picked);
        Ok(json!({"ok": true, "mode": mode}))
    }

    pub fn unpin(&self, engine: &Arc<Engine>) -> Value {
        let uri = Self::cur_uri(engine);
        let had = engine.is_pinned();
        if had {
            engine.clear_pin();
        }
        let prev = self.stash.lock().unwrap().remove(&uri).or_else(|| {
            let raw = self.raw.lock().unwrap();
            raw.iter().rev().find(|(u, _)| *u == uri).and_then(|(_, m)| np_extras::lyrics_from_raw(m, "Spicy"))
        });
        let restored = match prev {
            Some(l) if had => {
                engine.set_lyrics(l);
                true
            }
            _ => false,
        };
        json!({"had": had, "restored": restored})
    }
}

/// extras["palette"] for a cover (no colours: no cover, or it could not load).
pub fn palette_json(uri: &str, art: &str, colors: Option<&np_art::CoverColors>) -> Value {
    let Some(c) = colors.filter(|c| !c.palette.is_empty()) else {
        return json!({"uri": uri, "art": art, "accent": null, "tint": null, "tokens": null, "colors": [], "base": null, "blobs": [], "light": null});
    };
    let (base, blobs) = np_colors::normalise(&c.palette, true, [26, 31, 38]);
    let (lbase, lblobs) = np_colors::normalise(&c.palette, false, [236, 238, 242]);
    let tokens = |dark: bool| -> Value {
        match c.tint {
            Some(t) => {
                let mut m = Map::new();
                for (k, v) in np_colors::tinted_palette(t, dark) {
                    m.insert(k.to_string(), json!(np_colors::hex(v)));
                }
                Value::Object(m)
            }
            None => Value::Null,
        }
    };
    let accent = |dark: bool| {
        c.tint.and_then(|t| np_colors::tinted_palette(t, dark).into_iter().find(|(k, _)| *k == "ACCENT").map(|(_, v)| np_colors::hex(v)))
    };
    json!({
        "uri": uri, "art": art,
        "accent": accent(true), "tint": c.tint.map(np_colors::hex), "tokens": tokens(true),
        "colors": c.palette.iter().map(|p| np_colors::hex(*p)).collect::<Vec<_>>(),
        "base": np_colors::hex(base), "blobs": blobs.iter().map(|p| np_colors::hex(*p)).collect::<Vec<_>>(),
        "light": {"accent": accent(false), "tokens": tokens(false), "base": np_colors::hex(lbase),
                  "blobs": lblobs.iter().map(|p| np_colors::hex(*p)).collect::<Vec<_>>()},
    })
}

/// extras["np"]: what the lyric page reads from statusify.cfg.
pub fn np_json(cfg: &Config, uri: &str, pinned: bool) -> Value {
    let p = |k: &str, d: &str| cfg.get_or("preferences", k, d);
    let song_offset = !uri.is_empty() && !cfg.get_or("offsets", &np_extras::offset_key(uri), "").is_empty();
    let rq = p("render_quality", "auto").to_lowercase();
    json!({
        "animations": cfg.get_bool("preferences", "animations", true),
        "render_quality": if ["auto", "high", "low"].contains(&rq.as_str()) { rq } else { "auto".into() },
        "album_tint": cfg.get_bool("preferences", "album_tint", true),
        "dark_mode": cfg.get_bool("preferences", "dark_mode", true),
        "accent": p("accent_color", "#1db954"),
        "lyric_font": p("lyric_font", ""),
        "lyric_font_boost": cfg.get_i64("preferences", "lyric_font_boost", 0).clamp(-2, 10),
        "beat_react": cfg.get_bool("preferences", "beat_react", true),
        "lyric_subline": p("lyric_subline", "off").to_lowercase(),
        "translate_to": p("translate_to", "auto"),
        "always_on_top": cfg.get_bool("preferences", "always_on_top", false),
        "lyric_delay_ms": cfg.get_i64("preferences", "lyric_delay_ms", 0),
        "pinned": pinned,
        "song_offset": song_offset,
    })
}

pub fn clean_filename(s: &str) -> String {
    let t: String = s.chars().filter(|c| !"\\/:*?\"<>|".contains(*c)).collect();
    let t = t.trim_matches(|c: char| c == ' ' || c == '.' || c == '-');
    t.chars().take(100).collect()
}

fn apply_topmost(ctx: &Ctx) {
    use tauri::Manager;
    if let Some(w) = ctx.app.get_webview_window("main") {
        let _ = w.set_always_on_top(ctx.config.get_bool("preferences", "always_on_top", false));
    }
}

#[derive(Default)]
pub struct NowPlaying {
    state: OnceLock<Arc<State>>,
}

impl NowPlaying {
    fn st(&self, ctx: &Ctx) -> Arc<State> {
        self.state.get_or_init(|| State::new(&ctx.data_dir)).clone()
    }
}

fn push_np(st: &State, ctx: &Ctx) {
    let uri = State::cur_uri(&ctx.engine);
    st.push(&ctx.engine, "np", np_json(&ctx.config, &uri, ctx.engine.is_pinned()));
}

fn arg_str<'a>(a: &'a Value, k: &str) -> &'a str {
    a.get(k).and_then(|v| v.as_str()).unwrap_or("")
}

impl Feature for NowPlaying {
    fn name(&self) -> &'static str {
        "nowplaying"
    }

    fn start(&self, ctx: &Arc<Ctx>) {
        let st = self.st(ctx);
        let s2 = st.clone();
        tokio::task::spawn_blocking(move || s2.art.prune());
        let player = st.player.lock().unwrap().to_json();
        st.push(&ctx.engine, "player", player);
        st.push(&ctx.engine, "queue", json!([]));
        push_np(&st, ctx);
        apply_topmost(ctx);
        let ctx = ctx.clone();
        let mut rx = ctx.engine.subscribe();
        tokio::spawn(async move {
            loop {
                match rx.recv().await {
                    Ok(Event::Bridge(m)) => st.on_bridge(&ctx.engine, &m),
                    Ok(Event::TrackChanged) => {
                        st.on_track(&ctx.engine);
                        push_np(&st, &ctx);
                    }
                    Ok(Event::LyricsChanged) => {
                        st.on_lyrics(&ctx.engine);
                        push_np(&st, &ctx);
                    }
                    Ok(Event::ConfigChanged) => {
                        push_np(&st, &ctx);
                        apply_topmost(&ctx);
                    }
                    Ok(_) => {}
                    Err(tokio::sync::broadcast::error::RecvError::Lagged(_)) => {}
                    Err(_) => break,
                }
            }
        });
    }

    fn call(&self, ctx: &Arc<Ctx>, action: &str, a: Value) -> Result<Value, String> {
        let st = self.st(ctx);
        match action {
            "get_state" => {
                let uri = State::cur_uri(&ctx.engine);
                let player = st.player.lock().unwrap().to_json();
                Ok(json!({"np": np_json(&ctx.config, &uri, ctx.engine.is_pinned()), "player": player}))
            }
            // prev / next / toggle / play / pause / shuffle / repeat / like
            "player_cmd" => {
                let act = arg_str(&a, "action").to_string();
                if !["prev", "next", "toggle", "play", "pause", "shuffle", "repeat", "like"].contains(&act.as_str()) {
                    return Err(format!("unknown player action {act}"));
                }
                if !ctx.outbox.send(json!({"type": "player", "action": act})) {
                    return Ok(json!(false));
                }
                let mut p = st.player.lock().unwrap();
                if p.optimistic(&act, Instant::now()) {
                    let j = p.to_json();
                    drop(p);
                    st.push(&ctx.engine, "player", j);
                }
                Ok(json!(true))
            }
            "set_volume" => {
                let v = a.get("value").and_then(|v| v.as_f64()).ok_or("value must be a number")?;
                let v = v.clamp(0.0, 1.0);
                if !ctx.outbox.send(json!({"type": "volume", "value": v})) {
                    return Ok(json!(false));
                }
                let j = {
                    let mut p = st.player.lock().unwrap();
                    p.set_volume(v, Instant::now());
                    p.to_json()
                };
                st.push(&ctx.engine, "player", j);
                Ok(json!(true))
            }
            "queue_pick" => {
                let uri = arg_str(&a, "uri");
                if uri.is_empty() {
                    return Ok(json!(false));
                }
                Ok(json!(ctx.outbox.send(json!({"type": "skip_to", "uri": uri, "uid": arg_str(&a, "uid")}))))
            }
            "lyric_search" => {
                let q = arg_str(&a, "query").trim().to_string();
                let gen = st.search_gen.fetch_add(1, Ordering::SeqCst) + 1;
                if q.is_empty() {
                    st.results.lock().unwrap().clear();
                    return Ok(json!({"results": [], "status": "Type an artist and a song title"}));
                }
                let s = ctx.engine.snapshot();
                let (artist, title) = s.track.as_ref().map(|t| (t.artist.clone(), t.title.clone())).unwrap_or_default();
                let found = tauri::async_runtime::block_on(np_extras::search(&st.http, crate::lrclib::URL, &q, &artist, &title));
                if st.search_gen.load(Ordering::SeqCst) != gen {
                    return Ok(json!({"stale": true}));
                }
                match found {
                    Ok(v) => {
                        let res = np_extras::clean_results(&v, s.duration_ms, np_extras::SEARCH_LIMIT);
                        *st.results.lock().unwrap() = res.clone();
                        let status = if res.is_empty() {
                            "No lyrics found. Try different words".to_string()
                        } else {
                            format!("{} result{} · pick the right one", res.len(), if res.len() != 1 { "s" } else { "" })
                        };
                        // The raw LRCLIB record stays in the backend; the page gets what it shows.
                        let slim: Vec<Value> = res
                            .iter()
                            .map(|r| json!({"track": r["track"], "artist": r["artist"], "album": r["album"], "duration": r["duration"], "synced": r["synced"]}))
                            .collect();
                        Ok(json!({"results": slim, "status": status}))
                    }
                    Err(e) => {
                        crate::log(&format!("LRCLIB search failed: {e}"));
                        st.results.lock().unwrap().clear();
                        Ok(json!({"results": [], "status": "Search failed. Check your connection and try again", "error": true}))
                    }
                }
            }
            "pin_result" => {
                let i = a.get("index").and_then(|v| v.as_u64()).ok_or("index required")? as usize;
                let r = st.pin_result(&ctx.engine, i)?;
                push_np(&st, ctx);
                Ok(r)
            }
            "unpin" => {
                let r = st.unpin(&ctx.engine);
                push_np(&st, ctx);
                Ok(r)
            }
            // Cover as a data: URI, so a canvas (share image) is not tainted.
            "art" => {
                let url = arg_str(&a, "url").to_string();
                let size = a.get("size").and_then(|v| v.as_u64()).unwrap_or(640).clamp(32, 1024) as u32;
                let img = tauri::async_runtime::block_on(st.art.fetch(&url, size));
                Ok(img.and_then(|i| np_art::data_uri(&i)).map_or(Value::Null, Value::String))
            }
            // Writes a PNG: to `path` when given (a Save dialog's pick), else
            // Pictures\Statusify\<name>.png. Returns the path written.
            "save_image" => {
                use base64::Engine as _;
                let data = arg_str(&a, "data");
                let b64 = data.split_once(',').map_or(data, |(_, d)| d);
                let bytes = base64::engine::general_purpose::STANDARD.decode(b64).map_err(|e| e.to_string())?;
                if !bytes.starts_with(b"\x89PNG") {
                    return Err("not a PNG".into());
                }
                let path = match arg_str(&a, "path") {
                    "" => {
                        let home = std::env::var_os("USERPROFILE")
                            .or_else(|| std::env::var_os("HOME"))
                            .map(std::path::PathBuf::from)
                            .ok_or("no home folder")?;
                        let name = clean_filename(arg_str(&a, "name"));
                        let dir = home.join("Pictures").join("Statusify");
                        std::fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
                        dir.join(format!("{}.png", if name.is_empty() { "lyric" } else { &name }))
                    }
                    p => std::path::PathBuf::from(p),
                };
                if path.extension().and_then(|e| e.to_str()).map(|e| e.eq_ignore_ascii_case("png")) != Some(true) {
                    return Err("path must end in .png".into());
                }
                std::fs::write(&path, bytes).map_err(|e| e.to_string())?;
                crate::log(&format!("Saved lyric image: {}", path.display()));
                Ok(json!(path.to_string_lossy()))
            }
            "set_pref" => {
                let k = arg_str(&a, "key");
                if !SETTABLE.contains(&k) {
                    return Err(format!("preference {k} is not settable here"));
                }
                let v = match a.get("value") {
                    Some(Value::Bool(b)) => b.to_string(),
                    Some(Value::Number(n)) => n.to_string(),
                    Some(Value::String(s)) => s.clone(),
                    _ => return Err("value required".into()),
                };
                ctx.config.set("preferences", k, &v);
                ctx.engine.config_changed();
                Ok(json!(true))
            }
            // Shift-click on the delay stepper: the global delay every song follows.
            "nudge_global_delay" => {
                let d = a.get("delta_ms").and_then(|v| v.as_i64()).ok_or("delta_ms required")?;
                let cur = ctx.config.get_i64("preferences", "lyric_delay_ms", 0);
                let v = (cur + d).clamp(-5000, 5000);
                ctx.config.set("preferences", "lyric_delay_ms", &v.to_string());
                ctx.engine.config_changed();
                Ok(json!(v))
            }
            "toggle_topmost" => {
                let on = !ctx.config.get_bool("preferences", "always_on_top", false);
                ctx.config.set("preferences", "always_on_top", if on { "true" } else { "false" });
                ctx.engine.config_changed();
                crate::log(&format!("Always on top {}", if on { "enabled" } else { "disabled" }));
                Ok(json!(on))
            }
            _ => Err(format!("nowplaying: unknown action {action}")),
        }
    }
}

#[cfg(test)]
mod tests;
