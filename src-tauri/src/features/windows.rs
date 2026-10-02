//! Mini player and desktop lyrics overlay windows (owner: windows agent).
//!
//! Both are extra Tauri windows created at runtime (transparent, borderless,
//! always on top, off the taskbar) that load mini.html / overlay.html and draw
//! themselves from the same snapshot the main window gets. This module owns
//! only what a web page cannot do: creating/closing the windows, placing and
//! sizing them per monitor (work area + DPI), click-through while the overlay
//! is locked, remembering where they were, the mini player's magnetic edge
//! snapping and its native context menu.
//!
//! Config (statusify.cfg, same keys as the Python app):
//!   [window]      mini_geometry, overlay_geometry, overlay_locked
//!   [preferences] overlay_enabled, overlay_next_line, overlay_size,
//!                 overlay_opacity (new, percent, optional)
//! Actions: toggle_mini, open_mini, close_mini, mini_menu, show_main,
//!   toggle_overlay, set_overlay_enabled {enabled}, toggle_overlay_lock,
//!   set_overlay_locked {locked}, set_overlay_size {size},
//!   nudge_overlay_size {delta}, set_overlay_next {enabled}, get_state.
//! Pushes extras["windows"] = {mini, overlay, overlay_locked, overlay_size,
//!   overlay_next, overlay_opacity, animations, dark, tint, offset_ms, accent,
//!   layout}.

use super::{Ctx, Feature};
use crate::config::Config;
use crate::engine::Event;
use serde_json::{json, Value};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tauri::menu::{ContextMenu, Menu, MenuItem, PredefinedMenuItem};
use tauri::{Manager, PhysicalPosition, PhysicalSize, WebviewUrl, WebviewWindow, WebviewWindowBuilder, WindowEvent};

#[path = "../win_geom.rs"]
mod win_geom;
#[path = "../win_mon.rs"]
mod win_mon;

use win_geom::*;

const MINI: &str = "mini";
const OVERLAY: &str = "overlay";
/// Mini pill size in logical px (Python: 440x68 at UI scale 1).
const MINI_W: f64 = 440.0;
const MINI_H: f64 = 68.0;
/// How long after the last Moved event a native drag counts as finished.
const DRAG_SETTLE: Duration = Duration::from_millis(300);
/// Moved events this soon after we moved the window ourselves are ignored.
const OWN_MOVE_ECHO: Duration = Duration::from_millis(250);
const MINI_GLIDE_S: f64 = 0.2;
const MENU_PREFIX: &str = "win:mini:";

// ── Preferences ──────────────────────────────────────────────────────

#[derive(Clone, Debug, PartialEq)]
struct Prefs {
    overlay_enabled: bool,
    overlay_next: bool,
    overlay_locked: bool,
    overlay_size: i64,
    overlay_opacity: i64,
    animations: bool,
    dark: bool,
    tint: bool,
    offset_ms: i64,
    accent: String,
}

fn read_prefs(c: &Config) -> Prefs {
    Prefs {
        overlay_enabled: c.get_bool("preferences", "overlay_enabled", false),
        overlay_next: c.get_bool("preferences", "overlay_next_line", true),
        // Python: locked unless the value is literally "false".
        overlay_locked: c.get_bool("window", "overlay_locked", true),
        overlay_size: c.get_i64("preferences", "overlay_size", DEFAULT_OVERLAY_SIZE).clamp(MIN_OVERLAY_SIZE, MAX_OVERLAY_SIZE),
        overlay_opacity: c.get_i64("preferences", "overlay_opacity", 100).clamp(20, 100),
        animations: c.get_bool("preferences", "animations", true),
        dark: c.get_bool("preferences", "dark_mode", true),
        tint: c.get_bool("preferences", "album_tint", true),
        offset_ms: c.get_i64("preferences", "lyric_delay_ms", -40),
        accent: Some(c.get_or("preferences", "accent_color", "#1db954")).filter(|a| !a.trim().is_empty()).unwrap_or_else(|| "#1db954".into()),
    }
}

// ── State ────────────────────────────────────────────────────────────

#[derive(Default)]
struct Ov {
    /// (centre x, edge y, pinned by top edge) — survives size changes.
    anchor: Option<(i32, i32, bool)>,
    metrics: Option<OverlayMetrics>,
    loaded_anchor: bool,
    /// What the live window was last laid out for: (size, next, locked).
    applied: Option<(i64, bool, bool)>,
}

#[derive(Default)]
struct Inner {
    /// Serialises create / close / relayout, which may arrive from the config
    /// event loop, a command, and a drag settle at the same time.
    op: Mutex<()>,
    ov: Mutex<Ov>,
    open: [AtomicBool; 2],
    /// Millis (since `epoch`) of our own last programmatic move, per window.
    own_move: [AtomicU64; 2],
    moved_at: [Mutex<Option<Instant>>; 2],
    waiting: [AtomicBool; 2],
    glide_gen: AtomicU64,
    last_extra: Mutex<Option<Value>>,
    epoch: Mutex<Option<Instant>>,
}

#[derive(Default)]
pub struct Windows {
    inner: Arc<Inner>,
}

type C = Arc<Ctx>;
type I = Arc<Inner>;

impl Inner {
    fn now_ms(&self) -> u64 {
        let mut e = self.epoch.lock().unwrap();
        e.get_or_insert_with(Instant::now).elapsed().as_millis() as u64 + 1
    }
    fn mark_own_move(&self, i: usize) {
        self.own_move[i].store(self.now_ms(), Ordering::SeqCst);
    }
    fn own_move_recent(&self, i: usize) -> bool {
        let t = self.own_move[i].load(Ordering::SeqCst);
        t != 0 && self.now_ms().saturating_sub(t) < OWN_MOVE_ECHO.as_millis() as u64
    }
}

// ── Monitors ─────────────────────────────────────────────────────────

/// Work area + DPI of the monitor at physical (x, y); Win32 first, Tauri's
/// monitor list (full rect, no taskbar subtraction) as the fallback.
fn monitor(ctx: &C, x: i32, y: i32, primary: bool) -> win_mon::MonitorInfo {
    if let Some(m) = win_mon::monitor_at(x, y, primary) {
        return m;
    }
    let tm = ctx
        .app
        .monitor_from_point(x as f64, y as f64)
        .ok()
        .flatten()
        .or_else(|| ctx.app.primary_monitor().ok().flatten());
    match tm {
        Some(m) => {
            let (p, s) = (m.position(), m.size());
            win_mon::MonitorInfo {
                work: (p.x, p.y, p.x + s.width as i32, p.y + s.height as i32),
                dpi: (m.scale_factor() * 96.0).round() as u32,
                found: true,
            }
        }
        None => win_mon::MonitorInfo { work: (0, 0, 1920, 1040), dpi: 96, found: false },
    }
}

fn win(ctx: &C, label: &str) -> Option<WebviewWindow> {
    ctx.app.get_webview_window(label)
}

fn physical_geometry(w: &WebviewWindow) -> Option<(i32, i32, i32, i32)> {
    let p = w.outer_position().ok()?;
    let s = w.outer_size().ok()?;
    Some((s.width as i32, s.height as i32, p.x, p.y))
}

/// Move + resize without the Moved echo being mistaken for a user drag.
fn place(inner: &I, w: &WebviewWindow, i: usize, size: Option<(i32, i32)>, pos: (i32, i32)) {
    inner.mark_own_move(i);
    if let Some((sw, sh)) = size {
        let _ = w.set_size(PhysicalSize::new(sw.max(1) as u32, sh.max(1) as u32));
    }
    let _ = w.set_position(PhysicalPosition::new(pos.0, pos.1));
    inner.mark_own_move(i);
}

// ── Extras ───────────────────────────────────────────────────────────

fn css(v: i32, s: f64) -> f64 {
    ((v as f64 / s) * 10.0).round() / 10.0
}

/// Overlay metrics are physical px; the page works in CSS px (physical / DPI scale).
fn layout_json(m: &OverlayMetrics) -> Value {
    json!({
        "w": css(m.w, m.s), "h": css(m.h, m.s), "px": css(m.px, m.s), "npx": css(m.npx, m.s),
        "lh": css(m.lh, m.s), "nlh": css(m.nlh, m.s), "pad": css(m.pad, m.s),
        "bar": css(m.bar, m.s), "gap": css(m.gap, m.s),
    })
}

fn extras_json(p: &Prefs, mini: bool, overlay: bool, metrics: Option<&OverlayMetrics>) -> Value {
    let fallback = overlay_metrics(p.overlay_size, 96, 1920, p.overlay_next);
    json!({
        "mini": mini,
        "overlay": overlay,
        "overlay_locked": p.overlay_locked,
        "overlay_size": p.overlay_size,
        "overlay_next": p.overlay_next,
        "overlay_opacity": p.overlay_opacity,
        "animations": p.animations,
        "dark": p.dark,
        "tint": p.tint,
        "offset_ms": p.offset_ms,
        "accent": p.accent,
        "layout": layout_json(metrics.unwrap_or(&fallback)),
    })
}

fn push_extras(ctx: &C, inner: &I) {
    let p = read_prefs(&ctx.config);
    let metrics = inner.ov.lock().unwrap().metrics;
    let v = extras_json(&p, inner.open[0].load(Ordering::SeqCst), inner.open[1].load(Ordering::SeqCst), metrics.as_ref());
    let mut last = inner.last_extra.lock().unwrap();
    if last.as_ref() != Some(&v) {
        *last = Some(v.clone());
        drop(last);
        ctx.engine.set_extra("windows", v);
    }
}

// ── Mini player ──────────────────────────────────────────────────────

fn mini_open(ctx: &C, inner: &I) -> Result<(), String> {
    let g = inner.op.lock().unwrap();
    if win(ctx, MINI).is_some() {
        return Ok(());
    }
    // Saved spot, else top centre of the primary monitor.
    let saved = parse_position(&ctx.config.get_or("window", "mini_geometry", ""));
    let mi = match saved {
        Some((x, y)) => monitor(ctx, x, y, false),
        None => monitor(ctx, 0, 0, true),
    };
    let s = mi.dpi as f64 / 96.0;
    let (w, h) = ((MINI_W * s).round() as i32, (MINI_H * s).round() as i32);
    let pos = saved.unwrap_or((mi.work.0 + (mi.work.2 - mi.work.0 - w) / 2, mi.work.1 + (40.0 * s) as i32));
    // Keep a saved spot on screen (its monitor may be gone).
    let mi = monitor(ctx, pos.0 + w / 2, pos.1 + h / 2, false);
    let (x, y) = snap_position(pos.0, pos.1, w, h, mi.work, 0, 0);

    let window = WebviewWindowBuilder::new(&ctx.app, MINI, WebviewUrl::App("mini.html".into()))
        .title("Statusify mini player")
        .inner_size(MINI_W, MINI_H)
        .decorations(false)
        .transparent(true)
        .shadow(false)
        .resizable(false)
        .maximizable(false)
        .minimizable(false)
        .always_on_top(true)
        .skip_taskbar(true)
        .focusable(false)
        .focused(false)
        .visible(false)
        .build()
        .map_err(|e| format!("mini window: {e}"))?;
    place(inner, &window, 0, Some((w, h)), (x, y));
    watch(ctx, inner, &window, 0);
    inner.open[0].store(true, Ordering::SeqCst);
    let _ = window.show();
    crate::log("Mini player on");
    drop(g);
    push_extras(ctx, inner);
    Ok(())
}

fn mini_save_pos(ctx: &C) {
    if let Some(w) = win(ctx, MINI) {
        if let Some((gw, gh, x, y)) = physical_geometry(&w) {
            ctx.config.set("window", "mini_geometry", &format_geometry(gw, gh, x, y));
        }
    }
}

fn mini_close(ctx: &C, inner: &I) {
    let g = inner.op.lock().unwrap();
    let Some(w) = win(ctx, MINI) else { return };
    if !inner.open[0].swap(false, Ordering::SeqCst) {
        return; // already closing (the window outlives destroy() briefly)
    }
    mini_save_pos(ctx);
    inner.glide_gen.fetch_add(1, Ordering::SeqCst);
    let _ = w.destroy();
    crate::log("Mini player off");
    drop(g);
    push_extras(ctx, inner);
}

/// After a drag: glide to the magnetic edge / corner of the monitor it is on.
fn mini_settle(ctx: &C, inner: &I) {
    let Some(w) = win(ctx, MINI) else { return };
    let Some((gw, gh, x, y)) = physical_geometry(&w) else { return };
    let mi = monitor(ctx, x + gw / 2, y + gh / 2, false);
    let (tx, ty) = snap_position(x, y, gw, gh, mi.work, SNAP_THRESHOLD, SNAP_MARGIN);
    if (tx, ty) == (x, y) {
        mini_save_pos(ctx);
        return;
    }
    if !read_prefs(&ctx.config).animations {
        place(inner, &w, 0, None, (tx, ty));
        mini_save_pos(ctx);
        return;
    }
    let gen = inner.glide_gen.fetch_add(1, Ordering::SeqCst) + 1;
    let t0 = Instant::now();
    loop {
        // A new drag (button down / newer generation) takes over.
        if inner.glide_gen.load(Ordering::SeqCst) != gen || win_mon::left_button_down() {
            return;
        }
        let p = t0.elapsed().as_secs_f64() / MINI_GLIDE_S;
        let e = ease_out(p);
        let nx = x as f64 + (tx - x) as f64 * e;
        let ny = y as f64 + (ty - y) as f64 * e;
        place(inner, &w, 0, None, (nx.round() as i32, ny.round() as i32));
        if p >= 1.0 {
            break;
        }
        std::thread::sleep(Duration::from_millis(16));
    }
    mini_save_pos(ctx);
}

fn mini_menu(ctx: &C) -> Result<(), String> {
    let w = win(ctx, MINI).ok_or("mini player is not open")?;
    let app = ctx.app.clone();
    ctx.app
        .run_on_main_thread(move || {
            let build = || -> tauri::Result<Menu<tauri::Wry>> {
                let item = |id: &str, label: &str| MenuItem::with_id(&app, format!("{MENU_PREFIX}{id}"), label, true, None::<&str>);
                let (play, prev, next) = (item("toggle", "Play/Pause")?, item("prev", "Previous")?, item("next", "Next")?);
                let (open, close) = (item("open", "Open Statusify")?, item("close", "Close mini player")?);
                let sep = PredefinedMenuItem::separator(&app)?;
                Menu::with_items(&app, &[&play, &prev, &next, &sep, &open, &close])
            };
            match build() {
                Ok(menu) => {
                    let _ = menu.popup(w.as_ref().window().clone());
                }
                Err(e) => crate::log(&format!("Mini menu failed: {e}")),
            }
        })
        .map_err(|e| e.to_string())
}

fn show_main(ctx: &C) {
    if let Some(w) = win(ctx, "main") {
        let _ = w.unminimize();
        let _ = w.show();
        let _ = w.set_focus();
    }
}

// ── Desktop overlay ──────────────────────────────────────────────────

fn overlay_open(ctx: &C, inner: &I) -> Result<(), String> {
    let g = inner.op.lock().unwrap();
    if win(ctx, OVERLAY).is_some() {
        return Ok(());
    }
    let window = WebviewWindowBuilder::new(&ctx.app, OVERLAY, WebviewUrl::App("overlay.html".into()))
        .title("Statusify lyrics overlay")
        .inner_size(900.0, 100.0)
        .decorations(false)
        .transparent(true)
        .shadow(false)
        .resizable(false)
        .maximizable(false)
        .minimizable(false)
        .always_on_top(true)
        .skip_taskbar(true)
        .focusable(false)
        .focused(false)
        .visible(false)
        .build()
        .map_err(|e| format!("overlay window: {e}"))?;
    watch(ctx, inner, &window, 1);
    inner.open[1].store(true, Ordering::SeqCst);
    overlay_load_anchor(ctx, inner);
    overlay_relayout_locked(ctx, inner, &window);
    let _ = window.set_ignore_cursor_events(read_prefs(&ctx.config).overlay_locked);
    let _ = window.show();
    crate::log("Lyrics overlay on");
    drop(g);
    push_extras(ctx, inner);
    Ok(())
}

fn overlay_close(ctx: &C, inner: &I) {
    let g = inner.op.lock().unwrap();
    let Some(w) = win(ctx, OVERLAY) else { return };
    if !inner.open[1].swap(false, Ordering::SeqCst) {
        return; // already closing (the window outlives destroy() briefly)
    }
    let _ = w.destroy();
    inner.ov.lock().unwrap().applied = None;
    crate::log("Lyrics overlay off");
    drop(g);
    push_extras(ctx, inner);
}

/// The saved strip position becomes an anchor, but only on a monitor that
/// still exists. Read once; afterwards drags and resizes own the anchor.
fn overlay_load_anchor(ctx: &C, inner: &I) {
    let mut ov = inner.ov.lock().unwrap();
    if ov.loaded_anchor {
        return;
    }
    ov.loaded_anchor = true;
    if let Some((w, h, x, y)) = parse_geometry(&ctx.config.get_or("window", "overlay_geometry", "")) {
        let mi = monitor(ctx, x + w / 2, y + h - 1, false);
        if mi.found {
            ov.anchor = Some(anchor_for(x, y, w, h, Some(mi.work)));
        }
    }
}

/// Size the strip for the monitor it is on and the text size. Caller holds `op`.
fn overlay_relayout_locked(ctx: &C, inner: &I, w: &WebviewWindow) {
    let p = read_prefs(&ctx.config);
    let existing = inner.ov.lock().unwrap().anchor;
    let anchor = existing.unwrap_or_else(|| {
        let mi = monitor(ctx, 0, 0, true);
        default_overlay_anchor(mi.work, mi.dpi)
    });
    let (cx, ey, top) = anchor;
    let mi = monitor(ctx, cx, ey + if top { 1 } else { -1 }, false);
    let m = overlay_metrics(p.overlay_size, mi.dpi, mi.work.2 - mi.work.0, p.overlay_next);
    // The strip grows away from the edge it is pinned by. Clamping only moves
    // what is shown: the anchor is left alone, or every resize near a screen
    // edge would creep the strip.
    let (x, y) = clamp_rect(cx - m.w.div_euclid(2), if top { ey } else { ey - m.h }, m.w, m.h, mi.work);
    place(inner, w, 1, Some((m.w, m.h)), (x, y));
    ctx.config.set("window", "overlay_geometry", &format_geometry(m.w, m.h, x, y));
    let mut ov = inner.ov.lock().unwrap();
    ov.anchor = Some(anchor);
    ov.metrics = Some(m);
    ov.applied = Some((p.overlay_size, p.overlay_next, p.overlay_locked));
}

/// A drag of the unlocked overlay ended: re-anchor and re-fit it for the
/// monitor it was dropped on (work area and DPI).
fn overlay_dropped(ctx: &C, inner: &I) {
    let g = inner.op.lock().unwrap();
    let Some(w) = win(ctx, OVERLAY) else { return };
    let Some((gw, gh, x, y)) = physical_geometry(&w) else { return };
    let mi = monitor(ctx, x + gw / 2, y + gh / 2, false);
    inner.ov.lock().unwrap().anchor = Some(anchor_for(x, y, gw, gh, Some(mi.work)));
    overlay_relayout_locked(ctx, inner, &w);
    drop(g);
    push_extras(ctx, inner);
}

// ── Drag tracking (both windows) ─────────────────────────────────────

fn watch(ctx: &C, inner: &I, w: &WebviewWindow, i: usize) {
    let (ctx, inner) = (ctx.clone(), inner.clone());
    w.on_window_event(move |ev| match ev {
        WindowEvent::Moved(_) => note_moved(&ctx, &inner, i),
        WindowEvent::Destroyed => {
            if inner.open[i].swap(false, Ordering::SeqCst) {
                push_extras(&ctx, &inner);
            }
        }
        _ => {}
    });
}

/// A native drag (startDragging) emits a stream of Moved events and no "drop"
/// event, so the drop is the moment they stop and the mouse button is up.
/// One waiter thread per drag.
fn note_moved(ctx: &C, inner: &I, i: usize) {
    if inner.own_move_recent(i) {
        return;
    }
    if i == 0 {
        inner.glide_gen.fetch_add(1, Ordering::SeqCst); // cancels a glide in progress
    }
    // Overlay only reacts while unlocked; locked it is click-through anyway.
    if i == 1 && read_prefs(&ctx.config).overlay_locked {
        return;
    }
    *inner.moved_at[i].lock().unwrap() = Some(Instant::now());
    if inner.waiting[i].swap(true, Ordering::SeqCst) {
        return;
    }
    let (ctx, inner) = (ctx.clone(), inner.clone());
    std::thread::spawn(move || {
        loop {
            std::thread::sleep(Duration::from_millis(100));
            let quiet = inner.moved_at[i].lock().unwrap().map(|t| t.elapsed() >= DRAG_SETTLE).unwrap_or(true);
            if quiet && !win_mon::left_button_down() {
                break;
            }
        }
        inner.waiting[i].store(false, Ordering::SeqCst);
        if i == 0 {
            mini_settle(&ctx, &inner);
        } else {
            overlay_dropped(&ctx, &inner);
        }
    });
}

// ── Applying preferences ─────────────────────────────────────────────

/// Make the windows match statusify.cfg. Idempotent: the settings page, the
/// tray and hotkeys all just write the config and tell us (ConfigChanged).
fn sync(ctx: &C, inner: &I) {
    let p = read_prefs(&ctx.config);
    let exists = win(ctx, OVERLAY).is_some();
    if p.overlay_enabled && !exists {
        if let Err(e) = overlay_open(ctx, inner) {
            crate::log(&e);
        }
    } else if !p.overlay_enabled && exists {
        overlay_close(ctx, inner);
    } else if exists {
        let applied = inner.ov.lock().unwrap().applied;
        let now = (p.overlay_size, p.overlay_next, p.overlay_locked);
        if applied != Some(now) {
            let g = inner.op.lock().unwrap();
            if let Some(w) = win(ctx, OVERLAY) {
                if applied.map(|a| (a.0, a.1)) != Some((now.0, now.1)) {
                    overlay_relayout_locked(ctx, inner, &w);
                }
                let _ = w.set_ignore_cursor_events(p.overlay_locked);
                inner.ov.lock().unwrap().applied = Some(now);
            }
            drop(g);
        }
    }
    push_extras(ctx, inner);
}

fn cfg_bool(v: bool) -> &'static str {
    if v {
        "true"
    } else {
        "false"
    }
}

fn set_overlay_enabled(ctx: &C, inner: &I, on: bool) {
    ctx.config.set("preferences", "overlay_enabled", cfg_bool(on));
    ctx.engine.config_changed();
    sync(ctx, inner);
}

fn set_overlay_locked(ctx: &C, inner: &I, locked: bool) {
    ctx.config.set("window", "overlay_locked", cfg_bool(locked));
    // Unlocking a hidden overlay switches it on: there would be nothing to move.
    if !locked && !read_prefs(&ctx.config).overlay_enabled {
        ctx.config.set("preferences", "overlay_enabled", "true");
    }
    ctx.engine.config_changed();
    sync(ctx, inner);
}

fn set_overlay_size(ctx: &C, inner: &I, size: i64) {
    let size = size.clamp(MIN_OVERLAY_SIZE, MAX_OVERLAY_SIZE);
    if size == read_prefs(&ctx.config).overlay_size {
        return;
    }
    ctx.config.set("preferences", "overlay_size", &size.to_string());
    ctx.engine.config_changed();
    sync(ctx, inner);
}

fn arg_bool(args: &Value, key: &str) -> Option<bool> {
    args.get(key).and_then(|v| v.as_bool())
}

fn arg_i64(args: &Value, key: &str) -> Option<i64> {
    args.get(key).and_then(|v| v.as_f64()).map(|f| f.round() as i64)
}

// ── Feature ──────────────────────────────────────────────────────────

impl Feature for Windows {
    fn name(&self) -> &'static str {
        "windows"
    }

    fn start(&self, ctx: &Arc<Ctx>) {
        let inner = self.inner.clone();
        push_extras(ctx, &inner);

        // Mini player context menu clicks (global listener; our ids are prefixed).
        {
            let (c, i) = (ctx.clone(), inner.clone());
            ctx.app.on_menu_event(move |_app, ev| {
                let Some(cmd) = ev.id().0.strip_prefix(MENU_PREFIX) else { return };
                match cmd {
                    "toggle" | "prev" | "next" => {
                        c.outbox.send(json!({"type": "player", "action": cmd}));
                    }
                    "open" => show_main(&c),
                    "close" => {
                        let (c, i) = (c.clone(), i.clone());
                        std::thread::spawn(move || mini_close(&c, &i));
                    }
                    _ => {}
                }
            });
        }

        // React to config writes from the settings page, tray and hotkeys.
        {
            let (c, i) = (ctx.clone(), inner.clone());
            let mut rx = ctx.engine.subscribe();
            tauri::async_runtime::spawn(async move {
                loop {
                    match rx.recv().await {
                        Ok(Event::ConfigChanged) | Err(tokio::sync::broadcast::error::RecvError::Lagged(_)) => {
                            let (c, i) = (c.clone(), i.clone());
                            let _ = tauri::async_runtime::spawn_blocking(move || sync(&c, &i)).await;
                        }
                        Ok(_) => {}
                        Err(_) => break,
                    }
                }
            });
        }

        // The overlay comes back if it was on (Python waited 700 ms for the UI).
        if read_prefs(&ctx.config).overlay_enabled {
            let c = ctx.clone();
            tauri::async_runtime::spawn(async move {
                tokio::time::sleep(Duration::from_millis(700)).await;
                let _ = tauri::async_runtime::spawn_blocking(move || sync(&c, &inner)).await;
            });
        }
    }

    fn call(&self, ctx: &Arc<Ctx>, action: &str, args: Value) -> Result<Value, String> {
        let inner = &self.inner;
        let ok = Ok(json!(true));
        match action {
            "get_state" => {
                let p = read_prefs(&ctx.config);
                let m = inner.ov.lock().unwrap().metrics;
                Ok(extras_json(&p, inner.open[0].load(Ordering::SeqCst), inner.open[1].load(Ordering::SeqCst), m.as_ref()))
            }
            "toggle_mini" => {
                if inner.open[0].load(Ordering::SeqCst) {
                    mini_close(ctx, inner);
                } else {
                    mini_open(ctx, inner)?;
                }
                ok
            }
            "open_mini" => mini_open(ctx, inner).map(|_| json!(true)),
            "close_mini" => {
                mini_close(ctx, inner);
                ok
            }
            "mini_menu" => mini_menu(ctx).map(|_| json!(true)),
            "show_main" => {
                show_main(ctx);
                ok
            }
            "toggle_overlay" => {
                let on = !read_prefs(&ctx.config).overlay_enabled;
                set_overlay_enabled(ctx, inner, on);
                ok
            }
            "set_overlay_enabled" => {
                set_overlay_enabled(ctx, inner, arg_bool(&args, "enabled").ok_or("enabled: bool required")?);
                ok
            }
            "toggle_overlay_lock" => {
                let locked = !read_prefs(&ctx.config).overlay_locked;
                set_overlay_locked(ctx, inner, locked);
                ok
            }
            "set_overlay_locked" => {
                set_overlay_locked(ctx, inner, arg_bool(&args, "locked").ok_or("locked: bool required")?);
                ok
            }
            "set_overlay_size" => {
                set_overlay_size(ctx, inner, arg_i64(&args, "size").ok_or("size: number required")?);
                ok
            }
            "nudge_overlay_size" => {
                let cur = read_prefs(&ctx.config).overlay_size;
                set_overlay_size(ctx, inner, cur + arg_i64(&args, "delta").ok_or("delta: number required")?);
                ok
            }
            "set_overlay_next" => {
                let on = arg_bool(&args, "enabled").ok_or("enabled: bool required")?;
                ctx.config.set("preferences", "overlay_next_line", cfg_bool(on));
                ctx.engine.config_changed();
                sync(ctx, inner);
                ok
            }
            _ => Err(format!("windows: unknown action {action}")),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cfg(text: &str) -> (Config, std::path::PathBuf) {
        static N: AtomicU64 = AtomicU64::new(0);
        let d = std::env::temp_dir().join(format!("statusify-win-{}-{}", std::process::id(), N.fetch_add(1, Ordering::SeqCst)));
        std::fs::create_dir_all(&d).unwrap();
        std::fs::write(d.join("statusify.cfg"), text).unwrap();
        (Config::open(&d), d)
    }

    #[test]
    fn prefs_default_like_the_python_app() {
        let (c, d) = cfg("[window]\ngeometry = 520x720+1+1\n");
        let p = read_prefs(&c);
        assert!(!p.overlay_enabled);
        assert!(p.overlay_next);
        assert!(p.overlay_locked);
        assert_eq!(p.overlay_size, 30);
        assert_eq!(p.overlay_opacity, 100);
        assert!(p.animations && p.dark && p.tint);
        assert_eq!(p.offset_ms, -40);
        assert_eq!(p.accent, "#1db954");
        let _ = std::fs::remove_dir_all(d);
    }

    #[test]
    fn prefs_read_python_values_and_clamp() {
        let (c, d) = cfg(
            "[preferences]\noverlay_enabled = True\noverlay_next_line = False\noverlay_size = 999\nanimations = false\noverlay_opacity = 5\nlyric_delay_ms = 120\n[window]\noverlay_locked = false\n",
        );
        let p = read_prefs(&c);
        assert!(p.overlay_enabled && !p.overlay_next && !p.overlay_locked && !p.animations);
        assert_eq!(p.overlay_size, 72);
        assert_eq!(p.overlay_opacity, 20);
        assert_eq!(p.offset_ms, 120);
        let _ = std::fs::remove_dir_all(d);
    }

    #[test]
    fn config_writes_round_trip_in_python_format() {
        let (c, d) = cfg("");
        c.set("window", "overlay_geometry", &format_geometry(900, 110, -1280, 940));
        let c2 = Config::open(&d);
        assert_eq!(parse_geometry(&c2.get_or("window", "overlay_geometry", "")), Some((900, 110, -1280, 940)));
        c.set("window", "mini_geometry", &format_geometry(440, 68, 12, 12));
        assert_eq!(parse_position(&Config::open(&d).get_or("window", "mini_geometry", "")), Some((12, 12)));
        let _ = std::fs::remove_dir_all(d);
    }

    #[test]
    fn extras_expose_state_and_css_pixel_layout() {
        let (c, d) = cfg("");
        let p = read_prefs(&c);
        let hi = overlay_metrics(30, 192, 3840, true); // 200% display
        let v = extras_json(&p, true, false, Some(&hi));
        assert_eq!(v["mini"], true);
        assert_eq!(v["overlay"], false);
        assert_eq!(v["overlay_locked"], true);
        // Physical metrics are reported in CSS px: 60 physical px at 2x = 30.
        assert_eq!(v["layout"]["px"], 30.0);
        assert_eq!(v["layout"]["bar"], 26.0);
        let none = extras_json(&p, false, false, None);
        assert_eq!(none["layout"]["px"], 30.0);
        assert_eq!(none["layout"]["w"], 900.0);
        let _ = std::fs::remove_dir_all(d);
    }

    #[test]
    fn echo_of_our_own_moves_is_recognised_then_expires() {
        let inner = Inner::default();
        assert!(!inner.own_move_recent(0));
        inner.mark_own_move(0);
        assert!(inner.own_move_recent(0));
        assert!(!inner.own_move_recent(1));
        std::thread::sleep(OWN_MOVE_ECHO + Duration::from_millis(30));
        assert!(!inner.own_move_recent(0));
    }

    #[test]
    fn arg_helpers() {
        let a = json!({"enabled": true, "size": 33.4, "delta": -2});
        assert_eq!(arg_bool(&a, "enabled"), Some(true));
        assert_eq!(arg_bool(&a, "x"), None);
        assert_eq!(arg_i64(&a, "size"), Some(33));
        assert_eq!(arg_i64(&a, "delta"), Some(-2));
    }
}
