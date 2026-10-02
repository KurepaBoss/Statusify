// Pure helpers shared by the mini player and the overlay window (owner: windows
// agent). No imports and no DOM, so they run under `node --test tests/`.
// Ported from statusify_lyrics.select_line, statusify_ui_overlay.py and
// statusify_ui_mini.py.

export type L = { startMs: number; words: string; endMs?: number; syl?: unknown };

/** Lines that mean "nothing is being sung". */
export const PLACEHOLDER = new Set(["", "—", "— ", "-", "♪", "• • •", "…"]);
export const BLANK = new Set(["", "♪", "♫", "…", "...", "• • •"]);
export const GAP_MIN_MS = 3000; // a silence at least this long shows the dots

export function easeOut(p: number): number {
  p = Math.max(0, Math.min(1, p));
  return 1 - (1 - p) ** 3;
}

/** (current, next) lyric for `pos` (lyric offset ALREADY applied). Synced:
 * the last line whose startMs has been reached, next skips immediate repeats.
 * Plain: linear interpolation across the track. */
export function selectLine(mode: string, synced: L[], plain: string[], pos: number, duration: number): [string, string] {
  if (mode === "synced" && synced.length) {
    let idx = 0;
    let found = false;
    for (let i = 0; i < synced.length; i++) {
      if (synced[i].startMs <= pos) {
        idx = i;
        found = true;
      } else break;
    }
    if (!found || synced[idx].startMs > pos) return ["", ""];
    const cur = synced[idx].words;
    let nxt = "";
    for (let j = idx + 1; j < synced.length; j++) {
      if (synced[j].words !== cur) {
        nxt = synced[j].words;
        break;
      }
    }
    return [cur, nxt];
  }
  if (mode === "plain" && plain.length && duration > 0) {
    const ratio = Math.max(0, Math.min(pos / duration, 1));
    const i = Math.min(Math.floor(ratio * plain.length), plain.length - 1);
    return [plain[i], plain[Math.min(i + 1, plain.length - 1)]];
  }
  return ["", ""];
}

/** Effective lyric offset: core's per-track value, else the global delay. */
export function lyricOffset(extras: Record<string, any> | undefined): number {
  const c = extras?.core?.track_offset_ms;
  if (typeof c === "number") return c;
  const w = extras?.windows?.offset_ms;
  return typeof w === "number" ? w : -40;
}

/** What the mini player shows as the current lyric. */
export function miniLyric(
  title: string | undefined,
  mode: string,
  synced: L[],
  plain: string[],
  pos: number,
  duration: number,
): string {
  if (!title) return "Waiting for Spotify…";
  let cur = "";
  if ((mode === "synced" || mode === "plain") && (synced.length || plain.length)) {
    cur = selectLine(mode, synced, plain, pos, duration)[0].trim();
  }
  return PLACEHOLDER.has(cur) ? "♪" : cur;
}

/** Index of the line on screen at `pos`, or -1 before the first line. */
export function currentIndex(synced: L[], pos: number): number {
  let idx = -1;
  for (let i = 0; i < synced.length; i++) {
    if (synced[i].startMs <= pos) idx = i;
    else break;
  }
  return idx;
}

export type Syl = [number, number, string];

/** Word timing of a line as [start, end, text] on the track timeline, or null
 * when it has none. A list that clearly starts before its own line is read as
 * relative to the line start instead of shown wrong. */
export function lineSyl(line: L): Syl[] | null {
  const raw = line?.syl;
  if (!Array.isArray(raw) || !raw.length) return null;
  let out: Syl[] = [];
  try {
    for (const s of raw as any[]) {
      const a = Math.trunc(Number(s[0]));
      const b = Math.trunc(Number(s[1]));
      if (!Number.isFinite(a) || !Number.isFinite(b)) return null;
      out.push([a, Math.max(a, b), String(s[2])]);
    }
  } catch {
    return null;
  }
  if (!out.map((s) => s[2]).join("").trim()) return null;
  const st = Math.trunc(Number(line.startMs) || 0);
  if (st > 1000 && out[0][0] < st - 1000) out = out.map(([a, b, t]) => [a + st, b + st, t] as Syl);
  return out;
}

/** Characters of the syllables' text sung at `pos`, as a float. */
export function sungChars(syl: Syl[], pos: number): number {
  let n = 0;
  for (const [a, b, t] of syl) {
    if (pos >= b) n += t.length;
    else if (pos > a) {
      n += b > a ? (t.length * (pos - a)) / (b - a) : t.length;
      break;
    } else break;
  }
  return n;
}

export type View =
  | { kind: "line"; idx: number; cur: string; next: string; syl: Syl[] | null }
  | { kind: "gap"; idx: number; start: number; end: number; next: string; dots: number }
  | { kind: "hint"; idx: number; cur: string; next: string; syl: null };

const words = (l: L) => (l.words || "").trim();

/** What the overlay shows at `pos`, or null when there are no synced lines. */
export function pickView(synced: L[], pos: number, duration = 0): View | null {
  if (!synced.length) return null;
  const idx = currentIndex(synced, pos);
  const n = synced.length;
  const ni = idx + 1;
  const nxt = ni < n ? words(synced[ni]) : "";
  const end = ni < n ? synced[ni].startMs : duration || 0;

  let gapStart: number | null = null;
  if (idx < 0) gapStart = 0;
  else {
    const line = synced[idx];
    if (BLANK.has(words(line))) gapStart = line.startMs;
    else {
      const le = line.endMs != null && Number.isFinite(Number(line.endMs)) ? Math.trunc(Number(line.endMs)) : null;
      if (le !== null && pos >= le && end && end - le >= GAP_MIN_MS) gapStart = le;
    }
  }
  const next = BLANK.has(nxt) ? "" : nxt;
  if (gapStart !== null) {
    const span = Math.max(1, (end || pos + 1) - gapStart);
    const dots = Math.max(0, Math.min(3, Math.trunc((3 * (pos - gapStart)) / span)));
    return { kind: "gap", idx, start: gapStart, end, next, dots };
  }
  return { kind: "line", idx, cur: words(synced[idx]), next, syl: lineSyl(synced[idx]) };
}

/** Milliseconds until the overlay's picture can next change on its own. */
export function msToNextEvent(synced: L[], pos: number, view: View | null): number {
  let best = 10_000;
  for (const e of synced) {
    if (e.startMs > pos) {
      best = e.startMs - pos;
      break;
    }
  }
  if (view && view.kind === "gap" && view.end && view.dots < 3) {
    const span = Math.max(1, view.end - view.start);
    best = Math.min(best, view.start + (span * (view.dots + 1)) / 3 - pos);
  }
  if (view && view.kind === "line" && view.idx >= 0 && view.idx < synced.length) {
    const i = view.idx;
    const le = synced[i].endMs != null ? Math.trunc(Number(synced[i].endMs)) : NaN;
    const ns = i + 1 < synced.length ? synced[i + 1].startMs : null;
    if (Number.isFinite(le) && le > pos && ns && ns - le >= GAP_MIN_MS) best = Math.min(best, le - pos);
  }
  return Math.trunc(Math.max(4, best + 2));
}

/** The accent, lifted towards white until it reads on a dark shadow. */
export function readableAccent(hex: string): [number, number, number] {
  const c = (hex || "").replace(/^#/, "");
  let r: number, g: number, b: number;
  if (!/^[0-9a-fA-F]{6}$/.test(c)) return [29, 185, 84];
  r = parseInt(c.slice(0, 2), 16);
  g = parseInt(c.slice(2, 4), 16);
  b = parseInt(c.slice(4, 6), 16);
  for (let i = 0; i < 10; i++) {
    const lum = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255;
    if (lum >= 0.42) break;
    r = Math.trunc(r + (255 - r) * 0.2);
    g = Math.trunc(g + (255 - g) * 0.2);
    b = Math.trunc(b + (255 - b) * 0.2);
  }
  return [r, g, b];
}

export const rgb = (c: [number, number, number]) => `rgb(${c[0]}, ${c[1]}, ${c[2]})`;

/** "#rrggbb" or [r,g,b] (0-255) -> [r,g,b], else null. */
export function parseColor(v: unknown): [number, number, number] | null {
  if (Array.isArray(v) && v.length >= 3 && v.slice(0, 3).every((n) => Number.isFinite(Number(n)))) {
    return [Number(v[0]), Number(v[1]), Number(v[2])];
  }
  if (typeof v === "string" && /^#?[0-9a-fA-F]{6}$/.test(v.trim())) {
    const c = v.trim().replace(/^#/, "");
    return [parseInt(c.slice(0, 2), 16), parseInt(c.slice(2, 4), 16), parseInt(c.slice(4, 6), 16)];
  }
  return null;
}

export function mix(a: [number, number, number], b: [number, number, number], t: number): [number, number, number] {
  return [0, 1, 2].map((i) => Math.trunc(a[i] + (b[i] - a[i]) * t)) as [number, number, number];
}

/** Perceived brightness 0-255 (the mini play glyph flips on it). */
export function luma(c: [number, number, number]): number {
  return 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2];
}

/** How long (ms) a paused overlay stays before fading out. */
export const PAUSE_HIDE_MS = 5000;

/** Should the overlay be visible? Unlocked always; else only with a view that is
 * playing or was paused less than PAUSE_HIDE_MS ago. */
export function overlayWanted(unlocked: boolean, hasView: boolean, playing: boolean, pausedForMs: number): boolean {
  return unlocked || (hasView && (playing || pausedForMs < PAUSE_HIDE_MS));
}
