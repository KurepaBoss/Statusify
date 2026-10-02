// Pure logic of the Lyrics page, kept free of the DOM and of imports so it is
// testable on its own (tests-ts/now_logic.test.mjs, run with `node --test`).
// Ported from statusify_np_fx.py and statusify_ui_now_playing.py.

export const KARA_DIM = 0.5; // unsung words, relative to the sung ones
export const DOT_REST = 0.62; // an inactive break's dots, relative to the active size
export const DOTS_IN_S = 0.4; // dots grow in at a break's start ...
export const DOTS_OUT_MS = 450; // ... and shrink away this long before the next line
export const BREATHE_S = 2.4; // one breath of the active dots
export const GAP_MIN_MS = 4000; // silence between two lines that earns the dots
export const BEAT_MS = 180; // how long a beat's swell decays
export const BEAT_LIFT = 7; // peak brightness lift, levels of 255
export const COVER_S = 0.35; // cover crossfade
export const LINE_MOVE_S = 0.72; // how long a line change glides
export const LINE_STAGGER_S = 0.045; // each following line starts this much later
export const CHROME_IDLE_S = 2.5; // fullscreen: controls hide after this long still
export const CHROME_FADE_S = 0.3;
export const USER_SCROLL_HOLD_S = 3.5; // manual browsing lasts this long after the last wheel
export const SHEET_LYRIC_PT = 20; // lyric size in points, before the user's boost
export const PX_PER_PT = 96 / 72;

export type Syl = [number, number, string];
export type SyncedLine = { startMs: number; words: string; endMs?: number; syl?: Syl[] };
export type PlanRow = { kind: "intro" | "line" | "gap"; k: number; t0: number; t1: number | null };
export type Gap = { startMs: number; endMs: number; gap_ms: number; key: number };

export const clamp = (v: number, lo = 0, hi = 1) => (v < lo ? lo : v > hi ? hi : v);
/** Quartic ease-out: quick departure, long soft landing. */
export const easeOut = (t: number) => 1 - (1 - t) ** 4;
export const smooth = (t: number) => t * t * (3 - 2 * t);

export function fmtTime(ms: number): string {
  const s = Math.floor(Math.max(0, Number(ms) || 0) / 1000);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

/** Python's `float.hex`-free offset label: "+0.3s", "-1.0s", "0.0s". */
export function fmtOffset(ms: number): string {
  const s = ms / 1000;
  return `${s > 0 ? "+" : ""}${s.toFixed(1)}s`;
}

// ── Instrumental gaps (statusify_lyrics._calc_instrumental_gaps) ─

export function calcInstrumentalGaps(synced: { startMs: number }[], durationMs: number): Gap[] {
  if (!synced.length || durationMs <= 0) return [];
  const n = synced.length;
  const all: number[] = [];
  for (let i = 0; i < n - 1; i++) all.push(synced[i + 1].startMs - synced[i].startMs);
  if (synced[0].startMs > 0) all.push(synced[0].startMs);
  all.push(durationMs - synced[n - 1].startMs);
  const sorted = [...all].sort((a, b) => a - b);
  const mid = sorted.length >> 1;
  const median = sorted.length % 2 === 0 ? (sorted[mid - 1] + sorted[mid]) / 2 : sorted[mid];
  const MULT = 2.0, ABS = 8000;
  const gaps: Gap[] = [];
  const intro = synced[0].startMs;
  if (intro >= ABS && intro >= median * MULT) gaps.push({ startMs: 0, endMs: intro, gap_ms: intro, key: -2 });
  for (let i = 0; i < n - 1; i++) {
    const cur = synced[i], nxt = synced[i + 1];
    const midGap = nxt.startMs - cur.startMs;
    if (midGap < ABS || midGap < median * MULT) continue;
    const sungEnd = cur.startMs + Math.min(Math.trunc(median), midGap - 1000);
    if (nxt.startMs - sungEnd < 4000) continue;
    const gapStart = cur.startMs + 3000;
    if (gapStart >= nxt.startMs) continue;
    gaps.push({ startMs: gapStart, endMs: nxt.startMs, gap_ms: midGap, key: i });
  }
  const last = synced[n - 1].startMs;
  const outro = durationMs - last;
  if (outro >= ABS && outro >= median * MULT) {
    const gs = last + Math.min(Math.trunc(median), outro - 1000);
    if (gs < durationMs) gaps.push({ startMs: gs, endMs: durationMs, gap_ms: outro, key: -3 });
  }
  return gaps.sort((a, b) => a.startMs - b.startMs);
}

// ── The sheet plan: lines plus the breaks between them ──────────

/** Rows of the synced lyric sheet, in time order. Row 0 is always the intro
 *  (before the first line). A break is an empty lyric line, a silence of
 *  GAP_MIN_MS or more after a line whose end is known (endMs or its last
 *  syllable), or an instrumental gap worked out from the timings (keyed by the
 *  line before it). */
export function buildPlan(synced: SyncedLine[], gaps: Gap[] = [], durationMs = 0): PlanRow[] {
  const n = synced.length;
  const first = n ? synced[0].startMs : 0;
  const plan: PlanRow[] = [{ kind: "intro", k: -1, t0: -1e9, t1: first }];
  const byKey = new Map<number, Gap>();
  for (const g of gaps) if (Number.isFinite(g.key) && g.key >= 0) byKey.set(g.key, g);
  for (let k = 0; k < n; k++) {
    const e = synced[k];
    const start = e.startMs;
    const nxt = k + 1 < n ? synced[k + 1].startMs : null;
    const words = (e.words || "").trim();
    if (!words) {
      const end = nxt !== null ? nxt : Math.max(start, durationMs || start);
      plan.push({ kind: "gap", k, t0: start, t1: end });
      continue;
    }
    plan.push({ kind: "line", k, t0: start, t1: null });
    if (nxt === null || !(synced[k + 1].words || "").trim()) continue; // last line, or the next row is a break itself
    let end = e.endMs || 0;
    if (!end && e.syl && e.syl.length) {
      const v = Number(e.syl[e.syl.length - 1][1]);
      end = Number.isFinite(v) ? Math.trunc(v) : 0;
    }
    let g0: number | null = null;
    if (end) {
      if (nxt - end >= GAP_MIN_MS) g0 = end + 250;
    } else if (byKey.has(k)) {
      g0 = byKey.get(k)!.startMs;
    }
    if (g0 !== null && start < g0 && g0 < nxt - 800) plan.push({ kind: "gap", k, t0: Math.trunc(g0), t1: nxt });
  }
  return plan;
}

/** Row current at `pos` (ms, offset applied): the last row started. */
export function planIndex(t0s: number[], pos: number): number {
  let lo = 0, hi = t0s.length;
  while (lo < hi) {
    const m = (lo + hi) >> 1;
    if (t0s[m] <= pos) lo = m + 1; else hi = m;
  }
  return Math.max(0, lo - 1);
}

/** Plain lyrics have no timings: interpolate across the track, as the line sent to Discord. */
export function plainIndex(n: number, pos: number, dur: number): number {
  if (n <= 0) return 0;
  if (dur <= 0) return 0;
  return Math.min(Math.trunc(clamp(pos / dur) * n), n - 1);
}

// ── Karaoke ──────────────────────────────────────────────────────

export type Piece = { a: number; b: number; t0: number; t1: number };

/** Where each timed piece of `text` sits: [a, b) character offsets with its
 *  time span, in order, or null when the syllables do not match the text (the
 *  line then highlights whole). */
export function karaPieces(text: string, syl: Syl[] | undefined): Piece[] | null {
  if (!syl || !syl.length) return null;
  const out: Piece[] = [];
  let cursor = 0;
  for (const p of syl) {
    if (!Array.isArray(p) || p.length < 3) return null;
    const t0 = Math.trunc(Number(p[0])), t1 = Math.trunc(Number(p[1]));
    if (!Number.isFinite(t0) || !Number.isFinite(t1)) return null;
    const s = String(p[2]).trim();
    if (!s) continue;
    const a = text.indexOf(s, cursor);
    if (a < 0) return null;
    out.push({ a, b: a + s.length, t0, t1 });
    cursor = a + s.length;
  }
  return out.length ? out : null;
}

export const karaEdge = (px: number) => Math.max(8, Math.trunc(px * 0.55));

/** Fill front of one piece, px from the piece's left edge: bright left of it,
 *  a soft ramp `edge` px wide to its right, dim beyond. Not started: the ramp
 *  is entirely left of the piece; sung: past its right edge. */
export function karaFront(t: number, t0: number, t1: number, width: number, edge: number): number {
  if (t <= t0) return -edge;
  if (t >= t1) return width + 1;
  const f = clamp((t - t0) / Math.max(1, t1 - t0));
  return (width + edge) * f - edge;
}

/** True while the line's syllables are being sung (with a little lead and tail). */
export function karaBusy(pieces: Piece[] | null, t: number): boolean {
  return !!pieces && pieces.length > 0 && pieces[0].t0 - 400 <= t && t <= pieces[pieces.length - 1].t1 + 300;
}

// ── Instrumental dots ────────────────────────────────────────────

/** (scale, [three levels]) for a break row at `tMs`. */
export function dotsState(
  entry: PlanRow | { kind: string; t0: number; t1: number | null },
  tMs: number,
  nowS: number,
  active: boolean,
  playing: boolean,
  breathing: boolean,
): { scale: number; levels: [number, number, number] } {
  if (!active) return { scale: DOT_REST, levels: [0.8, 0.8, 0.8] };
  let t0 = entry.t0;
  const t1 = entry.t1 ?? entry.t0;
  if (entry.kind === "intro") t0 = 0;
  const span = Math.max(1, t1 - t0);
  const p = clamp((tMs - t0) / span);
  const grow = easeOut(clamp((tMs - t0) / (DOTS_IN_S * 1000)));
  const shrink = easeOut(clamp((t1 - tMs) / DOTS_OUT_MS));
  let breathe = 1;
  if (playing && breathing) breathe = 1 + 0.09 * Math.sin((2 * Math.PI * nowS) / BREATHE_S);
  const scale = (DOT_REST + (1 - DOT_REST) * grow) * breathe * shrink;
  const lv = (j: number) => 0.3 + 0.7 * clamp(p * 3 - j);
  return { scale, levels: [lv(0), lv(1), lv(2)] };
}

/** Is row `i` of the plan the one whose dots are alive right now. */
export function dotsActive(entry: PlanRow, i: number, focus: number, tMs: number): boolean {
  if (i !== focus) return false;
  if (entry.kind === "intro") return tMs < (entry.t1 ?? 0);
  return entry.kind === "gap";
}

// ── Beat swell ───────────────────────────────────────────────────

/** (beat index, lift 0..BEAT_LIFT) at `pos`. */
export function beatLevel(beats: number[], pos: number): { j: number; lift: number } {
  let lo = 0, hi = beats.length;
  while (lo < hi) {
    const m = (lo + hi) >> 1;
    if (beats[m] <= pos) lo = m + 1; else hi = m;
  }
  const j = lo - 1;
  if (j < 0) return { j: -1, lift: 0 };
  const dt = pos - beats[j];
  if (dt >= BEAT_MS) return { j, lift: 0 };
  const k = 1 - dt / BEAT_MS;
  return { j, lift: Math.round(BEAT_LIFT * k * k) };
}

// ── Cover drift (statusify_backdrop.Backdrop.drift) ──────────────

/** (scale, angle in degrees, tx, ty as a share of the cover) at time t; there
 *  and back over 2 * driftS, eased at both ends. */
export function drift(t: number, driftS = 60): { s: number; deg: number; tx: number; ty: number } {
  const p = 0.5 - 0.5 * Math.cos(Math.PI * (t / driftS));
  return { s: 1 + 0.18 * p, deg: 8 * p, tx: 0.04 * p, ty: -0.03 * p };
}

// ── Sub-lines ────────────────────────────────────────────────────

export type SubEntry = { rom?: string | null; tr?: string | null };

/** Text under lyric line `index`: "Both" joins the romanisation and the translation with " · ". */
export function subline(mode: string, e: SubEntry | undefined | null): string | null {
  if (!e) return null;
  const rom = mode === "rom" || mode === "both" ? e.rom : null;
  const tr = mode === "tr" || mode === "both" ? e.tr : null;
  const parts = [rom, tr].filter((p): p is string => !!p);
  return parts.length ? parts.join("  ·  ") : null;
}

// ── Lyric size ───────────────────────────────────────────────────

export const SIZE_PRESETS: [string, number][] = [["Small", -2], ["Default", 0], ["Large", 4], ["Huge", 8]];

/** Lyric font size in CSS px: the base 20 pt plus the user's boost, at least 12 pt,
 *  times the fullscreen multiplier. */
export function lyricPx(boost: number, fsScale = 1): number {
  return Math.trunc(Math.max(8, Math.round(Math.max(12, SHEET_LYRIC_PT + boost) * PX_PER_PT)) * fsScale);
}

/** Lyric size multiplier in fullscreen, by screen height. */
export function fsScale(h: number): number {
  return clamp((h / 760) * 1.6, 1.6, 3.2);
}

// ── Distance buckets of a row from the sung one ──────────────────

/** Opacity and blur (css px) of a row `d` rows from the focus (statusify's alpha table). */
export function rowLook(d: number): { alpha: number; blur: number } {
  const ad = Math.abs(d);
  if (ad === 0) return { alpha: 1, blur: 0 };
  const alpha = d > 0 ? Math.max(0.16, 0.42 - 0.07 * (ad - 1)) : Math.max(0.08, 0.34 - 0.1 * (ad - 1));
  return { alpha, blur: Math.min(3, ad * 1.15) * 1.6 };
}

// ── Seek bar ─────────────────────────────────────────────────────

export function seekFrac(x: number, left: number, width: number): number {
  return clamp((x - left) / Math.max(1, width));
}

// ── Share image text wrapping (measure injected) ─────────────────

/** Greedy word wrap of `text` into rows no wider than maxw; a word wider than
 *  maxw is split by characters. */
export function wrapText(text: string, maxw: number, measure: (s: string) => number): string[] {
  const rows: string[] = [];
  for (const para of text.split("\n")) {
    const words = para.split(/\s+/).filter(Boolean);
    let cur = "";
    const flush = () => { if (cur) { rows.push(cur); cur = ""; } };
    for (const w of words) {
      const trial = cur ? `${cur} ${w}` : w;
      if (measure(trial) <= maxw) { cur = trial; continue; }
      flush();
      if (measure(w) <= maxw) { cur = w; continue; }
      let piece = "";
      for (const ch of w) {
        if (piece && measure(piece + ch) > maxw) { rows.push(piece); piece = ""; }
        piece += ch;
      }
      cur = piece;
    }
    flush();
    if (!words.length && rows.length === 0) rows.push("");
  }
  return rows;
}

/** Longest prefix of `s` (with an ellipsis) that fits in maxw. */
export function ellipsize(s: string, maxw: number, measure: (s: string) => number): string {
  if (measure(s) <= maxw) return s;
  let lo = 0, hi = s.length;
  while (lo < hi) {
    const m = (lo + hi + 1) >> 1;
    if (measure(s.slice(0, m).trimEnd() + "…") <= maxw) lo = m; else hi = m - 1;
  }
  return s.slice(0, lo).trimEnd() + "…";
}

/** File-name-safe "Artist - Title". */
export function safeName(artist: string, title: string): string {
  const t = `${artist} - ${title}`.replace(/[\\/:*?"<>|]/g, "").replace(/^[ .-]+|[ .-]+$/g, "");
  return t.slice(0, 100) || "lyric";
}

// ── Adaptive quality (statusify: auto tiers) ─────────────────────

/** Tier 0 draws everything; tier 1 drops the beat swell and the breathing;
 *  tier 2 keeps the background still. `ema` is the frame interval in ms. */
export function tierFor(ema: number): 0 | 1 | 2 {
  return ema > 40 ? 2 : ema > 24 ? 1 : 0;
}

/** Brightness of the backdrop per page. */
export const LYRICS_BRIGHTNESS = 0.55;
export const PAGE_BRIGHTNESS = 0.24;

/** text colour over the water: white on dark, ink on light. */
export const fgRgb = (dark: boolean): [number, number, number] => (dark ? [255, 255, 255] : [18, 20, 26]);

/** Accent that stays visible on the water: pulled 20 % towards the text colour. */
export function mixAccent(accent: [number, number, number], fg: [number, number, number]): [number, number, number] {
  return [0, 1, 2].map((i) => Math.trunc(accent[i] * 0.8 + fg[i] * 0.2)) as [number, number, number];
}

export function parseHex(c: string): [number, number, number] | null {
  const m = /^#?([0-9a-f]{3}|[0-9a-f]{6})$/i.exec((c || "").trim());
  if (!m) return null;
  let h = m[1];
  if (h.length === 3) h = h.split("").map((x) => x + x).join("");
  return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
}

/** Which footer actions fit in `avail` px, keeping Mini before Overlay before On top before Copy;
 *  the rest go to the "..." menu. `widths` by key. */
export function fitActions(order: string[], widths: Record<string, number>, avail: number, moreW: number): { shown: string[]; overflow: string[] } {
  const total = order.reduce((s, k) => s + widths[k], 0);
  if (total <= avail) return { shown: [...order], overflow: [] };
  let room = avail - moreW;
  const keep = new Set<string>();
  for (const k of ["mini", "overlay", "top", "copy"]) {
    if (!order.includes(k)) continue;
    if (widths[k] > room) break;
    keep.add(k);
    room -= widths[k];
  }
  return { shown: order.filter((k) => keep.has(k)), overflow: ["mini", "overlay", "top", "copy"].filter((k) => order.includes(k) && !keep.has(k)) };
}
