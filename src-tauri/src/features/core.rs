//! Core playback features (owner: core agent): the RPC switch, per-track and
//! global lyric offsets, instrumental-gap state, the blacklist, the sleep
//! timer, update check / install and Spicetify bridge install / repair /
//! health. Pushes extras["core"]:
//!
//! { rpc_enabled, presence_on, discord_line, line, next_line (the lyric
//!   being sung / the next different one, offset applied), track_offset_ms,
//!   track_offset_is_song, global_delay_ms, sleep_label, sleep_remaining_s,
//!   sleep_value, in_instrumental, instrumental_gap {start_ms,end_ms}|null,
//!   dropped_lines, rate_limited_until_ms, blacklisted, lrclib_enabled,
//!   update_available ({tag,url,changelog,setup}|null), bridge_warning,
//!   bridge_outdated }
//!
//! Actions: get_state; set_rpc_enabled {enabled}; toggle_rpc;
//! set_track_offset {ms|null, global?}; nudge_track_offset {delta_ms, global?};
//! clear_track_offset; set_global_delay {ms}; sleep_set {value};
//! skip_instrumental; blacklist_current; unblacklist {artist?, title?};
//! check_update; install_update; repair_bridge; test_presence; reconnect_rpc.

use super::{Ctx, Feature};
use crate::bridge::Outbox;
use crate::config::Config;
use crate::engine::core_state::{self, BridgeHealth, PlayInfo, SleepSpec, Verdict};
use crate::engine::{is_blacklisted, maint, parse_blacklist, Engine, OFFSET_LIMIT_MS};
use crate::lyrics::{instrumental_gaps, offset_key, select_line};
use crate::state::{now_ms, Snapshot};
use serde_json::{json, Value};
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

const TICK: Duration = Duration::from_millis(200);
const HEALTH_EVERY: Duration = Duration::from_secs(15);
const REPAIR_SNOOZE: Duration = Duration::from_secs(120);
const REPAIR_DEADLINE: Duration = Duration::from_secs(300);
const OUTDATED_MSG: &str = "Lyrics bridge out of date in Spotify — click here to repair";
const REPAIRING_MSG: &str = "Repairing — follow the window that just opened (Spotify will restart)";

#[derive(Default)]
pub struct Core {
    rt: Mutex<Runtime>,
}

#[derive(Default)]
struct Runtime {
    health: Option<BridgeHealth>,
    health_checked: Option<Instant>,
    health_busy: bool,
    bridge_was_connected: bool,
    repair_until: Option<Instant>,
    last_extras: Option<Value>,
}

/// What the core actions work on (no Tauri handle, so they are testable).
pub struct Env<'a> {
    pub engine: &'a Arc<Engine>,
    pub config: &'a Config,
    pub outbox: &'a Outbox,
    pub appdata: Option<PathBuf>,
}

fn play_info(s: &Snapshot, now: i64) -> Option<PlayInfo> {
    s.track.as_ref().map(|t| PlayInfo {
        uri: t.uri.clone(),
        duration_ms: s.duration_ms,
        position_ms: s.estimated_position(now),
        is_playing: s.is_playing,
    })
}

/// The instrumental gap the current lyric position is in, if any.
fn current_gap(s: &Snapshot, offset_ms: i64, now: i64) -> Option<(i64, i64)> {
    if s.lyrics.mode != "synced" || s.track.is_none() {
        return None;
    }
    let pos = s.estimated_position(now) + offset_ms;
    instrumental_gaps(&s.lyrics.synced, s.duration_ms)
        .into_iter()
        .find(|g| g.start_ms <= pos && pos < g.end_ms)
        .map(|g| (g.start_ms, g.end_ms))
}

/// extras["core"] as of now.
pub fn state_json(e: &Engine, cfg: &Config) -> Value {
    let snap = e.snapshot();
    let now = now_ms();
    let inst = Instant::now();
    let uri = snap.track.as_ref().map(|t| t.uri.clone()).unwrap_or_default();
    let offset = e.offset_ms();
    let (line, next_line) = select_line(&snap.lyrics, snap.estimated_position(now) + offset, snap.duration_ms);
    let is_song = !uri.is_empty() && cfg.get("offsets", &offset_key(&uri)).is_some_and(|v| !v.is_empty());
    let gap = current_gap(&snap, offset, now);
    let play = play_info(&snap, now);
    let rpc_enabled = e.core.rpc_enabled();
    let lrclib = e.lrclib_enabled.load(std::sync::atomic::Ordering::Relaxed);
    e.core.with(|c| {
        let left = c.sleep.remaining(inst, play.as_ref());
        json!({
            "rpc_enabled": rpc_enabled,
            "presence_on": c.presence_running,
            "discord_line": c.discord_line,
            "line": line,
            "next_line": next_line,
            "track_offset_ms": offset,
            "track_offset_is_song": is_song,
            "global_delay_ms": e.global_delay_ms(),
            "sleep_label": c.sleep.label(inst, play.as_ref()),
            "sleep_remaining_s": left.map(|l| l.round()),
            "sleep_value": c.sleep.value(),
            "in_instrumental": gap.is_some(),
            "instrumental_gap": gap.map(|(a, b)| json!({"start_ms": a, "end_ms": b})),
            "dropped_lines": c.dropped_lines,
            "rate_limited_until_ms": c.rate_limited.map(|(w, at)| at + (w * 1000.0) as i64),
            "blacklisted": snap.track.as_ref().is_some_and(|t| t.blacklisted),
            "lrclib_enabled": lrclib,
            "update_available": c.update.clone(),
            "bridge_warning": c.bridge_warning,
            "bridge_outdated": c.bridge_outdated,
        })
    })
}

fn clamp(ms: i64) -> i64 {
    ms.clamp(-OFFSET_LIMIT_MS, OFFSET_LIMIT_MS)
}

fn arg_i64(a: &Value, k: &str) -> Option<i64> {
    a.get(k).and_then(|v| v.as_f64()).map(|f| f.round() as i64)
}

fn current(e: &Engine) -> Option<(String, String, String)> {
    e.snapshot().track.map(|t| (t.uri, t.artist, t.title))
}

/// Persist a per-track offset (None clears it back to the global).
fn set_track_offset(env: &Env, uri: &str, ms: Option<i64>) {
    let key = offset_key(uri);
    match ms {
        Some(v) => env.config.set("offsets", &key, &clamp(v).to_string()),
        None => env.config.remove("offsets", &key),
    }
    env.engine.config_changed();
}

fn set_global(env: &Env, ms: i64) {
    let v = clamp(ms);
    env.config.set("preferences", "lyric_delay_ms", &v.to_string());
    env.engine.config_changed();
    crate::log(&format!("Lyric delay set to {:+.1}s", v as f64 / 1000.0));
}

fn save_blacklist(env: &Env, terms: &[String]) {
    // Python writes the newlines as a literal "\n".
    env.config.set("preferences", "blacklist", &terms.join("\\n"));
    env.engine.config_changed();
}

/// Drop [offsets] rows written before offset_key existed: they parse as the
/// option "spotify" and can never match a track.
pub fn migrate_offset_keys(cfg: &Config) -> usize {
    let bad: Vec<String> = cfg.section("offsets").into_iter().map(|(k, _)| k).filter(|k| k.contains(':') || k == "spotify").collect();
    for k in &bad {
        cfg.remove("offsets", k);
    }
    bad.len()
}

/// Every action that needs no Tauri handle.
pub fn act(env: &Env, action: &str, args: &Value) -> Result<Value, String> {
    let e = env.engine;
    match action {
        "get_state" => {}
        "set_rpc_enabled" | "toggle_rpc" => {
            let on = if action == "toggle_rpc" {
                !e.core.rpc_enabled()
            } else {
                args.get("enabled").and_then(|v| v.as_bool()).ok_or("enabled: true/false expected")?
            };
            e.core.set_rpc_enabled(on);
            crate::log(&format!("RPC {}", if on { "enabled" } else { "disabled" }));
        }
        "set_track_offset" => {
            let ms = arg_i64(args, "ms");
            let global = args.get("global").and_then(|v| v.as_bool()).unwrap_or(false);
            match (current(e), global) {
                (Some((uri, ..)), false) => {
                    set_track_offset(env, &uri, ms);
                    crate::log(&match ms {
                        Some(v) => format!("Lyric offset for this song set to {:+.1}s", clamp(v) as f64 / 1000.0),
                        None => "Lyric offset for this song cleared; using the global delay".into(),
                    });
                }
                _ => set_global(env, ms.unwrap_or(0)),
            }
        }
        "nudge_track_offset" => {
            let d = arg_i64(args, "delta_ms").ok_or("delta_ms expected")?;
            let global = args.get("global").and_then(|v| v.as_bool()).unwrap_or(false);
            // While a song plays the stepper adjusts that song's own offset,
            // starting from whatever applies now; else the global delay.
            match (current(e), global) {
                (Some((uri, ..)), false) => {
                    let v = clamp(e.offset_ms_for(&uri) + d);
                    set_track_offset(env, &uri, Some(v));
                    crate::log(&format!("Lyric offset for this song set to {:+.1}s", v as f64 / 1000.0));
                }
                _ => set_global(env, e.global_delay_ms() + d),
            }
        }
        "clear_track_offset" => {
            if let Some((uri, ..)) = current(e) {
                set_track_offset(env, &uri, None);
                crate::log("Lyric offset for this song cleared; using the global delay");
            }
        }
        "set_global_delay" => set_global(env, arg_i64(args, "ms").ok_or("ms expected")?),
        "sleep_set" => {
            let spec = SleepSpec::from_json(args.get("value").unwrap_or(&Value::Null))?;
            let snap = e.snapshot();
            let play = play_info(&snap, now_ms());
            let label = e.core.with(|c| {
                c.sleep.set(spec, Instant::now(), play.as_ref());
                c.sleep.label(Instant::now(), play.as_ref())
            });
            crate::log(&format!("Sleep timer: {}", if label.is_empty() { "off" } else { &label }));
        }
        "skip_instrumental" => {
            if !env.outbox.send(json!({"type": "skip_instrumental"})) {
                return Err("Spotify isn't connected, so it can't be controlled from here".into());
            }
            crate::log("Skip instrumental → seeking to next lyric");
        }
        "blacklist_current" => {
            let (_, artist, title) = current(e).ok_or("Nothing is playing")?;
            let mut terms = parse_blacklist(&env.config.get_or("preferences", "blacklist", ""));
            let term = format!("{artist} {title}").trim().to_lowercase();
            if !is_blacklisted(&terms, &artist, &title) {
                terms.push(term);
                save_blacklist(env, &terms);
            }
        }
        "unblacklist" => {
            let cur = current(e);
            let artist = args.get("artist").and_then(|v| v.as_str()).map(str::to_string).or(cur.as_ref().map(|c| c.1.clone())).ok_or("artist expected")?;
            let title = args.get("title").and_then(|v| v.as_str()).map(str::to_string).or(cur.as_ref().map(|c| c.2.clone())).unwrap_or_default();
            let terms = parse_blacklist(&env.config.get_or("preferences", "blacklist", ""));
            // Every term that matches this track has to go for it to show.
            let (gone, keep): (Vec<String>, Vec<String>) = terms.into_iter().partition(|t| is_blacklisted(std::slice::from_ref(t), &artist, &title));
            if !gone.is_empty() {
                save_blacklist(env, &keep);
                crate::log(&format!("Blacklist: removed {}", gone.join(", ")));
            }
        }
        "test_presence" => {
            let running = e.core.with(|c| c.presence_running);
            if !running {
                return Err("No DISCORD_APP_ID in .env — presence is off".into());
            }
            if e.snapshot().discord_user.is_none() {
                return Err("Not connected to Discord — cannot send test".into());
            }
            e.core.with(|c| c.test_request = true);
        }
        "reconnect_rpc" => crate::discord::request_reconnect(),
        _ => return Err(format!("core: unknown action {action}")),
    }
    Ok(state_json(e, env.config))
}

/// Copy our bridge into Spicetify's Extensions folder if stale, and note
/// whether Spotify is running it (port of _install_bridge).
fn install_bridge(e: &Engine, appdata: &std::path::Path) {
    match maint::install_bridge(appdata, maint::BRIDGE_JS) {
        Ok(r) => {
            if r.copied {
                crate::log(&format!("[Bridge] Installed to: {}", r.path.display()));
            }
            if r.needs_apply {
                crate::log("Spicetify bridge is OUT OF DATE inside Spotify — run `spicetify apply` (restarting Spotify is not enough)");
            } else {
                crate::log("Spicetify bridge is current");
            }
            e.core.with(|c| {
                c.bridge_outdated = r.needs_apply;
                if r.needs_apply {
                    c.bridge_warning = OUTDATED_MSG.into();
                }
            });
        }
        Err(m) => crate::log(&m),
    }
}

impl Core {
    fn env<'a>(ctx: &'a Arc<Ctx>) -> Env<'a> {
        Env { engine: &ctx.engine, config: &ctx.config, outbox: &ctx.outbox, appdata: std::env::var_os("APPDATA").map(PathBuf::from) }
    }

    fn push(&self, ctx: &Ctx) {
        let v = state_json(&ctx.engine, &ctx.config);
        let mut rt = self.rt.lock().unwrap();
        if rt.last_extras.as_ref() != Some(&v) {
            rt.last_extras = Some(v.clone());
            drop(rt);
            ctx.engine.set_extra("core", v);
        }
    }

    /// Every TICK: the sleep timer, bridge health, the repair watch, extras.
    fn tick(self: &Arc<Self>, ctx: &Arc<Ctx>) {
        let e = &ctx.engine;
        let snap = e.snapshot();
        let now = Instant::now();
        let play = play_info(&snap, now_ms());
        if e.core.with(|c| c.sleep.tick(now, play.as_ref())) {
            ctx.outbox.send(json!({"type": "player", "action": "pause"}));
            crate::log("Sleep timer: paused Spotify");
        }

        // Bridge health: Spotify running, no bridge connected for a while.
        let mut verdict = None;
        let mut check = false;
        {
            let mut rt = self.rt.lock().unwrap();
            let was = rt.bridge_was_connected;
            let h = rt.health.get_or_insert_with(|| BridgeHealth::new(now));
            if snap.bridge_connected && !h.connected {
                verdict = h.on_connect();
            } else if !snap.bridge_connected && was {
                h.on_disconnect(now);
            }
            let needs = rt.health.as_ref().is_some_and(|h| h.needs_check());
            rt.bridge_was_connected = snap.bridge_connected;
            if needs && !rt.health_busy && rt.health_checked.is_none_or(|t| now - t >= HEALTH_EVERY) {
                rt.health_busy = true;
                rt.health_checked = Some(now);
                check = true;
            }
        }
        if let Some(v) = verdict {
            self.apply_health(ctx, v);
        }
        if check {
            let (me, c) = (self.clone(), ctx.clone());
            tokio::task::spawn_blocking(move || {
                let running = maint::spotify_running();
                let v = {
                    let mut rt = me.rt.lock().unwrap();
                    rt.health_busy = false;
                    rt.health.as_mut().and_then(|h| h.evaluate(running, Instant::now()))
                };
                if let Some(v) = v {
                    me.apply_health(&c, v);
                }
            });
        }

        // After a repair: clear the warning once Spotify runs our bridge.
        let until = self.rt.lock().unwrap().repair_until;
        if let (Some(until), Some(appdata)) = (until, Core::env(ctx).appdata) {
            if !maint::bridge_needs_apply(&appdata, maint::BRIDGE_JS) && snap.bridge_connected {
                self.rt.lock().unwrap().repair_until = None;
                e.core.with(|c| {
                    c.bridge_outdated = false;
                    c.bridge_warning.clear();
                });
                crate::log("Bridge repaired — Spotify is running the current bridge");
            } else if now >= until {
                self.rt.lock().unwrap().repair_until = None;
                let outdated = maint::bridge_needs_apply(&appdata, maint::BRIDGE_JS);
                e.core.with(|c| {
                    c.bridge_outdated = outdated;
                    c.bridge_warning = if outdated { OUTDATED_MSG.into() } else { String::new() };
                });
            }
        }
        self.push(ctx);
    }

    fn apply_health(&self, ctx: &Ctx, v: Verdict) {
        let repairing = self.rt.lock().unwrap().repair_until.is_some();
        match v {
            Verdict::Flag if !repairing => {
                crate::log(&format!("⚠ {}", core_state::HEALTH_MSG));
                ctx.engine.core.with(|c| c.bridge_warning = core_state::HEALTH_MSG.into());
            }
            Verdict::Clear => ctx.engine.core.with(|c| {
                if c.bridge_warning == core_state::HEALTH_MSG {
                    c.bridge_warning = if c.bridge_outdated { OUTDATED_MSG.into() } else { String::new() };
                }
            }),
            _ => {}
        }
    }

    fn check_update(&self, ctx: &Ctx) -> Result<Value, String> {
        let client = crate::lrclib::client();
        let res = tauri::async_runtime::block_on(maint::check_update(&client, maint::RELEASES_URL, maint::APP_VERSION));
        match res {
            Ok(u) => {
                if let Some(u) = &u {
                    crate::log(&format!("Update available: v{}", u["tag"].as_str().unwrap_or("?")));
                }
                ctx.engine.core.with(|c| c.update = u.clone());
                self.push(ctx);
                Ok(u.unwrap_or(Value::Null))
            }
            Err(err) => {
                crate::log(&format!("Update check failed: {err}"));
                Err(format!("Update check failed: {err}"))
            }
        }
    }

    /// Setup.exe installs update in place: download, verify the published
    /// SHA-256, run the installer silently and quit (it relaunches us).
    /// Portable/dev builds get the release page to open instead.
    fn install_update(&self, ctx: &Ctx) -> Result<Value, String> {
        let u = ctx.engine.core.with(|c| c.update.clone()).ok_or("No update available — check first")?;
        let url = u["url"].as_str().unwrap_or("").to_string();
        let app_dir = std::env::current_exe().ok().and_then(|p| p.parent().map(PathBuf::from)).unwrap_or_default();
        let (Some(setup), Some(sha)) = (u["setup"]["url"].as_str(), u["setup"]["sha256_url"].as_str()) else {
            return Ok(json!({"installed": false, "open_url": url}));
        };
        if !maint::is_installed(&app_dir) {
            return Ok(json!({"installed": false, "open_url": url}));
        }
        let client = crate::lrclib::client();
        let dest = std::env::temp_dir().join("statusify-update");
        let path = tauri::async_runtime::block_on(maint::download_verified(&client, setup, sha, &dest)).map_err(|err| {
            crate::log(&format!("Update download failed: {err}"));
            format!("Update failed: {err}")
        })?;
        crate::log(&format!("Update v{} downloaded and verified — installing", u["tag"].as_str().unwrap_or("?")));
        maint::launch_silent_update(&path).map_err(|err| format!("Could not start the installer: {err}"))?;
        ctx.app.exit(0);
        Ok(json!({"installed": true}))
    }

    /// Re-wire the bridge: refresh the Extensions copy, then run the
    /// Spicetify setup script in its own window (it runs `spicetify apply`,
    /// which restarts Spotify) and watch for the fix.
    fn repair_bridge(&self, ctx: &Arc<Ctx>) -> Result<Value, String> {
        let env = Core::env(ctx);
        if let Some(a) = &env.appdata {
            install_bridge(&ctx.engine, a);
        }
        let (script, bridge) = maint::stage_repair_files(&std::env::temp_dir().join("statusify-repair"))
            .map_err(|e| format!("Could not start repair: {e}"))?;
        maint::launch_repair(&script, &bridge).map_err(|e| format!("Could not start repair: {e}"))?;
        crate::log("Bridge repair started");
        let now = Instant::now();
        {
            let mut rt = self.rt.lock().unwrap();
            rt.repair_until = Some(now + REPAIR_DEADLINE);
            if let Some(h) = rt.health.as_mut() {
                h.snooze(now, REPAIR_SNOOZE); // the repair restarts Spotify
            }
        }
        ctx.engine.core.with(|c| c.bridge_warning = REPAIRING_MSG.into());
        self.push(ctx);
        Ok(state_json(&ctx.engine, &ctx.config))
    }
}

impl Feature for Core {
    fn name(&self) -> &'static str {
        "core"
    }

    fn start(&self, ctx: &Arc<Ctx>) {
        ctx.engine.set_config(ctx.config.clone());
        let n = migrate_offset_keys(&ctx.config);
        if n > 0 {
            crate::log(&format!("Cleared {n} unreadable per-track offset(s) from statusify.cfg"));
        }
        // Core is not held in an Arc by the registry's trait object, so the
        // ticker owns its own handle to the shared runtime state.
        let me = Arc::new(Core { rt: Mutex::new(Runtime::default()) });
        CORE.set(me.clone()).ok();
        let c = ctx.clone();
        tokio::spawn(async move {
            if let Some(a) = std::env::var_os("APPDATA").map(PathBuf::from) {
                let e = c.engine.clone();
                let _ = tokio::task::spawn_blocking(move || install_bridge(&e, &a)).await;
            }
            // Tick on a timer, and at once when the track, lyrics or settings
            // change so offsets and blacklist state never lag a beat.
            let mut events = c.engine.subscribe();
            loop {
                me.tick(&c);
                tokio::select! {
                    _ = tokio::time::sleep(TICK) => {}
                    ev = events.recv() => {
                        if matches!(ev, Err(tokio::sync::broadcast::error::RecvError::Closed)) {
                            tokio::time::sleep(TICK).await;
                        }
                    }
                }
            }
        });
        // Check for updates once the UI has settled.
        let c = ctx.clone();
        tokio::spawn(async move {
            tokio::time::sleep(Duration::from_secs(5)).await;
            if let Some(core) = CORE.get().cloned() {
                let _ = tokio::task::spawn_blocking(move || core.check_update(&c)).await;
            }
        });
    }

    fn call(&self, ctx: &Arc<Ctx>, action: &str, args: Value) -> Result<Value, String> {
        let core = CORE.get().cloned();
        let res = match (action, &core) {
            ("check_update", Some(c)) => return c.check_update(ctx),
            ("install_update", Some(c)) => return c.install_update(ctx),
            ("repair_bridge", Some(c)) => return c.repair_bridge(ctx),
            ("check_update" | "install_update" | "repair_bridge", None) => Err("core is not started yet".into()),
            _ => act(&Core::env(ctx), action, &args),
        };
        if let (Ok(v), Some(c)) = (&res, &core) {
            let mut rt = c.rt.lock().unwrap();
            if rt.last_extras.as_ref() != Some(v) {
                rt.last_extras = Some(v.clone());
                drop(rt);
                ctx.engine.set_extra("core", v.clone());
            }
        }
        res
    }
}

static CORE: std::sync::OnceLock<Arc<Core>> = std::sync::OnceLock::new();

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::AtomicBool;

    struct T {
        engine: Arc<Engine>,
        config: Config,
        outbox: Outbox,
        dir: PathBuf,
    }

    impl T {
        fn new(cfg: &str) -> T {
            let dir = std::env::temp_dir().join(format!("statusify-core-{}-{}", now_ms(), rand()));
            std::fs::create_dir_all(&dir).unwrap();
            std::fs::write(dir.join("statusify.cfg"), cfg).unwrap();
            let mut engine = Engine::new(None, |_| {});
            Arc::get_mut(&mut engine).unwrap().lrclib_enabled = AtomicBool::new(false);
            let config = Config::open(&dir);
            T { engine, config, outbox: Outbox::default(), dir }
        }
        fn env(&self) -> Env<'_> {
            Env { engine: &self.engine, config: &self.config, outbox: &self.outbox, appdata: None }
        }
        fn act(&self, a: &str, args: Value) -> Result<Value, String> {
            act(&self.env(), a, &args)
        }
        fn track(&self, uri: &str, artist: &str, title: &str) {
            self.engine.handle(&json!({"type":"track_change","track_uri":uri,"artist":artist,"title":title,"duration_ms":100000}));
        }
    }

    impl Drop for T {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.dir);
        }
    }

    fn rand() -> u128 {
        std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos()
    }

    #[tokio::test]
    async fn rpc_switch_toggles() {
        let t = T::new("");
        assert_eq!(t.act("get_state", json!({})).unwrap()["rpc_enabled"], true);
        assert_eq!(t.act("toggle_rpc", json!({})).unwrap()["rpc_enabled"], false);
        assert!(!t.engine.core.rpc_enabled());
        assert_eq!(t.act("set_rpc_enabled", json!({"enabled": true})).unwrap()["rpc_enabled"], true);
        assert!(t.act("set_rpc_enabled", json!({})).is_err());
        assert!(t.act("nope", json!({})).is_err());
    }

    #[tokio::test]
    async fn offsets_are_per_track_with_a_global_fallback() {
        let t = T::new("[preferences]\nlyric_delay_ms = -40\n");
        // One Config instance shared by the engine and the actions, as in the app.
        let cfg = Arc::new(Config::open(&t.dir));
        let mut engine = Engine::new(None, |_| {});
        Arc::get_mut(&mut engine).unwrap().lrclib_enabled = AtomicBool::new(false);
        engine.set_config(cfg.clone());
        let env = Env { engine: &engine, config: &cfg, outbox: &t.outbox, appdata: None };

        // No track: the stepper moves the global delay.
        let s = act(&env, "nudge_track_offset", &json!({"delta_ms": 250})).unwrap();
        assert_eq!(s["global_delay_ms"], 210);
        engine.handle(&json!({"type":"track_change","track_uri":"spotify:track:abc","artist":"A","title":"T","duration_ms":100000}));
        let s = act(&env, "get_state", &json!({})).unwrap();
        assert_eq!(s["track_offset_ms"], 210);
        assert_eq!(s["track_offset_is_song"], false);
        // With a track: that track's own offset, starting from what applies now.
        let s = act(&env, "nudge_track_offset", &json!({"delta_ms": 250})).unwrap();
        assert_eq!(s["track_offset_ms"], 460);
        assert_eq!(s["track_offset_is_song"], true);
        assert_eq!(cfg.get("offsets", "abc").as_deref(), Some("460"));
        assert_eq!(engine.offset_ms(), 460);
        // Clamped to ±5 s
        let s = act(&env, "set_track_offset", &json!({"ms": 9000})).unwrap();
        assert_eq!(s["track_offset_ms"], 5000);
        let s = act(&env, "set_track_offset", &json!({"ms": null})).unwrap();
        assert_eq!(s["track_offset_ms"], 210);
        assert!(cfg.get("offsets", "abc").is_none());
        let s = act(&env, "set_global_delay", &json!({"ms": -100})).unwrap();
        assert_eq!(s["track_offset_ms"], -100);
        // On disk, readable by the Python app
        let text = std::fs::read_to_string(t.dir.join("statusify.cfg")).unwrap();
        assert!(text.contains("lyric_delay_ms = -100"));
    }

    #[tokio::test]
    async fn blacklist_follows_config_live() {
        let t = T::new("[preferences]\nblacklist = nickelback\\nfoo\n");
        let cfg = Arc::new(Config::open(&t.dir));
        let mut engine = Engine::new(None, |_| {});
        Arc::get_mut(&mut engine).unwrap().lrclib_enabled = AtomicBool::new(false);
        engine.set_config(cfg.clone());
        let env = Env { engine: &engine, config: &cfg, outbox: &t.outbox, appdata: None };
        engine.handle(&json!({"type":"track_change","track_uri":"u1","artist":"Nickelback","title":"Photograph","duration_ms":1000}));
        assert!(engine.snapshot().track.unwrap().blacklisted);
        let s = act(&env, "unblacklist", &json!({})).unwrap();
        assert_eq!(s["blacklisted"], false);
        assert_eq!(cfg.get("preferences", "blacklist").as_deref(), Some("foo"));
        let s = act(&env, "blacklist_current", &json!({})).unwrap();
        assert_eq!(s["blacklisted"], true);
        assert_eq!(cfg.get("preferences", "blacklist").as_deref(), Some("foo\\nnickelback photograph"));
        // A hand edit (settings page) applies on config_changed
        cfg.set("preferences", "blacklist", "");
        engine.config_changed();
        assert!(!engine.snapshot().track.unwrap().blacklisted);
    }

    #[tokio::test]
    async fn lrclib_switch_follows_config() {
        let t = T::new("[preferences]\nlrclib_fallback = false\n");
        let cfg = Arc::new(Config::open(&t.dir));
        let engine = Engine::new(None, |_| {});
        engine.set_config(cfg.clone());
        assert!(!engine.lrclib_enabled.load(std::sync::atomic::Ordering::Relaxed));
        cfg.set("preferences", "lrclib_fallback", "true");
        engine.config_changed();
        assert!(engine.lrclib_enabled.load(std::sync::atomic::Ordering::Relaxed));
    }

    #[tokio::test]
    async fn sleep_timer_state_is_reported() {
        let t = T::new("");
        let s = t.act("sleep_set", json!({"value": 30})).unwrap();
        assert_eq!(s["sleep_value"], "30");
        assert_eq!(s["sleep_label"], "Sleep in 30:00");
        assert_eq!(s["sleep_remaining_s"], 1800.0);
        t.track("u", "A", "T");
        let s = t.act("sleep_set", json!({"value": "eos"})).unwrap();
        assert_eq!(s["sleep_value"], "eos");
        assert!(s["sleep_label"].as_str().unwrap().starts_with("Sleep at end of song"));
        let s = t.act("sleep_set", json!({"value": null})).unwrap();
        assert_eq!(s["sleep_value"], "off");
        assert_eq!(s["sleep_label"], "");
        assert!(s["sleep_remaining_s"].is_null());
        assert!(t.act("sleep_set", json!({"value": "later"})).is_err());
    }

    #[tokio::test]
    async fn bridge_commands_need_a_bridge_and_test_presence_needs_discord() {
        let t = T::new("");
        assert!(t.act("skip_instrumental", json!({})).is_err());
        assert!(t.act("test_presence", json!({})).unwrap_err().contains("DISCORD_APP_ID"));
        t.engine.core.with(|c| c.presence_running = true);
        assert!(t.act("test_presence", json!({})).unwrap_err().contains("Not connected"));
        t.engine.update(|s| s.discord_user = Some("me".into()));
        t.act("test_presence", json!({})).unwrap();
        assert!(t.engine.core.with(|c| c.test_request));
    }

    #[tokio::test]
    async fn instrumental_state_and_dropped_reset() {
        let t = T::new("");
        t.engine.core.with(|c| c.dropped_lines = 3);
        t.track("u", "A", "T");
        assert_eq!(t.act("get_state", json!({})).unwrap()["dropped_lines"], 0);
        // a 20 s break between lines at 0..9 s and 30 s
        let mut synced: Vec<Value> = (0..4).map(|i| json!({"startMs": i * 3000, "words": "w"})).collect();
        synced.push(json!({"startMs": 30000, "words": "w"}));
        synced.push(json!({"startMs": 33000, "words": "w"}));
        t.engine.handle(&json!({"type":"lyrics","track_uri":"u","mode":"synced","synced":synced}));
        t.engine.handle(&json!({"type":"position","position_ms":20000,"duration_ms":36000,"is_playing":false}));
        let s = t.act("get_state", json!({})).unwrap();
        assert_eq!(s["in_instrumental"], true);
        assert_eq!(s["instrumental_gap"]["start_ms"], 12000);
        assert_eq!(s["instrumental_gap"]["end_ms"], 30000);
        t.engine.handle(&json!({"type":"position","position_ms":31000,"duration_ms":36000,"is_playing":false}));
        assert_eq!(t.act("get_state", json!({})).unwrap()["in_instrumental"], false);
    }

    #[test]
    fn bad_offset_keys_are_migrated() {
        let dir = std::env::temp_dir().join(format!("statusify-core-mig-{}", rand()));
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("statusify.cfg"), "[offsets]\nspotify = track:abc = 250\nabc = 100\n").unwrap();
        let c = Config::open(&dir);
        assert_eq!(migrate_offset_keys(&c), 1);
        assert_eq!(c.section("offsets"), vec![("abc".to_string(), "100".to_string())]);
        let _ = std::fs::remove_dir_all(dir);
    }
}
