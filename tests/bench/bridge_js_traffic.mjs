// What the REAL src-tauri/resources/lyrics-bridge.js puts on the socket, and
// how fast it reports a seek, under a stub Spicetify with real timers. No
// Spotify, no Statusify, no network: WebSocket, Spicetify and fetch are stubs
// and the socket's sends are counted in memory.
//
//   node tests/bench/bridge_js_traffic.mjs [--seconds 20] [--seeks 12] [--no-update-events]
//        [--bridge path/to/lyrics-bridge.js] [--out file.json]
//
// Reports, per phase: messages and bytes per minute by type (playing, then
// paused), and the time from Spicetify.Player.seek() to the first position
// message that carries the new position. --no-update-events models a Spotify
// whose PlayerAPI does not emit "update" (the bridge must then notice the seek
// on its own heartbeat); the default models the real one, which does.
import fs from "node:fs";
import vm from "node:vm";

const argv = process.argv.slice(2);
const opt = (n, d) => { const i = argv.indexOf("--" + n); return i >= 0 ? argv[i + 1] : d; };
const SECONDS = Number(opt("seconds", "20"));
const SEEKS = Number(opt("seeks", "12"));
const OUT = opt("out", "");
const BRIDGE = opt("bridge", new URL("../../src-tauri/resources/lyrics-bridge.js", import.meta.url).pathname);
const updateEvents = !argv.includes("--no-update-events");

const src = fs.readFileSync(BRIDGE, "utf8");
const DUR = 240000;
let playing = true, pos0 = 0, wall0 = performance.now();
const pos = () => (playing ? pos0 + (performance.now() - wall0) : pos0);
const item = (n) => ({ uri: `spotify:track:traffic${n}`, name: `Track ${n}`, metadata: { artist_name: "Artist", title: `Track ${n}`, duration: String(DUR), image_url: "spotify:image:x", album_title: "Album" } });
const listeners = {}, apiListeners = {};
const fire = (type) => (listeners[type] || []).forEach((f) => f({ type, data: Player.data }));
const PlayerAPI = { _events: { addListener(t, f) { (apiListeners[t] ||= []).push(f); }, removeListener() {} } };
const fireUpdate = () => {
  if (!updateEvents) return;
  const data = { item: Player.data.item, is_paused: !playing, position_as_of_timestamp: pos0, timestamp: Date.now(), duration: DUR, playback_speed: playing ? 1 : 0 };
  (apiListeners.update || []).forEach((f) => f({ data }));
};
const setPlaying = (v) => { pos0 = pos(); wall0 = performance.now(); playing = v; };
const Player = {
  data: { item: item(1) },
  getProgress: pos, isPlaying: () => playing, getVolume: () => 0.5, getRepeat: () => 0, getShuffle: () => false, getHeart: () => false,
  next() {}, back() {}, togglePlay() {},
  pause() { setPlaying(false); fireUpdate(); fire("onplaypause"); },
  play() { setPlaying(true); fireUpdate(); fire("onplaypause"); },
  seek(ms) { pos0 = ms; wall0 = performance.now(); fireUpdate(); },
  addEventListener(t, f) { (listeners[t] ||= []).push(f); },
  origin: PlayerAPI,
};

const sent = []; // { at, type, bytes, msg }
class FakeWS {
  static OPEN = 1;
  constructor() { this.readyState = 0; FakeWS.last = this; setTimeout(() => { this.readyState = 1; this.onopen?.(); }, 20); }
  send(d) { sent.push({ at: performance.now(), type: JSON.parse(d).type, bytes: d.length, msg: JSON.parse(d) }); }
  close() { this.readyState = 3; }
}
globalThis.WebSocket = FakeWS;
globalThis.fetch = async () => ({ status: 500, json: async () => ({}) });
globalThis.Spicetify = {
  Player, Queue: { nextTracks: [] },
  Platform: { AuthorizationAPI: { _tokenProvider: { _token: { accessToken: "tok" } } }, PlayerAPI },
  CosmosAsync: { get: () => new Promise((res) => setTimeout(() => res({ lyrics: { syncType: "LINE_SYNCED", lines: Array.from({ length: 20 }, (_, k) => ({ startTimeMs: String(k * 10000), words: `line ${k}`, endTimeMs: "0" })) } }), 50)) },
};
console.log = console.warn = console.info = () => {};
vm.runInThisContext(src, { filename: "lyrics-bridge.js" });

const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const r1 = (x) => Math.round(x * 10) / 10;
const stats = (v) => {
  if (!v.length) return { n: 0 };
  const s = [...v].sort((a, b) => a - b);
  const p = (q) => s[Math.min(s.length - 1, Math.max(0, Math.ceil(s.length * q) - 1))];
  return { n: s.length, min: r1(s[0]), p50: r1(p(0.5)), p95: r1(p(0.95)), max: r1(s[s.length - 1]), mean: r1(s.reduce((a, b) => a + b, 0) / s.length) };
};
function window_(from, to) {
  const inWin = sent.filter((m) => m.at >= from && m.at < to);
  const mins = (to - from) / 60000;
  const by = {};
  for (const m of inWin) { const b = (by[m.type] ||= { n: 0, bytes: 0 }); b.n++; b.bytes += m.bytes; }
  const types = Object.fromEntries(Object.entries(by).map(([k, v]) => [k, { per_min: r1(v.n / mins), bytes_per_min: Math.round(v.bytes / mins) }]));
  return { seconds: r1((to - from) / 1000), messages_per_min: r1(inWin.length / mins), bytes_per_min: Math.round(inWin.reduce((a, m) => a + m.bytes, 0) / mins), by_type: types };
}

// Boot: the bridge waits ~1 s, connects, sends hello + track + lyrics.
await wait(1300);
await wait(1000);
if (!sent.some((m) => m.type === "lyrics")) throw new Error("the bridge did not send lyrics; stub mismatch");

const half = (SECONDS * 1000) / 2;
// Phase 1: playing, untouched.
const p1 = performance.now();
await wait(half);
const playingWin = window_(p1, performance.now());

// Phase 2: seeks while playing, spaced so each has room to be reported.
const seekMs = [];
for (let i = 0; i < SEEKS; i++) {
  await wait(700 + (i % 3) * 230);
  const target = 20000 + i * 5000;
  const t0 = performance.now();
  const n0 = sent.length;
  Player.seek(target);
  // Wait for the first position message that is at (or just after) the target.
  let got = null;
  while (performance.now() - t0 < 3000) {
    for (let k = n0; k < sent.length; k++) {
      const m = sent[k];
      if (m.type === "position" && m.msg.position_ms >= target && m.msg.position_ms < target + 1500) { got = m; break; }
    }
    if (got) break;
    await wait(1);
  }
  seekMs.push(got ? got.at - t0 : 3000);
}

// Phase 3: paused, untouched.
Player.pause();
await wait(600);
const p3 = performance.now();
await wait(half);
const pausedWin = window_(p3, performance.now());
Player.play();
await wait(300);

const out = {
  bridge: BRIDGE.split("/").slice(-1)[0],
  bridge_version: (src.match(/BRIDGE_VERSION\s*=\s*"([^"]+)"/) || [])[1] || null,
  spotify_update_events: updateEvents,
  playing: playingWin,
  paused: pausedWin,
  seek_to_position_report_ms: stats(seekMs),
};
const text = JSON.stringify(out, null, 2);
process.stdout.write(text + "\n");
if (OUT) fs.writeFileSync(OUT, text);
process.exit(0);
