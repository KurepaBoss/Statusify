mod app_icon;
mod backoff;
mod bridge;
#[cfg(test)]
mod bench_bridge;
mod config;
mod db;
mod discord;
mod emitter;
mod engine;
mod features;
mod lrclib;
mod lyrics;
mod panic_log;
mod presence;
mod shell_hotkeys;
mod shell_tray;
mod shell_window;
mod state;
mod autostart;
mod datadir;

use engine::Engine;
use features::{Ctx, Feature};
use serde_json::{json, Value};
use std::io::Write;
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

/// What must be saved when the process goes away: the play in progress (its
/// listening time, and the play itself if it just passed the 20 s mark) and
/// "Remember history = off". Runs from shell_window::quit_prepare, which every
/// quit, restart and update-install path goes through, and again from the
/// runtime's Exit event as the catch-all (so a path that forgot still saves).
/// Safe to run twice.
pub fn shutdown(ctx: &Ctx) {
    ctx.engine.finish_play();
    features::history::wipe_if_off(ctx);
}

/// What the Discord note says until an Application ID is saved. (The UI makes
/// the note clickable when it mentions the bridge, Spicetify, a repair or an
/// update, so none of those words may appear here.)
pub const NO_APP_ID_NOTE: &str = "No Discord Application ID yet. Add one in Settings, under Discord, to show your status.";

/// The Discord connection and presence loop, and the App ID they were started
/// with. Restarting them is how an App ID switch and "Reconnect" take effect
/// without restarting the app.
static DISCORD: Mutex<Option<(String, Vec<tauri::async_runtime::JoinHandle<()>>)>> = Mutex::new(None);

/// The Discord App ID the connection is using right now.
pub fn running_app_id() -> Option<String> {
    DISCORD.lock().unwrap().as_ref().map(|(id, _)| id.clone())
}

/// (Re)start the Discord connection with `id`, dropping any previous one.
pub fn start_discord(engine: &Arc<Engine>, id: &str) {
    let mut slot = DISCORD.lock().unwrap();
    if let Some((_, old)) = slot.take() {
        for h in old {
            h.abort();
        }
        engine.update(|s| s.discord_user = None);
    }
    let (tx, rx) = mpsc::unbounded_channel();
    let e = engine.clone();
    let conn = tauri::async_runtime::spawn(discord::run(id.to_string(), rx, move |st| match st {
        // (discord::run logs the handshake: once per outage, not per READY)
        discord::Status::Connected(u) => e.update(|s| s.discord_user = Some(u)),
        discord::Status::Disconnected => e.update(|s| s.discord_user = None),
    }));
    let presence = tauri::async_runtime::spawn(presence::run_loop(engine.clone(), tx));
    *slot = Some((id.to_string(), vec![conn, presence]));
    engine.update(|s| {
        if s.note == NO_APP_ID_NOTE {
            s.note.clear();
        }
    });
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
    // First, so that a panic anywhere (the release build aborts on one) leaves a line in the log.
    panic_log::install();
    let resolved = datadir::choose();
    let dir = resolved.dir.clone();
    let made = datadir::prepare(&dir);
    datadir::rotate_log(&dir.join("statusify-rs.log"), 1_000_000);
    let _ = dotenvy::from_path(dir.join(".env"));
    let _ = LOG.set(Mutex::new(
        std::fs::OpenOptions::new().create(true).append(true).open(dir.join("statusify-rs.log")).ok(),
    ));
    log(&format!("Statusify {} · data in {} ({})", env!("CARGO_PKG_VERSION"), dir.display(), resolved.mode.label()));
    if let Err(e) = made {
        log(&format!("Could not create the data folder: {e}"));
    }

    // The taskbar identity must be set before the first window exists.
    let context = tauri::generate_context!();
    app_icon::set_app_user_model_id(&context.config().identifier);

    tauri::Builder::default()
        // Shell plugins (single-instance first: a second launch only focuses the first).
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| shell_window::show_main(app)))
        .plugin(app_icon::plugin())
        .on_window_event(app_icon::on_window_event)
        .plugin(tauri_plugin_global_shortcut::Builder::new().build())
        .plugin(tauri_plugin_autostart::Builder::new().app_name(autostart::RUN_VALUE).build())
        .plugin(tauri_plugin_dialog::init())
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
            // Changes reach the windows through the emitter: one "snapshot"
            // per burst, a small "position" for the clock alone.
            let emitter = Arc::new(emitter::Emitter::default());
            let em = emitter.clone();
            let engine = Engine::new(store, move |_, c| em.mark(c));
            let (h, e) = (handle.clone(), engine.clone());
            tauri::async_runtime::spawn(async move {
                emitter
                    .run(emitter::GAP, |c| {
                        let _ = match c {
                            engine::Change::Full => h.emit("snapshot", e.snapshot()),
                            engine::Change::Position => h.emit("position", e.position_json()),
                        };
                    })
                    .await
            });
            let config = Arc::new(config::Config::open(&dir));
            let outbox = bridge::Outbox::default();
            let ctx = Arc::new(Ctx { engine: engine.clone(), outbox: outbox.clone(), config, app: handle, data_dir: dir.clone(), features: Default::default() });
            let feats = features::all();
            let _ = ctx.features.set(feats.clone());
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
                        e.update(|s| s.note = format!("Port {port} is in use. Close any other Statusify (the older Python one too), then start this one again."));
                    }
                }
            });

            match std::env::var("DISCORD_APP_ID").ok().filter(|s| !s.trim().is_empty()) {
                Some(id) => start_discord(&engine, id.trim()),
                None => engine.update(|s| s.note = NO_APP_ID_NOTE.into()),
            }

            // The shell starts first and on its own task: it shows the window and
            // the tray, so a slow or panicking feature can never leave the app with
            // neither. The window is also force-shown if the shell never got there.
            let (c, shell_feats): (_, Vec<_>) = (ctx.clone(), feats.iter().filter(|f| f.name() == "shell").cloned().collect());
            tauri::async_runtime::spawn(async move {
                for f in shell_feats {
                    f.start(&c);
                }
            });
            let c = ctx.clone();
            tauri::async_runtime::spawn(async move {
                for f in feats.into_iter().filter(|f| f.name() != "shell") {
                    f.start(&c);
                }
            });
            shell_window::show_fallback(&ctx);
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![snapshot, recent_plays, player, seek, call])
        .build(context)
        .expect("error while building tauri application")
        .run(|app, event| {
            if let tauri::RunEvent::Exit = event {
                if let Some(a) = app.try_state::<App>() {
                    shutdown(&a.ctx);
                }
            }
        });
}
