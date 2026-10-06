// Stub Spicetify for running the REAL src-tauri/resources/lyrics-bridge.js
// outside Spotify, so the JS hop of the pipeline can be measured. Used by the
// bench_js_* tests in src-tauri/src/bench_bridge.rs (they drive it over stdin).
//
//   node bridge_js_probe.mjs --port 8799 --bridge path/to/lyrics-bridge.js \
//        --scenario e2e|lyrics-hang [--hang-ms 30000] [--verbose]
//
// Nothing here touches Spotify or Spicetify: Spicetify, CosmosAsync and fetch
// are stubs, and the bridge's hard-coded ws://127.0.0.1:8765 is rewritten IN
// MEMORY to --port, so the live Statusify (port 8765) is never contacted.
//
// stdin commands (e2e): songchange <n> | pause | resume | seek <ms>
// The stub changes its state first and then fires the same Spicetify events
// the real spicetifyWrapper.js fires (songchange, onplaypause); there is no
// seek event in the real API either.
import fs from "node:fs";
import readline from "node:readline";
import vm from "node:vm";

const argv = process.argv.slice(2);
const opt = (name, dflt) => {
  const i = argv.indexOf("--" + name);
  return i >= 0 ? argv[i + 1] : dflt;
};
const port = Number(opt("port", "8799"));
const bridgePath = opt("bridge");
const scenario = opt("scenario", "e2e");
const hangMs = Number(opt("hang-ms", "30000"));
const verbose = argv.includes("--verbose");
if (!bridgePath) throw new Error("--bridge is required");
if (port === 8765) throw new Error("refusing to talk to port 8765 (the live Statusify)");

if (!verbose) {
  console.log = console.warn = console.info = () => {};
}

let src = fs.readFileSync(bridgePath, "utf8");
if (!src.includes("ws://127.0.0.1:8765")) throw new Error("bridge has no ws://127.0.0.1:8765 literal to rewrite");
src = src.replace("ws://127.0.0.1:8765", `ws://127.0.0.1:${port}`);

// ---- stub Spotify player ----
const DUR = 240000;
let playing = true;
let pos0 = 0;
let wall0 = performance.now();
const pos = () => (playing ? pos0 + (performance.now() - wall0) : pos0);
const uriOf = (n) => `spotify:track:bench${String(n).padStart(4, "0")}`;
const mkItem = (n) => ({
  uri: uriOf(n),
  name: `Bench Track ${n}`,
  metadata: { artist_name: "Bench Artist", title: `Bench Track ${n}`, duration: String(DUR), image_url: "spotify:image:benchimg", album_title: "Bench Album" },
  artists: [{ name: "Bench Artist" }],
});
const listeners = {};
const fire = (type) => {
  for (const f of listeners[type] || []) f({ type, data: Player.data });
};
const setPlaying = (v) => {
  pos0 = pos();
  wall0 = performance.now();
  playing = v;
};
const Player = {
  data: { item: mkItem(0) },
  getProgress: pos,
  isPlaying: () => playing,
  getVolume: () => 0.5,
  getRepeat: () => 0,
  getShuffle: () => false,
  getHeart: () => false,
  next() {}, back() {}, togglePlay() {},
  pause() { setPlaying(false); fire("onplaypause"); },
  play() { setPlaying(true); fire("onplaypause"); },
  seek(ms) { pos0 = ms; wall0 = performance.now(); },
  addEventListener(t, f) { (listeners[t] ||= []).push(f); },
};

// Spotify's color-lyrics answer: 12 LINE_SYNCED lines 30 s apart, "L000 ..." tokens.
const lyricsFor = () => ({
  lyrics: {
    syncType: "LINE_SYNCED",
    lines: Array.from({ length: 12 }, (_, k) => ({ startTimeMs: String(k * 30000), words: `L${String(k).padStart(3, "0")} stub line`, endTimeMs: "0" })),
  },
});

globalThis.Spicetify = {
  Player,
  Queue: { nextTracks: [] },
  Platform: { AuthorizationAPI: { _tokenProvider: { _token: { accessToken: "stub" } } }, PlayerAPI: {} },
  CosmosAsync: {
    get: (url) =>
      scenario === "lyrics-hang"
        ? new Promise((_, rej) => setTimeout(() => rej(new Error("Resolver not found!")), hangMs))
        : new Promise((res) => setTimeout(() => res(lyricsFor(url)), 100)),
  },
};
// Spicy's API answers 500 at once, so the bridge falls through to the stubbed Spotify path.
globalThis.fetch = async () => ({ status: 500, json: async () => ({}) });

vm.runInThisContext(src, { filename: "lyrics-bridge.js" });

if (scenario === "e2e") {
  const rl = readline.createInterface({ input: process.stdin });
  rl.on("line", (line) => {
    const [cmd, arg] = line.trim().split(/\s+/);
    if (cmd === "songchange") {
      const wasPaused = !playing;
      Player.data = { item: mkItem(Number(arg)) };
      pos0 = 0;
      wall0 = performance.now();
      playing = true;
      fire("songchange");
      if (wasPaused) fire("onplaypause");
    } else if (cmd === "pause") {
      Player.pause();
    } else if (cmd === "resume") {
      Player.play();
    } else if (cmd === "seek") {
      Player.seek(Number(arg));
    }
  });
  rl.on("close", () => process.exit(0));
}
