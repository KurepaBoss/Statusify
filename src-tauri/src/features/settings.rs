//! Settings page backend (owner: shell agent).

use super::{Ctx, Feature};
use serde_json::Value;
use std::sync::Arc;

#[derive(Default)]
pub struct Settings;

impl Feature for Settings {
    fn name(&self) -> &'static str {
        "settings"
    }
    fn start(&self, _ctx: &Arc<Ctx>) {}
    fn call(&self, _ctx: &Arc<Ctx>, action: &str, _args: Value) -> Result<Value, String> {
        Err(format!("settings: unknown action {action}"))
    }
}
