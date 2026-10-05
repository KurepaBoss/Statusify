// Run with: node --test   (no directory argument: "node --test tests/" is an error on Node 24)
// The REAL src-tauri/resources/lyrics-bridge.js against stubs: a fake
// Spicetify player, a fake WebSocket and fake timers. No Spotify, no sockets,
// no network. BRIDGE_JS=<file> tests another version of the bridge (the
// reaction-time tests fail on versions before 2.2).
import test, { mock } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const SRC = fs.readFileSync(process.env.BRIDGE_JS || new URL("../src-tauri/resources/lyrics-bridge.js", import.meta.url), "utf8");
const flush = () => new Promise((r) => setImmediate(r));

const item = (n) => ({
  uri: `spotify:track:t${n}`,
  name: `Track ${n}`,
  metadata: { artist_name: "Artist", title: `Track ${n}`, duration: "200000", image_url: "spotify:image:img", album_title: "Album" },
  artists: [{ name: "Artist" }],
});

const LYRICS = {
  lyrics: { syncType: "LINE_SYNCED", lines: [{ startTimeMs: "0", words: "first line", endTimeMs: "0" }, { startTimeMs: "5000", words: "second line", endTimeMs: "0" }] },
};
const quickLyrics = () => new Promise((res) => setTimeout(() => res(LYRICS), 100));
const spicy500 = async () => ({ status: 500, json: async () => ({}) });

/** Start the bridge (it waits 1 s on fake time first) with the given stubs. */
async function boot(t, { cosmos = quickLyrics, fetchStub = spicy500, token = "tok" } = {}) {
  const saved = {};
  for (const k of ["WebSocket", "Spicetify", "fetch", "console"]) saved[k] = Object.getOwnPropertyDescriptor(globalThis, k);
  const savedTimeout = AbortSignal.timeout;
  const log = console.log;
  mock.timers.enable({ apis: ["setTimeout", "setInterval", "Date"], now: 1_000_000 });
  t.after(() => {
    mock.timers.reset();
    AbortSignal.timeout = savedTimeout;
    for (const [k, d] of Object.entries(saved)) d ? Object.defineProperty(globalThis, k, d) : delete globalThis[k];
    console.log = log;
  });

  const b = { sent: [], sockets: [], listeners: {}, playing: true, pos: 0, abortTimeouts: [] };
  b.advance = async (ms) => {
    for (let spent = 0; spent < ms; spent += 10) {
      mock.timers.tick(Math.min(10, ms - spent));
      await flush();
    }
    await flush();
  };
  class FakeWS {
    static OPEN = 1;
    constructor(url) {
      this.url = url;
      this.readyState = 0;
      b.sockets.push(this);
    }
    send(d) {
      b.sent.push(JSON.parse(d));
    }
    close() {
      this.readyState = 3;
    }
  }
  b.open = () => {
    const s = b.sockets.at(-1);
    s.readyState = 1;
    s.onopen().catch(() => {});
  };
  b.drop = () => {
    const s = b.sockets.at(-1);
    s.readyState = 3;
    s.onclose();
  };
  b.fire = (type) => (b.listeners[type] || []).forEach((f) => f({ type }));
  b.setTrack = (n) => {
    b.player.data = { item: item(n) };
    b.pos = 0;
  };
  b.of = (type) => b.sent.filter((m) => m.type === type);

  b.player = {
    data: { item: item(1) },
    getProgress: () => b.pos,
    isPlaying: () => b.playing,
    getVolume: () => 0.5,
    getRepeat: () => 0,
    getShuffle: () => false,
    getHeart: () => false,
    next() {},
    back() {},
    togglePlay() {},
    pause() {},
    play() {},
    seek() {},
    addEventListener: (type, f) => (b.listeners[type] ||= []).push(f),
  };
  AbortSignal.timeout = (ms) => {
    b.abortTimeouts.push(ms);
    const c = new AbortController();
    setTimeout(() => c.abort(new DOMException("timed out", "TimeoutError")), ms);
    return c.signal;
  };
  console.log = console.warn = console.info = () => {};
  globalThis.WebSocket = FakeWS;
  globalThis.fetch = fetchStub;
  globalThis.Spicetify = {
    Player: b.player,
    Queue: { nextTracks: [] },
    Platform: { AuthorizationAPI: { _tokenProvider: { _token: { accessToken: token } } }, PlayerAPI: {} },
    CosmosAsync: { get: cosmos },
  };
  vm.runInThisContext(SRC);
  await b.advance(1100);
  assert.equal(b.sockets.length, 1, "the bridge connects once started");
  return b;
}

/** Connect and let the first track's lyrics arrive. */
async function ready(t, opts) {
  const b = await boot(t, opts);
  b.open();
  await b.advance(500);
  assert.ok(b.of("hello").length, "hello sent");
  assert.ok(b.of("lyrics").length, "first track's lyrics sent");
  b.sent.length = 0;
  return b;
}

test("a song change is reported when Spotify announces it, not on the next poll", async (t) => {
  const b = await ready(t);
  b.setTrack(2);
  b.fire("songchange");
  await b.advance(0); // no time passes: the 500 ms poll has not run
  const tc = b.of("track_change");
  assert.equal(tc.length, 1);
  assert.equal(tc[0].track_uri, "spotify:track:t2");
  assert.equal(tc[0].title, "Track 2");
  // the heartbeat finds nothing more to report
  await b.advance(1200);
  assert.equal(b.of("track_change").length, 1);
});

test("pause and resume are reported when Spotify announces them", async (t) => {
  const b = await ready(t);
  b.playing = false;
  b.fire("onplaypause");
  await b.advance(0);
  assert.equal(b.of("paused").length, 1);
  assert.equal(b.of("position").at(-1).is_playing, false);

  b.sent.length = 0;
  b.playing = true;
  b.fire("onplaypause");
  await b.advance(0);
  assert.equal(b.of("position").at(-1).is_playing, true);
  assert.equal(b.of("paused").length, 0);
  assert.equal(b.of("track_change").length, 0, "resuming is not a new track");
});

test("a hung color-lyrics request is abandoned after 5 s and not retried", async (t) => {
  const b = await boot(t, { cosmos: () => new Promise(() => {}) });
  b.open();
  await b.advance(4800);
  assert.equal(b.of("track_change").length, 1);
  assert.equal(b.of("lyrics").length, 0, "still waiting");
  await b.advance(500);
  const l = b.of("lyrics");
  assert.equal(l.length, 1, "the verdict is in");
  assert.equal(l[0].mode, "none");
  await b.advance(20_000);
  assert.equal(b.of("lyrics").length, 1, "no retry of a request that hung");
  assert.ok(!b.of("lyrics_debug").some((m) => /retrying/.test(m.message)));
});

test("a timeout while there is no auth token is still retried", async (t) => {
  const b = await boot(t, { cosmos: () => new Promise(() => {}), token: null });
  b.open();
  await b.advance(6000);
  assert.ok(b.of("lyrics_debug").some((m) => /retrying/.test(m.message)), "second try announced");
  assert.equal(b.of("lyrics").length, 0);
  await b.advance(9000); // 5 s + 3 s + 5 s
  assert.equal(b.of("lyrics").length, 1);
  assert.equal(b.of("lyrics")[0].mode, "none");
});

test("a Spicy request that hangs is abandoned after 6 s and Spotify's lyrics are used", async (t) => {
  const hang = (url, init) => new Promise((_, rej) => init.signal.addEventListener("abort", () => rej(init.signal.reason)));
  const b = await boot(t, { fetchStub: hang });
  b.open();
  await b.advance(5800);
  assert.equal(b.of("lyrics").length, 0);
  assert.deepEqual(b.abortTimeouts, [6000]);
  await b.advance(500);
  const l = b.of("lyrics");
  assert.equal(l.length, 1);
  assert.equal(l[0].mode, "synced");
  assert.match(l[0].source, /Spotify/);
});

test("a fast 'Resolver not found' is still waited out while Spotify starts up", async (t) => {
  let calls = 0;
  const cosmos = () => (++calls < 3 ? Promise.reject(new Error("Resolver not found!")) : quickLyrics());
  const b = await boot(t, { cosmos });
  b.open();
  await b.advance(5000);
  assert.equal(calls, 3);
  assert.equal(b.of("lyrics")[0].mode, "synced");
  assert.ok(b.of("lyrics_debug").some((m) => /still starting up/.test(m.message)));
});

test("after a dropped socket the bridge reconnects after 250 ms, backs off to 3 s, and starts over once connected", async (t) => {
  const b = await boot(t);
  for (const delay of [250, 500, 1000, 2000, 3000, 3000]) {
    const n = b.sockets.length;
    b.drop();
    await b.advance(delay - 20);
    assert.equal(b.sockets.length, n, `not yet after ${delay - 20} ms`);
    await b.advance(40);
    assert.equal(b.sockets.length, n + 1, `reconnected after ${delay} ms`);
  }
  b.open();
  await b.advance(100);
  const n = b.sockets.length;
  b.drop();
  await b.advance(230);
  assert.equal(b.sockets.length, n);
  await b.advance(40);
  assert.equal(b.sockets.length, n + 1, "a connection that opened starts the delays over");
});

// The order of things in the real Spicetify, read (not executed) in the copy
// installed under %APPDATA%: Apps/xpui/helper/spicetifyWrapper.js adds a
// listener for Spotify's player "update" event to Spicetify.Player.origin
// (= Platform.PlayerAPI). The PlayerCore behind it registered its own listener
// first, in its constructor (_events.addListener(UPDATE, ({data}) => this._state
// = data)), so by the time the wrapper's listener runs the state that
// isPlaying() and getProgress() read (origin._state) is already the new one.
// The wrapper then sets Player.data and dispatches, synchronously and in this
// order: songchange if the item's uri changed, then onplaypause if isPaused
// changed (Player.data is null when the new state has no item). The stub above
// is built the same way: state first, then the events. Not exercised against
// a live Spotify (that needs spicetify apply and a restart).
test("a song picked while paused fires songchange then onplaypause, and is one track_change, playing", async (t) => {
  const b = await ready(t);
  b.playing = false;
  b.fire("onplaypause");
  await b.advance(0);
  assert.equal(b.of("paused").length, 1);
  b.sent.length = 0;

  // one Spotify update: the new item and isPaused false together
  b.setTrack(2);
  b.playing = true;
  b.fire("songchange");
  b.fire("onplaypause");
  await b.advance(0);
  const tc = b.of("track_change");
  assert.equal(tc.length, 1);
  assert.equal(tc[0].track_uri, "spotify:track:t2");
  assert.ok(b.of("position").length >= 1 && b.of("position").every((p) => p.is_playing === true));
  assert.equal(b.of("paused").length, 0, "never reported as paused");
});

test("a listener that throws cannot be left to the dispatcher: events never produce an unhandled rejection", async (t) => {
  // The real dispatchEvent calls listeners without a try/catch, and tick()
  // is async: a rejection inside it must not escape as an unhandled one.
  const b = await ready(t);
  let unhandled = 0;
  const on = () => unhandled++;
  process.on("unhandledRejection", on);
  t.after(() => process.off("unhandledRejection", on));
  b.player.getProgress = () => {
    throw new Error("player is not ready");
  };
  b.fire("songchange");
  b.fire("onplaypause");
  await b.advance(0);
  await new Promise((r) => setImmediate(r));
  assert.equal(unhandled, 0);
});

/** A Spicy request that never answers until it is aborted; records each signal. */
function hangingSpicy(signals) {
  return (url, init) => {
    signals.push(init.signal);
    return new Promise((_, rej) => init.signal.addEventListener("abort", () => rej(init.signal.reason)));
  };
}

test("a skip burst abandons the lyric requests of every skipped track", async (t) => {
  const signals = [];
  const cosmosUrls = [];
  const cosmos = (url) => {
    cosmosUrls.push(url);
    return quickLyrics();
  };
  const b = await boot(t, { fetchStub: hangingSpicy(signals), cosmos });
  b.open();
  await b.advance(200);
  for (let n = 2; n <= 6; n++) {
    b.setTrack(n);
    b.fire("songchange");
    await b.advance(100);
  }
  assert.equal(b.of("track_change").length, 6, "every change is still reported at once");
  assert.equal(signals.length, 6, "each track started its request");
  assert.deepEqual(signals.map((s) => s.aborted), [true, true, true, true, true, false], "only the playing track's request is still wanted");
  // the playing track's Spicy request times out; Spotify answers for it
  await b.advance(7000);
  const l = b.of("lyrics");
  assert.equal(l.length, 1, "no lyrics message for a song that is not playing");
  assert.equal(l[0].track_uri, "spotify:track:t6");
  assert.equal(cosmosUrls.length, 1, "and no Spotify fallback for the skipped ones");
  assert.match(cosmosUrls[0], /\/t6\?/);
  assert.ok(!b.of("lyrics_debug").some((m) => /Track [1-5]\b/.test(m.message)), "nothing is said about the skipped tracks");
});

test("a skipped track's hanging color-lyrics request is dropped, not waited out", async (t) => {
  // Spicy has no lyrics, Spotify's endpoint hangs: without cancelling, every
  // skipped track still delivers its own 'none' verdict after 5 s.
  const b = await boot(t, { cosmos: () => new Promise(() => {}) });
  b.open();
  await b.advance(200);
  for (let n = 2; n <= 6; n++) {
    b.setTrack(n);
    b.fire("songchange");
    await b.advance(100);
  }
  await b.advance(6000);
  const l = b.of("lyrics");
  assert.equal(l.length, 1);
  assert.equal(l[0].track_uri, "spotify:track:t6");
  assert.equal(l[0].mode, "none");
});

test("skipping on and coming back to a track fetches it again", async (t) => {
  const signals = [];
  const b = await boot(t, { fetchStub: hangingSpicy(signals) });
  b.open();
  await b.advance(200);
  b.setTrack(2);
  b.fire("songchange");
  await b.advance(100);
  b.setTrack(1);
  b.fire("songchange");
  await b.advance(7000);
  const l = b.of("lyrics");
  assert.deepEqual(l.map((m) => m.track_uri), ["spotify:track:t1"], "the track that is playing gets its lyrics, once");
  assert.equal(l[0].mode, "synced");
  assert.equal(signals.length, 3, "track 1, track 2, track 1 again");
});

test("when the track is gone (queue ended, an ad break) its lyric request is abandoned at once", async (t) => {
  // Spicetify's wrapper sets Player.data to null when the new state has no
  // item, and still fires songchange: tick() has nothing to start, so only the
  // listener can cancel the request that is still running.
  const signals = [];
  const b = await boot(t, { fetchStub: hangingSpicy(signals) });
  b.open();
  await b.advance(200);
  assert.equal(signals.length, 1);
  assert.equal(signals[0].aborted, false);
  b.player.data = null;
  b.fire("songchange");
  await b.advance(0);
  assert.equal(signals[0].aborted, true);
  await b.advance(7000);
  assert.equal(b.of("lyrics").length, 0, "nothing is sent for a track that is no longer there");
});

test("a track change that only the 500 ms poll notices also abandons the request of the track before", async (t) => {
  // No songchange here (a missed event): the poll starts the new track's fetch,
  // and that start must be what cancels the old one.
  const signals = [];
  const b = await boot(t, { fetchStub: hangingSpicy(signals) });
  b.open();
  await b.advance(200);
  assert.equal(signals.length, 1);
  b.setTrack(2);
  await b.advance(600);
  assert.equal(b.of("track_change").at(-1).track_uri, "spotify:track:t2");
  assert.equal(signals.length, 2);
  assert.equal(signals[0].aborted, true);
  assert.equal(signals[1].aborted, false);
});
