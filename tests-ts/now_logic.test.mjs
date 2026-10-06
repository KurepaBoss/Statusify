// node --test tests-ts/   (Node 24 strips the TypeScript types itself)
import test from "node:test";
import assert from "node:assert/strict";
import * as L from "../src/pages/now_logic.ts";

const line = (startMs, words, extra = {}) => ({ startMs, words, ...extra });

test("plan starts with the intro and gives empty lines a break row", () => {
  const p = L.buildPlan([line(5000, "a"), line(9000, ""), line(12000, "b")], [], 20000);
  assert.deepEqual(p.map((r) => r.kind), ["intro", "line", "gap", "line"]);
  assert.equal(p[0].t1, 5000);
  assert.equal(p[2].t0, 9000);
  assert.equal(p[2].t1, 12000);
});

test("a long silence after a line with a known end earns a break", () => {
  const syn = [line(1000, "a", { endMs: 3000 }), line(20000, "b")];
  const p = L.buildPlan(syn, [], 30000);
  assert.deepEqual(p.map((r) => r.kind), ["intro", "line", "gap", "line"]);
  assert.equal(p[2].t0, 3250);
  assert.equal(p[2].t1, 20000);
  // too short a silence: no break
  const q = L.buildPlan([line(1000, "a", { endMs: 3000 }), line(6000, "b")], [], 30000);
  assert.equal(q.length, 3);
  // the end can come from the last syllable
  const r = L.buildPlan([line(1000, "a", { syl: [[1000, 2000, "a"]] }), line(20000, "b")], [], 30000);
  assert.equal(r[2].kind, "gap");
  assert.equal(r[2].t0, 2250);
});

test("an instrumental gap computed from line starts is used when no end is known", () => {
  const syn = [line(0, "a"), line(3000, "b"), line(6000, "c"), line(30000, "d"), line(33000, "e")];
  const gaps = L.calcInstrumentalGaps(syn, 40000);
  assert.ok(gaps.some((g) => g.key === 2 && g.startMs === 9000));
  const p = L.buildPlan(syn, gaps, 40000);
  const gapRow = p.find((r) => r.kind === "gap");
  assert.equal(gapRow.t0, 9000);
  assert.equal(gapRow.t1, 30000);
  // ordinary songs have none
  assert.deepEqual(L.calcInstrumentalGaps([line(0, "a"), line(3000, "b"), line(6000, "c")], 9000), []);
  assert.deepEqual(L.calcInstrumentalGaps([], 1000), []);
});

test("intro and outro gaps", () => {
  const syn = [20000, 23000, 26000, 29000, 32000, 35000].map((t) => line(t, "x"));
  const g = L.calcInstrumentalGaps(syn, 60000);
  assert.ok(g.some((x) => x.key === -2 && x.endMs === 20000));
  assert.ok(g.some((x) => x.key === -3 && x.endMs === 60000));
});

test("plan index is the last row started", () => {
  const t0s = [-1e9, 1000, 5000];
  assert.equal(L.planIndex(t0s, 0), 0);
  assert.equal(L.planIndex(t0s, 1000), 1);
  assert.equal(L.planIndex(t0s, 4999), 1);
  assert.equal(L.planIndex(t0s, 99999), 2);
});

test("plain lyrics interpolate across the track", () => {
  assert.equal(L.plainIndex(10, 0, 100000), 0);
  assert.equal(L.plainIndex(10, 50000, 100000), 5);
  assert.equal(L.plainIndex(10, 999999, 100000), 9);
  assert.equal(L.plainIndex(10, 1000, 0), 0);
  assert.equal(L.plainIndex(0, 1000, 1000), 0);
});

test("karaoke pieces follow the text and reject a mismatch", () => {
  const syl = [[1000, 1500, "Hel"], [1500, 2000, "lo"], [2000, 3000, "world"]];
  const p = L.karaPieces("Hello world", syl);
  assert.deepEqual(p.map((x) => [x.a, x.b]), [[0, 3], [3, 5], [6, 11]]);
  assert.equal(L.karaPieces("Goodbye", syl), null);
  assert.equal(L.karaPieces("Hello", undefined), null);
  assert.equal(L.karaPieces("x", [[0, 1, "  "]]), null);
});

test("karaoke front sweeps in across the piece", () => {
  const edge = 10;
  assert.equal(L.karaFront(0, 1000, 2000, 100, edge), -edge);
  assert.equal(L.karaFront(1000, 1000, 2000, 100, edge), -edge);
  assert.equal(L.karaFront(1500, 1000, 2000, 100, edge), (100 + edge) * 0.5 - edge);
  assert.ok(L.karaFront(2000, 1000, 2000, 100, edge) > 100);
  assert.ok(L.karaFront(9000, 1000, 2000, 100, edge) > 100);
  assert.equal(L.karaEdge(40), 22);
  assert.equal(L.karaEdge(10), 8);
});

test("karaoke busy window has a lead and a tail", () => {
  const p = [{ a: 0, b: 1, t0: 1000, t1: 2000 }];
  assert.equal(L.karaBusy(p, 500), false);
  assert.equal(L.karaBusy(p, 700), true);
  assert.equal(L.karaBusy(p, 2200), true);
  assert.equal(L.karaBusy(p, 2400), false);
  assert.equal(L.karaBusy(null, 0), false);
});

test("dots: inactive rest, active grow, shrink before the next line, fill levels", () => {
  const e = { kind: "gap", t0: 10000, t1: 30000 };
  const idle = L.dotsState(e, 0, 0, false, true, true);
  assert.equal(idle.scale, L.DOT_REST);
  const start = L.dotsState(e, 10000, 0, true, true, false);
  assert.ok(start.scale <= L.DOT_REST + 1e-9);
  const mid = L.dotsState(e, 20000, 0, true, true, false);
  assert.equal(mid.scale, 1);
  assert.ok(mid.levels[0] >= mid.levels[1] && mid.levels[1] >= mid.levels[2]);
  const end = L.dotsState(e, 30000, 0, true, true, false);
  assert.equal(end.scale, 0);
  const full = L.dotsState(e, 29999, 0, true, true, false);
  assert.ok(full.levels.every((l) => l > 0.99));
  // the intro counts from 0
  const intro = { kind: "intro", t0: -1e9, t1: 10000 };
  assert.ok(L.dotsState(intro, 5000, 0, true, true, false).levels[0] > 0.3);
  assert.equal(L.dotsActive(intro, 0, 0, 5000), true);
  assert.equal(L.dotsActive(intro, 0, 0, 12000), false);
  assert.equal(L.dotsActive({ kind: "line", t0: 0, t1: null }, 1, 1, 0), false);
  assert.equal(L.dotsActive(e, 2, 2, 15000), true);
  assert.equal(L.dotsActive(e, 2, 3, 15000), false);
  // breathing only while playing
  const b1 = L.dotsState(e, 20000, 0.6, true, true, true).scale;
  const b2 = L.dotsState(e, 20000, 0.6, true, false, true).scale;
  assert.ok(b1 > b2);
});

test("beat swell decays over 180 ms", () => {
  const beats = [1000, 2000];
  assert.equal(L.beatLevel(beats, 500).lift, 0);
  assert.equal(L.beatLevel(beats, 500).j, -1);
  assert.equal(L.beatLevel(beats, 1000).lift, L.BEAT_LIFT);
  assert.ok(L.beatLevel(beats, 1090).lift < L.BEAT_LIFT);
  assert.equal(L.beatLevel(beats, 1180).lift, 0);
  assert.equal(L.beatLevel(beats, 2050).j, 1);
});

test("cover drift goes there and back", () => {
  const a = L.drift(0), b = L.drift(60), c = L.drift(120);
  assert.equal(a.s, 1);
  assert.ok(Math.abs(b.s - 1.18) < 1e-9 && Math.abs(b.deg - 8) < 1e-9);
  assert.ok(Math.abs(c.s - 1) < 1e-9);
});

test("sub-lines follow the mode", () => {
  const e = { rom: "r", tr: "t" };
  assert.equal(L.subline("both", e), "r  ·  t");
  assert.equal(L.subline("rom", e), "r");
  assert.equal(L.subline("tr", e), "t");
  assert.equal(L.subline("off", e), null);
  assert.equal(L.subline("tr", { rom: "r", tr: null }), null);
  assert.equal(L.subline("both", undefined), null);
});

test("lyric size: preset boosts, minimum, fullscreen", () => {
  assert.equal(L.lyricPx(0), 27);
  assert.equal(L.lyricPx(-2), 24);
  assert.equal(L.lyricPx(-20), 16); // never under 12 pt
  assert.ok(L.lyricPx(8) > L.lyricPx(4));
  assert.equal(L.fsScale(300), 1.6);
  assert.equal(L.fsScale(9999), 3.2);
  assert.ok(L.lyricPx(0, L.fsScale(1080)) > 60);
});

test("row look: focus sharp, below brighter than above, blur capped", () => {
  assert.deepEqual(L.rowLook(0), { alpha: 1, blur: 0 });
  assert.ok(Math.abs(L.rowLook(1).alpha - 0.42) < 1e-9);
  assert.ok(Math.abs(L.rowLook(-1).alpha - 0.34) < 1e-9);
  assert.equal(L.rowLook(9).alpha, 0.16);
  assert.equal(L.rowLook(-9).alpha, 0.08);
  assert.ok(Math.abs(L.rowLook(5).blur - 4.8) < 1e-9);
  assert.ok(L.rowLook(1).blur < L.rowLook(2).blur);
});

test("formatting", () => {
  assert.equal(L.fmtTime(65000), "1:05");
  assert.equal(L.fmtTime(-5), "0:00");
  assert.equal(L.fmtTime(NaN), "0:00");
  assert.equal(L.fmtOffset(300), "+0.3s");
  assert.equal(L.fmtOffset(-1000), "-1.0s");
  assert.equal(L.fmtOffset(0), "0.0s");
  assert.equal(L.safeName("AC/DC", 'Back: In "Black"?'), "ACDC - Back In Black");
  assert.equal(L.safeName("", ""), "lyric");
});

test("wrapping and ellipsis use the injected measure", () => {
  const m = (s) => s.length * 10;
  assert.deepEqual(L.wrapText("aaa bbb ccc", 70, m), ["aaa bbb", "ccc"]);
  assert.deepEqual(L.wrapText("abcdefghij", 40, m), ["abcd", "efgh", "ij"]);
  assert.deepEqual(L.wrapText("one\ntwo", 500, m), ["one", "two"]);
  assert.deepEqual(L.wrapText("", 500, m), [""]);
  assert.equal(L.ellipsize("hello", 100, m), "hello");
  const e = L.ellipsize("hello world", 60, m);
  assert.ok(e.endsWith("…") && m(e) <= 60);
});

test("adaptive quality tiers", () => {
  assert.equal(L.tierFor(16.7), 0);
  assert.equal(L.tierFor(30), 1);
  assert.equal(L.tierFor(50), 2);
});

test("accent mixing and hex parsing", () => {
  assert.deepEqual(L.mixAccent([100, 200, 50], [255, 255, 255]), [131, 211, 91]);
  assert.deepEqual(L.parseHex("#1ed760"), [30, 215, 96]);
  assert.deepEqual(L.parseHex("fff"), [255, 255, 255]);
  assert.equal(L.parseHex("nope"), null);
  assert.deepEqual(L.fgRgb(false), [18, 20, 26]);
});

test("footer actions overflow into the menu, mini kept longest", () => {
  const w = { copy: 60, top: 70, overlay: 80, mini: 50 };
  const order = ["copy", "top", "overlay", "mini"];
  assert.deepEqual(L.fitActions(order, w, 400, 38), { shown: order, overflow: [] });
  const r = L.fitActions(order, w, 190, 38);
  assert.deepEqual(r.shown, ["overlay", "mini"]);
  assert.deepEqual(r.overflow, ["top", "copy"]);
  const tiny = L.fitActions(order, w, 40, 38);
  assert.deepEqual(tiny.shown, []);
  assert.deepEqual(tiny.overflow, ["mini", "overlay", "top", "copy"]);
});

test("seek fraction clamps", () => {
  assert.equal(L.seekFrac(50, 0, 100), 0.5);
  assert.equal(L.seekFrac(-5, 0, 100), 0);
  assert.equal(L.seekFrac(500, 0, 100), 1);
});

// ── Page switcher ──
const ORDER = ["now", "history", "stats", "settings"];

test("a slide brings the target in from its side of the tab order and sends the rest away", () => {
  assert.deepEqual(L.slidePlan({ now: 0 }, "stats", ORDER), { now: [0, -1], stats: [1, 0] });
  assert.deepEqual(L.slidePlan({ stats: 0 }, "history", ORDER), { stats: [0, 1], history: [-1, 0] });
});

test("a click mid-slide carries on like a strip instead of jumping", () => {
  // now -> history is half done; history -> settings picks up from there.
  const p = L.slidePlan({ now: -0.5, history: 0.5 }, "settings", ORDER);
  assert.deepEqual(p.now, [-0.5, -1]);
  assert.deepEqual(p.history, [0.5, -1]);
  assert.deepEqual(p.settings, [1.5, 0]); // beside the nearest page on its side
  // retargeting back to a page that is still on screen starts from where it is
  assert.deepEqual(L.slidePlan({ now: -0.5, history: 0.5 }, "now", ORDER), { history: [0.5, 1], now: [-0.5, 0] });
});

test("visibility is the share of a page on screen", () => {
  assert.equal(L.visibility(0), 1);
  assert.equal(L.visibility(-0.25), 0.75);
  assert.equal(L.visibility(1.4), 0);
});

test("a drag pulls the neighbour on its side; the ends have none", () => {
  assert.equal(L.dragNeighbour(ORDER, "history", -40), "stats");
  assert.equal(L.dragNeighbour(ORDER, "history", 40), "now");
  assert.equal(L.dragNeighbour(ORDER, "now", 40), null);
  assert.equal(L.dragNeighbour(ORDER, "settings", -40), null);
});

test("a drag switches past 60 px or when flicked the same way", () => {
  assert.equal(L.dragSwitches(-70, 0, true), true);
  assert.equal(L.dragSwitches(-30, 0, true), false);
  assert.equal(L.dragSwitches(-30, -1200, true), true);
  assert.equal(L.dragSwitches(-30, 1200, true), false); // thrown back
  assert.equal(L.dragSwitches(-200, -2000, false), false);
});

// ── Volume ──
test("wheel ticks before Spotify answers all count", () => {
  // The page keeps its own value, so ten ticks from 50 % reach 100 %, not 55 %.
  let v = 0.5;
  for (let i = 0; i < 10; i++) v = L.stepVolume(v, 1);
  assert.equal(v, 1);
  for (let i = 0; i < 30; i++) v = L.stepVolume(v, -1);
  assert.equal(v, 0);
  assert.equal(L.stepVolume(0.15, 1), 0.2); // no float drift off the 5 % grid
  assert.equal(L.stepVolume(0.7, 1), 0.75);
});

// ── Status row ──
test("the rate limit counts down and disappears", () => {
  assert.equal(L.rateLimitLabel(10_000, 0), "Rate limited \u00b7 10s");
  assert.equal(L.rateLimitLabel(10_000, 6_400), "Rate limited \u00b7 4s");
  assert.equal(L.rateLimitLabel(10_000, 10_000), "");
  assert.equal(L.rateLimitLabel(null, 5), "");
  assert.equal(L.rateLimitLabel(undefined, 5), "");
});

test("dropped lines and the rate limit share the right side", () => {
  assert.equal(L.statusRight(0, ""), "");
  assert.equal(L.statusRight(1, ""), "1 line dropped");
  assert.equal(L.statusRight(3, "Rate limited \u00b7 9s"), "3 lines dropped \u00b7 Rate limited \u00b7 9s");
  assert.equal(L.statusRight(0, "Rate limited \u00b7 9s"), "Rate limited \u00b7 9s");
});

test("a long save path keeps its end", () => {
  assert.equal(L.shortPath("C:\a\b.png"), "C:\a\b.png");
  const p = "C:\Users\Someone\Pictures\Statusify\A very long artist - A very long title (2).png";
  const s = L.shortPath(p);
  assert.equal(s.length, 52);
  assert.ok(s.startsWith("\u2026") && p.endsWith(s.slice(1)));
});

test("the Discord pill is green only when presence is on and Discord is connected", () => {
  // The bug: presence switched on (the default) but no Discord connected still said "On Discord".
  const noDiscord = L.rpcPill(true, null, true);
  assert.equal(noDiscord.state, "waiting");
  assert.notEqual(noDiscord.label, "On Discord");
  assert.equal(L.rpcPill(true, "", true).state, "waiting", "an empty user name is not a connection");
  assert.equal(L.rpcPill(true, undefined, true).state, "waiting");

  const on = L.rpcPill(true, "kurepa", true);
  assert.deepEqual([on.state, on.label], ["on", "On Discord"]);
  assert.ok(on.title.includes("kurepa"));

  // The user's own pause wins over everything, even a live connection.
  assert.deepEqual(L.rpcPill(false, "kurepa", true).state, "off");
  assert.equal(L.rpcPill(false, null, true).label, "Not sharing");
  assert.equal(L.rpcPill(false, null, false).label, "Not sharing");
});

test("the Discord pill says why it is waiting", () => {
  // First run: nothing to connect with yet.
  const first = L.rpcPill(true, null, false);
  assert.equal(first.state, "waiting");
  assert.equal(first.label, "No App ID");
  assert.ok(first.title.includes("Settings"));
  // An App ID but no Discord: the reason from the connection, if there is one.
  const why = L.rpcPill(true, null, true, "Discord isn't running");
  assert.equal(why.label, "Discord not connected");
  assert.ok(why.title.includes("Discord isn't running"));
  assert.ok(L.rpcPill(true, null, true, "  ").title.includes("Open Discord"));
  // Before the core feature has reported (rpcEnabled unknown) a live connection still reads as on.
  assert.equal(L.rpcPill(undefined, "kurepa", true).state, "on");
  assert.equal(L.rpcPill(undefined, null, undefined).state, "waiting");
});

test("the bridge warning reaches the footer, after the engine's own notes", () => {
  const warn = "Spotify is running but the lyrics bridge isn't connected — click to repair";
  const n = L.footerNotice("", "", warn);
  assert.deepEqual(n, { text: warn, fix: true });
  // A failed button press or a note outranks it (and is not a repair button).
  assert.deepEqual(L.footerNotice("Spotify isn't connected, so it can't be controlled from here", "", warn).fix, false);
  assert.equal(L.footerNotice("", "Port 8765 is in use — is the old Statusify still running?", warn).text.startsWith("Port 8765"), true);
  assert.equal(L.footerNotice("", "", "").text, "");
  assert.equal(L.footerNotice("", "", "").fix, false);
});

test("only the bridge's own messages are clickable, and not while a repair runs", () => {
  assert.equal(L.footerNotice("", "", "Lyrics bridge out of date in Spotify — click here to repair").fix, true);
  assert.equal(L.footerNotice("", "", "Repairing — follow the window that just opened (Spotify will restart)").fix, false);
  assert.equal(L.footerNotice("", "No Discord Application ID yet. Add one in Settings, under Discord, to show your status.", "").fix, false);
  assert.equal(L.footerNotice("", "Hotkey ctrl+alt+n is taken by another app", "").fix, false);
});
