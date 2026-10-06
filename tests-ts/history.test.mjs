// Tests for the pure Stats/Wrapped helpers. Run: node --test tests-ts/
// (node strips the TypeScript types itself, no build step).
import test from "node:test";
import assert from "node:assert/strict";
import {
  addDays, buildHeat, ellipsize, fmtDay, fmtHm, fmtSession, heatLevel, heatTip, heatWeeks, hexToRgb,
  hlsToRgb, parseDay, plural, rgbToHls, weekday, wrapText,
} from "../src/pages/stats_util.ts";

test("heat levels follow the share of the busiest day", () => {
  assert.deepEqual([0, 1, 3, 4, 7, 12].map((n) => heatLevel(n, 12)), [0, 1, 1, 2, 3, 4]);
  assert.equal(heatLevel(5, 0), 0);
});

test("heat tips", () => {
  const d = parseDay("2026-09-14");
  assert.equal(heatTip(d, 12), "12 plays · Mon 14 Sep");
  assert.equal(heatTip(d, 1), "1 play · Mon 14 Sep");
  assert.equal(heatTip(d, 0), "No plays · Mon 14 Sep");
});

test("durations", () => {
  assert.equal(fmtHm(0), "0m");
  assert.equal(fmtHm(65 * 60000), "1h 5m");
  assert.equal(fmtHm(42 * 60000 + 30000), "42m");
  assert.equal(fmtSession(249), "4m 9s");
  assert.equal(fmtSession(3900), "1h 5m");
  assert.equal(plural(1, "play"), "1 play");
  assert.equal(plural(0, "day"), "0 days");
});

test("days are timezone and DST safe", () => {
  const d = parseDay("2026-03-29"); // a DST change in much of Europe
  assert.equal(fmtDay(addDays(d, 1)), "2026-03-30");
  assert.equal(fmtDay(addDays(d, -1)), "2026-03-28");
  assert.equal(weekday(parseDay("2026-09-14")), 0); // Monday
  assert.equal(weekday(parseDay("2026-09-20")), 6); // Sunday
});

test("how many weeks fit", () => {
  assert.equal(heatWeeks(100, 34, 15, 3), 4); // never fewer than 4
  assert.equal(heatWeeks(5000, 34, 15, 3), 53); // never more than a year
  assert.equal(heatWeeks(34 + 15 * 20 - 3, 34, 15, 3), 20);
});

test("the activity grid ends on today's column, Monday on top", () => {
  const counts = { "2026-09-23": 4, "2026-09-22": 2, "2026-09-01": 1 };
  const h = buildHeat("2026-09-23", 8, counts, 15, 500, () => 20); // today is a Wednesday
  assert.equal(h.start, fmtDay(addDays(parseDay("2026-09-21"), -7 * 7))); // Monday of the first week
  const today = h.cells.at(-1);
  assert.deepEqual([today.date, today.col, today.row, today.n, today.level], ["2026-09-23", 7, 2, 4, 4]);
  assert.equal(h.cells.length, 7 * 7 + 3); // nothing drawn after today
  assert.equal(h.total, 7);
  assert.equal(h.active, 3);
  assert.equal(h.cells.find((c) => c.date === "2026-09-22").level, 2);
});

test("month labels sit on the first week of each month and never collide", () => {
  const h = buildHeat("2026-09-23", 20, {}, 15, 300, () => 24);
  const cols = h.months.map((m) => m.col);
  for (let i = 1; i < cols.length; i++) assert.ok((cols[i] - cols[i - 1]) * 15 >= 24 + 6);
  assert.ok(h.months.every((m) => m.col * 15 + 24 <= 300));
  assert.ok(h.months.some((m) => m.label === "Sep"));
});

test("colours: hex parsing and an hls round trip", () => {
  assert.deepEqual(hexToRgb("#1ed760"), [30, 215, 96]);
  assert.deepEqual(hexToRgb("fff"), [255, 255, 255]);
  assert.deepEqual(hexToRgb("rgb(1, 2, 3)"), [1, 2, 3]);
  assert.deepEqual(hexToRgb("nonsense"), [30, 215, 96]);
  for (const c of [[30, 215, 96], [200, 40, 90], [10, 10, 10], [255, 255, 255]]) {
    const [h, l, s] = rgbToHls(...c);
    const back = hlsToRgb(h, l, s);
    back.forEach((v, i) => assert.ok(Math.abs(v - c[i]) <= 1, `${c} -> ${back}`));
  }
});

test("text wrapping and ellipsis", () => {
  const m = (s) => s.length * 10;
  assert.deepEqual(wrapText("the quick brown fox", 100, m), ["the quick", "brown fox"]);
  assert.deepEqual(wrapText("supercalifragilistic", 50, m), ["supercalifragilistic"]);
  assert.deepEqual(wrapText("", 50, m), [""]);
  assert.equal(ellipsize("short", 100, m), "short");
  assert.equal(ellipsize("a very long title", 80, m), "a very…");
  assert.equal(ellipsize("abc", 5, m), "…");
});
