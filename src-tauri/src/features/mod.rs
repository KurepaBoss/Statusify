//! Feature modules. Each owns its own file(s) and plugs in here, so features
//! can be built in parallel without editing shared code.
//!
//! A feature gets a shared `Ctx` (engine, bridge outbox, config, app handle,
//! data dir). The frontend reaches any feature through one Tauri command:
//!   invoke("call", { feature: "history", action: "top_songs", args: {...} })
//! which lands in `Feature::call`. Push data to the UI with
//! `ctx.engine.set_extra(key, value)` (arrives in snapshot.extras) or
//! `ctx.app.emit(event, payload)`. React to the app with
//! `ctx.engine.subscribe()` (see engine::Event).

use crate::{bridge::Outbox, config::Config, engine::Engine};
use serde_json::Value;
use std::path::PathBuf;
use std::sync::Arc;

pub mod core;
pub mod history;
pub mod nowplaying;
pub mod settings;
pub mod shell;
pub mod translate;
pub mod windows;

pub struct Ctx {
    pub engine: Arc<Engine>,
    pub outbox: Outbox,
    pub config: Arc<Config>,
    pub app: tauri::AppHandle,
    pub data_dir: PathBuf,
    /// All features, for calling one from another (set once at startup).
    pub features: std::sync::OnceLock<Vec<Arc<dyn Feature>>>,
}

impl Ctx {
    /// Call another feature's action, e.g. ctx.call("core", "toggle_rpc", json!({})).
    pub fn call(self: &Arc<Self>, feature: &str, action: &str, args: Value) -> Result<Value, String> {
        let f = self
            .features
            .get()
            .and_then(|fs| fs.iter().find(|f| f.name() == feature).cloned())
            .ok_or_else(|| format!("no feature {feature}"))?;
        f.call(self, action, args)
    }
}

pub trait Feature: Send + Sync {
    fn name(&self) -> &'static str;
    /// Called once at startup, inside the async runtime (tokio::spawn works).
    fn start(&self, _ctx: &Arc<Ctx>) {}
    /// Runs on a worker thread, not the UI thread; blocking is fine.
    /// Use tauri::async_runtime::block_on for async work.
    fn call(&self, _ctx: &Arc<Ctx>, action: &str, _args: Value) -> Result<Value, String> {
        Err(format!("{}: unknown action {action}", self.name()))
    }
}

pub fn all() -> Vec<Arc<dyn Feature>> {
    vec![
        Arc::new(core::Core::default()),
        Arc::new(history::History::default()),
        Arc::new(nowplaying::NowPlaying::default()),
        Arc::new(settings::Settings::default()),
        Arc::new(shell::Shell::default()),
        Arc::new(translate::Translate::default()),
        Arc::new(windows::Windows::default()),
    ]
}
