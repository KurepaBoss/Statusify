// Headless-Chromium profile of the Lyrics page (tests-ts/harness.html) under
// a scripted 20 s of playback with one track change in the middle. Prints one
// JSON object with, per run: main-thread work from the DevTools trace
// (style recalcs, layouts, paints, raster tasks and their time), frame
// intervals from a requestAnimationFrame probe, and the cost of one snapshot
// publish (what every bridge position message costs the page).
//
//   npx vite --port 1420 &              # the harness is served by the dev server
//   node tests/bench/ui_profile.mjs [--runs 3] [--seconds 20] [--out file.json]
//
// Needs the Playwright package (global in this environment) and a Chromium;
// PW_CHROMIUM points at the executable when Playwright cannot find its own.
import fs from "node:fs";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const argv = process.argv.slice(2);
const opt = (n, d) => { const i = argv.indexOf("--" + n); return i >= 0 ? argv[i + 1] : d; };
const RUNS = Number(opt("runs", "3"));
const SECONDS = Number(opt("seconds", "20"));
const OUT = opt("out", "");
const URL = opt("url", "http://localhost:1420/tests-ts/harness.html");

function loadPlaywright() {
  const roots = ["playwright", "/opt/node22/lib/node_modules/playwright", "/opt/node-tools/node_modules/playwright"];
  try {
    const g = require("node:child_process").execSync("npm root -g", { encoding: "utf8" }).trim();
    roots.push(g + "/playwright");
  } catch { /* no npm */ }
  for (const p of roots) {
    try { return require(p); } catch { /* next */ }
  }
  throw new Error("playwright not found; npm i -g playwright");
}

const r1 = (x) => Math.round(x * 10) / 10;
function stats(v) {
  if (!v.length) return { n: 0 };
  const s = [...v].sort((a, b) => a - b);
  const p = (q) => s[Math.min(s.length - 1, Math.max(0, Math.ceil(s.length * q) - 1))];
  return { n: s.length, min: r1(s[0]), p50: r1(p(0.5)), p95: r1(p(0.95)), max: r1(s[s.length - 1]), mean: r1(s.reduce((a, b) => a + b, 0) / s.length) };
}

async function metrics(cdp) {
  const { metrics } = await cdp.send("Performance.getMetrics");
  return Object.fromEntries(metrics.map((m) => [m.name, m.value]));
}

async function profileOnce(browser, run) {
  const page = await browser.newPage({ viewport: { width: 520, height: 720 }, deviceScaleFactor: 1 });
  await page.goto(URL, { waitUntil: "load" });
  await page.waitForFunction(() => !!window.__np && document.querySelector(".np .ln"), null, { timeout: 15000 });
  await page.waitForTimeout(1500);
  const cdp = await page.context().newCDPSession(page);
  await cdp.send("Performance.enable", { timeDomain: "timeTicks" });

  // The frame probe runs for the whole window.
  await page.evaluate((secs) => {
    const gaps = [];
    let last = performance.now();
    const until = last + secs * 1000;
    window.__frameGaps = gaps;
    const step = (t) => { gaps.push(t - last); last = t; if (t < until) requestAnimationFrame(step); };
    requestAnimationFrame(step);
  }, SECONDS);

  const m0 = await metrics(cdp);
  const t0 = Date.now();
  await cdp.send("Tracing.start", {
    categories: "devtools.timeline,disabled-by-default-devtools.timeline,blink,cc,viz",
    options: "sampling-frequency=0",
    transferMode: "ReportEvents",
  });
  const events = [];
  cdp.on("Tracing.dataCollected", (e) => events.push(...e.value));
  const done = new Promise((res) => cdp.once("Tracing.tracingComplete", res));

  // Scenario: play on; a track change half way through (new song, cover, palette, lyrics).
  await page.waitForTimeout((SECONDS * 1000) / 2);
  await page.evaluate(() => {
    const s = window.__snap;
    const mk = (n, t0, step) => Array.from({ length: n }, (_, i) => ({ startMs: t0 + i * step, words: `Second song line ${i} with a few more words in it` }));
    const synced = mk(28, 3000, 3900);
    window.__seekTo(0);
    window.__set({
      track: { uri: "spotify:track:harness2", artist: "Another Band", title: "Second Song", album: "B", album_art: window.__covers.b, blacklisted: false },
      lyrics: { mode: "synced", synced, plain: [], source: "Spicy" },
      duration_ms: 180000,
    });
    s.extras.palette = { uri: "spotify:track:harness2", art: window.__covers.b, accent: "#ff8c69", tint: "#c0392b", tokens: null, colors: ["#c0392b", "#4a0d2a"], base: "#3a0d14", blobs: ["#8a2a2a", "#6a1f30", "#9a3a3a", "#5a1a22", "#7a2a2a"], light: { accent: "#b03a2a", tokens: null, base: "#f4dcdc", blobs: ["#ebc", "#dab", "#ecc", "#dbb", "#ebb"] } };
    s.extras.np_timing = { uri: "spotify:track:harness2", n: synced.length, start0: 3000, startN: synced[synced.length - 1].startMs, lines: null };
    window.__emit();
  });
  await page.waitForTimeout((SECONDS * 1000) / 2);

  await cdp.send("Tracing.end");
  await done;
  const m1 = await metrics(cdp);
  const wall = (Date.now() - t0) / 1000;

  // Count main-thread work by trace event name.
  const want = ["UpdateLayoutTree", "Layout", "Paint", "RasterTask", "FunctionCall", "EventDispatch", "HitTest", "PrePaint", "UpdateLayerTree", "CompositeLayers", "Commit"];
  const by = {};
  for (const e of events) {
    if (!want.includes(e.name) || e.ph !== "X") continue;
    const b = (by[e.name] ||= { count: 0, ms: 0 });
    b.count++;
    b.ms += (e.dur || 0) / 1000;
  }
  for (const k of Object.keys(by)) { by[k].count_per_s = r1(by[k].count / wall); by[k].ms_per_s = r1(by[k].ms / wall); by[k].ms = r1(by[k].ms); }

  const gaps = await page.evaluate(() => window.__frameGaps.slice(1));
  const long = gaps.filter((g) => g > 25).length;

  // The cost of one snapshot publish, as the page pays it (JS + the forced layout it causes).
  const publish = await page.evaluate(() => {
    const N = 200;
    const t = performance.now();
    for (let i = 0; i < N; i++) window.__emit();
    return (performance.now() - t) / N;
  });
  const bytes = await page.evaluate(() => JSON.stringify(window.__snap).length);
  await page.close();
  const d = (k) => r1(((m1[k] || 0) - (m0[k] || 0)) / wall);
  return {
    run,
    window_s: r1(wall),
    per_second: {
      style_recalcs: d("RecalcStyleCount"),
      layouts: d("LayoutCount"),
      style_ms: r1(d("RecalcStyleDuration") * 1000),
      layout_ms: r1(d("LayoutDuration") * 1000),
      script_ms: r1(d("ScriptDuration") * 1000),
      task_ms: r1(d("TaskDuration") * 1000),
    },
    trace_per_second: by,
    frame_gap_ms: stats(gaps),
    frames_over_25ms: long,
    frames_over_25ms_pct: r1((100 * long) / Math.max(1, gaps.length)),
    snapshot_publish_ms: r1(publish * 100) / 100,
    snapshot_json_bytes: bytes,
    js_heap_mb: r1((m1.JSHeapUsedSize || 0) / 1048576),
  };
}

const { chromium } = loadPlaywright();
// Playwright's own Chromium when it has one; else the one at /opt/pw-browsers (this dev container) or $PW_CHROMIUM.
const exe = process.env.PW_CHROMIUM || (fs.existsSync("/opt/pw-browsers") ? fs.readdirSync("/opt/pw-browsers").filter((d) => /^chromium-\d+$/.test(d)).map((d) => `/opt/pw-browsers/${d}/chrome-linux/chrome`).find((p) => fs.existsSync(p)) : undefined);
const browser = await chromium.launch({ ...(exe ? { executablePath: exe } : {}), headless: true, args: ["--no-sandbox", "--disable-dev-shm-usage"] });
const runs = [];
for (let i = 0; i < RUNS; i++) runs.push(await profileOnce(browser, i + 1));
await browser.close();
const med = (f) => { const v = runs.map(f).sort((a, b) => a - b); return v[v.length >> 1]; };
const summary = {
  seconds: SECONDS,
  runs: RUNS,
  median: {
    frames_per_s: med((r) => r1(r.frame_gap_ms.n / r.window_s)),
    style_recalcs_per_s: med((r) => r.trace_per_second.UpdateLayoutTree?.count_per_s ?? 0),
    style_ms_per_s: med((r) => r.trace_per_second.UpdateLayoutTree?.ms_per_s ?? 0),
    style_ms_per_frame: med((r) => r1(((r.trace_per_second.UpdateLayoutTree?.ms ?? 0) / Math.max(1, r.frame_gap_ms.n)) * 100) / 100),
    layouts_per_s: med((r) => r.trace_per_second.Layout?.count_per_s ?? 0),
    script_ms_per_s: med((r) => r.trace_per_second.FunctionCall?.ms_per_s ?? 0),
    paints_per_s: med((r) => r.trace_per_second.Paint?.count_per_s ?? 0),
    raster_tasks_per_s: med((r) => r.trace_per_second.RasterTask?.count_per_s ?? 0),
    raster_ms_per_s: med((r) => r.trace_per_second.RasterTask?.ms_per_s ?? 0),
    frame_gap_p95_ms: med((r) => r.frame_gap_ms.p95),
    frames_over_25ms_pct: med((r) => r.frames_over_25ms_pct),
    snapshot_publish_ms: med((r) => r.snapshot_publish_ms),
    snapshot_json_bytes: med((r) => r.snapshot_json_bytes),
    js_heap_mb: med((r) => r.js_heap_mb),
  },
  runs_detail: runs,
};
const text = JSON.stringify(summary, null, 2);
console.log(text);
if (OUT) fs.writeFileSync(OUT, text);
