//! System tray icon and menu (port of the tray half of statusify_ui_mini.py).
//!
//! Menu and behaviour match the Python app: left click shows the window,
//! middle click is play/pause, the tooltip is "Title — Artist" plus the
//! current lyric (or "Paused"), and the dynamic items (mini player, overlay,
//! lock, always on top) follow the real state.

use crate::features::Ctx;
use crate::shell_window;
use serde_json::{json, Value};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tauri::menu::{CheckMenuItem, Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIcon, TrayIconBuilder, TrayIconEvent};

const PLACEHOLDER_LINES: [&str; 8] = ["", "—", "— ", "-", "♪", "• • •", "…", "♪ "];

fn u16len(s: &str) -> usize {
    s.encode_utf16().count()
}

/// `s` cut to at most `n` UTF-16 units (what NOTIFYICONDATA.szTip counts), with "…".
pub fn clip16(s: &str, n: usize) -> String {
    if u16len(s) <= n {
        return s.to_string();
    }
    let mut out = String::new();
    let mut used = 0;
    for ch in s.chars() {
        let u = ch.len_utf16();
        if used + u > n.saturating_sub(1) {
            break;
        }
        out.push(ch);
        used += u;
    }
    format!("{}…", out.trim_end())
}

/// Tray hover text: "Title — Artist" and, on a second line, the current
/// lyric (or "Paused"). szTip holds 128 UTF-16 units (127 + NUL): the lyric
/// is shortened first, the track line only when it alone would not fit.
pub fn tray_tooltip(title: &str, artist: &str, lyric: &str, playing: bool, limit: usize) -> String {
    let one = |s: &str| s.split_whitespace().collect::<Vec<_>>().join(" ");
    let (title, artist, lyric) = (one(title), one(artist), one(lyric));
    if title.is_empty() {
        return clip16("Statusify — waiting for Spotify", limit);
    }
    let head = clip16(&if artist.is_empty() { title.clone() } else { format!("{title} — {artist}") }, limit.min(90));
    let second = if !playing {
        "Paused".to_string()
    } else if PLACEHOLDER_LINES.contains(&lyric.as_str()) {
        String::new()
    } else {
        format!("♪ {lyric}")
    };
    let room = limit as isize - u16len(&head) as isize - 1;
    if !second.is_empty() && room >= 6 {
        return format!("{head}\n{}", clip16(&second, room as usize));
    }
    head
}

/// The lyric offset in force for a track: its own [offsets] entry, else the
/// global delay (statusify_lyrics.resolve_offset_ms).
fn lyric_offset_ms(ctx: &Ctx, uri: &str) -> i64 {
    let global = ctx.config.get_i64("preferences", "lyric_delay_ms", 0);
    let key = uri.rsplit(':').next().unwrap_or("");
    if key.is_empty() {
        return global;
    }
    match ctx.config.get("offsets", key).map(|v| v.trim().to_string()) {
        Some(v) if !v.is_empty() => v.parse::<i64>().unwrap_or(global),
        _ => global,
    }
}

/// The line the tooltip shows: the sung line (offset applied; interpolated
/// across the track for plain lyrics, as select_line does), else what the
/// presence shows when there is nothing to sing (instrumental text, ...).
pub fn tooltip_lyric(lyrics: &crate::lyrics::Lyrics, pos_ms: i64, duration_ms: i64, discord_line: &str) -> String {
    match lyrics.mode.as_str() {
        "synced" if !lyrics.synced.is_empty() => {
            crate::lyrics::current_index(&lyrics.synced, pos_ms).map(|i| lyrics.synced[i].words.clone()).unwrap_or_default()
        }
        "plain" if !lyrics.plain.is_empty() && duration_ms > 0 => {
            let ratio = (pos_ms as f64 / duration_ms as f64).clamp(0.0, 1.0);
            let i = ((ratio * lyrics.plain.len() as f64) as usize).min(lyrics.plain.len() - 1);
            lyrics.plain[i].clone()
        }
        "synced" | "plain" => String::new(),
        _ => discord_line.to_string(),
    }
}

/// What the tray shows right now, derived from the engine snapshot.
fn tooltip_now(ctx: &Ctx) -> String {
    let s = ctx.engine.snapshot();
    let Some(t) = &s.track else { return tray_tooltip("", "", "", false, 127) };
    let pos = s.estimated_position(crate::state::now_ms()) + lyric_offset_ms(ctx, &t.uri);
    let core = s.extras.get("core");
    let text = |k: &str| core.and_then(|c| c.get(k)).and_then(|v| v.as_str()).unwrap_or("").to_string();
    // Instrumental, blacklisted or lyric-less: whatever the presence loop shows.
    let mut idle = text("discord_line");
    if idle.is_empty() && core.and_then(|c| c.get("in_instrumental")).and_then(|v| v.as_bool()).unwrap_or(false) {
        idle = ctx.config.get_or("preferences", "instrumental_text", crate::features::settings::INSTRUMENTAL_DEFAULT);
    }
    let lyric = tooltip_lyric(&s.lyrics, pos, s.duration_ms, &idle);
    tray_tooltip(&t.title, &t.artist, &lyric, s.is_playing, 127)
}

struct Handles {
    icon: TrayIcon,
    mini: MenuItem<tauri::Wry>,
    topmost: CheckMenuItem<tauri::Wry>,
    overlay: CheckMenuItem<tauri::Wry>,
    lock: MenuItem<tauri::Wry>,
    /// (mini open, overlay on, always on top, overlay locked) as last shown.
    last: Mutex<(bool, bool, bool, bool)>,
    last_tip: Mutex<String>,
}

#[derive(Default)]
pub struct Tray {
    h: Mutex<Option<Arc<Handles>>>,
}

fn blocking(ctx: &Arc<Ctx>, f: impl FnOnce(&Arc<Ctx>) + Send + 'static) {
    let c = ctx.clone();
    tauri::async_runtime::spawn_blocking(move || f(&c));
}

fn player(ctx: &Arc<Ctx>, action: &'static str) {
    if !ctx.outbox.send(json!({"type": "player", "action": action})) {
        crate::log("Playback control needs Spotify connected (Spicetify bridge)");
    }
}

pub fn toggle_always_on_top(ctx: &Arc<Ctx>) {
    let on = !ctx.config.get_bool("preferences", "always_on_top", false);
    ctx.config.set("preferences", "always_on_top", if on { "true" } else { "false" });
    shell_window::apply_always_on_top(ctx);
    crate::log(&format!("Always on top {}", if on { "enabled" } else { "disabled" }));
    ctx.engine.config_changed();
}

/// Flip the overlay lock (unlocking a hidden overlay also switches it on).
pub fn toggle_overlay_lock(ctx: &Arc<Ctx>) {
    blocking(ctx, |c| {
        let locked = c.config.get_bool("window", "overlay_locked", true);
        shell_window::set_overlay_locked(c, !locked);
    });
}

fn on_menu(ctx: &Arc<Ctx>, id: &str) {
    match id {
        "show" => shell_window::show_main(&ctx.app),
        "hide" => shell_window::hide_to_tray(ctx, true),
        "playpause" => player(ctx, "toggle"),
        "next" => player(ctx, "next"),
        "prev" => player(ctx, "prev"),
        "mini" => blocking(ctx, |c| log_err(c, "mini player", c.call("windows", "toggle_mini", json!({})))),
        "topmost" => toggle_always_on_top(ctx),
        "overlay" => blocking(ctx, |c| log_err(c, "overlay", c.call("windows", "toggle_overlay", json!({})))),
        "lock" => toggle_overlay_lock(ctx),
        "rpc" => blocking(ctx, |c| log_err(c, "toggle RPC", c.call("core", "toggle_rpc", json!({})))),
        "reconnect" => blocking(ctx, |c| log_err(c, "reconnect RPC", c.call("shell", "reconnect_rpc", json!({})))),
        "quit" => shell_window::quit(ctx),
        _ => {}
    }
}

/// A failed tray action is logged and shown on the app's error line, so the
/// person who clicked it is told (the tray itself has nowhere to say it).
fn log_err(ctx: &Arc<Ctx>, what: &str, r: Result<Value, String>) {
    if let Err(e) = r {
        crate::log(&format!("Tray: {what} failed: {e}"));
        ctx.engine.update(|s| s.note = format!("{what}: {e}"));
    }
}

impl Tray {
    pub fn exists(&self) -> bool {
        self.h.lock().unwrap().is_some()
    }

    /// Build the icon. Must run on the main thread.
    fn build(ctx: &Arc<Ctx>) -> Result<Handles, tauri::Error> {
        let app = &ctx.app;
        let item = |id: &str, text: &str| MenuItem::with_id(app, id, text, true, None::<&str>);
        let check = |id: &str, text: &str, on: bool| CheckMenuItem::with_id(app, id, text, true, on, None::<&str>);
        let mini = item("mini", "Show mini player")?;
        let topmost = check("topmost", "Always on top", ctx.config.get_bool("preferences", "always_on_top", false))?;
        let overlay = check("overlay", "Desktop overlay", false)?;
        let lock = item("lock", "Unlock overlay to move")?;
        let sep = || PredefinedMenuItem::separator(app);
        let menu = Menu::with_items(
            app,
            &[
                &item("show", "Show Statusify")?,
                &item("hide", "Hide to tray")?,
                &sep()?,
                &item("playpause", "Play/Pause")?,
                &item("next", "Next")?,
                &item("prev", "Previous")?,
                &sep()?,
                &mini,
                &topmost,
                &overlay,
                &lock,
                &sep()?,
                &item("rpc", "Toggle Discord RPC")?,
                &item("reconnect", "Reconnect RPC")?,
                &sep()?,
                &item("quit", "Quit")?,
            ],
        )?;
        let (c1, c2) = (ctx.clone(), ctx.clone());
        let mut b = TrayIconBuilder::with_id("statusify")
            .tooltip("Statusify")
            .menu(&menu)
            .show_menu_on_left_click(false)
            .on_menu_event(move |_app, ev| on_menu(&c1, ev.id.as_ref()))
            .on_tray_icon_event(move |_tray, ev| {
                if let TrayIconEvent::Click { button, button_state: MouseButtonState::Up, .. } = ev {
                    match button {
                        MouseButton::Left => shell_window::show_main(&c2.app),
                        MouseButton::Middle => player(&c2, "toggle"),
                        _ => {}
                    }
                }
            });
        // The logo cut at the tray's own size; the window icon (32 px) is the fallback.
        if let Some(icon) = crate::app_icon::tray_icon().or_else(|| app.default_window_icon().cloned()) {
            b = b.icon(icon);
        }
        let icon = b.build(app)?;
        Ok(Handles { icon, mini, topmost, overlay, lock, last: Mutex::new((false, false, ctx.config.get_bool("preferences", "always_on_top", false), true)), last_tip: Mutex::new(String::new()) })
    }

    /// Create the tray on the main thread and start the 1 s sync loop.
    /// Returns whether a tray now exists.
    pub fn start(self: &Arc<Self>, ctx: &Arc<Ctx>) -> bool {
        let (tx, rx) = std::sync::mpsc::channel();
        let c = ctx.clone();
        let _ = ctx.app.run_on_main_thread(move || {
            let _ = tx.send(Tray::build(&c));
        });
        match rx.recv_timeout(Duration::from_secs(10)) {
            Ok(Ok(h)) => {
                *self.h.lock().unwrap() = Some(Arc::new(h));
                crate::log("Tray icon started");
            }
            Ok(Err(e)) => {
                crate::log(&format!("Tray icon failed: {e}"));
                return false;
            }
            Err(_) => {
                crate::log("Tray icon failed: main thread did not answer");
                return false;
            }
        }
        let (me, c) = (self.clone(), ctx.clone());
        tauri::async_runtime::spawn(async move {
            loop {
                me.sync(&c);
                tokio::time::sleep(Duration::from_secs(1)).await;
            }
        });
        true
    }

    /// Re-evaluate the tooltip and the dynamic menu items; only touches the
    /// OS when something actually changed.
    pub fn sync(&self, ctx: &Arc<Ctx>) {
        let Some(h) = self.h.lock().unwrap().clone() else { return };
        let tip = tooltip_now(ctx);
        {
            let mut last = h.last_tip.lock().unwrap();
            if *last != tip {
                *last = tip.clone();
                let _ = h.icon.set_tooltip(Some(tip));
            }
        }
        let extras = ctx.engine.snapshot().extras;
        let flag = |k: &str| extras.get("windows").and_then(|w| w.get(k)).and_then(|v| v.as_bool()).unwrap_or(false);
        let want = (
            flag("mini"),
            flag("overlay"),
            ctx.config.get_bool("preferences", "always_on_top", false),
            ctx.config.get_bool("window", "overlay_locked", true),
        );
        let mut last = h.last.lock().unwrap();
        if last.0 != want.0 {
            let _ = h.mini.set_text(if want.0 { "Hide mini player" } else { "Show mini player" });
        }
        if last.1 != want.1 {
            let _ = h.overlay.set_checked(want.1);
        }
        if last.2 != want.2 {
            let _ = h.topmost.set_checked(want.2);
        }
        if last.3 != want.3 {
            let _ = h.lock.set_text(if want.3 { "Unlock overlay to move" } else { "Lock overlay in place" });
        }
        *last = want;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tooltip_shapes() {
        assert_eq!(tray_tooltip("", "", "", true, 127), "Statusify — waiting for Spotify");
        assert_eq!(tray_tooltip("Song", "Band", "la la", true, 127), "Song — Band\n♪ la la");
        assert_eq!(tray_tooltip("Song", "Band", "la la", false, 127), "Song — Band\nPaused");
        assert_eq!(tray_tooltip("Song", "", "♪", true, 127), "Song");
        assert_eq!(tray_tooltip("Song", "Band", "• • •", true, 127), "Song — Band");
        assert_eq!(tray_tooltip("  Song \n x", "Band", "  a   b ", true, 127), "Song x — Band\n♪ a b");
    }

    fn lyr(mode: &str) -> crate::lyrics::Lyrics {
        use crate::lyrics::{Line, Lyrics};
        let line = |t, w: &str| Line { start_ms: t, words: w.into() };
        Lyrics {
            mode: mode.into(),
            synced: if mode == "synced" { vec![line(1000, "one"), line(5000, "two")] } else { vec![] },
            plain: if mode == "plain" { vec!["a".into(), "b".into(), "c".into(), "d".into()] } else { vec![] },
            source: String::new(),
        }
    }

    #[test]
    fn tooltip_lyric_follows_the_offset_position() {
        let l = lyr("synced");
        assert_eq!(tooltip_lyric(&l, 500, 10_000, ""), "");
        assert_eq!(tooltip_lyric(&l, 1000, 10_000, ""), "one");
        // The same playback moment with a +4000 ms offset lands on the next line.
        assert_eq!(tooltip_lyric(&l, 2000 + 3000, 10_000, ""), "two");
    }

    #[test]
    fn tooltip_lyric_plain_instrumental_and_blacklisted() {
        let p = lyr("plain");
        assert_eq!(tooltip_lyric(&p, 0, 8000, ""), "a");
        assert_eq!(tooltip_lyric(&p, 4000, 8000, ""), "c");
        assert_eq!(tooltip_lyric(&p, 99_000, 8000, ""), "d");
        assert_eq!(tooltip_lyric(&p, 0, 0, ""), "", "plain needs a duration");
        // No lyrics: whatever the presence shows (instrumental text, blacklist note).
        let none = crate::lyrics::Lyrics::none();
        assert_eq!(tooltip_lyric(&none, 0, 1000, "🎵 ─ 🎵"), "🎵 ─ 🎵");
        assert_eq!(tray_tooltip("Song", "Band", &tooltip_lyric(&none, 0, 1000, "🎵 ─ 🎵"), true, 127), "Song — Band\n♪ 🎵 ─ 🎵");
        // A synced song's own silence never shows the idle text.
        assert_eq!(tooltip_lyric(&lyr("synced"), 0, 1000, "idle"), "");
    }

    #[test]
    fn tooltip_never_exceeds_the_tip_limit() {
        let long = "x".repeat(300);
        for (t, a, l) in [(&long[..], "Band", "lyric"), ("Song", &long[..], &long[..]), ("Song", "Band", &long[..])] {
            let s = tray_tooltip(t, a, l, true, 127);
            assert!(u16len(&s) <= 127, "{} units", u16len(&s));
        }
        // Emoji are two UTF-16 units each.
        let emoji = "🎵".repeat(100);
        let s = tray_tooltip("Song", "Band", &emoji, true, 127);
        assert!(u16len(&s) <= 127);
        assert!(s.ends_with('…'));
    }

    #[test]
    fn clip_keeps_short_text_and_counts_surrogates() {
        assert_eq!(clip16("abc", 10), "abc");
        assert_eq!(clip16("abcdefgh", 5), "abcd…");
        assert_eq!(clip16("🎵🎵🎵", 5), "🎵🎵…");
    }
}
