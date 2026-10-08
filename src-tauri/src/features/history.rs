//! History, Stats and Wrapped export (owner: history agent).
//!
//! Frontend actions (call("history", action, args)):
//!   list {query?, limit?}          the History page: day groups, caption
//!   entry {id}                     one play with its lyrics (the sheet)
//!   clear                          wipe plays, lyric cache and pins
//!   export {id, fmt}               write exports/<Artist - Title>.lrc|.txt
//!   open_spotify {uri}             open a track in the Spotify client
//!   unpin {uri}                    forget a hand-picked lyric choice (and its cached copy)
//!   stats {year?, month?, recent_n?}  everything the Stats page shows
//!   month {year, month}            one Wrapped month
//!   recent {limit}                 "Recently played"
//!   session                        songs / listening time this session
//!   fetch_art {url}                a cover as a data: URL (for the Wrapped canvas)
//!   pick_wrapped_path {year, month}  ask where to save the Wrapped PNG (before rendering)
//!   save_wrapped {png, year, month}  write the rendered Wrapped PNG to the picked path
//!   on_quit                        for the shell: honour "Remember history = off"
//!
//! The app event "history_changed" ({count, latest_id}) fires when a play was
//! committed (or history was cleared); both pages refresh on it.

use super::{Ctx, Feature};
use crate::db::{Entry, Store};
use crate::engine::Event;
#[path = "../history_export.rs"]
mod export;
#[path = "../history_fmt.rs"]
mod fmt;
#[path = "../history_session.rs"]
mod session;

use chrono::{Datelike, Duration as Days, NaiveDateTime};
use serde_json::{json, Value};
use std::sync::{Arc, Mutex};
use tauri::{Emitter, Manager};
use tokio::sync::broadcast::error::RecvError;

/// Plays shown per page of the History list ("Show more" adds this many).
const PAGE: i64 = 60;
/// "Recently played" rows per page on the Stats page.
const RECENT_PAGE: i64 = 30;
const HEAT_MAX_WEEKS: i64 = 53;
const MAX_ART_BYTES: usize = 8 * 1024 * 1024;
/// "Show more" has no ceiling of its own (Python's limit grew unbounded);
/// this only keeps a bogus argument from asking for an absurd allocation.
const MAX_LIMIT: i64 = 1_000_000;

#[derive(Default)]
pub struct History {
    session: Arc<Mutex<session::Session>>,
    /// Where the user chose to save the Wrapped image (set by pick_wrapped_path,
    /// consumed by save_wrapped), so the renderer never names a path itself.
    picked: Mutex<Option<std::path::PathBuf>>,
}

fn now() -> NaiveDateTime {
    chrono::Local::now().naive_local()
}

fn iso(t: NaiveDateTime) -> String {
    t.format("%Y-%m-%dT%H:%M:%S").to_string()
}

fn arg_i64(a: &Value, k: &str) -> Option<i64> {
    a.get(k).and_then(|v| v.as_f64()).map(|f| f as i64)
}

fn arg_str<'a>(a: &'a Value, k: &str) -> &'a str {
    a.get(k).and_then(|v| v.as_str()).unwrap_or("")
}

fn entry_json(e: &Entry, now: NaiveDateTime) -> Value {
    let t = fmt::parse(&e.played_at).unwrap_or(now);
    json!({
        "id": e.id,
        "track_uri": e.track_uri,
        "artist": e.artist,
        "title": e.title,
        "album_art": e.album_art,
        "played_at": e.played_at,
        "hm": t.format("%H:%M").to_string(),
        "time": fmt::display_time(&e.played_at, now),
        "listened_ms": e.listened_ms,
        "badge": fmt::lyric_badge(e.synced_n, e.plain_n),
        "lines": if e.synced_n > 0 { e.synced_n } else { e.plain_n },
    })
}

/// The History page: plays (or search hits) grouped by day, plus the caption.
fn list_json(st: &Store, query: &str, limit: i64, now: NaiveDateTime) -> Result<Value, String> {
    let q = query.trim();
    let entries = if q.is_empty() { st.entries(limit) } else { st.search(q, limit) }.map_err(|e| e.to_string())?;
    let more = entries.len() as i64 >= limit;
    let groups: Vec<Value> = fmt::group_by_day(entries, now, |e| e.played_at.as_str(), |e| e.listened_ms)
        .into_iter()
        .map(|g| {
            json!({
                "day": g.day.format("%Y-%m-%d").to_string(),
                "label": g.label,
                "summary": fmt::day_summary(g.sessions, g.listened_ms),
                "sessions": g.sessions,
                "listened_ms": g.listened_ms,
                "entries": g.entries.iter().map(|e| entry_json(e, now)).collect::<Vec<_>>(),
            })
        })
        .collect();
    let count = st.count();
    Ok(json!({
        "query": q,
        "groups": groups,
        "more": more,
        "count": count,
        "caption": fmt::plays_caption(count, st.first_played().as_deref(), now.date()),
        "latest_id": st.fingerprint().1,
        "current_id": st.current_play(),
    }))
}

fn play_json(p: &crate::db::Play, now: NaiveDateTime) -> Value {
    json!({
        "id": p.id,
        "track_uri": p.track_uri,
        "artist": p.artist,
        "title": p.title,
        "album_art": p.album_art,
        "played_at": p.played_at,
        "listened_ms": p.listened_ms,
        "when": fmt::rel_time(&p.played_at, now),
        "dur": fmt::fmt_duration(p.listened_ms),
    })
}

fn month_json(st: &Store, year: i32, month: u32) -> Result<Value, String> {
    let m = st.month_summary(year, month).map_err(|e| e.to_string())?;
    let mut v = serde_json::to_value(&m).map_err(|e| e.to_string())?;
    v["label"] = json!(fmt::month_name(year, month));
    v["days_in_month"] = json!(days_in_month(year, month));
    Ok(v)
}

fn days_in_month(year: i32, month: u32) -> u32 {
    let (ny, nm) = if month >= 12 { (year + 1, 1) } else { (year, month + 1) };
    chrono::NaiveDate::from_ymd_opt(ny, nm, 1).and_then(|d| d.pred_opt()).map(|d| d.day()).unwrap_or(30)
}

/// Everything the Stats page shows that comes from the history database.
fn stats_json(st: &Store, ym: (i32, u32), recent_n: i64, now: NaiveDateTime) -> Result<Value, String> {
    let e = |e: rusqlite::Error| e.to_string();
    let today = now.date();
    let week = st.stats(Some(&iso(now - Days::days(7))), 5).map_err(e)?;
    let all = st.stats(None, 5).map_err(e)?;
    let recent: Vec<Value> = st.recent_plays_page(recent_n, 0).map_err(e)?.iter().map(|p| play_json(p, now)).collect();
    let heat_since = (today - Days::days(7 * (HEAT_MAX_WEEKS + 1))).format("%Y-%m-%d").to_string();
    let heat = st.plays_per_day(&heat_since).map_err(e)?;
    let top_all = st.top_tracks(None, 8).map_err(e)?;
    let top_30 = st.top_tracks(Some(&iso(now - Days::days(30))), 8).map_err(e)?;
    let latest = st.recent_plays_page(1, 0).map_err(e)?.into_iter().next();
    Ok(json!({
        "off": false,
        "today": today.format("%Y-%m-%d").to_string(),
        "first": st.first_played(),
        "week": week,
        "all": all,
        "recent": recent,
        "recent_n": recent_n,
        "heat": heat,
        "month": month_json(st, ym.0, ym.1)?,
        "top": {"all": top_all, "30": top_30},
        "latest": latest.map(|p| json!({"id": p.id, "track_uri": p.track_uri, "played_at": p.played_at})),
        "current_id": st.current_play(),
    }))
}

fn history_on(ctx: &Ctx) -> bool {
    ctx.config.get_bool("preferences", "save_history", true)
}

/// Mirror the "Remember history" setting onto the store.
fn sync_enabled(ctx: &Ctx) {
    if let Some(st) = &ctx.engine.store {
        st.set_enabled(history_on(ctx));
    }
}

/// With "Remember history" off: delete what is on record (on quit, and again
/// at startup in case the app was killed before it could).
pub(crate) fn wipe_if_off(ctx: &Ctx) {
    if history_on(ctx) {
        return;
    }
    if let Some(st) = ctx.engine.store.as_ref() {
        if st.count() == 0 {
            return;
        }
        match st.clear() {
            Ok(()) => crate::log("History deleted (history disabled)"),
            Err(e) => crate::log(&format!("Could not delete history: {e}")),
        }
    }
}

fn announce(ctx: &Ctx, st: &Store) {
    let (count, latest_id) = st.fingerprint();
    let _ = ctx.app.emit(
        "history_changed",
        json!({"count": count, "latest_id": latest_id, "current_id": st.current_play()}),
    );
}

/// Fetch a cover for the Wrapped canvas as a data: URL, so drawing it never
/// taints the canvas (and the PNG can be read back out).
fn fetch_art(url: &str) -> Result<String, String> {
    use base64::Engine as _;
    if !(url.starts_with("https://") || url.starts_with("http://")) {
        return Err("not an http(s) url".into());
    }
    let url = url.to_string();
    tauri::async_runtime::block_on(async move {
        // One client (and its connection pool) for every cover, not one per call.
        static CLIENT: std::sync::OnceLock<reqwest::Client> = std::sync::OnceLock::new();
        let client = CLIENT.get_or_init(|| {
            reqwest::Client::builder().timeout(std::time::Duration::from_secs(10)).user_agent("Statusify").build().unwrap_or_else(|_| reqwest::Client::new())
        });
        let r = client.get(&url).send().await.map_err(|e| e.to_string())?;
        if !r.status().is_success() {
            return Err(format!("HTTP {}", r.status()));
        }
        let ct = r
            .headers()
            .get("content-type")
            .and_then(|v| v.to_str().ok())
            .unwrap_or("")
            .split(';')
            .next()
            .unwrap_or("")
            .trim()
            .to_string();
        if !ct.starts_with("image/") {
            return Err("not an image".into());
        }
        let bytes = r.bytes().await.map_err(|e| e.to_string())?;
        if bytes.len() > MAX_ART_BYTES {
            return Err("image too large".into());
        }
        Ok(format!("data:{ct};base64,{}", base64::engine::general_purpose::STANDARD.encode(&bytes)))
    })
}

/// The dialog plugin is registered here (not in lib.rs) so this area stays
/// self-contained; a no-op if the shell already registered it.
fn ensure_dialog(app: &tauri::AppHandle) {
    if app.try_state::<tauri_plugin_dialog::Dialog<tauri::Wry>>().is_none() {
        if let Err(e) = app.plugin(tauri_plugin_dialog::init()) {
            crate::log(&format!("Dialog plugin unavailable: {e}"));
        }
    }
}

/// `dir/name`, or `dir/name (2)` and so on when that file exists, so the
/// no-dialog fallback never silently overwrites an earlier export.
fn unique_path(dir: &std::path::Path, name: &str) -> std::path::PathBuf {
    let first = dir.join(name);
    if !first.exists() {
        return first;
    }
    let (stem, ext) = match name.rsplit_once('.') {
        Some((s, e)) => (s, format!(".{e}")),
        None => (name, String::new()),
    };
    (2..10_000).map(|n| dir.join(format!("{stem} ({n}){ext}"))).find(|p| !p.exists()).unwrap_or(first)
}

/// Where to save the Wrapped PNG: the save dialog, else <data dir>/exports
/// (never overwriting). None = the user cancelled.
fn pick_png_path(ctx: &Ctx, name: &str) -> Option<std::path::PathBuf> {
    ensure_dialog(&ctx.app);
    if ctx.app.try_state::<tauri_plugin_dialog::Dialog<tauri::Wry>>().is_some() {
        use tauri_plugin_dialog::DialogExt;
        return ctx
            .app
            .dialog()
            .file()
            .set_title("Save Wrapped image")
            .add_filter("PNG image", &["png"])
            .set_file_name(name)
            .blocking_save_file()
            .and_then(|p| p.into_path().ok());
    }
    let dir = ctx.data_dir.join("exports");
    let _ = std::fs::create_dir_all(&dir);
    Some(unique_path(&dir, name))
}

fn is_png(bytes: &[u8]) -> bool {
    bytes.len() >= 8 && &bytes[..8] == b"\x89PNG\r\n\x1a\n" && bytes.len() <= 40 * 1024 * 1024
}

fn wrapped_name(a: &Value) -> String {
    let (y, m) = (arg_i64(a, "year").unwrap_or(0) as i32, arg_i64(a, "month").unwrap_or(0) as u32);
    format!("Statusify Wrapped {}.png", fmt::month_name(y, m).trim())
}

/// Step 1 of saving: ask where (the save dialog), before any rendering.
fn pick_wrapped_path(h: &History, ctx: &Ctx, a: &Value) -> Result<Value, String> {
    let Some(mut path) = pick_png_path(ctx, &wrapped_name(a)) else {
        *h.picked.lock().unwrap() = None;
        return Ok(json!({"cancelled": true}));
    };
    if path.extension().is_none() {
        path.set_extension("png");
    }
    let shown = path.display().to_string();
    *h.picked.lock().unwrap() = Some(path);
    Ok(json!({"path": shown}))
}

/// Step 2: write the rendered PNG to the picked path (or ask now if none).
fn save_wrapped(h: &History, ctx: &Ctx, a: &Value) -> Result<Value, String> {
    use base64::Engine as _;
    let b64 = arg_str(a, "png");
    let bytes = base64::engine::general_purpose::STANDARD
        .decode(b64.rsplit(',').next().unwrap_or(""))
        .map_err(|e| format!("bad image data: {e}"))?;
    if !is_png(&bytes) {
        return Err("not a PNG image".into());
    }
    let picked = h.picked.lock().unwrap().take();
    let path = match picked {
        Some(p) => p,
        None => {
            let Some(mut p) = pick_png_path(ctx, &wrapped_name(a)) else {
                return Ok(json!({"cancelled": true}));
            };
            if p.extension().is_none() {
                p.set_extension("png");
            }
            p
        }
    };
    std::fs::write(&path, bytes).map_err(|e| format!("Couldn't save the image: {e}"))?;
    crate::log(&format!("Wrapped image saved  ·  {}", path.display()));
    Ok(json!({"path": path.display().to_string()}))
}

/// Only a plain spotify: URI ever reaches the OS (no shell involved). Local
/// files (spotify:local:Artist:Album:Title:123) carry %-escapes, '+' and '.'.
fn valid_spotify_uri(uri: &str) -> bool {
    uri.starts_with("spotify:") && uri.chars().all(|c| c.is_ascii_alphanumeric() || ":_-%+.".contains(c))
}

fn open_spotify(ctx: &Ctx, uri: &str) -> Result<Value, String> {
    use tauri_plugin_opener::OpenerExt;
    if !valid_spotify_uri(uri) {
        crate::log("No Spotify URI stored for this entry");
        return Err("No Spotify URI stored for this entry".into());
    }
    ctx.app.opener().open_url(uri, None::<&str>).map_err(|e| {
        crate::log(&format!("Could not open Spotify: {e}"));
        e.to_string()
    })?;
    crate::log(&format!("Opening in Spotify  ·  {uri}"));
    Ok(json!({"ok": true}))
}

impl Feature for History {
    fn name(&self) -> &'static str {
        "history"
    }

    fn start(&self, ctx: &Arc<Ctx>) {
        ensure_dialog(&ctx.app);
        sync_enabled(ctx);
        // "Remember history" off means nothing is kept: if the last quit never
        // ran on_quit (crash, kill), honour it now.
        wipe_if_off(ctx);
        if let Some(st) = &ctx.engine.store {
            let legacy = ctx.data_dir.join("history.json");
            if legacy.exists() {
                match st.import_json(&legacy) {
                    Ok(n) => crate::log(&format!("Migrated {n} entries from history.json")),
                    Err(e) => crate::log(&format!("Could not migrate history.json: {e}")),
                }
            }
            crate::log(&format!("History  ·  {} plays on record", st.count()));
        }
        let session = self.session.clone();
        let ctx = ctx.clone();
        tokio::spawn(async move {
            let mut rx = ctx.engine.subscribe();
            let mut tick = tokio::time::interval(std::time::Duration::from_millis(1500));
            let mut last = ctx.engine.store.as_ref().map(|s| s.change_token());
            loop {
                tokio::select! {
                    ev = rx.recv() => match ev {
                        Ok(Event::Bridge(m)) => session.lock().unwrap().on_bridge(&m),
                        // A new song is not "playing now" as a play until it is committed.
                        Ok(Event::TrackChanged) => {
                            if let Some(st) = &ctx.engine.store {
                                st.clear_current();
                            }
                        }
                        Ok(Event::ConfigChanged) => sync_enabled(&ctx),
                        Ok(_) | Err(RecvError::Lagged(_)) => {}
                        Err(RecvError::Closed) => break,
                    },
                    _ = tick.tick() => {
                        // The engine commits plays; this notices (one cheap query)
                        // and tells the open pages so they refresh.
                        let c = ctx.clone();
                        let fp = tokio::task::spawn_blocking(move || c.engine.store.as_ref().map(|s| s.change_token()))
                            .await
                            .ok()
                            .flatten();
                        if fp.is_some() && fp != last {
                            last = fp;
                            if let Some(st) = &ctx.engine.store {
                                announce(&ctx, st);
                            }
                        }
                    }
                }
            }
        });
    }

    fn call(&self, ctx: &Arc<Ctx>, action: &str, a: Value) -> Result<Value, String> {
        sync_enabled(ctx);
        let store = || ctx.engine.store.as_ref().ok_or_else(|| "history.db is unavailable".to_string());
        match action {
            "list" => {
                let limit = arg_i64(&a, "limit").unwrap_or(PAGE).clamp(1, MAX_LIMIT);
                list_json(store()?, arg_str(&a, "query"), limit, now())
            }
            "entry" => {
                let st = store()?;
                let id = arg_i64(&a, "id").ok_or("missing id")?;
                let e = st.entry(id).ok_or("no such play")?;
                let n = now();
                let mut v = serde_json::to_value(&e).map_err(|e| e.to_string())?;
                v["time"] = json!(fmt::display_time(&e.entry.played_at, n));
                v["meta"] = json!(fmt::meta_line(&e.entry.played_at, e.entry.synced_n, e.entry.plain_n, n.date()));
                v["badge"] = json!(fmt::lyric_badge(e.entry.synced_n, e.entry.plain_n));
                Ok(v)
            }
            "clear" => {
                let st = store()?;
                st.clear().map_err(|e| e.to_string())?;
                // Drop the engine's notion of a pin on the current track too.
                ctx.engine.clear_pin();
                crate::log("History cleared");
                announce(ctx, st);
                Ok(json!({"ok": true}))
            }
            "export" => {
                let st = store()?;
                let id = arg_i64(&a, "id").ok_or("missing id")?;
                let e = st.entry(id).ok_or("no such play")?;
                let kind = if arg_str(&a, "fmt") == "lrc" { "lrc" } else { "txt" };
                match export::export(&ctx.data_dir, &e, kind) {
                    Ok(p) => {
                        let file = p.file_name().map(|f| f.to_string_lossy().to_string()).unwrap_or_default();
                        crate::log(&format!("Exported {}  ·  {file}", kind.to_uppercase()));
                        Ok(json!({"path": p.display().to_string(), "file": file, "message": format!("Saved to exports/{file}")}))
                    }
                    Err(err) => {
                        crate::log(&format!("Export failed: {err}"));
                        Err(format!("Export failed: {err}"))
                    }
                }
            }
            "unpin" => {
                // Forget the lyrics the user picked for a track (and the cached copy of them).
                let st = store()?;
                let uri = arg_str(&a, "uri");
                let had = st.unpin_lyrics(uri);
                if ctx.engine.snapshot().track.is_some_and(|t| t.uri == uri) {
                    ctx.engine.clear_pin();
                }
                Ok(json!({"had_pin": had}))
            }
            "open_spotify" => open_spotify(ctx, arg_str(&a, "uri")),
            "stats" => {
                if !history_on(ctx) {
                    return Ok(json!({"off": true}));
                }
                let st = store()?;
                let n = now();
                let ym = (
                    arg_i64(&a, "year").map(|y| y as i32).unwrap_or(n.year()),
                    arg_i64(&a, "month").map(|m| m.clamp(1, 12) as u32).unwrap_or(n.month()),
                );
                stats_json(st, ym, arg_i64(&a, "recent_n").unwrap_or(RECENT_PAGE).clamp(1, MAX_LIMIT), n)
            }
            "month" => {
                let st = store()?;
                let y = arg_i64(&a, "year").ok_or("missing year")? as i32;
                let m = arg_i64(&a, "month").ok_or("missing month")?.clamp(1, 12) as u32;
                month_json(st, y, m)
            }
            "recent" => {
                let st = store()?;
                let n = arg_i64(&a, "limit").unwrap_or(RECENT_PAGE).clamp(1, MAX_LIMIT);
                let t = now();
                let plays = st.recent_plays_page(n, 0).map_err(|e| e.to_string())?;
                Ok(json!({"recent": plays.iter().map(|p| play_json(p, t)).collect::<Vec<_>>(), "recent_n": n}))
            }
            "session" => {
                let s = self.session.lock().unwrap();
                Ok(json!({"songs": s.songs, "listened_ms": s.listened_ms}))
            }
            "fetch_art" => fetch_art(arg_str(&a, "url")).map(Value::String),
            "pick_wrapped_path" => pick_wrapped_path(self, ctx, &a),
            "save_wrapped" => save_wrapped(self, ctx, &a),
            "on_quit" => {
                // With "Remember history" off, what is on record goes when the
                // app quits, as the setting promises.
                wipe_if_off(ctx);
                Ok(json!({"ok": true}))
            }
            _ => Err(format!("history: unknown action {action}")),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::NaiveDate;

    fn store() -> (Store, std::path::PathBuf) {
        let d = std::env::temp_dir()
            .join(format!("statusify-hist-feat-{}-{:?}", crate::state::now_ms(), std::thread::current().id()));
        std::fs::create_dir_all(&d).unwrap();
        (Store::open(&d).unwrap(), d)
    }
    fn dt(y: i32, mo: u32, d: u32, h: u32, mi: u32) -> NaiveDateTime {
        NaiveDate::from_ymd_opt(y, mo, d).unwrap().and_hms_opt(h, mi, 0).unwrap()
    }

    #[test]
    fn list_groups_plays_by_day_and_reports_caption() {
        let (st, d) = store();
        let n = dt(2026, 9, 23, 20, 0);
        for (i, t) in [dt(2026, 9, 23, 19, 50), dt(2026, 9, 23, 19, 40), dt(2026, 9, 22, 12, 0)].iter().enumerate() {
            let id = st.record_play(&format!("u{i}"), "Artist", &format!("T{i}"), "", &iso(*t)).unwrap();
            st.set_listened(id, 180_000).unwrap();
        }
        let v = list_json(&st, "", 60, n).unwrap();
        assert_eq!(v["groups"][0]["label"], "Today");
        assert_eq!(v["groups"][0]["entries"].as_array().unwrap().len(), 2);
        assert_eq!(v["groups"][0]["summary"], "1 session · 6 min");
        assert_eq!(v["groups"][1]["label"], "Yesterday");
        assert_eq!(v["caption"], "3 plays · since 22 Sep");
        assert_eq!(v["more"], false);
        assert_eq!(v["groups"][0]["entries"][0]["hm"], "19:50");
        // Searching narrows it, and "more" tracks the limit.
        let v = list_json(&st, "t2", 60, n).unwrap();
        assert_eq!(v["groups"].as_array().unwrap().len(), 1);
        assert_eq!(list_json(&st, "", 2, n).unwrap()["more"], true);
        let _ = std::fs::remove_dir_all(d);
    }

    #[test]
    fn stats_bundle_has_every_section() {
        let (st, d) = store();
        let n = dt(2026, 9, 23, 20, 0);
        st.record_play("u1", "A", "One", "art", &iso(dt(2026, 9, 22, 21, 0))).unwrap();
        st.record_play("u1", "A", "One", "art", &iso(dt(2026, 9, 23, 19, 59))).unwrap();
        let v = stats_json(&st, (2026, 9), 30, n).unwrap();
        assert_eq!(v["all"]["plays"], 2);
        assert_eq!(v["week"]["top_artists"][0][0], "A");
        assert_eq!(v["recent"][0]["when"], "1 min ago");
        assert_eq!(v["heat"]["2026-09-22"], 1);
        assert_eq!(v["month"]["label"], "September 2026");
        assert_eq!(v["month"]["days_in_month"], 30);
        assert_eq!(v["top"]["30"][0]["plays"], 2);
        assert_eq!(v["latest"]["id"], 2);
        assert_eq!(v["first"], "2026-09-22T21:00:00");
        let _ = std::fs::remove_dir_all(d);
    }

    #[test]
    fn days_in_month_handles_leap_years_and_december() {
        assert_eq!(days_in_month(2024, 2), 29);
        assert_eq!(days_in_month(2026, 2), 28);
        assert_eq!(days_in_month(2026, 12), 31);
    }

    #[test]
    fn spotify_uris_are_validated_before_reaching_the_os() {
        assert!(valid_spotify_uri("spotify:track:4uLU6hMCjMI75M1A2tKUQC"));
        assert!(!valid_spotify_uri("spotify:track:x & calc"));
        assert!(!valid_spotify_uri("https://example.com"));
        assert!(!valid_spotify_uri(""));
    }

    #[test]
    fn local_file_uris_are_allowed_but_not_shell_metacharacters() {
        assert!(valid_spotify_uri("spotify:local:Some+Artist:Album%20Name:Title.v2:215"));
        assert!(!valid_spotify_uri("spotify:local:a b"));
        assert!(!valid_spotify_uri("spotify:track:x\"&calc"));
        assert!(!valid_spotify_uri("spotify:track:x;y"));
    }

    #[test]
    fn list_reports_current_play_and_goes_past_5000() {
        let (st, d) = store();
        let n = dt(2026, 9, 23, 20, 0);
        let id = st.record_play("u", "A", "T", "", &iso(dt(2026, 9, 23, 19, 0))).unwrap();
        assert_eq!(list_json(&st, "", 60, n).unwrap()["current_id"], id);
        st.clear_current();
        assert_eq!(list_json(&st, "", 60, n).unwrap()["current_id"], 0);
        assert!(MAX_LIMIT > 5000);
        // "more" is judged against the limit actually asked for, however large.
        assert_eq!(list_json(&st, "", 6000, n).unwrap()["more"], false);
        let _ = std::fs::remove_dir_all(d);
    }

    #[test]
    fn unique_path_never_overwrites() {
        let d = std::env::temp_dir().join(format!("statusify-uniq-{}", crate::state::now_ms()));
        std::fs::create_dir_all(&d).unwrap();
        let a = unique_path(&d, "Statusify Wrapped May 2026.png");
        std::fs::write(&a, b"x").unwrap();
        let b = unique_path(&d, "Statusify Wrapped May 2026.png");
        assert_ne!(a, b);
        assert!(b.file_name().unwrap().to_string_lossy().ends_with("May 2026 (2).png"));
        std::fs::write(&b, b"x").unwrap();
        assert!(unique_path(&d, "Statusify Wrapped May 2026.png").to_string_lossy().ends_with("(3).png"));
        let _ = std::fs::remove_dir_all(d);
    }

    #[test]
    fn png_signature_check() {
        assert!(is_png(b"\x89PNG\r\n\x1a\n....."));
        assert!(!is_png(b"GIF89a......"));
        assert!(!is_png(b""));
    }
}
