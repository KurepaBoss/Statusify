//! Lyric translation and romanisation (owner: nowplaying agent).

use super::{Ctx, Feature};
use serde_json::Value;
use std::sync::Arc;

#[derive(Default)]
pub struct Translate;

impl Feature for Translate {
    fn name(&self) -> &'static str {
        "translate"
    }
    fn start(&self, _ctx: &Arc<Ctx>) {}
    fn call(&self, _ctx: &Arc<Ctx>, action: &str, _args: Value) -> Result<Value, String> {
        Err(format!("translate: unknown action {action}"))
    }
}
