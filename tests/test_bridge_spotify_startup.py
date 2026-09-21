"""Tests that the Spotify-lyrics fallback survives Spotify's startup window.

THE BUG: CosmosAsync requests fail with "Resolver not found!" until Spotify's
internal request router has started. Every one of the 28 Spotify-fallback
failures logged since 2026-09-13 landed 0-17 s after the bridge connected,
i.e. while Spotify was still booting, and the bridge's only retry (one more
attempt, 3 s later) gave up long before the router was ready. Any song
missing from Spicy's catalogue played at startup got no lyrics at all.

Runs the real lyrics-bridge.js under Node with a Cosmos that fails with the
resolver error for the first few seconds.
"""
import json
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
const READY_AFTER_MS = +process.argv[3];
const sent = [];
let sock;
global.WebSocket = class { static OPEN = 1; constructor() { this.readyState = 1; sock = this; }
  send(d) { sent.push(JSON.parse(d)); } close() {} };
const item = { uri: "spotify:track:bbbbbbbbbbbbbbbbbbbbbb",
               metadata: { title: "T", artist_name: "A", duration: "200000" } };
const t0 = Date.now();
global.Spicetify = {
  Player: { data: { item }, getProgress: () => 1000, isPlaying: () => true,
            addEventListener() {}, next() {}, seek() {} },
  Platform: {},
  CosmosAsync: { get: async () => {
    if (Date.now() - t0 < READY_AFTER_MS)
      throw new Error("GET request to https://spclient.wg.spotify.com/x request failed with error code -1 (Resolver not found!)");
    return { lyrics: { syncType: "LINE_SYNCED",
                       lines: [{ startTimeMs: "1000", words: "hello" }] } };
  } },
};
global.fetch = async () => ({ ok: false, status: 404, json: async () => ({}), text: async () => "" });
global.setInterval = () => 0;
eval(src);
(async () => {
  await new Promise(r => setTimeout(r, 1500));
  sock.onopen();
  await new Promise(r => setTimeout(r, READY_AFTER_MS + 12000));
  const ly = sent.filter(m => m.type === "lyrics");
  console.log(JSON.stringify(ly.map(m => [m.mode, (m.synced || []).length])));
  process.exit(0);
})();
"""


def run(tmp_path, ready_after_ms):
    h = tmp_path / "harness.js"
    h.write_text(HARNESS, encoding="utf-8")
    out = subprocess.run(["node", str(h), os.path.join(ROOT, "lyrics-bridge.js"),
                          str(ready_after_ms)], capture_output=True, text=True, timeout=90)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_spotify_lyrics_arrive_once_router_is_ready(tmp_path):
    # Router comes up 12 s after connect, well inside the logged 0-17 s window.
    assert run(tmp_path, 12000) == [["synced", 1]]
