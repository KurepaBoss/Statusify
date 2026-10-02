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
//! Actions: toggle_mini, open_mini, close_mini, mini_menu, mini_press, show_main,
//!   player_failed, get_timing {uri}, overlay_visible {visible}, raise_overlay,
//!   toggle_overlay, set_overlay_enabled {enabled}, toggle_overlay_lock,
//!   set_overlay_locked {locked}, set_overlay_size {size},
//!   nudge_overlay_size {delta}, set_overlay_next {enabled}, get_state.
//! Pushes extras["windows"] = {mini, overlay, overlay_locked, overlay_size,
//!   overlay_next, overlay_opacity, animations, dark, tint, offset_ms, offsets,
//!   instrumental_text, timing_rev, accent, layout}.
//! Event "mini-drag-end" {inside} (to the mini window) after a native drag.
//! Word timing (endMs, syl) is not in the engine's Line type, so it is read
//! off the raw bridge messages and served through get_timing.

use super::{Ctx, Feature};
use crate::config::Config;
use crate::engine::Event;
use serde_json::{json, Value};
use std::collections::VecDeque;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tauri::menu::{ContextMenu, Menu, MenuItem, PredefinedMenuItem};
use tauri::{Emitter, Manager, PhysicalPosition, PhysicalSize, WebviewUrl, WebviewWindow, WebviewWindowBuilder, WindowEvent};

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
/// Python's default instrumental text (INSTRUMENTAL_TEXT).
const DEFAULT_INSTRUMENTAL: &str = "\u{1F3B5} \u{2500} \u{2500} \u{2500} \u{2500} \u{2500} \u{2500} \u{2500} \u{2500} \u{2500} \u{1F3B5}";
/// How long a moved overlay position waits before it is written (Python: _cfg_set_soon).
const GEO_DEBOUNCE: Duration = Duration::from_millis(400);
/// The overlay re-asserts topmost this often while shown.
const RAISE_EVERY: Duration = Duration::from_millis(2000);
/// Distinct tracks whose word timing is kept.
const TIMING_KEEP: usize = 6;

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
    instrumental_text: String,
    accent: String,
}

fn read_prefs(c: &Config) -> Prefs {
    Prefs {
        overlay_enabled: only_true_is_on(c.get("preferences", "overlay_enabled").as_deref(), false),
        overlay_next: only_true_is_on(c.get("preferences", "overlay_next_line").as_deref(), true),
        // Python reads these three as literals: only "true" turns the first two
        // on, only "false" unlocks (hand-edited "1"/"no" keep their old meaning).
        overlay_locked: only_false_is_off(c.get("window", "overlay_locked").as_deref(), true),
        overlay_size: c.get_i64("preferences", "overlay_size", DEFAULT_OVERLAY_SIZE).clamp(MIN_OVERLAY_SIZE, MAX_OVERLAY_SIZE),
        overlay_opacity: c.get_i64("preferences", "overlay_opacity", 100).clamp(20, 100),
        animations: c.get_bool("preferences", "animations", true),
        dark: c.get_bool("preferences", "dark_mode", true),
        tint: c.get_bool("preferences", "album_tint", true),
        offset_ms: c.get_i64("preferences", "lyric_delay_ms", 0),
        instrumental_text: Some(c.get_or("preferences", "instrumental_text", "")).filter(|t| !t.trim().is_empty()).unwrap_or_else(|| DEFAULT_INSTRUMENTAL.into()),
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
    /// Bumped per open of each window; a dying window's late Destroyed event
    /// must not clear the flag of the window that replaced it.
    open_gen: [AtomicU64; 2],
    /// Overlay window currently mapped (it is hidden while faded out).
    ov_shown: AtomicBool,
    /// Debounced overlay_geometry write.
    geo: Arc<Debounce>,
    /// Word timing per track from the raw bridge messages (newest last).
    timing: Mutex<VecDeque<(String, Vec<Value>)>>,
    timing_rev: AtomicU64,
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
///
/// Position first, then size: the sizes are physical px for the TARGET
/// monitor's DPI, and a window that crosses to a monitor with another DPI is
/// rescaled by WM_DPICHANGED, which would rescale an already-set size again.
/// The position is set once more afterwards in case the DPI change nudged it.
fn place(inner: &I, w: &WebviewWindow, i: usize, size: Option<(i32, i32)>, pos: (i32, i32)) {
    inner.mark_own_move(i);
    let _ = w.set_position(PhysicalPosition::new(pos.0, pos.1));
    if let Some((sw, sh)) = size {
        let _ = w.set_size(PhysicalSize::new(sw.max(1) as u32, sh.max(1) as u32));
        let _ = w.set_position(PhysicalPosition::new(pos.0, pos.1));
        if i == 0 {
            mini_region(w, sw, sh);
        }
    }
    inner.mark_own_move(i);
}

fn hwnd_of(w: &WebviewWindow) -> Option<isize> {
    w.hwnd().ok().map(|h| h.0 as isize)
}

/// Clip the mini window to its pill so the transparent corners are not hit.
fn mini_region(w: &WebviewWindow, pw: i32, ph: i32) {
    if let Some(h) = hwnd_of(w) {
        win_mon::round_region(h, pw, ph);
    }
}

/// Wait for a destroyed window's label to be released (the window outlives
/// destroy() briefly, and a new one cannot reuse the label until it is gone).
fn wait_label_free(ctx: &C, label: &str) {
    for _ in 0..150 {
        if win(ctx, label).is_none() {
            return;
        }
        std::thread::sleep(Duration::from_millis(20));
    }
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

/// [offsets] (per-track lyric offsets, lower-cased keys as Config stores them) as
/// {key: ms}; unparsable values are dropped (the global delay applies, as in Python).
fn offsets_json(c: &Config) -> Value {
    let m: serde_json::Map<String, Value> = c
        .section("offsets")
        .into_iter()
        .filter_map(|(k, v)| v.trim().parse::<i64>().ok().map(|n| (k, json!(n))))
        .collect();
    Value::Object(m)
}

fn extras_json(p: &Prefs, mini: bool, overlay: bool, metrics: Option<&OverlayMetrics>, offsets: Value, timing_rev: u64) -> Value {
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
        "offsets": offsets,
        "instrumental_text": p.instrumental_text,
        "timing_rev": timing_rev,
        "accent": p.accent,
        "layout": layout_json(metrics.unwrap_or(&fallback)),
    })
}

fn push_extras(ctx: &C, inner: &I) {
    let p = read_prefs(&ctx.config);
    let metrics = inner.ov.lock().unwrap().metrics;
    let v = extras_json(
        &p,
        inner.open[0].load(Ordering::SeqCst),
        inner.open[1].load(Ordering::SeqCst),
        metrics.as_ref(),
        offsets_json(&ctx.config),
        inner.timing_rev.load(Ordering::SeqCst),
    );
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
    if inner.open[0].load(Ordering::SeqCst) {
        return Ok(());
    }
    wait_label_free(ctx, MINI);
    // Saved spot, else top centre of the primary monitor.
    let saved = parse_position(&ctx.config.get_or("window", "mini_geometry", ""));
    let mi = match saved {
        Some((x, y)) => monitor(ctx, x, y, false),
        None => monitor(ctx, 0, 0, true),
    };
    let s = mi.dpi as f64 / 96.0;
    let (w, h) = ((MINI_W * s).round() as i32, (MINI_H * s).round() as i32);
    // Python: 40 unscaled px below the top of the screen.
    let pos = saved.unwrap_or((mi.work.0 + (mi.work.2 - mi.work.0 - w) / 2, mi.work.1 + 40));
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
    tidy_style(&window);
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
    if !inner.open[0].swap(false, Ordering::SeqCst) {
        return; // not open, or already closing (the window outlives destroy() briefly)
    }
    mini_save_pos(ctx);
    inner.glide_gen.fetch_add(1, Ordering::SeqCst);
    if let Some(w) = win(ctx, MINI) {
        let _ = w.destroy();
    }
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
    // The native drag swallowed the page's pointerup: tell it the gesture is
    // over and whether the pointer is still on the pill (Python: _mini_leave).
    mini_drag_end(&w);
    if (tx, ty) == (x, y) {
        mini_save_pos(ctx);
        return;
    }
    if !read_prefs(&ctx.config).animations {
        place(inner, &w, 0, None, (tx, ty));
        mini_save_pos(ctx);
        mini_drag_end(&w);
        return;
    }
    let gen = inner.glide_gen.fetch_add(1, Ordering::SeqCst) + 1;
    let t0 = Instant::now();
    loop {
        // Only a press on the pill (mini_press) or a new drag (Moved events)
        // bumps the generation and takes over; a click elsewhere does not.
        if inner.glide_gen.load(Ordering::SeqCst) != gen {
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
    mini_drag_end(&w); // the glide may have carried the pill out from under the pointer
}

/// Is the pointer over the mini window right now?
fn pointer_inside(w: &WebviewWindow) -> bool {
    match (win_mon::cursor_pos(), physical_geometry(w)) {
        (Some((cx, cy)), Some((gw, gh, x, y))) => cx >= x && cx < x + gw && cy >= y && cy < y + gh,
        _ => true, // unknown: do not collapse the pill under a pointer that may be on it
    }
}

fn mini_drag_end(w: &WebviewWindow) {
    let _ = w.emit_to(MINI, "mini-drag-end", json!({ "inside": pointer_inside(w) }));
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

/// Python: _tray_player logs this when the bridge is not connected.
fn log_player_failed() {
    crate::log("Playback control needs Spotify connected (Spicetify bridge)");
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
    if inner.open[1].load(Ordering::SeqCst) {
        return Ok(());
    }
    wait_label_free(ctx, OVERLAY);
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
    inner.ov_shown.store(true, Ordering::SeqCst);
    tidy_style(&window);
    raise_overlay(&window);
    spawn_raiser(ctx, inner);
    crate::log("Lyrics overlay on");
    drop(g);
    push_extras(ctx, inner);
    Ok(())
}

fn overlay_close(ctx: &C, inner: &I) {
    let g = inner.op.lock().unwrap();
    if !inner.open[1].swap(false, Ordering::SeqCst) {
        return; // not open, or already closing (the window outlives destroy() briefly)
    }
    inner.ov_shown.store(false, Ordering::SeqCst);
    flush_geometry(ctx, inner);
    if let Some(w) = win(ctx, OVERLAY) {
        let _ = w.destroy();
    }
    inner.ov.lock().unwrap().applied = None;
    crate::log("Lyrics overlay off");
    drop(g);
    push_extras(ctx, inner);
}

/// overlay_geometry is rewritten on every resize notch and drag drop; only the
/// last value within the quiet window reaches statusify.cfg (Python: _cfg_set_soon).
fn save_geometry_soon(ctx: &C, inner: &I, geometry: String) {
    let cfg = ctx.config.clone();
    inner.geo.push(geometry, GEO_DEBOUNCE, Arc::new(move |g| cfg.set("window", "overlay_geometry", &g)));
}

fn flush_geometry(ctx: &C, inner: &I) {
    inner.geo.flush(|g| ctx.config.set("window", "overlay_geometry", &g));
}

/// Out of Alt+Tab / Win+Tab, as Python's WS_EX_TOOLWINDOW (see win_mon).
fn tidy_style(w: &WebviewWindow) {
    if let Some(h) = hwnd_of(w) {
        win_mon::hide_from_switcher(h);
    }
}

fn raise_overlay(w: &WebviewWindow) {
    if let Some(h) = hwnd_of(w) {
        win_mon::raise_topmost(h);
    }
}

/// Python re-raised the overlay on every scene change so a game or another
/// topmost window that raised itself later cannot bury it. The page asks for
/// that on scene changes (raise_overlay); this covers the quiet stretches.
fn spawn_raiser(ctx: &C, inner: &I) {
    let (ctx, inner) = (ctx.clone(), inner.clone());
    let my = inner.open_gen[1].load(Ordering::SeqCst);
    std::thread::spawn(move || loop {
        std::thread::sleep(RAISE_EVERY);
        if inner.open_gen[1].load(Ordering::SeqCst) != my || !inner.open[1].load(Ordering::SeqCst) {
            return;
        }
        if inner.ov_shown.load(Ordering::SeqCst) {
            if let Some(w) = win(&ctx, OVERLAY) {
                tidy_style(&w);
                raise_overlay(&w);
            }
        }
    });
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
    save_geometry_soon(ctx, inner, format_geometry(m.w, m.h, x, y));
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
    let my = inner.open_gen[i].fetch_add(1, Ordering::SeqCst) + 1;
    let hwnd = hwnd_of(w);
    w.on_window_event(move |ev| match ev {
        WindowEvent::Moved(_) => note_moved(&ctx, &inner, i),
        // A DPI change resizes the pill: the click-through clip must follow.
        WindowEvent::Resized(sz) if i == 0 => {
            if let Some(h) = hwnd {
                win_mon::round_region(h, sz.width as i32, sz.height as i32);
            }
        }
        WindowEvent::Destroyed => {
            // Only the current window may clear the flag: a quick off/on leaves
            // the old one's Destroyed arriving after the new one opened.
            if inner.open_gen[i].load(Ordering::SeqCst) == my && inner.open[i].swap(false, Ordering::SeqCst) {
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
    let exists = inner.open[1].load(Ordering::SeqCst);
    if p.overlay_enabled && !exists {
        if let Err(e) = overlay_open(ctx, inner) {
            crate::log(&e);
        }
    } else if !p.overlay_enabled && exists {
        overlay_close(ctx, inner);
    } else if exists {
        let now = (p.overlay_size, p.overlay_next, p.overlay_locked);
        if inner.ov.lock().unwrap().applied != Some(now) {
            // Re-read under the op lock: a ConfigChanged event and the direct
            // call from an action both land here, and only one should relayout.
            let g = inner.op.lock().unwrap();
            let applied = inner.ov.lock().unwrap().applied;
            if applied != Some(now) {
                if let Some(w) = win(ctx, OVERLAY) {
                    if applied.map(|a| (a.0, a.1)) != Some((now.0, now.1)) {
                        overlay_relayout_locked(ctx, inner, &w);
                    }
                    let _ = w.set_ignore_cursor_events(p.overlay_locked);
                    tidy_style(&w);
                    inner.ov.lock().unwrap().applied = Some(now);
                }
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

// -- Word timing (karaoke fill, end-of-line gaps) -----------------------

/// Per-line timing out of a raw bridge "lyrics"/"lyrics_prefetch" message:
/// (track uri, [{s: startMs, w: words, e: endMs?, syl: [[start, end, text]]?}])
/// for the lines that carry any. The engine's Line type keeps only startMs and
/// words, so this is the only place the extra fields survive.
fn timing_from_bridge(msg: &Value) -> Option<(String, Vec<Value>)> {
    let t = msg.get("type")?.as_str()?;
    if t != "lyrics" && t != "lyrics_prefetch" {
        return None;
    }
    let uri = msg.get("track_uri")?.as_str()?.to_string();
    let mut out = Vec::new();
    for line in msg.get("synced")?.as_array()? {
        let Some(s) = line.get("startMs").and_then(|v| v.as_f64()) else { continue };
        let words = line.get("words").and_then(|v| v.as_str()).unwrap_or("");
        let end = line.get("endMs").and_then(|v| v.as_f64()).map(|f| f as i64);
        let syl = line.get("syl").filter(|v| v.as_array().is_some_and(|a| !a.is_empty()));
        if end.is_none() && syl.is_none() {
            continue;
        }
        out.push(json!({ "s": s as i64, "w": words, "e": end, "syl": syl }));
    }
    Some((uri, out))
}

/// Remember a track's timing (newest last, at most TIMING_KEEP tracks).
/// Returns whether anything changed.
fn store_timing(inner: &Inner, uri: String, lines: Vec<Value>) -> bool {
    let mut q = inner.timing.lock().unwrap();
    let before = q.iter().find(|(u, _)| *u == uri).map(|(_, l)| l.clone());
    if before.as_ref() == Some(&lines) {
        return false;
    }
    if before.is_none() && lines.is_empty() {
        return false;
    }
    q.retain(|(u, _)| *u != uri);
    q.push_back((uri, lines));
    while q.len() > TIMING_KEEP {
        q.pop_front();
    }
    drop(q);
    inner.timing_rev.fetch_add(1, Ordering::SeqCst);
    true
}

fn timing_for(inner: &Inner, uri: &str) -> Value {
    let q = inner.timing.lock().unwrap();
    q.iter().find(|(u, _)| u == uri).map(|(_, l)| json!(l)).unwrap_or_else(|| json!([]))
}

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
                        if !c.outbox.send(json!({"type": "player", "action": cmd})) {
                            log_player_failed();
                        }
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
                        Ok(Event::Bridge(m)) => {
                            if let Some((uri, lines)) = timing_from_bridge(&m) {
                                if store_timing(&i, uri, lines) {
                                    push_extras(&c, &i);
                                }
                            }
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
                Ok(extras_json(
                    &p,
                    inner.open[0].load(Ordering::SeqCst),
                    inner.open[1].load(Ordering::SeqCst),
                    m.as_ref(),
                    offsets_json(&ctx.config),
                    inner.timing_rev.load(Ordering::SeqCst),
                ))
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
            // The pill was pressed: a snap glide in progress stops (Python: _mini_press).
            "mini_press" => {
                inner.glide_gen.fetch_add(1, Ordering::SeqCst);
                ok
            }
            "player_failed" => {
                log_player_failed();
                ok
            }
            "get_timing" => Ok(timing_for(inner, args.get("uri").and_then(|v| v.as_str()).unwrap_or(""))),
            "raise_overlay" => {
                if let Some(w) = win(ctx, OVERLAY) {
                    if inner.ov_shown.load(Ordering::SeqCst) {
                        raise_overlay(&w);
                    }
                }
                ok
            }
            // The faded overlay is unmapped (Python: SW_HIDE), not left as an
            // invisible topmost window.
            "overlay_visible" => {
                let show = arg_bool(&args, "visible").ok_or("visible: bool required")?;
                if inner.open[1].load(Ordering::SeqCst) && inner.ov_shown.swap(show, Ordering::SeqCst) != show {
                    if let Some(w) = win(ctx, OVERLAY) {
                        if show {
                            let _ = w.show();
                            tidy_style(&w);
                            raise_overlay(&w);
                        } else {
                            let _ = w.hide();
                        }
                    }
                }
                ok
            }
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
        assert_eq!(p.offset_ms, 0, "Python: LYRIC_DELAY_MS defaults to 0");
        assert_eq!(p.instrumental_text, DEFAULT_INSTRUMENTAL);
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
        let v = extras_json(&p, true, false, Some(&hi), json!({}), 3);
        assert_eq!(v["timing_rev"], 3);
        assert_eq!(v["mini"], true);
        assert_eq!(v["overlay"], false);
        assert_eq!(v["overlay_locked"], true);
        // Physical metrics are reported in CSS px: 60 physical px at 2x = 30.
        assert_eq!(v["layout"]["px"], 30.0);
        assert_eq!(v["layout"]["bar"], 26.0);
        let none = extras_json(&p, false, false, None, json!({}), 0);
        assert_eq!(none["layout"]["px"], 30.0);
        assert_eq!(none["layout"]["w"], 900.0);
        let _ = std::fs::remove_dir_all(d);
    }

    #[test]
    fn hand_edited_booleans_keep_their_python_meaning() {
        let (c, d) = cfg("[preferences]\noverlay_enabled = 1\noverlay_next_line = yes\n[window]\noverlay_locked = 0\n");
        let p = read_prefs(&c);
        assert!(!p.overlay_enabled, "only the literal true enables the overlay");
        assert!(!p.overlay_next, "yes is not true");
        assert!(p.overlay_locked, "0 is not the literal false, so it stays locked");
        let _ = std::fs::remove_dir_all(d);
        let (c, d) = cfg("[preferences]\noverlay_enabled = TRUE\noverlay_next_line = true\n[window]\noverlay_locked = False\n");
        let p = read_prefs(&c);
        assert!(p.overlay_enabled && p.overlay_next && !p.overlay_locked);
        let _ = std::fs::remove_dir_all(d);
    }

    #[test]
    fn per_track_offsets_and_instrumental_text_reach_the_extras() {
        let (c, d) = cfg("[preferences]\ninstrumental_text = ~ solo ~\nlyric_delay_ms = 15\n[offsets]\nabc123 = -250\nbad = nope\n");
        let p = read_prefs(&c);
        assert_eq!(p.instrumental_text, "~ solo ~");
        let off = offsets_json(&c);
        assert_eq!(off["abc123"], -250);
        assert!(off.get("bad").is_none(), "unparsable offsets fall back to the global delay");
        let v = extras_json(&p, false, false, None, off, 0);
        assert_eq!(v["offsets"]["abc123"], -250);
        assert_eq!(v["offset_ms"], 15);
        assert_eq!(v["instrumental_text"], "~ solo ~");
        let _ = std::fs::remove_dir_all(d);
    }

    #[test]
    fn word_timing_is_read_off_bridge_lyrics_messages() {
        let msg = json!({"type": "lyrics", "track_uri": "spotify:track:t1", "mode": "synced", "synced": [
            {"startMs": 1000, "words": "plain line"},
            {"startMs": 2000, "words": "hello world", "endMs": 4500, "syl": [[2000, 3000, "hello "], [3000, 4500, "world"]]},
            {"startMs": 6000, "words": "only end", "endMs": 7000},
            {"words": "no start"}
        ]});
        let (uri, lines) = timing_from_bridge(&msg).unwrap();
        assert_eq!(uri, "spotify:track:t1");
        assert_eq!(lines.len(), 2, "lines without timing are left out");
        assert_eq!(lines[0]["s"], 2000);
        assert_eq!(lines[0]["e"], 4500);
        assert_eq!(lines[0]["syl"][1][2], "world");
        assert_eq!(lines[1]["e"], 7000);
        assert!(lines[1]["syl"].is_null());
        let pre = json!({"type": "lyrics_prefetch", "track_uri": "u2", "synced": [{"startMs": 1, "words": "x", "endMs": 9}]});
        assert!(timing_from_bridge(&pre).is_some());
        assert!(timing_from_bridge(&json!({"type": "position", "track_uri": "u"})).is_none());
        assert!(timing_from_bridge(&json!({"type": "lyrics", "synced": []})).is_none(), "needs a track uri");
    }

    #[test]
    fn timing_store_keeps_recent_tracks_and_bumps_the_revision() {
        let inner = Inner::default();
        let l = |n: i64| vec![json!({"s": n, "w": "x", "e": n + 5, "syl": null})];
        assert!(store_timing(&inner, "a".into(), l(1)));
        assert_eq!(inner.timing_rev.load(Ordering::SeqCst), 1);
        assert!(!store_timing(&inner, "a".into(), l(1)), "same timing again is not a change");
        assert!(!store_timing(&inner, "none".into(), vec![]), "nothing to remember");
        assert!(store_timing(&inner, "a".into(), l(2)));
        assert_eq!(timing_for(&inner, "a")[0]["s"], 2);
        assert_eq!(timing_for(&inner, "missing"), json!([]));
        for k in 0..TIMING_KEEP {
            store_timing(&inner, format!("t{k}"), l(k as i64));
        }
        assert_eq!(timing_for(&inner, "a"), json!([]), "oldest track dropped");
        assert_eq!(inner.timing.lock().unwrap().len(), TIMING_KEEP);
        // A track whose timing vanished (re-fetched without it) is forgotten.
        assert!(store_timing(&inner, "t0".into(), vec![]));
        assert_eq!(timing_for(&inner, "t0"), json!([]));
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
