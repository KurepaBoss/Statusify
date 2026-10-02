//! App shell (owner: shell agent): tray, hotkeys, start with Windows, single
//! instance (lib.rs plugin), window geometry / close-to-tray / start
//! minimised / always on top, Discord App ID profiles.
//!
//! The window, tray and hotkey mechanics live in src/shell_*.rs; this file is
//! the feature the frontend and other features call.
//!
//! Actions: get_state, set_autostart {enabled}, set_hotkeys {skip, toggle,
//! skip_instr, overlay}, center_window, create_shortcut, add_profile {name,
//! app_id}, delete_profile {name}, switch_profile {app_id}, set_app_id
//! {app_id} (both apply live, no restart), restart_app, reconnect_rpc, show_window,
//! hide_to_tray, toggle_fullscreen, exit_fullscreen, quit.

use super::{Ctx, Feature};
use crate::{shell_hotkeys as hk, shell_tray::Tray, shell_window as win};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use tauri::Emitter;
use tauri_plugin_autostart::ManagerExt;

#[derive(Default)]
struct Inner {
    tray: Arc<Tray>,
    applied: Mutex<Vec<(&'static str, String)>>,
    errors: Mutex<HashMap<String, String>>,
    pushed: Mutex<Value>,
}

#[derive(Default)]
pub struct Shell {
    inner: Arc<Inner>,
}

// ── .env and profiles (pure helpers, tested below) ───────────────

/// `text` with KEY=value set: the existing line replaced, else appended.
pub fn env_set(text: &str, key: &str, value: &str) -> String {
    let prefix = format!("{key}=");
    let mut out = String::new();
    let mut written = false;
    for line in text.lines() {
        if line.starts_with(&prefix) {
            if !written {
                out.push_str(&format!("{prefix}{value}\n"));
                written = true;
            }
        } else {
            out.push_str(line);
            out.push('\n');
        }
    }
    if !written {
        if !out.is_empty() && !out.ends_with("\n\n") {
            out.push('\n');
        }
        out.push_str(&format!("{prefix}{value}\n"));
    }
    out
}

pub fn env_get(text: &str, key: &str) -> Option<String> {
    let prefix = format!("{key}=");
    text.lines()
        .find_map(|l| l.trim().strip_prefix(prefix.as_str()).map(|v| v.trim().trim_matches('"').trim_matches('\'').to_string()))
        .filter(|v| !v.is_empty())
}

/// A Discord application id: a long run of digits.
pub fn valid_app_id(id: &str) -> bool {
    id.len() >= 16 && id.chars().all(|c| c.is_ascii_digit())
}

pub fn check_profile(name: &str, app_id: &str) -> Result<(String, String), String> {
    let (n, a) = (name.trim(), app_id.trim());
    if n.is_empty() || a.is_empty() {
        return Err("Both a name and an App ID are required".into());
    }
    if n.chars().any(|c| "=:[]\n".contains(c)) {
        return Err("Name cannot contain  =  :  [  ]".into());
    }
    if !valid_app_id(a) {
        return Err("App ID must be a long numeric ID".into());
    }
    Ok((n.to_string(), a.to_string()))
}

fn env_path(ctx: &Ctx) -> std::path::PathBuf {
    ctx.data_dir.join(".env")
}

/// The id written in .env (what the next start will use).
fn configured_app_id(ctx: &Ctx) -> Option<String> {
    std::fs::read_to_string(env_path(ctx)).ok().and_then(|t| env_get(&t, "DISCORD_APP_ID"))
}

/// The id the Discord connection is using right now (not the process
/// environment: dotenvy sets that once at launch and a live switch changes
/// the connection, not the environment).
fn running_app_id() -> Option<String> {
    crate::running_app_id()
}

fn write_app_id(ctx: &Ctx, id: &str) -> Result<(), String> {
    let p = env_path(ctx);
    let old = std::fs::read_to_string(&p).unwrap_or_default();
    std::fs::write(&p, env_set(&old, "DISCORD_APP_ID", id)).map_err(|e| format!("Could not write .env: {e}"))?;
    ctx.config.set("preferences", "discord_app_id_active", id);
    crate::log(&format!("Discord App ID saved: {id}"));
    ctx.engine.config_changed();
    Ok(())
}

/// Python's _reconnect_rpc + the loop that follows it: drop the Discord pipe and
/// connect again with the App ID in .env. This is also how a profile switch
/// takes effect, with no restart. Returns the id in use.
fn reconnect_discord(ctx: &Arc<Ctx>) -> Result<String, String> {
    let id = configured_app_id(ctx).or_else(running_app_id).ok_or("No Discord App ID yet. Add one under Discord first.")?;
    // A changed id needs a fresh connection; the same id can try the core
    // feature's lighter pipe reset first (when it has one).
    if running_app_id().as_deref() == Some(id.as_str()) {
        match ctx.call("core", "reconnect_rpc", json!({})) {
            Ok(_) => {
                crate::log("Discord reconnect requested");
                return Ok(id);
            }
            Err(e) if e.contains("unknown action") || e.contains("no feature") => {}
            Err(e) => return Err(e),
        }
    }
    crate::start_discord(&ctx.engine, &id);
    crate::log(&format!("Discord reconnecting with App ID {id}"));
    Ok(id)
}

// ── Start with Windows ───────────────────────────────────────────

/// The Startup-folder shortcut the Python app creates
/// (statusify_startup._startup_lnk_path), under the given %APPDATA%.
pub fn legacy_startup_lnk_in(appdata: &std::path::Path) -> std::path::PathBuf {
    appdata.join("Microsoft").join("Windows").join("Start Menu").join("Programs").join("Startup").join("Statusify.lnk")
}

fn legacy_startup_lnk() -> Option<std::path::PathBuf> {
    std::env::var_os("APPDATA").map(|a| legacy_startup_lnk_in(std::path::Path::new(&a)))
}

/// Start with Windows is on when either launcher exists: this app's Run entry
/// or the Python app's shortcut (which would start the old app at boot).
pub fn startup_enabled(plugin_on: bool, legacy_lnk: bool) -> bool {
    plugin_on || legacy_lnk
}

/// Delete the old shortcut so two launchers never fight over port 8765.
/// Returns whether one was removed.
pub fn remove_legacy_lnk(lnk: &std::path::Path) -> bool {
    std::fs::remove_file(lnk).is_ok()
}

fn autostart_now(ctx: &Ctx) -> bool {
    startup_enabled(ctx.app.autolaunch().is_enabled().unwrap_or(false), legacy_startup_lnk().is_some_and(|p| p.exists()))
}

/// Turn Start with Windows on or off, migrating away from the Python shortcut.
fn set_autostart(ctx: &Ctx, on: bool) -> Result<bool, String> {
    let al = ctx.app.autolaunch();
    if on {
        al.enable().map_err(|e| format!("Could not change start with Windows: {e}"))?;
        // Enabling here replaces the Python app's launcher.
        if legacy_startup_lnk().is_some_and(|p| remove_legacy_lnk(&p)) {
            crate::log("Replaced the old Startup shortcut with this app's own entry");
        }
    } else {
        // Nothing to remove is not an error (Python ignored FileNotFoundError too).
        if al.is_enabled().unwrap_or(false) {
            al.disable().map_err(|e| format!("Could not change start with Windows: {e}"))?;
        }
        if let Some(p) = legacy_startup_lnk() {
            if p.exists() && !remove_legacy_lnk(&p) {
                return Err("Could not remove the old Startup shortcut".into());
            }
        }
    }
    let now = autostart_now(ctx);
    crate::log(&format!("Launch with Windows {}", if now { "enabled" } else { "disabled" }));
    Ok(now)
}

// ── Desktop shortcut / restart ───────────────────────────────────

fn ps_quote(s: &str) -> String {
    format!("'{}'", s.replace('\'', "''"))
}

#[cfg(windows)]
fn no_window(c: &mut std::process::Command) -> &mut std::process::Command {
    use std::os::windows::process::CommandExt;
    c.creation_flags(0x0800_0000) // CREATE_NO_WINDOW
}
#[cfg(not(windows))]
fn no_window(c: &mut std::process::Command) -> &mut std::process::Command {
    c
}

fn create_desktop_shortcut() -> Result<String, String> {
    let exe = std::env::current_exe().map_err(|e| e.to_string())?;
    let dir = exe.parent().map(|p| p.to_path_buf()).unwrap_or_default();
    // The Desktop may be redirected (OneDrive); ask Windows where it is.
    let script = format!(
        "$d=[Environment]::GetFolderPath('Desktop');$l=Join-Path $d 'Statusify.lnk';\
         $s=(New-Object -COM WScript.Shell).CreateShortcut($l);\
         $s.TargetPath={exe};$s.WorkingDirectory={dir};$s.IconLocation={exe};$s.Description='Statusify';$s.Save();Write-Output $l",
        exe = ps_quote(&exe.to_string_lossy()),
        dir = ps_quote(&dir.to_string_lossy()),
    );
    let mut cmd = std::process::Command::new("powershell.exe");
    cmd.args(["-NoProfile", "-NonInteractive", "-Command", &script]);
    let out = no_window(&mut cmd).output().map_err(|e| format!("Could not create shortcut: {e}"))?;
    if !out.status.success() {
        return Err(format!("Could not create shortcut: {}", String::from_utf8_lossy(&out.stderr).trim()));
    }
    let path = String::from_utf8_lossy(&out.stdout).trim().to_string();
    crate::log("Shortcut created on Desktop");
    Ok(path)
}

/// Start a fresh copy a moment after this one exits (so the single-instance
/// lock is free), then exit. Tauri's own restart() can race that lock.
fn relaunch(ctx: &Ctx) -> Result<(), String> {
    let exe = std::env::current_exe().map_err(|e| e.to_string())?;
    win::quit_prepare(ctx);
    relaunch_command(&exe).spawn().map_err(|e| format!("Could not restart: {e}"))?;
    crate::log("Restarting");
    ctx.app.exit(0);
    Ok(())
}

/// The command that starts the new copy. It must NOT inherit DISCORD_APP_ID:
/// lib.rs loaded .env into this process's environment at launch, and dotenvy
/// never overrides a variable that is already set, so a child that inherited
/// it would run on the old App ID however .env has changed since.
pub fn relaunch_command(exe: &std::path::Path) -> std::process::Command {
    #[cfg(windows)]
    let mut c = {
        use std::os::windows::process::CommandExt;
        let mut c = std::process::Command::new("cmd.exe");
        c.raw_arg(format!("/C ping -n 3 127.0.0.1 >nul & start \"\" \"{}\"", exe.display()));
        c.creation_flags(0x0800_0000);
        c
    };
    #[cfg(not(windows))]
    let mut c = std::process::Command::new(exe);
    c.env_remove("DISCORD_APP_ID");
    c
}

impl Inner {
    /// Register the configured hotkeys when they differ from what is
    /// registered (or `force`). Returns the failures.
    fn sync_hotkeys(&self, ctx: &Arc<Ctx>, force: bool) -> HashMap<String, String> {
        let want = hk::all_configured(ctx);
        if !force && *self.applied.lock().unwrap() == want {
            return self.errors.lock().unwrap().clone();
        }
        let failed = hk::apply(ctx);
        *self.applied.lock().unwrap() = want;
        *self.errors.lock().unwrap() = failed.clone();
        failed
    }

    /// Publish extras["shell"] when it changed.
    fn push_state(&self, ctx: &Ctx) {
        let cfg = &ctx.config;
        let configured = configured_app_id(ctx);
        let v = json!({
            "always_on_top": cfg.get_bool("preferences", "always_on_top", false),
            "close_to_tray": cfg.get_bool("preferences", "close_to_tray", false),
            "start_minimized": cfg.get_bool("preferences", "start_minimized", false),
            "tray": self.tray.exists(),
            "hotkey_errors": *self.errors.lock().unwrap(),
            "app_id_set": configured.is_some() || running_app_id().is_some(),
            "needs_restart": configured != running_app_id(),
        });
        let mut last = self.pushed.lock().unwrap();
        if *last != v {
            *last = v.clone();
            drop(last);
            ctx.engine.set_extra("shell", v);
        }
    }

    fn state(&self, ctx: &Arc<Ctx>) -> Value {
        let active = configured_app_id(ctx).or_else(running_app_id);
        let profiles: Vec<Value> = ctx
            .config
            .section("profiles")
            .into_iter()
            .map(|(name, id)| json!({"name": name, "app_id": id, "active": Some(&id) == active.as_ref()}))
            .collect();
        let hotkeys: serde_json::Map<String, Value> = hk::all_configured(ctx).into_iter().map(|(n, c)| (n.to_string(), json!(c))).collect();
        let defaults: serde_json::Map<String, Value> = hk::BINDINGS.iter().map(|b| (b.0.to_string(), json!(b.2))).collect();
        json!({
            "version": env!("CARGO_PKG_VERSION"),
            "data_dir": ctx.data_dir.to_string_lossy(),
            "tray": self.tray.exists(),
            "autostart": autostart_now(ctx),
            "hotkeys": hotkeys,
            "hotkey_defaults": defaults,
            "hotkey_errors": *self.errors.lock().unwrap(),
            "app_id_set": active.is_some(),
            "active_app_id": active,
            "running_app_id": running_app_id(),
            "needs_restart": configured_app_id(ctx) != running_app_id(),
            "profiles": profiles,
        })
    }
}

impl Feature for Shell {
    fn name(&self) -> &'static str {
        "shell"
    }

    fn start(&self, ctx: &Arc<Ctx>) {
        let me = self.inner.clone();
        let have_tray = me.tray.start(ctx);
        win::watch(ctx, have_tray);
        if let Some(w) = win::main_window(&ctx.app) {
            win::restore_geometry(&ctx.config, &w);
            win::apply_always_on_top(ctx);
            // The window is created hidden (tauri.conf.json) so a minimised
            // start never flashes it.
            if ctx.config.get_bool("preferences", "start_minimized", false) {
                if have_tray {
                    crate::log("Started minimised to tray");
                } else {
                    let _ = w.show();
                    let _ = w.minimize();
                }
            } else {
                let _ = w.show();
            }
        }
        win::startup_handled();
        me.sync_hotkeys(ctx, true);
        me.push_state(ctx);

        // Config edits (Settings page, tray, other features): re-apply the
        // parts of the shell that depend on them.
        let c = ctx.clone();
        let mut rx = ctx.engine.subscribe();
        tauri::async_runtime::spawn(async move {
            while let Ok(ev) = rx.recv().await {
                if matches!(ev, crate::engine::Event::ConfigChanged) {
                    let (me, c) = (me.clone(), c.clone());
                    let _ = tauri::async_runtime::spawn_blocking(move || {
                        win::apply_always_on_top(&c);
                        // Tell every window (theme, mini player, overlay) the settings moved.
                        let _ = c.app.emit("settings-changed", super::settings::all(&c));
                        me.sync_hotkeys(&c, false);
                        me.push_state(&c);
                        me.tray.sync(&c);
                    })
                    .await;
                }
            }
        });
    }

    fn call(&self, ctx: &Arc<Ctx>, action: &str, args: Value) -> Result<Value, String> {
        let me = &self.inner;
        let s = |k: &str| args.get(k).and_then(|v| v.as_str()).map(|v| v.trim().to_string());
        match action {
            "get_state" => Ok(me.state(ctx)),
            "set_autostart" => {
                let on = args.get("enabled").and_then(|v| v.as_bool()).ok_or("shell.set_autostart: missing enabled")?;
                set_autostart(ctx, on).map(|now| json!(now)).inspect_err(|e| crate::log(&format!("Startup change failed: {e}")))
            }
            "toggle_fullscreen" => Ok(json!(win::toggle_fullscreen(&ctx.app))),
            "exit_fullscreen" => Ok(json!(win::exit_fullscreen(&ctx.app))),
            "set_hotkeys" => {
                for (name, key, _) in hk::BINDINGS {
                    if let Some(v) = s(name) {
                        ctx.config.set("preferences", key, &v);
                    }
                }
                ctx.engine.config_changed();
                let failed = me.sync_hotkeys(ctx, true);
                me.push_state(ctx);
                crate::log("Hotkeys saved & re-registered");
                Ok(json!({"errors": failed}))
            }
            "center_window" => {
                let w = win::main_window(&ctx.app).ok_or("no main window")?;
                win::centre(&ctx.config, &w, true);
                Ok(json!(true))
            }
            "create_shortcut" => create_desktop_shortcut().map(|p| json!({"path": p})),
            "add_profile" => {
                let (n, a) = check_profile(&s("name").unwrap_or_default(), &s("app_id").unwrap_or_default())?;
                ctx.config.set("profiles", &n, &a);
                crate::log(&format!("Profile saved  ·  {n}"));
                Ok(me.state(ctx))
            }
            "delete_profile" => {
                let n = s("name").ok_or("missing name")?;
                ctx.config.remove("profiles", &n);
                Ok(me.state(ctx))
            }
            "switch_profile" | "set_app_id" => {
                let id = s("app_id").ok_or("missing app_id")?;
                if !valid_app_id(&id) {
                    return Err("App ID must be a long numeric ID (e.g. 1480612100416999474)".into());
                }
                write_app_id(ctx, &id)?;
                // Takes effect now: no restart (Python needed Switch, then Reconnect).
                reconnect_discord(ctx)?;
                me.push_state(ctx);
                Ok(me.state(ctx))
            }
            "restart_app" => relaunch(ctx).map(|_| json!(true)),
            "reconnect_rpc" => {
                let r = reconnect_discord(ctx).map(|id| json!({"app_id": id}));
                me.push_state(ctx);
                r
            }
            "show_window" => {
                win::show_main(&ctx.app);
                Ok(json!(true))
            }
            "hide_to_tray" => {
                win::hide_to_tray(ctx, me.tray.exists());
                Ok(json!(true))
            }
            "quit" => {
                win::quit(ctx);
                Ok(json!(true))
            }
            _ => Err(format!("shell: unknown action {action}")),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn env_set_replaces_or_appends() {
        assert_eq!(env_set("", "DISCORD_APP_ID", "123"), "DISCORD_APP_ID=123\n");
        assert_eq!(env_set("A=1\nDISCORD_APP_ID=old\nB=2\n", "DISCORD_APP_ID", "new"), "A=1\nDISCORD_APP_ID=new\nB=2\n");
        assert_eq!(env_set("A=1\n", "DISCORD_APP_ID", "x"), "A=1\n\nDISCORD_APP_ID=x\n");
        // Duplicate lines collapse to one.
        assert_eq!(env_set("DISCORD_APP_ID=1\nDISCORD_APP_ID=2\n", "DISCORD_APP_ID", "3"), "DISCORD_APP_ID=3\n");
        // A similarly named key is left alone.
        assert_eq!(env_set("DISCORD_APP_ID_OLD=1\n", "DISCORD_APP_ID", "2"), "DISCORD_APP_ID_OLD=1\n\nDISCORD_APP_ID=2\n");
    }

    #[test]
    fn env_get_reads_the_value() {
        assert_eq!(env_get("A=1\nDISCORD_APP_ID=987\n", "DISCORD_APP_ID").as_deref(), Some("987"));
        assert_eq!(env_get("DISCORD_APP_ID=\"55\"\n", "DISCORD_APP_ID").as_deref(), Some("55"));
        assert_eq!(env_get("DISCORD_APP_ID=\n", "DISCORD_APP_ID"), None);
        assert_eq!(env_get("", "DISCORD_APP_ID"), None);
    }

    #[test]
    fn profile_validation_matches_the_python_dialog() {
        let id = "1480612100416999474";
        assert_eq!(check_profile(" Main ", id).unwrap(), ("Main".into(), id.into()));
        assert!(check_profile("", id).unwrap_err().contains("required"));
        assert!(check_profile("x", "").unwrap_err().contains("required"));
        assert!(check_profile("a=b", id).unwrap_err().contains("cannot contain"));
        assert!(check_profile("a[1]", id).unwrap_err().contains("cannot contain"));
        assert!(check_profile("x", "12345").unwrap_err().contains("long numeric"));
        assert!(check_profile("x", "14806121004169994ab").unwrap_err().contains("long numeric"));
    }

    #[test]
    fn relaunched_copy_does_not_inherit_the_old_app_id() {
        let c = relaunch_command(std::path::Path::new(r"C:\x\statusify.exe"));
        // env_remove registers (key, None): the child gets the variable stripped.
        assert!(c.get_envs().any(|(k, v)| k == "DISCORD_APP_ID" && v.is_none()), "{:?}", c.get_envs().collect::<Vec<_>>());
    }

    #[test]
    fn legacy_startup_shortcut_is_found_and_removed() {
        let root = std::env::temp_dir().join(format!("statusify-startup-{}", crate::state::now_ms()));
        let lnk = legacy_startup_lnk_in(&root);
        assert!(lnk.ends_with(r"Microsoft\Windows\Start Menu\Programs\Startup\Statusify.lnk") || lnk.ends_with("Startup/Statusify.lnk"));
        std::fs::create_dir_all(lnk.parent().unwrap()).unwrap();
        std::fs::write(&lnk, b"x").unwrap();
        assert!(startup_enabled(false, lnk.exists()), "the Python shortcut counts as enabled");
        assert!(!startup_enabled(false, false));
        assert!(startup_enabled(true, false));
        assert!(remove_legacy_lnk(&lnk));
        assert!(!lnk.exists());
        assert!(!remove_legacy_lnk(&lnk), "already gone is not a success");
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn powershell_quoting() {
        assert_eq!(ps_quote(r"C:\a b\it's.exe"), r"'C:\a b\it''s.exe'");
    }
}
