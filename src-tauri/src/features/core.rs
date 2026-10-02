//! Core playback features (owner: core agent): presence planner hooks, offsets, instrumental gaps, blacklist, RPC toggles, sleep timer, maintenance.

use super::{Ctx, Feature};
use serde_json::Value;
use std::sync::Arc;

#[derive(Default)]
pub struct Core;

impl Feature for Core {
    fn name(&self) -> &'static str {
        "core"
    }
    fn start(&self, _ctx: &Arc<Ctx>) {}
    fn call(&self, _ctx: &Arc<Ctx>, action: &str, _args: Value) -> Result<Value, String> {
        Err(format!("core: unknown action {action}"))
    }
}
