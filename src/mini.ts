// Mini player pill (owner: windows agent). Loaded by ../mini.html inside a
// 440x68 transparent always-on-top window created by features/windows.rs.
// Ported from statusify_ui_mini.py: cover, track line, the current lyric that
// slides on each line change, a play/pause disc with prev/next sliding in on
// hover, dimming when the pointer has been away, drag to move (the backend
// snaps it to screen edges), double-click the cover to open the main window,
// right-click for a menu.
import { getCurrentWindow } from "@tauri-apps/api/window";
import { call, onSnapshot, player, position, snapshot, type Snapshot } from "./api";
import { luma, lyricOffset, miniLyric, mix, parseColor, rgb, type L } from "./win_common";

const IDLE_DELAY_MS = 1500; // pointer away this long -> dim
const LYRIC_ANIM_MS = 280;
const SLIDE_PX = 12;
const DRAG_PX = 4;

const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;
const pill = $("pill");
const bg = pill.querySelector<HTMLElement>(".bg")!;
const cover = $("cover");
const coverImg = cover.querySelector("img")!;
const trackEl = $("track");
const trackTitle = trackEl.querySelector("b")!;
const trackArtist = trackEl.querySelector("span")!;
const lyricEl = $("lyric");
const toggleBtn = $("toggle");

let lyric: string | null = null; // text currently shown
let art = "";
let shown = { title: "\u0000", artist: "\u0000" };
let awayTimer = 0;
let anim = true;
let timer = 0;

// ── Pointer ──────────────────────────────────────────────────────────

function scheduleAway() {
  clearTimeout(awayTimer);
  awayTimer = window.setTimeout(() => pill.classList.add("away"), IDLE_DELAY_MS);
}

pill.addEventListener("pointerenter", () => {
  clearTimeout(awayTimer);
  pill.classList.remove("away");
  pill.classList.add("hover");
});
pill.addEventListener("pointerleave", () => {
  // Mid-drag the pointer can briefly outrun the window; only the end of the
  // gesture counts as leaving (Python ignored Leave while dragging).
  if (down?.moved) return;
  pill.classList.remove("hover");
  scheduleAway();
});

type Hit = "toggle" | "prev" | "next" | null;
let down: { x: number; y: number; hit: Hit; moved: boolean } | null = null;

function hitOf(t: EventTarget | null): Hit {
  const b = (t as HTMLElement | null)?.closest?.(".ctl") as HTMLElement | null;
  return b ? (b.id as Hit) : null;
}

pill.addEventListener("pointerdown", (e) => {
  if (e.button !== 0) return;
  down = { x: e.screenX, y: e.screenY, hit: hitOf(e.target), moved: false };
});
pill.addEventListener("pointermove", (e) => {
  const d = down;
  if (!d || d.moved || !(e.buttons & 1)) return;
  const dx = e.screenX - d.x;
  const dy = e.screenY - d.y;
  if (dx * dx + dy * dy < DRAG_PX * DRAG_PX) return; // a click with a shaky hand, not a drag
  d.moved = true;
  // Native drag: smooth, and the backend sees the Moved events and settles it.
  getCurrentWindow().startDragging().catch(() => {});
});
pill.addEventListener("pointerup", (e) => {
  const d = down;
  down = null;
  if (!d || d.moved || e.button !== 0) return;
  if (d.hit && d.hit === hitOf(e.target)) void player(d.hit);
});
const endGesture = () => {
  const wasDrag = down?.moved;
  down = null;
  if (wasDrag && !pill.matches(":hover")) {
    pill.classList.remove("hover");
    scheduleAway();
  }
};
pill.addEventListener("pointercancel", endGesture);
window.addEventListener("blur", endGesture);
// startDragging eats the pointerup; the next pointer entry tells us it ended.
pill.addEventListener("pointerenter", () => (down = null));

pill.addEventListener("dblclick", (e) => {
  if ((e.target as HTMLElement).closest("#cover")) void call("windows", "show_main");
});
pill.addEventListener("contextmenu", (e) => {
  e.preventDefault();
  void call("windows", "mini_menu");
});

// ── Content ──────────────────────────────────────────────────────────

/** Show a new lyric; slides the old one out and the new one in. */
function setLyric(text: string) {
  const prev = lyric;
  lyric = text;
  const el = document.createElement("div");
  el.className = "ly";
  el.textContent = text;
  const old = Array.from(lyricEl.children) as HTMLElement[];
  lyricEl.append(el);
  if (prev === null || !anim) {
    old.forEach((o) => o.remove());
    return;
  }
  const opts: KeyframeAnimationOptions = { duration: LYRIC_ANIM_MS, fill: "both", easing: "cubic-bezier(0.215, 0.61, 0.355, 1)" };
  for (const o of old) {
    o.animate([{ transform: "translateY(0)", opacity: 1 }, { transform: `translateY(${-SLIDE_PX}px)`, opacity: 0 }], opts).finished.then(
      () => o.remove(),
      () => o.remove(),
    );
  }
  el.animate([{ transform: `translateY(${SLIDE_PX}px)`, opacity: 0 }, { transform: "translateY(0)", opacity: 1 }], opts);
}

function paint(s: Snapshot) {
  const w = s.extras?.windows ?? {};
  anim = w.animations !== false;
  const dark = w.dark !== false;
  document.documentElement.classList.toggle("light", !dark);

  // Colours: the user accent a little toward the foreground; album tint from
  // the palette (when the now-playing area provides one) behind the cover blur.
  const accent = parseColor(w.accent) ?? [29, 185, 84];
  const fg: [number, number, number] = dark ? [255, 255, 255] : [18, 20, 26];
  const acc = mix(accent, fg, 0.2);
  pill.style.setProperty("--accent", rgb(acc));
  pill.style.setProperty("--glyph", luma(acc) > 150 ? "rgb(18, 20, 26)" : "#fff");
  const pal = s.extras?.palette;
  const cols = (pal?.colors ?? pal?.tint ?? pal?.blobs) as unknown;
  const first = Array.isArray(cols) ? parseColor(cols[1] ?? cols[0]) : null;
  if (first && w.tint !== false) {
    pill.style.setProperty("--base", rgb(dark ? mix(first, [0, 0, 0], 0.55) : mix(first, [255, 255, 255], 0.75)));
  } else {
    pill.style.removeProperty("--base");
  }

  const t = s.track;
  const title = t?.title ?? "";
  const artist = t?.artist ?? "";
  if (title !== shown.title || artist !== shown.artist) {
    shown = { title, artist };
    trackTitle.textContent = title || "Statusify";
    trackArtist.textContent = title && artist ? `  ·  ${artist}` : "";
    trackEl.style.opacity = title ? "" : "0.9";
  }
  const a = t?.album_art ?? "";
  if (a !== art) {
    art = a;
    cover.classList.toggle("has", !!a);
    if (a) coverImg.src = a;
    else coverImg.removeAttribute("src");
    bg.style.backgroundImage = a ? `url("${a}")` : "";
    bg.classList.toggle("on", !!a);
  }
  toggleBtn.classList.toggle("playing", s.is_playing);

  const synced = (s.lyrics.synced ?? []) as L[];
  const text = miniLyric(title, s.lyrics.mode, synced, s.lyrics.plain ?? [], position(s) + lyricOffset(s.extras), s.duration_ms);
  if (text !== lyric) setLyric(text);
}

coverImg.addEventListener("error", () => cover.classList.remove("has"));

/** Follows the playhead for the lyric (snapshots arrive on bridge updates, but
 * the line changes between them). Slower when nothing is moving. */
function tick() {
  const s = snapshot();
  if (s) paint(s);
  timer = window.setTimeout(tick, s?.is_playing ? 120 : 400);
}

onSnapshot(paint);
coverImg.addEventListener("load", () => cover.classList.add("has"));
scheduleAway(); // unless the pointer arrives, dim after the idle delay
tick();
window.addEventListener("beforeunload", () => clearTimeout(timer));
