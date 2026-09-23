"""Bridge v2.1 helpers: message parsing, the next-track lyric prefetch cache
and the "is the bridge actually connected?" health check.

Kept out of main.py so they can be unit-tested without Tk or a WebSocket.
Every parser is defensive: the bridge runs inside whatever Spotify /
Spicetify version the user has, so any field may be missing or malformed.
"""
import collections
import subprocess
import sys
import time

QUEUE_MAX = 10


def _num(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def parse_queue(tracks):
    """The bridge's `queue.tracks` → a clean list of up to QUEUE_MAX dicts
    {uri, uid, title, artist, album_art, duration_ms}. Junk entries dropped."""
    out = []
    if not isinstance(tracks, list):
        return out
    for t in tracks:
        if not isinstance(t, dict):
            continue
        uri = t.get("uri")
        if not isinstance(uri, str) or not uri:
            continue
        out.append({
            "uri": uri,
            "uid": str(t.get("uid") or ""),
            "title": str(t.get("title") or ""),
            "artist": str(t.get("artist") or ""),
            "album_art": str(t.get("album_art") or ""),
            "duration_ms": max(0, _num(t.get("duration_ms"))),
        })
        if len(out) >= QUEUE_MAX:
            break
    return out


def parse_player_state(data, current=None):
    """A `player_state` message → {volume, shuffle, repeat, liked}.

    Fields that are missing or invalid keep their value from `current`
    (a dict with the same keys), so a partial message never resets state."""
    cur = {"volume": 1.0, "shuffle": False, "repeat": 0, "liked": False}
    cur.update(current or {})
    out = dict(cur)
    if "volume" in data:
        try:
            v = float(data["volume"])
            if v == v:  # not NaN
                out["volume"] = min(1.0, max(0.0, v))
        except (TypeError, ValueError):
            pass
    if isinstance(data.get("shuffle"), bool):
        out["shuffle"] = data["shuffle"]
    r = data.get("repeat")
    if isinstance(r, int) and not isinstance(r, bool) and 0 <= r <= 2:
        out["repeat"] = r
    if isinstance(data.get("liked"), bool):
        out["liked"] = data["liked"]
    return out


class PrefetchCache:
    """LRU of lyric payloads the bridge fetched ahead for upcoming tracks,
    keyed by track URI. Only real lyrics are kept — a "none" result would
    have nothing to apply."""

    def __init__(self, maxlen=5):
        self.maxlen = maxlen
        self._d = collections.OrderedDict()

    def put(self, uri, mode, synced, plain, source):
        if not uri or mode not in ("synced", "plain") or not (synced or plain):
            return False
        self._d[uri] = (mode, synced or [], plain or [], source or "Spicy")
        self._d.move_to_end(uri)
        while len(self._d) > self.maxlen:
            self._d.popitem(last=False)
        return True

    def pop(self, uri):
        """(mode, synced, plain, source) for `uri`, removed from the cache."""
        return self._d.pop(uri, None)

    def __contains__(self, uri):
        return uri in self._d

    def __len__(self):
        return len(self._d)


def preloaded_label(source):
    """"Spicy" → "Spicy · preloaded"."""
    return f"{source or 'Spicy'} · preloaded"


# ── Bridge health ─────────────────────────────────────────────────
# A Spicetify/Spotify update wipes injected extensions. Then Spotify runs
# fine, nothing ever connects, and lyrics just never appear — with no error
# anywhere. Detect "Spotify is running but no bridge has connected for a
# while" and offer the repair.
HEALTH_MSG = "Spotify is running but the lyrics bridge isn't connected — click to repair"


class BridgeHealth:
    """Pure state machine; the caller supplies whether Spotify is running.

    evaluate() returns "flag" (show the warning), "clear" (remove it) or
    None (no change), so the warning is set once and cleared once."""

    GRACE_S = 45.0

    def __init__(self, clock=time.monotonic, grace_s=None):
        self.clock = clock
        self.grace_s = self.GRACE_S if grace_s is None else grace_s
        self.connected = False
        self.flagged = False
        self.since = clock()  # start of the current "not connected" window

    def on_connect(self):
        self.connected = True
        if self.flagged:
            self.flagged = False
            return "clear"
        return None

    def on_disconnect(self):
        if self.connected:
            self.connected = False
            self.since = self.clock()

    def snooze(self, extra_s=120.0):
        """A repair is under way (it restarts Spotify): stand down and give
        it extra_s on top of the usual grace before warning again."""
        self.flagged = False
        self.since = self.clock() + extra_s

    def needs_check(self):
        """Whether a (process-list) check is worth running at all."""
        return not self.connected

    def evaluate(self, spotify_running):
        if self.connected:
            return self.on_connect()
        if not spotify_running:
            # Grace restarts from Spotify's launch, not from ours: it needs
            # time to boot before the bridge could possibly connect.
            self.since = self.clock()
            if self.flagged:
                self.flagged = False
                return "clear"
            return None
        if not self.flagged and self.clock() - self.since >= self.grace_s:
            self.flagged = True
            return "flag"
        return None


def spotify_running():
    """True if Spotify.exe is running. Windows only; False elsewhere or on
    any failure (a false "not running" just means no warning)."""
    if sys.platform != "win32":
        return False
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Spotify.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return "spotify.exe" in (out.stdout or "").lower()
    except Exception:
        return False
