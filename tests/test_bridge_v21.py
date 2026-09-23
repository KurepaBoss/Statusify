"""Bridge v2.1: syllable timing, queue, player state, next-track prefetch,
beats and the bridge-health warning.

Python side is driven through the real ws_handler with scripted messages;
the JS side runs the real lyrics-bridge.js under Node against a fake
Spicetify (same pattern as test_bridge_dedupe.py).
"""
import asyncio
import json
import os
import queue as _queue
import shutil
import subprocess
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import main
import statusify_bridge as sb
from statusify_history import HistoryStore
from statusify_lyrics import _calc_instrumental_gaps, select_line

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CUR = "spotify:track:cur0cur0cur0cur0cur0cu"
NXT = "spotify:track:nxt0nxt0nxt0nxt0nxt0nx"

SYL_LINES = [
    {"startMs": 1000, "endMs": 3000, "words": "Hello world",
     "syl": [[1000, 1300, "Hel"], [1300, 1600, "lo "], [1700, 2900, "world"]]},
    {"startMs": 20000, "words": "no timing here"},
]


class FakeWS:
    def __init__(self, msgs):
        self._msgs = [json.dumps(m) for m in msgs]
        self.sent = []

    async def send(self, data):
        self.sent.append(data)

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for m in self._msgs:
            yield m


async def _nosleep(*_a, **_k):
    return None


def track(uri):
    return {"type": "track_change", "artist": "A", "title": uri[-4:],
            "track_uri": uri, "album_art": "", "duration_ms": 200_000}


def lyrics(uri, lines, kind="lyrics", source="Spicy"):
    return {"type": kind, "track_uri": uri, "mode": "synced",
            "synced": lines, "plain": [], "source": source}


def drain():
    out = []
    while True:
        try:
            out.append(main.event_queue.get_nowait())
        except _queue.Empty:
            return out


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(main, "_save_history", lambda *a, **k: None)
    monkeypatch.setattr(main, "_cached_lyrics", lambda uri: None)
    monkeypatch.setattr(main, "_start_play", lambda: None)
    monkeypatch.setattr(main.asyncio, "sleep", _nosleep)
    monkeypatch.setattr(main, "_PREFETCH", sb.PrefetchCache(5))
    for k, v in {"track_uri": "", "synced": [], "plain": [], "instrumental_gaps": [],
                 "lyrics_mode": "none", "queue": [], "volume": 1.0, "shuffle": False,
                 "repeat": 0, "liked": False, "beats": [], "tempo": 0.0}.items():
        monkeypatch.setattr(main.state, k, v, raising=False)
    drain()
    yield
    drain()


def run(msgs):
    asyncio.run(main.ws_handler(FakeWS(msgs)))


# ── State defaults ────────────────────────────────────────────────
def test_state_class_defaults_match_contract():
    s = main.State()
    assert s.queue == [] and s.beats == [] and s.translation == {}
    assert s.volume == 1.0 and s.tempo == 0.0
    assert s.shuffle is False and s.liked is False and s.repeat == 0


# ── Syllable timing passes through untouched ──────────────────────
def test_syl_lines_pass_through_to_state():
    run([track(CUR), lyrics(CUR, SYL_LINES)])
    assert main.state.synced == SYL_LINES
    assert main.state.synced[0]["syl"][1] == [1300, 1600, "lo "]
    assert "syl" not in main.state.synced[1]


def test_extra_keys_do_not_break_lyric_helpers():
    gaps = _calc_instrumental_gaps(SYL_LINES, 200_000)
    assert isinstance(gaps, list)
    cur, _nxt = select_line("synced", SYL_LINES, [], 1500, 200_000)
    assert cur == "Hello world"


def test_history_cache_round_trip_keeps_syl(tmp_path):
    st = HistoryStore(str(tmp_path / "h.db"))
    try:
        st.save_lyrics(CUR, "synced", SYL_LINES, [], "Spicy")
        mode, synced, plain, src = st.get_lyrics(CUR)
        assert synced == SYL_LINES and mode == "synced"
    finally:
        st.close()


# ── Prefetch ──────────────────────────────────────────────────────
PRE_LINES = [{"startMs": 500, "words": "prefetched line"}]


def test_prefetch_applies_instantly_on_track_change():
    run([track(CUR), lyrics(NXT, PRE_LINES, kind="lyrics_prefetch"), track(NXT)])
    assert main.state.track_uri == NXT
    assert main.state.synced == PRE_LINES
    srcs = [e[1] for e in drain() if e[0] == "lyrics"]
    assert srcs == ["Spicy · preloaded"]
    assert NXT not in main._PREFETCH   # consumed


def test_real_fetch_still_replaces_prefetched_lyrics():
    real = [{"startMs": 700, "words": "real line"}]
    run([track(CUR), lyrics(NXT, PRE_LINES, kind="lyrics_prefetch"), track(NXT),
         lyrics(NXT, real)])
    assert main.state.synced == real


def test_failed_real_fetch_keeps_prefetched_lyrics():
    run([track(CUR), lyrics(NXT, PRE_LINES, kind="lyrics_prefetch"), track(NXT),
         {"type": "lyrics", "track_uri": NXT, "mode": "none", "synced": [], "plain": [],
          "source": "none"}])
    assert main.state.synced == PRE_LINES


def test_prefetch_for_current_track_applies_if_nothing_yet():
    run([track(NXT), lyrics(NXT, PRE_LINES, kind="lyrics_prefetch")])
    assert main.state.synced == PRE_LINES


def test_prefetch_never_overrides_lyrics_already_shown():
    real = [{"startMs": 700, "words": "real line"}]
    run([track(NXT), lyrics(NXT, real), lyrics(NXT, PRE_LINES, kind="lyrics_prefetch")])
    assert main.state.synced == real


def test_prefetch_cache_is_lru_and_skips_none():
    c = sb.PrefetchCache(maxlen=5)
    for i in range(7):
        assert c.put(f"u{i}", "synced", PRE_LINES, [], "Spicy")
    assert len(c) == 5 and "u0" not in c and "u1" not in c and "u6" in c
    assert not c.put("x", "none", [], [], "none")
    assert c.pop("u6")[3] == "Spicy" and c.pop("u6") is None


def test_prefetch_takes_priority_over_cache(monkeypatch):
    monkeypatch.setattr(main, "_cached_lyrics",
                        lambda uri: ("synced", [{"startMs": 1, "words": "cached"}], [], "x"))
    run([track(CUR), lyrics(NXT, PRE_LINES, kind="lyrics_prefetch"), track(NXT)])
    assert main.state.synced == PRE_LINES


def test_cache_still_applies_without_prefetch(monkeypatch):
    cached = [{"startMs": 1, "words": "cached"}]
    monkeypatch.setattr(main, "_cached_lyrics", lambda uri: ("synced", cached, [], "LRCLIB"))
    run([track(NXT)])
    assert main.state.synced == cached


# ── Queue / player state / beats ──────────────────────────────────
QUEUE = [{"uri": NXT, "uid": "u1", "title": "N", "artist": "B",
          "album_art": "https://i.scdn.co/image/abc", "duration_ms": 180000}]


def test_queue_message_updates_state_and_emits_event():
    run([track(CUR), {"type": "queue", "tracks": QUEUE + [{"nope": 1}, "junk"]}])
    assert main.state.queue == QUEUE
    assert ("queue",) in drain()


def test_parse_queue_caps_and_defaults():
    many = [{"uri": f"spotify:track:{i}"} for i in range(15)]
    out = sb.parse_queue(many)
    assert len(out) == 10
    assert out[0] == {"uri": "spotify:track:0", "uid": "", "title": "", "artist": "",
                      "album_art": "", "duration_ms": 0}
    assert sb.parse_queue(None) == []


def test_player_state_message():
    run([{"type": "player_state", "volume": 0.42, "shuffle": True, "repeat": 2, "liked": True}])
    s = main.state
    assert (s.volume, s.shuffle, s.repeat, s.liked) == (0.42, True, 2, True)
    assert ("player_state",) in drain()


def test_player_state_parsing_is_defensive():
    cur = {"volume": 0.5, "shuffle": True, "repeat": 1, "liked": True}
    out = sb.parse_player_state({"volume": 7, "shuffle": "yes", "repeat": 9}, cur)
    assert out == {"volume": 1.0, "shuffle": True, "repeat": 1, "liked": True}
    assert sb.parse_player_state({"volume": "x"})["volume"] == 1.0


def test_beats_for_current_track_only():
    run([track(CUR), {"type": "beats", "track_uri": NXT, "tempo": 99.0, "beats": [1, 2]}])
    assert main.state.beats == []
    run([{"type": "beats", "track_uri": CUR, "tempo": 120.5, "beats": [1000, 500]}])
    assert main.state.beats == [500, 1000] and main.state.tempo == 120.5
    assert ("beats",) in drain()
    run([track(NXT)])
    assert main.state.beats == [] and main.state.tempo == 0.0


# ── Python → bridge commands ──────────────────────────────────────
@pytest.fixture
def sent(monkeypatch):
    box = []
    monkeypatch.setattr(main, "_send_bridge", lambda obj: box.append(obj) or True)
    return box


def test_set_volume_sends_clamped_value(sent):
    assert main.set_volume(0.25)
    assert main.set_volume(3)
    assert sent == [{"type": "volume", "value": 0.25}, {"type": "volume", "value": 1.0}]
    assert main.state.volume == 1.0
    assert not main.set_volume("loud")


def test_skip_to_queue_sends_uri_and_uid(sent):
    assert main.skip_to_queue(NXT, "u1")
    assert sent == [{"type": "skip_to", "uri": NXT, "uid": "u1"}]
    assert not main.skip_to_queue("")


def test_player_command_toggles_are_optimistic(sent):
    main.player_command("shuffle"); main.player_command("repeat")
    main.player_command("repeat"); main.player_command("like")
    assert [o["action"] for o in sent] == ["shuffle", "repeat", "repeat", "like"]
    s = main.state
    assert (s.shuffle, s.repeat, s.liked) == (True, 2, True)
    main.player_command("repeat")
    assert main.state.repeat == 0


def test_player_command_passes_extra_fields(sent):
    main.player_command("next", foo=1)
    assert sent == [{"type": "player", "action": "next", "foo": 1}]


def test_no_optimistic_update_when_disconnected(monkeypatch):
    monkeypatch.setattr(main, "_send_bridge", lambda obj: False)
    assert not main.player_command("like")
    assert main.state.liked is False
    assert not main.set_volume(0.1)
    assert main.state.volume == 1.0


def test_send_bridge_serialises_json(monkeypatch):
    got = []

    class WS:
        async def send(self, data):
            got.append(json.loads(data))

    loop = asyncio.new_event_loop()
    import threading
    t = threading.Thread(target=loop.run_forever, daemon=True); t.start()
    try:
        monkeypatch.setattr(main, "_spicetify_ws", WS())
        monkeypatch.setattr(main, "_backend_loop", loop)
        assert main.skip_to_queue(NXT, "u9")
        asyncio.run_coroutine_threadsafe(asyncio.sleep(0), loop).result(2)
        assert got == [{"type": "skip_to", "uri": NXT, "uid": "u9"}]
    finally:
        loop.call_soon_threadsafe(loop.stop); t.join(2)


# ── Bridge health ─────────────────────────────────────────────────
class Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t


def test_health_flags_once_after_grace_then_clears_on_connect():
    clk = Clock(); h = sb.BridgeHealth(clock=clk)
    assert h.evaluate(True) is None
    clk.t += 44; assert h.evaluate(True) is None
    clk.t += 2;  assert h.evaluate(True) == "flag"
    clk.t += 60; assert h.evaluate(True) is None      # no repeat
    assert h.on_connect() == "clear"
    assert not h.needs_check()


def test_health_no_warning_while_spotify_is_closed():
    clk = Clock(); h = sb.BridgeHealth(clock=clk)
    for _ in range(10):
        clk.t += 30; assert h.evaluate(False) is None
    # Spotify starts now: the grace period counts from here.
    clk.t += 30; assert h.evaluate(True) is None
    clk.t += 46; assert h.evaluate(True) == "flag"
    assert h.evaluate(False) == "clear"               # Spotify closed


def test_health_restarts_grace_after_disconnect():
    clk = Clock(); h = sb.BridgeHealth(clock=clk)
    h.on_connect(); clk.t += 500
    h.on_disconnect()
    assert h.needs_check()
    clk.t += 30; assert h.evaluate(True) is None
    clk.t += 16; assert h.evaluate(True) == "flag"


def test_health_snooze_during_repair():
    clk = Clock(); h = sb.BridgeHealth(clock=clk)
    clk.t += 50; assert h.evaluate(True) == "flag"
    h.snooze(120)
    clk.t += 100; assert h.evaluate(True) is None
    clk.t += 70;  assert h.evaluate(True) == "flag"


class FakeLabel:
    def __init__(self): self.text = ""; self.bound = None; self.cursor = ""
    def cget(self, k): return self.text
    def config(self, **kw): self.cursor = kw.get("cursor", self.cursor)
    def bind(self, ev, fn): self.bound = fn
    def unbind(self, ev): self.bound = None


def fake_app(clk):
    app = types.SimpleNamespace()
    app.lbl_err = FakeLabel()
    app._set_error = lambda m: setattr(app.lbl_err, "text", m)
    app.repaired = 0
    app._repair_bridge = lambda: setattr(app, "repaired", app.repaired + 1)
    app._bridge_health = sb.BridgeHealth(clock=clk)
    app.apply = lambda v: main.App._apply_bridge_health(app, v)
    return app


def test_app_shows_clickable_repair_and_clears_on_connect(monkeypatch):
    monkeypatch.setattr(main._maint, "repair_files", lambda *a: ("s.ps1", "b.js"))
    clk = Clock(); app = fake_app(clk)
    clk.t += 46
    app.apply(app._bridge_health.evaluate(True))
    assert sb.HEALTH_MSG in app.lbl_err.text
    app.lbl_err.bound(None)
    assert app.repaired == 1
    app.apply(app._bridge_health.on_connect())
    assert app.lbl_err.text == "" and app.lbl_err.bound is None


def test_app_reshows_after_line_blanked_but_never_covers_other_errors(monkeypatch):
    monkeypatch.setattr(main._maint, "repair_files", lambda *a: None)
    clk = Clock(); app = fake_app(clk)
    clk.t += 46
    app.apply(app._bridge_health.evaluate(True))
    app._set_error("")                                 # e.g. Discord connected
    app.apply(app._bridge_health.evaluate(True))
    assert sb.HEALTH_MSG in app.lbl_err.text
    app._set_error("Discord unreachable")
    app.apply(app._bridge_health.evaluate(True))
    assert app.lbl_err.text == "Discord unreachable"
    # A connect doesn't wipe someone else's message either.
    app.apply(app._bridge_health.on_connect())
    assert app.lbl_err.text == "Discord unreachable"


# ── The JS bridge under Node ──────────────────────────────────────
HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
const sent = [];
let sock;
global.WebSocket = class { static OPEN = 1; constructor() { this.readyState = 1; sock = this; }
  send(d) { sent.push(JSON.parse(d)); } close() {} };

// Minimal SLObjPack packer (inverse of the bridge's slUnpack).
function pack(root) {
  const values = [], idx = new Map(), stream = [];
  const ptr = v => { const k = typeof v + ":" + String(v);
    if (!idx.has(k)) { idx.set(k, values.length); values.push(v); } return idx.get(k); };
  const enc = v => {
    if (Array.isArray(v)) { stream.push(-2, v.length); v.forEach(enc); }
    else if (v && typeof v === "object") { const ks = Object.keys(v);
      stream.push(-1, ks.length); ks.forEach(k => stream.push(ptr(k))); ks.forEach(k => enc(v[k])); }
    else stream.push(ptr(v));
  };
  enc(root); return [values, stream];
}
const CUR = "spotify:track:cur0cur0cur0cur0cur0cu", NXT = "spotify:track:nxt0nxt0nxt0nxt0nxt0nx";
const SYL = { Type: "Syllable", Content: [
  { Type: "Vocal", Lead: { StartTime: 1.0, EndTime: 3.0, Syllables: [
      { Text: "Hel", StartTime: 1.0, EndTime: 1.3, IsPartOfWord: true },
      { Text: "lo", StartTime: 1.3, EndTime: 1.6, IsPartOfWord: false },
      { Text: "world", StartTime: 1.7, EndTime: 2.9, IsPartOfWord: false } ] } },
  { Type: "Vocal", Text: "Something else entirely", Lead: { StartTime: 4, EndTime: 5, Syllables: [
      { Text: "Totally", StartTime: 4, EndTime: 4.5, IsPartOfWord: false },
      { Text: "different", StartTime: 4.5, EndTime: 5, IsPartOfWord: false } ] } } ] };
const LINE = { Type: "Line", Content: [ { Type: "Vocal", Text: "next song", StartTime: 0.5, EndTime: 2 } ] };
const fetched = [];
global.fetch = async (url, opts) => {
  const id = JSON.parse(opts.body).queries[0].variables.id;
  fetched.push(id);
  const doc = CUR.endsWith(id) ? SYL : LINE;
  return { status: 200, json: async () => ({ queries: [
    { operation: "_notice", result: "x" },
    { operation: "lyrics", result: { httpStatus: 200, data: pack(doc) } } ] }) };
};
let vol = 0.8, shuffle = false, repeat = 0, heart = false;
const skipped = [], played = [];
let audioCalls = 0;
const item = { uri: CUR, metadata: { title: "T", artist_name: "A", duration: "200000" } };
global.Spicetify = {
  Player: { data: { item, nextItems: [
              { uri: NXT, uid: "u1", metadata: { title: "N", artist_name: "B", duration: "180000",
                                                image_url: "spotify:image:abc" } },
              { uri: "spotify:delimiter", uid: "d" } ] },
            getProgress: () => 1000, isPlaying: () => true, addEventListener() {},
            next() {}, back() {}, seek() {}, togglePlay() {}, play() {}, pause() {},
            getVolume: () => vol, setVolume: v => { vol = v; },
            getShuffle: () => shuffle, toggleShuffle: () => { shuffle = !shuffle; },
            getRepeat: () => repeat, toggleRepeat: () => { repeat = (repeat + 1) % 3; },
            getHeart: () => heart, toggleHeart: () => { heart = !heart; },
            playUri: async u => { played.push(u); } },
  CosmosAsync: { get: async () => { throw new Error("no spotify lyrics in test"); } },
  Platform: { AuthorizationAPI: { _tokenProvider: { _token: { accessToken: "tok" } } },
              PlayerAPI: { skipTo: async t => { skipped.push(t); } } },
  getAudioData: async () => { audioCalls++; throw new Error("audio-analysis gone"); },
};
global.setInterval = () => 0;
eval(src);
const wait = ms => new Promise(r => setTimeout(r, ms));
(async () => {
  await wait(1500);
  sock.onopen();
  await wait(5500);                     // lyrics + 4 s prefetch delay
  const msg = o => sock.onmessage({ data: JSON.stringify(o) });
  await msg({ type: "volume", value: 0.3 });
  await msg({ type: "player", action: "shuffle" });
  await msg({ type: "player", action: "like" });
  await msg({ type: "skip_to", uri: NXT, uid: "u1" });
  await wait(700);
  console.log(JSON.stringify({ sent, skipped, played, audioCalls, fetched }));
  process.exit(0);
})();
"""


@pytest.fixture(scope="module")
def bridge_run(tmp_path_factory):
    if not shutil.which("node"):
        pytest.skip("node not installed")
    h = tmp_path_factory.mktemp("js") / "harness.js"
    h.write_text(HARNESS, encoding="utf-8")
    out = subprocess.run(["node", str(h), os.path.join(ROOT, "lyrics-bridge.js")],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def _of(res, kind):
    return [m for m in res["sent"] if m["type"] == kind]


def test_js_hello_carries_bridge_version(bridge_run):
    assert _of(bridge_run, "hello")[0]["version"] == "2.1.0"


def test_js_syllables_reproduce_line_text(bridge_run):
    ly = [m for m in _of(bridge_run, "lyrics") if m["track_uri"] == CUR]
    assert len(ly) == 1
    l0, l1 = ly[0]["synced"]
    assert l0["words"] == "Hello world" and l0["endMs"] == 3000
    assert l0["syl"] == [[1000, 1300, "Hel"], [1300, 1600, "lo "], [1700, 2900, "world"]]
    assert "".join(p[2] for p in l0["syl"]) == l0["words"]
    # Syllables that don't spell the line's text are dropped, not guessed.
    assert l1["words"] == "Something else entirely" and "syl" not in l1


def test_js_queue_sent_on_track_change(bridge_run):
    q = _of(bridge_run, "queue")[0]["tracks"]
    assert q == [{"uri": NXT, "uid": "u1", "title": "N", "artist": "B",
                  "album_art": "https://i.scdn.co/image/abc", "duration_ms": 180000}]
    kinds = [m["type"] for m in bridge_run["sent"]]
    assert kinds.index("queue") == kinds.index("track_change") + 1


def test_js_prefetches_next_track_quietly(bridge_run):
    pre = _of(bridge_run, "lyrics_prefetch")
    assert len(pre) == 1 and pre[0]["track_uri"] == NXT
    assert pre[0]["mode"] == "synced"
    assert pre[0]["synced"] == [{"startMs": 500, "words": "next song", "endMs": 2000}]
    assert not any("nxt0" in m.get("message", "") for m in _of(bridge_run, "lyrics_debug"))
    # Prefetch comes after the current track's lyrics, never before.
    kinds = [m["type"] for m in bridge_run["sent"]]
    assert kinds.index("lyrics") < kinds.index("lyrics_prefetch")


def test_js_beats_fail_silently_once(bridge_run):
    assert _of(bridge_run, "beats") == []
    assert bridge_run["audioCalls"] == 1


def test_js_player_state_and_commands(bridge_run):
    ps = _of(bridge_run, "player_state")
    assert ps[0] == {"type": "player_state", "volume": 0.8, "shuffle": False,
                     "repeat": 0, "liked": False}
    assert ps[-1] == {"type": "player_state", "volume": 0.3, "shuffle": True,
                      "repeat": 0, "liked": True}
    assert bridge_run["skipped"] == [{"uri": NXT, "uid": "u1"}]
    assert bridge_run["played"] == []          # skipTo worked; no playUri fallback
