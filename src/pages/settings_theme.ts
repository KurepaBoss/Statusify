/// <reference types="vite/client" />
// Settings state shared by every window, and the look it implies (owner: shell agent).
//
// initTheme() loads the settings once, listens for "settings-changed" (the
// backend emits it after any statusify.cfg change, from any window or the
// tray) and writes the result onto <html>:
//   data-theme="dark|light"        data-motion="on|off"      data-quality="auto|high|low"
//   --accent, --accent-fg          (user accent, or the cover's when "Colours from the album art" is on)
//   --lyric-font, --lyric-boost    (lyric font family, size bump in px)
// Pages can also call onPrefs(cb) / getPrefs() for the raw values.
import { listen } from "@tauri-apps/api/event";
import { call, onSnapshot } from "../api";
import "./settings.css";

export type Prefs = Record<string, any>;

let prefs: Prefs = {};
let palette: { accent?: string; tint?: string } | null = null;
const subs = new Set<(p: Prefs) => void>();
let started = false;

export const getPrefs = () => prefs;

export function onPrefs(cb: (p: Prefs) => void): () => void {
  subs.add(cb);
  if (Object.keys(prefs).length) cb(prefs);
  return () => subs.delete(cb);
}

export function isHex(c: unknown): c is string {
  return typeof c === "string" && /^#[0-9a-f]{6}$/i.test(c);
}

/** Black or white, whichever reads on `hex`. */
export function accentFg(hex: string): string {
  const n = parseInt(hex.slice(1), 16);
  const [r, g, b] = [(n >> 16) & 255, (n >> 8) & 255, n & 255].map((v) => {
    const c = v / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b > 0.4 ? "#0a0a0a" : "#ffffff";
}

/** The accent in force: the cover's when tinting, else the user's. */
export function effectiveAccent(p: Prefs, pal: { accent?: string } | null): string {
  if (p.album_tint && pal && isHex(pal.accent)) return pal.accent;
  return isHex(p.accent_color) ? p.accent_color : "#1db954";
}

function paint() {
  const root = document.documentElement;
  const p = prefs;
  if (!Object.keys(p).length) return;
  root.dataset.theme = p.dark_mode === false ? "light" : "dark";
  root.dataset.motion = p.animations === false ? "off" : "on";
  root.dataset.quality = String(p.render_quality || "auto");
  const accent = effectiveAccent(p, palette);
  root.style.setProperty("--accent", accent);
  root.style.setProperty("--accent-fg", accentFg(accent));
  if (palette && isHex(palette.tint)) root.style.setProperty("--tint", palette.tint);
  const font = String(p.lyric_font || "Segoe UI").replace(/["\\]/g, "");
  root.style.setProperty("--lyric-font", `"${font}", "Segoe UI", system-ui, sans-serif`);
  root.style.setProperty("--lyric-boost", `${Number(p.lyric_font_boost) || 0}px`);
}

/** Replace the known settings (or merge a patch) and repaint every subscriber. */
export function setPrefs(next: Prefs, merge = false) {
  prefs = merge ? { ...prefs, ...next } : { ...next };
  paint();
  subs.forEach((cb) => cb(prefs));
}

export async function refreshPrefs() {
  try {
    setPrefs(await call<Prefs>("settings", "get_all"));
  } catch {
    /* backend not ready yet; the next settings-changed event fills it in */
  }
}

export function initTheme() {
  if (started) return;
  started = true;
  void refreshPrefs();
  listen<Prefs>("settings-changed", (e) => setPrefs(e.payload));
  onSnapshot((s) => {
    const pal = s.extras?.palette ?? null;
    if (pal?.accent !== palette?.accent || pal?.tint !== palette?.tint) {
      palette = pal;
      paint();
      subs.forEach((cb) => cb(prefs));
    }
  });
}
