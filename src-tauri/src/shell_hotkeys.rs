//! Global hotkeys (port of statusify_hotkeys.py).
//!
//! Combos are the same strings the Python app stored in statusify.cfg
//! ("ctrl+alt+n"): case- and space-insensitive, exactly one non-modifier key,
//! at least one of ctrl/alt/shift/win. Registration goes through
//! tauri-plugin-global-shortcut (RegisterHotKey underneath, so it works while
//! other apps have focus and installs no keyboard hook).

use crate::features::Ctx;
use serde_json::json;
use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tauri_plugin_global_shortcut::{Code, GlobalShortcutExt, Modifiers, Shortcut, ShortcutState};

/// (binding name, config key in [preferences], default combo).
pub const BINDINGS: [(&str, &str, &str); 4] = [
    ("skip", "hotkey_skip", "ctrl+alt+n"),
    ("toggle", "hotkey_toggle", "ctrl+alt+s"),
    ("skip_instr", "hotkey_skip_instr", "ctrl+alt+i"),
    ("overlay", "hotkey_overlay", "ctrl+alt+o"),
];

/// A held key must not fire the action over and over.
const REPEAT_GUARD: Duration = Duration::from_millis(300);

fn named_key(k: &str) -> Option<Code> {
    use Code::*;
    Some(match k {
        "space" => Space,
        "enter" | "return" => Enter,
        "tab" => Tab,
        "esc" | "escape" => Escape,
        "backspace" => Backspace,
        "insert" | "ins" => Insert,
        "delete" | "del" => Delete,
        "home" => Home,
        "end" => End,
        "pageup" | "pgup" => PageUp,
        "pagedown" | "pgdn" => PageDown,
        "left" => ArrowLeft,
        "up" => ArrowUp,
        "right" => ArrowRight,
        "down" => ArrowDown,
        "pause" => Pause,
        "printscreen" => PrintScreen,
        "capslock" => CapsLock,
        "playpause" => MediaPlayPause,
        "nexttrack" => MediaTrackNext,
        "prevtrack" => MediaTrackPrevious,
        "volumeup" => AudioVolumeUp,
        "volumedown" => AudioVolumeDown,
        "volumemute" => AudioVolumeMute,
        ";" => Semicolon,
        "=" => Equal,
        "," => Comma,
        "-" => Minus,
        "." => Period,
        "/" => Slash,
        "`" => Backquote,
        "[" => BracketLeft,
        "\\" => Backslash,
        "]" => BracketRight,
        "'" => Quote,
        _ => return None,
    })
}

const LETTERS: [Code; 26] = {
    use Code::*;
    [KeyA, KeyB, KeyC, KeyD, KeyE, KeyF, KeyG, KeyH, KeyI, KeyJ, KeyK, KeyL, KeyM, KeyN, KeyO, KeyP, KeyQ, KeyR, KeyS, KeyT, KeyU, KeyV, KeyW, KeyX, KeyY, KeyZ]
};
const DIGITS: [Code; 10] = {
    use Code::*;
    [Digit0, Digit1, Digit2, Digit3, Digit4, Digit5, Digit6, Digit7, Digit8, Digit9]
};
const NUMPAD: [Code; 10] = {
    use Code::*;
    [Numpad0, Numpad1, Numpad2, Numpad3, Numpad4, Numpad5, Numpad6, Numpad7, Numpad8, Numpad9]
};
const FKEYS: [Code; 24] = {
    use Code::*;
    [F1, F2, F3, F4, F5, F6, F7, F8, F9, F10, F11, F12, F13, F14, F15, F16, F17, F18, F19, F20, F21, F22, F23, F24]
};

/// "ctrl+alt+n" -> (modifiers, key). Errors read as the sentence the
/// Settings page shows next to the field.
pub fn parse_combo(combo: &str) -> Result<(Modifiers, Code), String> {
    let parts: Vec<String> = combo
        .split('+')
        .map(|p| p.trim().to_lowercase().replace(' ', ""))
        .filter(|p| !p.is_empty())
        .collect();
    if parts.is_empty() {
        return Err("empty hotkey".into());
    }
    let mut mods = Modifiers::empty();
    let mut keys: Vec<&str> = Vec::new();
    for p in &parts {
        match p.as_str() {
            "ctrl" | "control" => mods |= Modifiers::CONTROL,
            "alt" => mods |= Modifiers::ALT,
            "shift" => mods |= Modifiers::SHIFT,
            "win" | "windows" | "super" => mods |= Modifiers::SUPER,
            other => keys.push(other),
        }
    }
    if keys.len() != 1 {
        return Err(format!("'{combo}' needs exactly one non-modifier key"));
    }
    let k = keys[0];
    let mut chars = k.chars();
    let code = match (chars.next(), chars.next()) {
        (Some(c), None) if c.is_ascii_lowercase() => LETTERS[(c as u8 - b'a') as usize],
        (Some(c), None) if c.is_ascii_digit() => DIGITS[(c as u8 - b'0') as usize],
        _ => {
            if let Some(n) = k.strip_prefix('f').and_then(|n| n.parse::<usize>().ok()).filter(|n| (1..=24).contains(n)) {
                FKEYS[n - 1]
            } else if let Some(n) = k.strip_prefix("numpad").or_else(|| k.strip_prefix("num")).and_then(|n| n.parse::<usize>().ok()).filter(|n| *n <= 9) {
                NUMPAD[n]
            } else if let Some(c) = named_key(k) {
                c
            } else {
                return Err(format!("unknown key '{k}' in '{combo}'"));
            }
        }
    };
    if mods.is_empty() {
        return Err(format!("'{combo}' needs at least one of ctrl/alt/shift/win"));
    }
    Ok((mods, code))
}

/// The combo configured for a binding. A missing key means the default; a
/// present-but-empty value means the user turned that hotkey off.
///
/// Deliberate divergence from Python: main.py reads skip/toggle/skip_instr with
/// `_cfg_get(...) or default`, so clearing one only disabled it until the next
/// launch. Here an empty value stays off, which is what the person asked for.
/// (The overlay hotkey never had the `or default` in Python and behaves the same.)
pub fn configured(ctx: &Ctx, name: &str) -> String {
    let Some((_, key, default)) = BINDINGS.iter().find(|b| b.0 == name) else { return String::new() };
    ctx.config.get("preferences", key).unwrap_or_else(|| (*default).to_string()).trim().to_string()
}

pub fn all_configured(ctx: &Ctx) -> Vec<(&'static str, String)> {
    BINDINGS.iter().map(|b| (b.0, configured(ctx, b.0))).collect()
}

/// What a hotkey does. Runs off the UI thread (ctx.call may block).
fn dispatch(ctx: &Arc<Ctx>, name: &str) {
    let r = match name {
        "skip" => {
            // Python sent {"type": "skip_track"}; the bridge understands it.
            let sent = ctx.outbox.send(json!({"type": "skip_track"}));
            crate::log(if sent { "Hotkey: skip track" } else { "Hotkey: skip track - Spotify is not connected" });
            Ok(json!(null))
        }
        "toggle" => ctx.call("core", "toggle_rpc", json!({})),
        "skip_instr" => ctx.call("core", "skip_instrumental", json!({})),
        "overlay" => ctx.call("windows", "toggle_overlay", json!({})),
        _ => Ok(json!(null)),
    };
    if let Err(e) = r {
        crate::log(&format!("Hotkey '{name}' action failed: {e}"));
    }
}

/// Replace every registered hotkey with the configured set. Returns
/// {binding name: reason} for each one that could not be registered.
pub fn apply(ctx: &Arc<Ctx>) -> HashMap<String, String> {
    let gs = ctx.app.global_shortcut();
    let _ = gs.unregister_all();
    let mut failed = HashMap::new();
    let last_fire: Arc<Mutex<HashMap<&'static str, Instant>>> = Default::default();
    let mut summary = Vec::new();
    for (name, combo) in all_configured(ctx) {
        summary.push(format!("{name}={}", if combo.is_empty() { "none" } else { &combo }));
        if combo.is_empty() {
            continue;
        }
        let (mods, code) = match parse_combo(&combo) {
            Ok(v) => v,
            Err(e) => {
                failed.insert(name.to_string(), e);
                continue;
            }
        };
        let c = ctx.clone();
        let guard = last_fire.clone();
        let r = gs.on_shortcut(Shortcut::new(Some(mods), code), move |_app, _sc, ev| {
            if ev.state != ShortcutState::Pressed {
                return;
            }
            {
                let mut g = guard.lock().unwrap();
                let now = Instant::now();
                if g.get(name).is_some_and(|t| now.duration_since(*t) < REPEAT_GUARD) {
                    return;
                }
                g.insert(name, now);
            }
            let c = c.clone();
            tauri::async_runtime::spawn_blocking(move || dispatch(&c, name));
        });
        if let Err(e) = r {
            let s = e.to_string();
            let why = if s.to_lowercase().contains("already") {
                format!("'{combo}' is already in use by another program")
            } else {
                format!("'{combo}' could not be registered ({s})")
            };
            failed.insert(name.to_string(), why);
        }
    }
    crate::log(&format!("Hotkeys registered  ·  {}", summary.join("  ")));
    for (name, why) in &failed {
        crate::log(&format!("Hotkey '{name}' not active: {why}"));
    }
    // Python put this on the global error line; the page under Settings is not enough.
    let current = ctx.engine.snapshot().note;
    if let Some(note) = next_note(&current, &failed) {
        ctx.engine.update(|s| s.note = note);
    }
    failed
}

const NOTE_PREFIX: &str = "Hotkey not active: ";

/// The error line after a registration round: the failures (in binding order),
/// or None to leave it alone. A line we wrote earlier is cleared once every
/// hotkey works; other messages are never touched.
pub fn next_note(current: &str, failed: &HashMap<String, String>) -> Option<String> {
    let mut why: Vec<&str> = BINDINGS.iter().filter_map(|b| failed.get(b.0).map(|s| s.as_str())).collect();
    why.dedup();
    if !why.is_empty() {
        return Some(format!("{NOTE_PREFIX}{}", why.join("; ")));
    }
    current.starts_with(NOTE_PREFIX).then(String::new)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_the_default_combos() {
        for (_, _, d) in BINDINGS {
            assert!(parse_combo(d).is_ok(), "{d}");
        }
        let (m, c) = parse_combo("Ctrl + Alt + N").unwrap();
        assert_eq!(m, Modifiers::CONTROL | Modifiers::ALT);
        assert_eq!(c, Code::KeyN);
    }

    #[test]
    fn hotkey_failures_reach_the_error_line() {
        let mut failed = HashMap::new();
        assert_eq!(next_note("", &failed), None);
        failed.insert("toggle".to_string(), "'ctrl+alt+s' is already in use by another program".to_string());
        failed.insert("skip".to_string(), "unknown key 'zz' in 'ctrl+zz'".to_string());
        // Binding order (skip before toggle), not map order.
        assert_eq!(
            next_note("", &failed).as_deref(),
            Some("Hotkey not active: unknown key 'zz' in 'ctrl+zz'; 'ctrl+alt+s' is already in use by another program")
        );
        // Fixed: our own line goes away, anyone else's stays.
        let none = HashMap::new();
        assert_eq!(next_note("Hotkey not active: x", &none).as_deref(), Some(""));
        assert_eq!(next_note("Port 8765 is in use", &none), None);
    }

    #[test]
    fn keys_and_aliases() {
        assert_eq!(parse_combo("win+f5").unwrap(), (Modifiers::SUPER, Code::F5));
        assert_eq!(parse_combo("shift+f24").unwrap().1, Code::F24);
        assert_eq!(parse_combo("ctrl+num3").unwrap().1, Code::Numpad3);
        assert_eq!(parse_combo("ctrl+numpad0").unwrap().1, Code::Numpad0);
        assert_eq!(parse_combo("ctrl+pgdn").unwrap().1, Code::PageDown);
        assert_eq!(parse_combo("ctrl+alt+]").unwrap().1, Code::BracketRight);
        assert_eq!(parse_combo("alt+playpause").unwrap().1, Code::MediaPlayPause);
        assert_eq!(parse_combo("control+7").unwrap().1, Code::Digit7);
        assert_eq!(parse_combo("super+ctrl+space").unwrap().0, Modifiers::SUPER | Modifiers::CONTROL);
    }

    #[test]
    fn rejects_bad_combos_with_readable_reasons() {
        assert_eq!(parse_combo("").unwrap_err(), "empty hotkey");
        assert_eq!(parse_combo(" + ").unwrap_err(), "empty hotkey");
        assert!(parse_combo("ctrl+alt").unwrap_err().contains("exactly one non-modifier key"));
        assert!(parse_combo("ctrl+a+b").unwrap_err().contains("exactly one non-modifier key"));
        assert!(parse_combo("n").unwrap_err().contains("at least one of ctrl/alt/shift/win"));
        assert!(parse_combo("ctrl+f25").unwrap_err().contains("unknown key 'f25'"));
        assert!(parse_combo("ctrl+banana").unwrap_err().contains("unknown key 'banana'"));
    }
}
