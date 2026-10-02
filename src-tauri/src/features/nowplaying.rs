//! Now Playing extras (owner: nowplaying agent): player state, queue, lyric search/pin, cover colours, art cache.

use super::{Ctx, Feature};
use serde_json::Value;
use std::sync::Arc;

#[derive(Default)]
pub struct NowPlaying;

impl Feature for NowPlaying {
    fn name(&self) -> &'static str {
        "nowplaying"
    }
    fn start(&self, _ctx: &Arc<Ctx>) {}
    fn call(&self, _ctx: &Arc<Ctx>, action: &str, _args: Value) -> Result<Value, String> {
        Err(format!("nowplaying: unknown action {action}"))
    }
}
