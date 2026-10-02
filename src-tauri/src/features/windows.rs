//! Mini player and desktop lyrics overlay windows (owner: windows agent).

use super::{Ctx, Feature};
use serde_json::Value;
use std::sync::Arc;

#[derive(Default)]
pub struct Windows;

impl Feature for Windows {
    fn name(&self) -> &'static str {
        "windows"
    }
    fn start(&self, _ctx: &Arc<Ctx>) {}
    fn call(&self, _ctx: &Arc<Ctx>, action: &str, _args: Value) -> Result<Value, String> {
        Err(format!("windows: unknown action {action}"))
    }
}
