mod bridge;
mod config;
mod db;
mod discord;
mod engine;
mod features;
mod lrclib;
mod lyrics;
mod presence;
mod state;

use engine::Engine;
use features::{Ctx, Feature};
use serde_json::{json, Value};
use std::io::Write;
use std::path::PathBuf;
use std::sync::{Arc, Mutex, OnceLock};
use tauri::{Emitter, Manager};
use tokio::sync::mpsc;

static LOG: OnceLock<Mutex<Option<std::fs::File>>> = OnceLock::new();

pub fn log(msg: &str) {
    let line = format!("{}  {}\n", chrono::Local::now().format("%Y-%m-%d %H:%M:%S"), msg);
    eprint!("{line}");
    if let Some(m) = LOG.get() {
        if let Some(f) = m.lock().unwrap().as_mut() {
            let _ = f.write_all(line.as_bytes());
        }
    }
}

/// Where history.db, statusify.cfg, .env and the log live:
/// STATUSIFY_DATA_DIR, else the folder the exe is in.
pub fn data_dir() -> PathBuf {
    std::env::var_os("STATUSIFY_DATA_DIR")
        .map(PathBuf::from)
        .or_else(|| std::env::current_exe().ok().and_then(|p| p.parent().map(PathBuf::from)))
        .unwrap_or_else(|| PathBuf::from("."))
}

struct App {
    ctx: Arc<Ctx>,
    features: Vec<Arc<dyn Feature>>,
}

#[tauri::command]
fn snapshot(app: tauri::State<App>) -> state::Snapshot {
    app.ctx.engine.snapshot()
}

#[tauri::command]
fn recent_plays(app: tauri::State<App>, limit: i64) -> Vec<db::Play> {
    app.ctx.engine.recent_plays(limit)
}

/// Transport: play, pause, next, prev, toggle, shuffle, repeat, like.
#[tauri::command]
fn player(app: tauri::State<App>, action: String) -> bool {
    app.ctx.outbox.send(json!({"type": "player", "action": action}))
}

#[tauri::command]
fn seek(app: tauri::State<App>, position_ms: i64) -> bool {
    app.ctx.outbox.send(json!({"type": "seek", "position_ms": position_ms.max(0)}))
}

/// The one entry point to every feature module (see features/mod.rs).
#[tauri::command]
async fn call(app: tauri::State<'_, App>, feature: String, action: String, args: Option<Value>) -> Result<Value, String> {
    let f = app
        .features
        .iter()
        .find(|f| f.name() == feature)
        .cloned()
        .ok_or_else(|| format!("no feature {feature}"))?;
    let ctx = app.ctx.clone();
    tauri::async_runtime::spawn_blocking(move || f.call(&ctx, &action, args.unwrap_or(Value::Null)))
        .await
        .map_err(|e| e.to_string())?
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let dir = data_dir();
    let _ = dotenvy::from_path(dir.join(".env"));
    let _ = LOG.set(Mutex::new(
        std::fs::OpenOptions::new().create(true).append(true).open(dir.join("statusify-rs.log")).ok(),
    ));
    log(&format!("Statusify-rs {} · data in {}", env!("CARGO_PKG_VERSION"), dir.display()));

    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .setup(move |app| {
            let handle = app.handle().clone();
            let store = match db::Store::open(&dir) {
                Ok(s) => Some(s),
                Err(e) => {
                    log(&format!("history.db unavailable: {e}"));
                    None
                }
            };
            let h = handle.clone();
            let engine = Engine::new(store, move |s| {
                let _ = h.emit("snapshot", s);
            });
            let config = Arc::new(config::Config::open(&dir));
            let outbox = bridge::Outbox::default();
            let ctx = Arc::new(Ctx { engine: engine.clone(), outbox: outbox.clone(), config, app: handle, data_dir: dir.clone() });
            let feats = features::all();
            app.manage(App { ctx: ctx.clone(), features: feats.clone() });

            let port: u16 = std::env::var("STATUSIFY_PORT").ok().and_then(|p| p.parse().ok()).unwrap_or(8765);
            let e = engine.clone();
            tauri::async_runtime::spawn(async move {
                match bridge::bind(port).await {
                    Ok(l) => {
                        log(&format!("WebSocket ready  ·  ws://127.0.0.1:{port}"));
                        bridge::serve(l, e, outbox).await
                    }
                    Err(err) => {
                        log(&format!("Port {port} unavailable: {err}"));
                        e.update(|s| s.note = format!("Port {port} is in use — is the old Statusify still running?"));
                    }
                }
            });

            match std::env::var("DISCORD_APP_ID").ok().filter(|s| !s.trim().is_empty()) {
                Some(id) => {
                    let (tx, rx) = mpsc::unbounded_channel();
                    let e = engine.clone();
                    tauri::async_runtime::spawn(discord::run(id, rx, move |st| match st {
                        discord::Status::Connected(u) => {
                            log(&format!("RPC handshake OK  ·  {u}"));
                            e.update(|s| s.discord_user = Some(u));
                        }
                        discord::Status::Disconnected => e.update(|s| s.discord_user = None),
                    }));
                    tauri::async_runtime::spawn(presence::run_loop(engine.clone(), tx));
                }
                None => engine.update(|s| s.note = "No DISCORD_APP_ID in .env — presence is off".into()),
            }

            let c = ctx.clone();
            tauri::async_runtime::spawn(async move {
                for f in feats {
                    f.start(&c);
                }
            });
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![snapshot, recent_plays, player, seek, call])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
