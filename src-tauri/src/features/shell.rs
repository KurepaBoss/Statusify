//! App shell (owner: shell agent): tray, hotkeys, start with Windows, single instance, window geometry, Discord App ID setup.

use super::{Ctx, Feature};
use serde_json::Value;
use std::sync::Arc;

#[derive(Default)]
pub struct Shell;

impl Feature for Shell {
    fn name(&self) -> &'static str {
        "shell"
    }
    fn start(&self, _ctx: &Arc<Ctx>) {}
    fn call(&self, _ctx: &Arc<Ctx>, action: &str, _args: Value) -> Result<Value, String> {
        Err(format!("shell: unknown action {action}"))
    }
}
