//! History, Stats and Wrapped export (owner: history agent).

use super::{Ctx, Feature};
use serde_json::Value;
use std::sync::Arc;

#[derive(Default)]
pub struct History;

impl Feature for History {
    fn name(&self) -> &'static str {
        "history"
    }
    fn start(&self, _ctx: &Arc<Ctx>) {}
    fn call(&self, _ctx: &Arc<Ctx>, action: &str, _args: Value) -> Result<Value, String> {
        Err(format!("history: unknown action {action}"))
    }
}
