// Desktop lyrics overlay (owner: windows agent). Loaded by ../overlay.html in a
// transparent, borderless, always-on-top window created by features/windows.rs
// (click-through while locked; draggable and resizable while unlocked).
// Ported from statusify_ui_overlay.py: the current synced line with a shadow,
// the next line dimmed, instrumental gaps as "♪ • • •", word-by-word fill for
// lines with timing, a slide between lines, fade in/out, and hiding 5 s after
// a pause.
import { getCurrentWindow } from "@tauri-apps/api/window";
import { call, onSnapshot, position, snapshot, type Snapshot } from "./api";
import {
  lyricOffset,
  msToNextEvent,
  overlayWanted,
  pickView,
  readableAccent,
  rgb,
  sungChars,
  PAUSE_HIDE_MS,
  type L,
  type Syl,
  type View,
} from "./win_common";

const SLIDE_MS = 300;
const IDLE_MS = 400;
const HINT: View = { kind: "hint", idx: -2, cur: "Statusify lyrics", next: "Drag to move · scroll to resize", syl: null };

const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;
const root = $("root");
const stage = $("stage");

type Scene = { el: HTMLElement; view: View; chars: HTMLElement[]; syl: Syl[] | null; lead: number; end: number; lit: number };

let scene: Scene | null = null;
let sceneKey = "";
let pausedAt: number | null = null;
let timer = 0;
let raf = 0;
let unlocked = false;
let anim = true;

// ── Scene building ───────────────────────────────────────────────────

function div(cls: string, text?: string): HTMLElement {
  const e = document.createElement("div");
  e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

/** Shrink the line until it fits in two rows (down to 62 %), then clamp it. */
function fit(txt: HTMLElement, px: number) {
  let p = px;
  txt.style.fontSize = `${p}px`;
  const rows = () => Math.round(txt.getBoundingClientRect().height / (p * 1.3));
  while (rows() > 2 && p > px * 0.62) {
    p = Math.floor(p * 0.9);
    txt.style.fontSize = `${p}px`;
  }
  if (rows() > 2) txt.classList.add("clamp");
}

function build(view: View, showNext: boolean): Scene {
  const el = div("scene" + (view.kind === "gap" ? " gap" : ""));
  const cur = div("cur");
  const txt = div("txt");
  cur.append(txt);
  el.append(cur);
  let chars: HTMLElement[] = [];
  let syl: Syl[] | null = null;
  let lead = 0;
  let end = 0;
  if (view.kind === "gap") {
    ["♪", "•", "•", "•"].forEach((t, i) => {
      const s = document.createElement("span");
      s.textContent = t;
      s.className = i === 0 || i <= view.dots ? "on" : "off";
      txt.append(s);
    });
  } else if (view.syl) {
    syl = view.syl;
    const raw = syl.map((s) => s[2]).join("");
    lead = raw.length - raw.trimStart().length;
    end = syl[syl.length - 1][1];
    chars = Array.from(raw.trim()).map((c) => {
      const s = document.createElement("span");
      s.className = "k";
      s.textContent = c;
      txt.append(s);
      return s;
    });
  } else {
    txt.textContent = view.cur;
  }
  if (showNext) el.append(div("nxt", view.next || ""));
  return { el, view, chars, syl, lead, end, lit: -1 };
}

// ── Karaoke fill ─────────────────────────────────────────────────────

function fill(pos: number) {
  const sc = scene;
  if (!sc || !sc.syl) return;
  const n = sungChars(sc.syl, pos) - sc.lead;
  const whole = Math.max(0, Math.min(sc.chars.length, Math.floor(n)));
  const frac = n - Math.floor(n);
  const partIdx = frac > 0.001 && whole < sc.chars.length && n > 0 ? whole : -1;
  // Only touch spans whose state may have changed.
  const lo = Math.min(sc.lit < 0 ? 0 : sc.lit, whole);
  const hi = Math.max(sc.lit, whole);
  for (let i = Math.max(0, lo - 1); i <= Math.min(sc.chars.length - 1, hi); i++) {
    sc.chars[i].classList.toggle("on", i < whole);
    sc.chars[i].classList.remove("part");
  }
  sc.lit = whole;
  if (partIdx >= 0) {
    const c = sc.chars[partIdx];
    c.style.setProperty("--f", frac.toFixed(3));
    c.classList.add("part");
  }
}

function karaokeFrame() {
  raf = 0;
  const s = snapshot();
  if (!s || !scene?.syl || !s.is_playing) return;
  const pos = position(s) + lyricOffset(s.extras);
  fill(pos);
  if (pos < scene.end) raf = requestAnimationFrame(karaokeFrame);
}

// ── Frame step ───────────────────────────────────────────────────────

function present(next: Scene, slide: boolean, up: boolean, lhPx: number) {
  const old = scene;
  scene = next;
  stage.append(next.el);
  const txt = next.el.querySelector<HTMLElement>(".cur > .txt");
  const px = parseFloat(getComputedStyle(root).getPropertyValue("--px")) || 30;
  if (txt && next.view.kind !== "gap") fit(txt, px);
  if (!old) return;
  if (!slide) {
    old.el.remove();
    return;
  }
  const dist = Math.trunc(lhPx * 0.45) * (up ? 1 : -1);
  const opts: KeyframeAnimationOptions = { duration: SLIDE_MS, fill: "both", easing: "cubic-bezier(0.215, 0.61, 0.355, 1)" };
  const gone = () => old.el.remove();
  old.el.animate([{ transform: "translateY(0)", opacity: 1 }, { transform: `translateY(${-dist}px)`, opacity: 0 }], opts).finished.then(gone, gone);
  next.el.animate([{ transform: `translateY(${dist}px)`, opacity: 0 }, { transform: "translateY(0)", opacity: 1 }], opts);
}

function applyLayout(s: Snapshot) {
  const w = s.extras?.windows ?? {};
  const l = w.layout ?? {};
  const set = (k: string, v: unknown) => {
    if (typeof v === "number") root.style.setProperty(k, `${v}px`);
  };
  set("--px", l.px);
  set("--npx", l.npx);
  set("--pad", l.pad);
  set("--bar", l.bar);
  set("--gap", l.gap);
  set("--nlh", l.nlh);
  const acc = readableAccent(typeof w.accent === "string" ? w.accent : "#1db954");
  root.style.setProperty("--acc", rgb(acc));
  root.style.setProperty("--acc-edge", `rgba(${acc[0]}, ${acc[1]}, ${acc[2]}, 0.9)`);
}

function step(): number {
  const s = snapshot();
  if (!s) return IDLE_MS;
  const now = performance.now();
  const w = s.extras?.windows ?? {};
  unlocked = w.overlay_locked === false;
  anim = w.animations !== false;
  const showNext = w.overlay_next !== false;
  root.classList.toggle("unlocked", unlocked);
  root.classList.toggle("noanim", !anim);
  applyLayout(s);

  const pos = position(s) + lyricOffset(s.extras);
  const synced: L[] = s.lyrics.mode === "synced" ? (s.lyrics.synced as L[]) : [];
  let view: View | null = synced.length ? pickView(synced, pos, s.duration_ms || 0) : null;
  const playing = s.is_playing;
  if (playing) pausedAt = null;
  else if (pausedAt === null) pausedAt = now;
  if (!view && unlocked) view = HINT;
  const want = overlayWanted(unlocked, !!view, playing, pausedAt === null ? 0 : now - pausedAt);

  // Scene: rebuild when what is shown changes.
  if (view) {
    const key = [
      view.kind, view.idx, "cur" in view ? view.cur : "", view.next, "dots" in view ? view.dots : "", w.accent,
      JSON.stringify(w.layout), showNext, unlocked, s.track?.uri, synced.length,
    ].join("|");
    if (key !== sceneKey) {
      const oldIdx = scene ? scene.view.idx : null;
      sceneKey = key;
      const visible = parseFloat(getComputedStyle(root).opacity) > 0.05;
      const slide = anim && scene !== null && visible && oldIdx !== view.idx;
      const lh = (parseFloat(getComputedStyle(root).getPropertyValue("--px")) || 30) * 1.3;
      present(build(view, showNext), slide, oldIdx === null || view.idx >= oldIdx, lh);
      if (scene && playing) fill(pos);
    }
    if (scene?.syl && playing && !raf) raf = requestAnimationFrame(karaokeFrame);
  }

  // Whole-window fade.
  const opacity = (Number(w.overlay_opacity) || 100) / 100;
  root.style.opacity = want && scene ? String(opacity) : "0";

  if (!want) return IDLE_MS;
  let next = synced.length && playing ? msToNextEvent(synced, pos, view) : IDLE_MS;
  if (!playing && pausedAt !== null && !unlocked) next = Math.min(next, PAUSE_HIDE_MS - (now - pausedAt) + 5);
  return Math.max(4, Math.min(IDLE_MS, next));
}

function tick() {
  clearTimeout(timer);
  let delay = 1000;
  try {
    delay = step();
  } catch (e) {
    console.error("overlay frame failed", e);
  }
  timer = window.setTimeout(tick, delay);
}

onSnapshot(() => tick());
tick();

// ── Mouse (unlocked only; locked is click-through, handled by the backend) ──

const inButton = (t: EventTarget | null) => !!(t as HTMLElement | null)?.closest?.("button");

root.addEventListener("mousedown", (e) => {
  if (!unlocked || e.button !== 0 || inButton(e.target)) return;
  getCurrentWindow().startDragging().catch(() => {});
});
root.addEventListener("dblclick", (e) => {
  if (unlocked && !inButton(e.target)) void call("windows", "set_overlay_locked", { locked: true });
});
root.addEventListener(
  "wheel",
  (e) => {
    if (!unlocked) return;
    e.preventDefault();
    void call("windows", "nudge_overlay_size", { delta: e.deltaY < 0 ? 2 : -2 });
  },
  { passive: false },
);
$("lock").addEventListener("click", () => void call("windows", "set_overlay_locked", { locked: true }));
$("close").addEventListener("click", () => void call("windows", "set_overlay_enabled", { enabled: false }));
