// Pure helpers for the Stats page and the Wrapped card (no DOM, no Tauri), so
// they can be tested in plain node: see tests-ts/history.test.mjs.

export const HEAT_MAX_WEEKS = 53;
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

export const plural = (n: number, one: string, many = one + "s") => `${n} ${n === 1 ? one : many}`;

/** "1h 5m" / "42m". */
export function fmtHm(ms: number): string {
  const total = Math.floor(Math.max(0, ms || 0) / 60000);
  const h = Math.floor(total / 60);
  return h ? `${h}h ${total % 60}m` : `${total}m`;
}

/** Session tile: "1h 5m" or "4m 9s". */
export function fmtSession(secs: number): string {
  const s = Math.floor(Math.max(0, secs));
  const m = Math.floor(s / 60) % 60;
  const h = Math.floor(s / 3600);
  return h ? `${h}h ${m}m` : `${m}m ${s % 60}s`;
}

/** 0 for no plays, else 1..4 by share of the busiest day. */
export function heatLevel(n: number, most: number): number {
  if (n <= 0 || most <= 0) return 0;
  return Math.max(1, Math.min(4, Math.ceil((4 * n) / most)));
}

// ── Dates: "YYYY-MM-DD" strings, handled at local noon so DST never shifts a day ──
export function parseDay(s: string): Date {
  const [y, m, d] = s.slice(0, 10).split("-").map(Number);
  return new Date(y, (m || 1) - 1, d || 1, 12);
}
export function fmtDay(d: Date): string {
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}
export function addDays(d: Date, n: number): Date {
  const r = new Date(d.getTime());
  r.setDate(r.getDate() + n);
  return r;
}
/** Monday = 0 … Sunday = 6. */
export const weekday = (d: Date) => (d.getDay() + 6) % 7;

/** "12 plays · Mon 14 Sep". */
export function heatTip(day: Date, n: number): string {
  const what = n === 0 ? "No plays" : plural(n, "play");
  return `${what} · ${WEEKDAYS[day.getDay()]} ${day.getDate()} ${MONTHS[day.getMonth()]}`;
}

export type HeatCell = { date: string; n: number; level: number; col: number; row: number };
export type Heat = {
  weeks: number;
  start: string;
  cells: HeatCell[];
  months: { col: number; label: string }[];
  total: number;
  active: number;
};

/** How many week columns fit in `width` px. */
export function heatWeeks(width: number, labelW: number, step: number, gap: number): number {
  return Math.max(4, Math.min(HEAT_MAX_WEEKS, Math.floor((width - labelW + gap) / step)));
}

/**
 * The activity grid: `weeks` columns ending with the week of `today`, Monday
 * at the top. `measure(label)` is the pixel width of a month label.
 */
export function buildHeat(
  today: string,
  weeks: number,
  counts: Record<string, number>,
  step: number,
  gridWidth: number,
  measure: (s: string) => number,
): Heat {
  const t = parseDay(today);
  const start = addDays(t, -(weekday(t) + 7 * (weeks - 1)));
  const cells: HeatCell[] = [];
  let total = 0;
  let active = 0;
  let most = 0;
  for (let i = 0; i < 7 * weeks; i++) {
    const d = addDays(start, i);
    if (d > t) continue;
    const n = counts[fmtDay(d)] ?? 0;
    total += n;
    if (n) active++;
    most = Math.max(most, n);
    cells.push({ date: fmtDay(d), n, level: 0, col: Math.floor(i / 7), row: i % 7 });
  }
  for (const c of cells) c.level = heatLevel(c.n, most);

  // Month labels over the first week (column) that starts in each month; the
  // leading partial month only when it has room.
  const months: { col: number; label: string }[] = [];
  let free = 0;
  for (let c = 0; c < weeks; c++) {
    const d = addDays(start, 7 * c);
    const prev = addDays(d, -7);
    if (c && d.getMonth() === prev.getMonth()) continue;
    const x = c * step;
    const label = MONTHS[d.getMonth()];
    if (x < free || x + measure(label) > gridWidth) continue;
    if (c === 0 && [1, 2].some((k) => addDays(d, 7 * k).getMonth() !== d.getMonth())) continue;
    months.push({ col: c, label });
    free = x + measure(label) + 6;
  }
  return { weeks, start: fmtDay(start), cells, months, total, active };
}

// ── Wrapped colours ──
export function hexToRgb(c: string): [number, number, number] {
  const s = c.trim();
  const m = /^#?([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(s);
  if (m) {
    let h = m[1];
    if (h.length === 3) h = [...h].map((x) => x + x).join("");
    return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
  }
  const r = /rgba?\(\s*(\d+)[ ,]+(\d+)[ ,]+(\d+)/i.exec(s);
  if (r) return [Number(r[1]), Number(r[2]), Number(r[3])];
  return [30, 215, 96];
}

export function rgbToHls(r: number, g: number, b: number): [number, number, number] {
  r /= 255; g /= 255; b /= 255;
  const mx = Math.max(r, g, b), mn = Math.min(r, g, b);
  const l = (mx + mn) / 2;
  if (mx === mn) return [0, l, 0];
  const d = mx - mn;
  const s = l <= 0.5 ? d / (mx + mn) : d / (2 - mx - mn);
  const rc = (mx - r) / d, gc = (mx - g) / d, bc = (mx - b) / d;
  let h = r === mx ? bc - gc : g === mx ? 2 + rc - bc : 4 + gc - rc;
  h = (h / 6) % 1;
  if (h < 0) h += 1;
  return [h, l, s];
}

export function hlsToRgb(h: number, l: number, s: number): [number, number, number] {
  if (s === 0) return [Math.round(l * 255), Math.round(l * 255), Math.round(l * 255)];
  const m2 = l <= 0.5 ? l * (1 + s) : l + s - l * s;
  const m1 = 2 * l - m2;
  const v = (hue: number) => {
    hue %= 1;
    if (hue < 0) hue += 1;
    if (hue < 1 / 6) return m1 + (m2 - m1) * hue * 6;
    if (hue < 0.5) return m2;
    if (hue < 2 / 3) return m1 + (m2 - m1) * (2 / 3 - hue) * 6;
    return m1;
  };
  return [Math.round(v(h + 1 / 3) * 255), Math.round(v(h) * 255), Math.round(v(h - 1 / 3) * 255)];
}

/** Greedy word wrap on a measuring function; a single over-long word stays whole. */
export function wrapText(text: string, width: number, measure: (s: string) => number): string[] {
  const lines: string[] = [];
  let cur = "";
  for (const word of text.split(/\s+/).filter(Boolean)) {
    const next = cur ? `${cur} ${word}` : word;
    if (cur && measure(next) > width) {
      lines.push(cur);
      cur = word;
    } else cur = next;
  }
  if (cur) lines.push(cur);
  return lines.length ? lines : [""];
}

/** `text` cut with an ellipsis to fit `width`. */
export function ellipsize(text: string, width: number, measure: (s: string) => number): string {
  if (measure(text) <= width) return text;
  let lo = 0, hi = text.length;
  while (lo < hi) {
    const mid = Math.ceil((lo + hi) / 2);
    if (measure(text.slice(0, mid).trimEnd() + "…") <= width) lo = mid;
    else hi = mid - 1;
  }
  return lo ? text.slice(0, lo).trimEnd() + "…" : "…";
}
