//! Lyric translation and romanisation (owner: nowplaying agent). The logic is
//! in np_translate.rs; this plugs it into the app.
//!
//! Pushes extras["translation"] = {uri, mode, lang, status, data}:
//!   status "off" | "loading" | "ready" | "error" (network failed: not cached, retried next play)
//!   data   {"<synced line index>": {rom?, tr?}}   (only lines that have something)
//! The page picks what to show under each line from `mode` ("rom", "tr", "both").
//!
//! Actions: get, languages, set_mode {mode}, set_target {code}.

use super::nowplaying::np_translate::{self as t, Cache, Entry};
use super::{Ctx, Feature};
use crate::engine::{Engine, Event};
use serde_json::{json, Map, Value};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex, OnceLock};

type PostFn = dyn Fn(&str, &str) -> Result<Value, String> + Send + Sync;

pub struct TState {
    cache: Cache,
    post: Arc<PostFn>,
    gen: AtomicU64,
    last: Mutex<(String, String)>,
}

pub fn lyric_lines(l: &crate::lyrics::Lyrics) -> Vec<String> {
    match l.mode.as_str() {
        "synced" if !l.synced.is_empty() => l.synced.iter().map(|x| x.words.clone()).collect(),
        "plain" if !l.plain.is_empty() => l.plain.clone(),
        _ => vec![],
    }
}

fn data_json(entries: &[Entry]) -> Value {
    let mut m = Map::new();
    for (i, e) in entries.iter().enumerate() {
        if e.rom.is_some() || e.tr.is_some() {
            m.insert(i.to_string(), json!({"rom": e.rom, "tr": e.tr}));
        }
    }
    Value::Object(m)
}

impl TState {
    /// `post` is the network call (injected so tests never touch the network).
    pub fn new(cache: Cache, post: Arc<PostFn>) -> Arc<Self> {
        Arc::new(TState { cache, post, gen: AtomicU64::new(0), last: Mutex::new((String::new(), String::new())) })
    }

    fn production(data_dir: &std::path::Path) -> Arc<Self> {
        let cache = Cache::open(data_dir)
            .or_else(|_| Cache::from_conn(rusqlite::Connection::open_in_memory().unwrap()))
            .expect("translation cache");
        // Google's free endpoint answers TLS 1.3 clients here with 429 while the same
        // request over TLS 1.2 (what Python's urllib ended up using) goes through.
        let client = reqwest::Client::builder()
            .max_tls_version(reqwest::tls::Version::TLS_1_2)
            .build()
            .unwrap_or_else(|_| reqwest::Client::new());
        let handle = tauri::async_runtime::handle().inner().clone();
        let lim = crate::backoff::Limiter::default();
        Self::new(cache, Arc::new(move |tl, text| t::http_post(&client, &lim, &handle, tl, text)))
    }

    fn settings(cfg: &crate::config::Config) -> (String, String) {
        let mode = cfg.get_or("preferences", "lyric_subline", "off").to_lowercase();
        let mode = if t::SUBLINE_MODES.contains(&mode.as_str()) { mode } else { "off".into() };
        let target = cfg.get_or("preferences", "translate_to", "auto");
        (mode, if target.is_empty() { "auto".into() } else { target })
    }

    /// Forget the old sublines and fetch new ones for the lyrics on screen.
    pub fn request(self: &Arc<Self>, engine: &Arc<Engine>, mode: &str, target: &str) {
        *self.last.lock().unwrap() = (mode.to_string(), target.to_string());
        let gen = self.gen.fetch_add(1, Ordering::SeqCst) + 1;
        let s = engine.snapshot();
        let uri = s.track.as_ref().map(|x| x.uri.clone()).unwrap_or_default();
        let lines = lyric_lines(&s.lyrics);
        let want = t::wanted(mode);
        if want.is_empty() || lines.is_empty() || uri.is_empty() {
            engine.set_extra("translation", json!({"uri": uri, "mode": mode, "lang": "", "status": "off", "data": {}}));
            return;
        }
        let lang = t::resolve_target(target);
        engine.set_extra("translation", json!({"uri": uri, "mode": mode, "lang": lang, "status": "loading", "data": {}}));
        let me = self.clone();
        let eng = engine.clone();
        let mode = mode.to_string();
        tokio::task::spawn_blocking(move || {
            let (result, err) = t::work(&me.cache, &uri, &lines, &lang, &want, &*me.post);
            if let Some(e) = &err {
                crate::log(&format!("Lyric translation unavailable: {e}"));
            }
            // Skipped on since, or the lyrics changed again: this answer is stale.
            let still = me.gen.load(Ordering::SeqCst) == gen && eng.snapshot().track.is_some_and(|x| x.uri == uri);
            if still {
                let status = if err.is_some() { "error" } else { "ready" };
                eng.set_extra("translation", json!({"uri": uri, "mode": mode, "lang": lang, "status": status, "data": data_json(&result)}));
            }
        });
    }

    /// The track changed and nothing is on the sheet yet.
    pub fn clear(&self, engine: &Engine, mode: &str) {
        self.gen.fetch_add(1, Ordering::SeqCst);
        let uri = engine.snapshot().track.map(|x| x.uri).unwrap_or_default();
        engine.set_extra("translation", json!({"uri": uri, "mode": mode, "lang": "", "status": "off", "data": {}}));
    }
}

#[derive(Default)]
pub struct Translate {
    state: OnceLock<Arc<TState>>,
}

impl Translate {
    fn st(&self, ctx: &Ctx) -> Arc<TState> {
        self.state.get_or_init(|| TState::production(&ctx.data_dir)).clone()
    }
}

impl Feature for Translate {
    fn name(&self) -> &'static str {
        "translate"
    }

    fn start(&self, ctx: &Arc<Ctx>) {
        let st = self.st(ctx);
        let (m, tg) = TState::settings(&ctx.config);
        st.request(&ctx.engine, &m, &tg);
        let ctx = ctx.clone();
        let mut rx = ctx.engine.subscribe();
        tokio::spawn(async move {
            loop {
                match rx.recv().await {
                    Ok(Event::LyricsChanged) => {
                        let (m, tg) = TState::settings(&ctx.config);
                        st.request(&ctx.engine, &m, &tg);
                    }
                    Ok(Event::TrackChanged) => {
                        if ctx.engine.snapshot().lyrics.is_none() {
                            let (m, _) = TState::settings(&ctx.config);
                            st.clear(&ctx.engine, &m);
                        }
                    }
                    Ok(Event::ConfigChanged) => {
                        let now = TState::settings(&ctx.config);
                        let changed = *st.last.lock().unwrap() != now;
                        if changed {
                            st.request(&ctx.engine, &now.0, &now.1);
                        }
                    }
                    Ok(_) => {}
                    Err(tokio::sync::broadcast::error::RecvError::Lagged(_)) => {}
                    Err(_) => break,
                }
            }
        });
    }

    fn call(&self, ctx: &Arc<Ctx>, action: &str, a: Value) -> Result<Value, String> {
        match action {
            "get" => Ok(ctx.engine.snapshot().extras.get("translation").cloned().unwrap_or(Value::Null)),
            "languages" => Ok(json!(t::LANGUAGES.iter().map(|(c, n)| json!({"code": c, "name": n})).collect::<Vec<_>>())),
            "set_mode" => {
                let m = a.get("mode").and_then(|v| v.as_str()).unwrap_or("").to_lowercase();
                if !t::SUBLINE_MODES.contains(&m.as_str()) {
                    return Err(format!("unknown mode {m}"));
                }
                ctx.config.set("preferences", "lyric_subline", &m);
                ctx.engine.config_changed();
                Ok(json!(m))
            }
            "set_target" => {
                let c = a.get("code").and_then(|v| v.as_str()).unwrap_or("auto");
                if !t::LANGUAGES.iter().any(|(k, _)| *k == c) {
                    return Err(format!("unknown language {c}"));
                }
                ctx.config.set("preferences", "translate_to", c);
                ctx.engine.config_changed();
                Ok(json!(c))
            }
            _ => Err(format!("translate: unknown action {action}")),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::lyrics::{Line, Lyrics};
    use std::sync::atomic::AtomicBool;
    use std::time::Duration;

    fn engine() -> Arc<Engine> {
        let mut e = Engine::new(None, |_, _| {});
        Arc::get_mut(&mut e).unwrap().lrclib_enabled = AtomicBool::new(false);
        e.handle(&json!({"type":"track_change","track_uri":"u1","artist":"A","title":"T","duration_ms":1000}));
        e.set_lyrics(Lyrics {
            mode: "synced".into(),
            synced: vec![Line { start_ms: 0, words: "안녕".into() }, Line { start_ms: 1, words: "Hello".into() }, Line { start_ms: 2, words: "".into() }],
            plain: vec![],
            source: "x".into(),
        });
        e
    }

    fn state(calls: Arc<Mutex<Vec<String>>>) -> Arc<TState> {
        let cache = Cache::from_conn(rusqlite::Connection::open_in_memory().unwrap()).unwrap();
        TState::new(
            cache,
            Arc::new(move |tl, text| {
                calls.lock().unwrap().push(format!("{tl}:{text}"));
                let parts: Vec<&str> = text.split("\n|\n").collect();
                let tr = parts.iter().map(|p| format!("T({p})")).collect::<Vec<_>>().join("\n|\n");
                Ok(json!([[[tr, text, null, null, 1]], null, "ko"]))
            }),
        )
    }

    async fn settle() {
        tokio::time::sleep(Duration::from_millis(300)).await;
    }

    #[tokio::test]
    async fn off_means_nothing_and_no_requests() {
        let calls = Arc::new(Mutex::new(vec![]));
        let st = state(calls.clone());
        let e = engine();
        st.request(&e, "off", "en");
        settle().await;
        assert_eq!(e.snapshot().extras["translation"]["status"], "off");
        assert!(calls.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn romanised_korean_is_offline_and_translation_uses_the_endpoint() {
        let calls = Arc::new(Mutex::new(vec![]));
        let st = state(calls.clone());
        let e = engine();
        st.request(&e, "rom", "en");
        settle().await;
        let x = e.snapshot().extras["translation"].clone();
        assert_eq!(x["status"], "ready");
        assert_eq!(x["data"]["0"]["rom"], "annyeong");
        assert!(x["data"].get("1").is_none() && x["data"].get("2").is_none());
        assert!(calls.lock().unwrap().is_empty()); // table romanisation: no request

        st.request(&e, "both", "en");
        settle().await;
        let x = e.snapshot().extras["translation"].clone();
        assert_eq!(x["mode"], "both");
        assert_eq!(x["lang"], "en");
        assert_eq!(x["data"]["1"]["tr"], "T(Hello)");
        assert_eq!(x["data"]["0"]["rom"], "annyeong");
        assert_eq!(calls.lock().unwrap().len(), 2); // hangul group and latin group
    }

    #[tokio::test]
    async fn a_network_failure_is_reported_and_not_cached() {
        let cache = Cache::from_conn(rusqlite::Connection::open_in_memory().unwrap()).unwrap();
        let st = TState::new(cache, Arc::new(|_, _| Err("offline".into())));
        let e = engine();
        st.request(&e, "tr", "es");
        settle().await;
        let x = e.snapshot().extras["translation"].clone();
        assert_eq!(x["status"], "error");
        assert_eq!(x["data"], json!({}));
    }

    #[tokio::test]
    async fn a_stale_answer_after_a_track_change_is_dropped() {
        let st = TState::new(
            Cache::from_conn(rusqlite::Connection::open_in_memory().unwrap()).unwrap(),
            Arc::new(|_, text| {
                std::thread::sleep(Duration::from_millis(200));
                Ok(json!([[[text, text, null, null, 1]], null, "ko"]))
            }),
        );
        let e = engine();
        st.request(&e, "tr", "es");
        e.handle(&json!({"type":"track_change","track_uri":"u2","artist":"A","title":"T2","duration_ms":1000}));
        st.clear(&e, "tr");
        tokio::time::sleep(Duration::from_millis(700)).await;
        let x = e.snapshot().extras["translation"].clone();
        assert_eq!(x["uri"], "u2");
        assert_eq!(x["status"], "off");
    }

    #[test]
    fn settings_are_validated() {
        let d = std::env::temp_dir().join(format!("sfy-tr-{}", crate::state::now_ms()));
        std::fs::create_dir_all(&d).unwrap();
        std::fs::write(d.join("statusify.cfg"), "[preferences]\nlyric_subline = BOTH\ntranslate_to = fr\n").unwrap();
        assert_eq!(TState::settings(&crate::config::Config::open(&d)), ("both".to_string(), "fr".to_string()));
        std::fs::write(d.join("statusify.cfg"), "[preferences]\nlyric_subline = nonsense\n").unwrap();
        assert_eq!(TState::settings(&crate::config::Config::open(&d)), ("off".to_string(), "auto".to_string()));
        let _ = std::fs::remove_dir_all(d);
    }

    #[test]
    fn lines_of_each_sheet_kind() {
        let s = Lyrics { mode: "synced".into(), synced: vec![Line { start_ms: 0, words: "a".into() }], plain: vec![], source: String::new() };
        assert_eq!(lyric_lines(&s), vec!["a"]);
        let p = Lyrics { mode: "plain".into(), synced: vec![], plain: vec!["x".into(), "".into()], source: String::new() };
        assert_eq!(lyric_lines(&p), vec!["x", ""]);
        assert!(lyric_lines(&Lyrics::none()).is_empty());
    }
}
