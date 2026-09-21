"""Tests that the Spicetify bridge fetches each track exactly once on connect.

THE BUG: on connect the bridge's onopen starts sendTrackAndLyrics() for the
current track. 300 ms later Statusify sends request_state, whose handler did
`fetchingUris.clear()` — wiping the very guard that stops concurrent fetches
of the same URI — and started a second pipeline. Result: two track_change
messages (session stats count the song twice, lyrics wiped mid-load) and two
lyric loads, visible as every "Now playing"/"Lyrics" log line appearing twice.

Runs the real lyrics-bridge.js under Node against a fake Spicetify/WebSocket.
"""
import json
import os
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
const sent = [];
let sock;
global.WebSocket = class {
  static OPEN = 1;
  constructor() { this.readyState = 1; sock = this; }
  send(d) { sent.push(JSON.parse(d)); }
  close() {}
};
const item = { uri: "spotify:track:aaaaaaaaaaaaaaaaaaaaaa",
               metadata: { title: "T", artist_name: "A", duration: "200000" } };
global.Spicetify = {
  Player: { data: { item }, getProgress: () => 1000, isPlaying: () => true,
            addEventListener() {}, next() {}, seek() {} },
  CosmosAsync: {}, Platform: {},
};
// Slow lyric fetch so the second request lands while the first is in flight.
global.fetch = () => new Promise(r => setTimeout(() => r({ ok: false, status: 404,
  json: async () => ({}), text: async () => "" }), 400));
const realSetInterval = setInterval;
global.setInterval = () => 0;           // no ticks: isolate the connect path
eval(src);
(async () => {
  await new Promise(r => setTimeout(r, 1500));   // bridge boot delay
  sock.onopen();
  await new Promise(r => setTimeout(r, 300));    // Statusify's request_state delay
  await sock.onmessage({ data: JSON.stringify({ type: "request_state" }) });
  await new Promise(r => setTimeout(r, 8000));
  console.log(JSON.stringify(sent.filter(m => m.type === "track_change" || m.type === "lyrics")
                                 .map(m => m.type)));
  process.exit(0);
})();
"""


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_connect_plus_request_state_fetches_once(tmp_path):
    h = tmp_path / "harness.js"
    h.write_text(HARNESS, encoding="utf-8")
    out = subprocess.run(["node", str(h), os.path.join(ROOT, "lyrics-bridge.js")],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    kinds = json.loads(out.stdout.strip().splitlines()[-1])
    assert kinds.count("track_change") == 1, kinds
    assert kinds.count("lyrics") == 1, kinds
