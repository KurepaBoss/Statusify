// Run with: node --test tests/   (Node >= 22.6 strips the TypeScript types)
import test from "node:test";
import assert from "node:assert/strict";
import {
  selectLine, miniLyric, currentIndex, pickView, msToNextEvent, lineSyl, sungChars, readableAccent,
  overlayWanted, lyricOffset, parseColor, mix, luma,
} from "../src/win_common.ts";

const S = [
  { startMs: 1000, words: "one" },
  { startMs: 5000, words: "two" },
  { startMs: 9000, words: "two" },
  { startMs: 12000, words: "three" },
];

test("selectLine: nothing before the first line, skips immediate repeats for next", () => {
  assert.deepEqual(selectLine("synced", S, [], 500, 20000), ["", ""]);
  assert.deepEqual(selectLine("synced", S, [], 1000, 20000), ["one", "two"]);
  assert.deepEqual(selectLine("synced", S, [], 6000, 20000), ["two", "three"]);
  assert.deepEqual(selectLine("synced", S, [], 99999, 20000), ["three", ""]);
});

test("selectLine: plain lyrics interpolate across the track", () => {
  const p = ["a", "b", "c", "d"];
  assert.deepEqual(selectLine("plain", [], p, 0, 100000), ["a", "b"]);
  assert.deepEqual(selectLine("plain", [], p, 60000, 100000), ["c", "d"]);
  assert.deepEqual(selectLine("plain", [], p, 100000, 100000), ["d", "d"]);
  assert.deepEqual(selectLine("plain", [], p, 5, 0), ["", ""]);
  assert.deepEqual(selectLine("none", S, p, 5000, 1), ["", ""]);
});

test("miniLyric: waiting, placeholders become a note", () => {
  assert.equal(miniLyric("", "none", [], [], 0, 0), "Waiting for Spotify…");
  assert.equal(miniLyric("T", "none", [], [], 0, 0), "♪");
  assert.equal(miniLyric("T", "synced", S, [], 500, 20000), "♪");
  assert.equal(miniLyric("T", "synced", S, [], 1500, 20000), "one");
  assert.equal(miniLyric("T", "synced", [{ startMs: 0, words: "  ♪ " }], [], 10, 20000), "♪");
  assert.equal(miniLyric("T", "plain", [], ["x", "y"], 0, 100), "x");
});

test("currentIndex mirrors the lyric sheet's rule", () => {
  assert.equal(currentIndex(S, 0), -1);
  assert.equal(currentIndex(S, 1000), 0);
  assert.equal(currentIndex(S, 11999), 2);
});

test("pickView: line, leading gap, blank-line gap, ended-line gap with dots", () => {
  let v = pickView(S, 1500, 20000);
  assert.equal(v.kind, "line");
  assert.equal(v.cur, "one");
  assert.equal(v.next, "two");

  v = pickView(S, 200, 20000); // before the first line: the intro gap
  assert.equal(v.kind, "gap");
  assert.equal(v.dots, 0);
  v = pickView(S, 900, 20000);
  assert.equal(v.dots, 2); // 900/1000 of the way

  const B = [{ startMs: 0, words: "hi" }, { startMs: 3000, words: "♪" }, { startMs: 9000, words: "bye" }];
  v = pickView(B, 6000, 20000);
  assert.equal(v.kind, "gap");
  assert.equal(v.start, 3000);
  assert.equal(v.dots, 1);
  assert.equal(v.next, "bye");

  const E = [{ startMs: 0, words: "a", endMs: 2000 }, { startMs: 9000, words: "b" }];
  assert.equal(pickView(E, 1500, 20000).kind, "line");
  v = pickView(E, 4000, 20000); // line ended, 7 s of silence ahead
  assert.equal(v.kind, "gap");
  assert.equal(v.start, 2000);
  const short = [{ startMs: 0, words: "a", endMs: 2000 }, { startMs: 3500, words: "b" }];
  assert.equal(pickView(short, 2500, 20000).kind, "line"); // 1.5 s gap is not worth dots
  assert.equal(pickView([], 0, 0), null);
});

test("msToNextEvent: next line, next dot, line end", () => {
  const v = pickView(S, 1500, 20000);
  assert.equal(msToNextEvent(S, 1500, v), 3500 + 2);
  const B = [{ startMs: 0, words: "hi" }, { startMs: 3000, words: "♪" }, { startMs: 9000, words: "bye" }];
  const g = pickView(B, 4000, 20000); // dots=0: next dot at 3000+2000
  assert.equal(msToNextEvent(B, 4000, g), 1000 + 2);
  const E = [{ startMs: 0, words: "a", endMs: 2000 }, { startMs: 9000, words: "b" }];
  assert.equal(msToNextEvent(E, 1000, pickView(E, 1000, 20000)), 1000 + 2);
  assert.ok(msToNextEvent([], 0, null) >= 4);
});

test("word timing: absolute, relative and junk", () => {
  const abs = lineSyl({ startMs: 5000, words: "hi there", syl: [[5000, 5400, "hi "], [5400, 6000, "there"]] });
  assert.deepEqual(abs, [[5000, 5400, "hi "], [5400, 6000, "there"]]);
  const rel = lineSyl({ startMs: 5000, words: "hi", syl: [[0, 400, "hi"]] });
  assert.deepEqual(rel, [[5000, 5400, "hi"]]);
  assert.equal(lineSyl({ startMs: 0, words: "", syl: [[0, 10, "  "]] }), null);
  assert.equal(lineSyl({ startMs: 0, words: "x", syl: "nope" }), null);
  assert.equal(lineSyl({ startMs: 0, words: "x" }), null);
  assert.deepEqual(lineSyl({ startMs: 0, words: "x", syl: [[10, 5, "x"]] }), [[10, 10, "x"]]);
});

test("sungChars: whole pieces plus the fraction of the one being sung", () => {
  const syl = [[1000, 2000, "abcd"], [2000, 3000, "ef"]];
  assert.equal(sungChars(syl, 500), 0);
  assert.equal(sungChars(syl, 1500), 2);
  assert.equal(sungChars(syl, 2000), 4);
  assert.equal(sungChars(syl, 2500), 5);
  assert.equal(sungChars(syl, 9000), 6);
});

test("readableAccent lifts dark accents and leaves light ones alone", () => {
  assert.deepEqual(readableAccent("#ffffff"), [255, 255, 255]);
  assert.deepEqual(readableAccent("#1ed760"), [30, 215, 96]);
  const lifted = readableAccent("#101040");
  assert.ok(lifted[2] > 0x40 && lifted[0] > 0x10);
  assert.deepEqual(readableAccent("nonsense"), [29, 185, 84]);
});

test("overlayWanted: unlocked always, else playing or paused < 5 s", () => {
  assert.equal(overlayWanted(true, false, false, 99999), true);
  assert.equal(overlayWanted(false, false, true, 0), false);
  assert.equal(overlayWanted(false, true, true, 0), true);
  assert.equal(overlayWanted(false, true, false, 4000), true);
  assert.equal(overlayWanted(false, true, false, 5000), false);
});

test("lyricOffset prefers core's value, then the config delay, then -40", () => {
  assert.equal(lyricOffset({ core: { track_offset_ms: 250 }, windows: { offset_ms: 10 } }), 250);
  assert.equal(lyricOffset({ windows: { offset_ms: 10 } }), 10);
  assert.equal(lyricOffset({}), -40);
  assert.equal(lyricOffset(undefined), -40);
});

test("colour helpers", () => {
  assert.deepEqual(parseColor("#1db954"), [29, 185, 84]);
  assert.deepEqual(parseColor([1, 2, 3]), [1, 2, 3]);
  assert.equal(parseColor("red"), null);
  assert.deepEqual(mix([0, 0, 0], [100, 200, 50], 0.5), [50, 100, 25]);
  assert.ok(luma([255, 255, 255]) > 150 && luma([0, 0, 0]) < 1);
});
