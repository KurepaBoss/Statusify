//! Main-window behaviour: saved geometry, show/hide, centre, always on top,
//! close-to-tray, start minimised, quit. Port of the window parts of main.py.
//!
//! Geometry lives in statusify.cfg as `[window] geometry = WxH+X+Y`, the same
//! string Tk wrote, so the Python app and this one share it. W and H are the
//! client size, X and Y the outer top-left, all in physical pixels.

use crate::config::Config;
use crate::features::Ctx;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;
use tauri::{Manager, PhysicalPosition, PhysicalSize, WebviewWindow};

pub const MAIN: &str = "main";
pub const DEFAULT_W: u32 = 520;
pub const DEFAULT_H: u32 = 720;
/// Python's WIN_MIN_W/WIN_MIN_H. Kept equal so a geometry saved by one app is
/// accepted by the other (Python re-centres anything smaller).
pub const MIN_W: u32 = 460;
pub const MIN_H: u32 = 580;
const SAVE_DEBOUNCE: Duration = Duration::from_millis(800);

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Geometry {
    pub w: u32,
    pub h: u32,
    pub x: i32,
    pub y: i32,
}

/// "520x720+877+143" (Tk also writes negatives as "+-8" or "-8").
pub fn parse_geometry(s: &str) -> Option<Geometry> {
    static RE: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
    let re = RE.get_or_init(|| regex::Regex::new(r"^(\d+)x(\d+)([+-]+)(\d+)([+-]+)(\d+)$").unwrap());
    let c = re.captures(s.trim())?;
    let signed = |sign: &str, digits: &str| -> Option<i32> {
        let n: i32 = digits.parse().ok()?;
        Some(if sign.matches('-').count() % 2 == 1 { -n } else { n })
    };
    Some(Geometry { w: c[1].parse().ok()?, h: c[2].parse().ok()?, x: signed(&c[3], &c[4])?, y: signed(&c[5], &c[6])? })
}

pub fn format_geometry(g: Geometry) -> String {
    format!("{}x{}+{}+{}", g.w, g.h, g.x, g.y)
}

/// Is `g` somewhere a person can reach? Python's rule, per monitor
/// (x, y, w, h): the window must overlap by 80 px sideways and its top edge
/// must be on screen. Stops an unplugged monitor stranding the window.
pub fn geometry_visible(g: Geometry, monitors: &[(i32, i32, u32, u32)]) -> bool {
    if g.w < MIN_W || g.h < MIN_H {
        return false;
    }
    monitors.iter().any(|&(mx, my, mw, mh)| {
        let (w, x, y) = (g.w as i32, g.x, g.y);
        mx - w + 80 < x && x < mx + mw as i32 - 80 && my - 40 < y && y < my + mh as i32 - 80
    })
}

pub fn centred(monitor: (i32, i32, u32, u32), w: u32, h: u32) -> Geometry {
    let (mx, my, mw, mh) = monitor;
    Geometry { w, h, x: mx + (mw as i32 - w as i32) / 2, y: my + (mh as i32 - h as i32) / 2 }
}

pub fn main_window(app: &tauri::AppHandle) -> Option<WebviewWindow> {
    app.get_webview_window(MAIN)
}

fn monitors(w: &WebviewWindow) -> Vec<(i32, i32, u32, u32)> {
    w.available_monitors()
        .unwrap_or_default()
        .iter()
        .map(|m| (m.position().x, m.position().y, m.size().width, m.size().height))
        .collect()
}

/// Geometry now, or None when it is not one to remember (hidden, minimised,
/// fullscreen: the numbers would be meaningless).
pub fn current_geometry(w: &WebviewWindow) -> Option<Geometry> {
    if !w.is_visible().unwrap_or(false) || w.is_minimized().unwrap_or(false) || w.is_fullscreen().unwrap_or(false) {
        return None;
    }
    let p = w.outer_position().ok()?;
    let s = w.inner_size().ok()?;
    Some(Geometry { w: s.width, h: s.height, x: p.x, y: p.y })
}

pub fn save_geometry(cfg: &Config, w: &WebviewWindow) {
    if let Some(g) = current_geometry(w) {
        let s = format_geometry(g);
        if cfg.get("window", "geometry").as_deref() != Some(s.as_str()) {
            cfg.set("window", "geometry", &s);
        }
    }
}

fn apply_geometry(w: &WebviewWindow, g: Geometry) {
    let _ = w.set_size(PhysicalSize::new(g.w, g.h));
    let _ = w.set_position(PhysicalPosition::new(g.x, g.y));
}

/// Put the window where it was last, else in the middle of the primary monitor.
pub fn restore_geometry(cfg: &Config, w: &WebviewWindow) {
    let mons = monitors(w);
    if let Some(g) = cfg.get("window", "geometry").as_deref().and_then(parse_geometry) {
        if geometry_visible(g, &mons) {
            apply_geometry(w, g);
            return;
        }
        crate::log("Saved window position is off-screen — recentring");
    }
    centre(cfg, w, false);
}

/// Centre on the monitor the window is on (primary when unknown). `persist`
/// writes it to the config at once: the debounced saver skips hidden windows.
pub fn centre(cfg: &Config, w: &WebviewWindow, persist: bool) {
    let mon = w
        .current_monitor()
        .ok()
        .flatten()
        .or_else(|| w.primary_monitor().ok().flatten())
        .map(|m| (m.position().x, m.position().y, m.size().width, m.size().height));
    let Some(mon) = mon else { return };
    let _ = w.unminimize();
    let g = centred(mon, DEFAULT_W, DEFAULT_H);
    apply_geometry(w, g);
    if persist {
        cfg.set("window", "geometry", &format_geometry(g));
        crate::log("Window position reset to centre");
    }
}

/// Bring the main window back (tray click, second launch, hotkey).
pub fn show_main(app: &tauri::AppHandle) {
    if let Some(w) = main_window(app) {
        let _ = w.unminimize();
        let _ = w.show();
        let _ = w.set_focus();
    }
}

/// Hide the window but keep everything running. Without a tray icon there
/// would be no way back, so the caller passes `have_tray`; if false, minimise.
pub fn hide_to_tray(ctx: &Ctx, have_tray: bool) {
    let Some(w) = main_window(&ctx.app) else { return };
    save_geometry(&ctx.config, &w);
    if have_tray {
        let _ = w.hide();
    } else {
        let _ = w.minimize();
    }
}

/// Set once the shell has decided what the main window does at startup.
static STARTUP_HANDLED: AtomicBool = AtomicBool::new(false);
const STARTUP_GRACE: Duration = Duration::from_secs(12);

/// The shell calls this when it has shown (or deliberately hidden) the window.
pub fn startup_handled() {
    STARTUP_HANDLED.store(true, Ordering::SeqCst);
}

/// The window is created hidden. If the shell feature never gets to show it
/// (a panic or a hang in its start-up), show it anyway so the app is never
/// invisible with no tray either.
pub fn show_fallback(ctx: &Arc<Ctx>) {
    let c = ctx.clone();
    tauri::async_runtime::spawn(async move {
        tokio::time::sleep(STARTUP_GRACE).await;
        if !STARTUP_HANDLED.load(Ordering::SeqCst) {
            crate::log("Shell start-up did not finish - showing the window anyway");
            show_main(&c.app);
        }
    });
}

/// F11. Returns the new state.
pub fn toggle_fullscreen(app: &tauri::AppHandle) -> bool {
    let Some(w) = main_window(app) else { return false };
    let now = !w.is_fullscreen().unwrap_or(false);
    let _ = w.set_fullscreen(now);
    now
}

/// Esc: leave fullscreen. Returns whether it was fullscreen.
pub fn exit_fullscreen(app: &tauri::AppHandle) -> bool {
    let Some(w) = main_window(app) else { return false };
    let was = w.is_fullscreen().unwrap_or(false);
    if was {
        let _ = w.set_fullscreen(false);
    }
    was
}

/// Is the desktop overlay showing? Prefers the windows feature's own report.
pub fn overlay_on(ctx: &Ctx) -> bool {
    let extras = ctx.engine.snapshot().extras;
    match extras.get("windows").and_then(|w| w.get("overlay")).and_then(|v| v.as_bool()) {
        Some(b) => b,
        None => ctx.config.get_bool("preferences", "overlay_enabled", false),
    }
}

/// Python's _overlay_set_locked: unlocking an overlay that is off also turns it on
/// (there would be nothing to move).
pub fn should_enable_overlay(locked: bool, overlay_on: bool) -> bool {
    !locked && !overlay_on
}

/// Lock or unlock the overlay: write the setting, switch the overlay on when
/// unlocking it, tell everyone. Runs on a worker thread (it calls a feature).
pub fn set_overlay_locked(ctx: &Arc<Ctx>, locked: bool) {
    ctx.config.set("window", "overlay_locked", if locked { "true" } else { "false" });
    if should_enable_overlay(locked, overlay_on(ctx)) {
        if let Err(e) = ctx.call("windows", "toggle_overlay", serde_json::json!({})) {
            crate::log(&format!("Overlay could not be switched on: {e}"));
        }
    }
    ctx.engine.config_changed();
}

pub fn apply_always_on_top(ctx: &Ctx) {
    if let Some(w) = main_window(&ctx.app) {
        let _ = w.set_always_on_top(ctx.config.get_bool("preferences", "always_on_top", false));
    }
}

/// Remember the window before the process goes away (quit and restart).
pub fn quit_prepare(ctx: &Ctx) {
    if let Some(w) = main_window(&ctx.app) {
        save_geometry(&ctx.config, &w);
    }
}

/// Quit for real: remember the window, exit.
pub fn quit(ctx: &Ctx) {
    quit_prepare(ctx);
    crate::log("Quitting");
    ctx.app.exit(0);
}

/// Wire window events: debounced geometry saving, and the close button
/// (close to tray when asked to and a tray exists, otherwise quit).
pub fn watch(ctx: &Arc<Ctx>, have_tray: bool) {
    let Some(w) = main_window(&ctx.app) else { return };
    let gen = Arc::new(AtomicU64::new(0));
    let c = ctx.clone();
    let win = w.clone();
    w.on_window_event(move |ev| match ev {
        tauri::WindowEvent::Moved(_) | tauri::WindowEvent::Resized(_) => {
            let n = gen.fetch_add(1, Ordering::SeqCst) + 1;
            let (gen, c, win) = (gen.clone(), c.clone(), win.clone());
            tauri::async_runtime::spawn(async move {
                tokio::time::sleep(SAVE_DEBOUNCE).await;
                if gen.load(Ordering::SeqCst) == n {
                    save_geometry(&c.config, &win);
                }
            });
        }
        tauri::WindowEvent::CloseRequested { api, .. } => {
            api.prevent_close();
            if have_tray && c.config.get_bool("preferences", "close_to_tray", false) {
                hide_to_tray(&c, true);
                crate::log("Hidden to tray — right-click the tray icon to quit");
            } else {
                quit(&c);
            }
        }
        _ => {}
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_tk_geometry() {
        assert_eq!(parse_geometry("520x720+877+143"), Some(Geometry { w: 520, h: 720, x: 877, y: 143 }));
        assert_eq!(parse_geometry("500x600+-8+-8"), Some(Geometry { w: 500, h: 600, x: -8, y: -8 }));
        assert_eq!(parse_geometry("500x600-8+10"), Some(Geometry { w: 500, h: 600, x: -8, y: 10 }));
        assert_eq!(parse_geometry("500x600+10-20"), Some(Geometry { w: 500, h: 600, x: 10, y: -20 }));
        assert_eq!(parse_geometry(""), None);
        assert_eq!(parse_geometry("garbage"), None);
        assert_eq!(parse_geometry("500x600"), None);
        assert_eq!(parse_geometry("500x600+10"), None);
        assert_eq!(parse_geometry("axb+1+2"), None);
    }

    #[test]
    fn format_round_trips() {
        let g = Geometry { w: 520, h: 720, x: -30, y: 12 };
        assert_eq!(parse_geometry(&format_geometry(g)), Some(g));
        assert_eq!(format_geometry(g), "520x720+-30+12");
    }

    #[test]
    fn off_screen_geometry_is_rejected() {
        let one = [(0, 0, 1920u32, 1080u32)];
        let g = |x, y| Geometry { w: 520, h: 720, x, y };
        assert!(geometry_visible(g(100, 100), &one));
        assert!(geometry_visible(g(-400, 0), &one)); // mostly off the left edge but 120 px visible
        assert!(!geometry_visible(g(-450, 0), &one));
        assert!(!geometry_visible(g(1850, 100), &one));
        assert!(!geometry_visible(g(100, 1050), &one));
        assert!(!geometry_visible(g(100, -50), &one));
        assert!(!geometry_visible(Geometry { w: 100, h: 100, x: 0, y: 0 }, &one));
        // A second monitor to the left makes negative x valid.
        let two = [(-1920, 0, 1920u32, 1080u32), (0, 0, 1920, 1080)];
        assert!(geometry_visible(g(-1500, 50), &two));
        // The unplugged-monitor case.
        assert!(!geometry_visible(g(-1500, 50), &one));
    }

    #[test]
    fn minimum_size_matches_python() {
        assert_eq!((MIN_W, MIN_H), (460, 580));
        let one = [(0, 0, 1920u32, 1080u32)];
        assert!(geometry_visible(Geometry { w: 460, h: 580, x: 10, y: 10 }, &one));
        assert!(!geometry_visible(Geometry { w: 459, h: 580, x: 10, y: 10 }, &one));
        assert!(!geometry_visible(Geometry { w: 460, h: 579, x: 10, y: 10 }, &one));
        // Whatever the config file asks for, tauri.conf.json must not allow smaller.
        let conf: serde_json::Value = serde_json::from_str(include_str!("../tauri.conf.json")).unwrap();
        let win = &conf["app"]["windows"][0];
        assert_eq!(win["minWidth"], MIN_W);
        assert_eq!(win["minHeight"], MIN_H);
    }

    #[test]
    fn unlocking_a_hidden_overlay_switches_it_on() {
        assert!(should_enable_overlay(false, false));
        assert!(!should_enable_overlay(false, true));
        assert!(!should_enable_overlay(true, false));
        assert!(!should_enable_overlay(true, true));
    }

    #[test]
    fn centring() {
        let g = centred((0, 0, 1920, 1080), 520, 720);
        assert_eq!((g.x, g.y), (700, 180));
        let g = centred((-1920, 0, 1920, 1080), 520, 720);
        assert_eq!(g.x, -1920 + 700);
    }
}
