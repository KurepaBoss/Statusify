"""Discord IPC client: the named-pipe protocol behind Rich Presence.

Split out of main.py. It needs three things from the app: a logger, a way to
post UI events, and the executors pipe reads and writes run on (main.py owns
them so shutdown can cancel them). main.py injects them with configure() at import time;
until then logging and events are no-ops, which keeps this importable in tests.
"""
import asyncio
import json
import os
import struct
import threading
import time
from concurrent.futures import TimeoutError as _FutureTimeout

from statusify_lyrics import join_lines

MAX_STATE = 128


def _noop(*_a, **_k):
    pass


log = _noop
emit = _noop
_recv_executor = None   # bounded pipe reads
executor = None         # pipe writes (SET_ACTIVITY sends)


uri_fn = lambda: ""     # returns the current Spotify track URI

# What the member list shows after "Listening to": the song (details line,
# STATUS_DISPLAY_DETAILS) or the Discord application's name (NAME). Without
# the field Discord always used the app name, so every friend saw
# "Listening to Spotify" rather than what was actually playing.
STATUS_DISPLAY_NAME    = 0
STATUS_DISPLAY_DETAILS = 2
status_display_type = STATUS_DISPLAY_DETAILS
link_track = True       # title/art link to the track on open.spotify.com
listen_button = True    # a "Listen on Spotify" button under the presence
album_fn = lambda: ""   # returns the current track's album name
LISTEN_LABEL = "Listen on Spotify"   # Discord caps button labels at 32 chars


def configure(log_fn, emit_fn, recv_executor, send_executor, max_state=None,
              current_uri=None):
    global log, emit, _recv_executor, executor, MAX_STATE, uri_fn
    log, emit, _recv_executor, executor = log_fn, emit_fn, recv_executor, send_executor
    if max_state is not None:
        MAX_STATE = max_state
    if current_uri is not None:
        uri_fn = current_uri


def track_url(uri):
    """open.spotify.com link for a spotify:track: URI, else None (local files,
    podcasts and ads have no public track page)."""
    if not uri or not uri.startswith("spotify:track:"):
        return None
    return "https://open.spotify.com/track/" + uri.rsplit(":", 1)[-1]


class DiscordRPC:
    OP_HANDSHAKE = 0; OP_FRAME = 1
    # If a single pipe operation takes longer than this, consider the pipe dead.
    # Prevents a blocking read() from wedging the worker thread forever.
    PIPE_TIMEOUT_S   = 5.0
    def __init__(self, app_id):
        self.app_id = app_id; self.pipe = None
        self._connected = False; self._nonce = 0
        # Pending RPC sends, newest last. We only need the newest, so older
        # unprocessed sends are dropped to avoid an unbounded queue backlog.
        self._pending = []
        self._drain_queued = False   # a _drain_sends task is already queued
        self._lock = threading.Lock()
    def _nxt(self): self._nonce += 1; return str(self._nonce)
    async def connect(self):
        for i in range(10):
            try: self.pipe = open(f"\\\\.\\pipe\\discord-ipc-{i}", "r+b", buffering=0); log(f"RPC connected  ·  discord-ipc-{i}"); break
            except OSError: continue
        if not self.pipe: raise RuntimeError("Discord IPC pipe not found")
        self._raw(self.OP_HANDSHAKE, {"v":1,"client_id":self.app_id})
        # Bounded handshake read — a stalled pipe can't hang the backend thread.
        try:
            fut = _recv_executor.submit(self._recv)
            resp = await asyncio.wrap_future(fut)
        except Exception:
            resp = None
        if resp and resp.get("evt") == "READY":
            self._connected = True
            user = resp["data"]["user"]["username"]
            log(f"RPC handshake OK  ·  {user}"); emit(("rpc_ok", user))
        else: raise RuntimeError(f"Handshake failed: {resp}")
    def _raw(self, op, data):
        p = json.dumps(data).encode()
        self.pipe.write(struct.pack("<II", op, len(p)) + p); self.pipe.flush()
    def _read_exact(self, n):
        """Read exactly n bytes, or return None if the pipe closes first.

        The pipe is opened with buffering=0, so read(n) is a single syscall
        that may legitimately return fewer bytes than asked for. The old code
        assumed a short read never happened; when it did, the header unpack or
        the JSON parse failed on a perfectly healthy connection and the frame
        was dropped as if the pipe had died."""
        chunks = []
        remaining = n
        while remaining > 0:
            chunk = self.pipe.read(remaining)
            if not chunk:
                return None
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _recv(self):
        try:
            h = self._read_exact(8)
            if h is None: return None
            _, n = struct.unpack("<II", h)
            body = self._read_exact(n)
            if body is None: return None
            return json.loads(body)
        except (OSError, ValueError, struct.error) as e:
            # OSError = pipe died, ValueError = malformed JSON frame. These
            # mean very different things; the bare `except:` that used to be
            # here made them indistinguishable and also swallowed
            # KeyboardInterrupt/SystemExit.
            log(f"RPC recv failed: {type(e).__name__}: {e}")
            return None
    def _send(self, payload):
        try:
            self._raw(self.OP_FRAME, payload)
            # Read the response on a SEPARATE bounded worker with a timeout.
            # A blocking read() on a stalled Discord pipe would otherwise sit
            # here forever, wedging the single RPC worker and preventing any
            # reconnection. We don't reuse the RPC executor for this read,
            # because the write above already occupies this thread.
            r = self._recv_timed()
            if r and r.get("evt") == "ERROR": log(f"RPC error: {r.get('data',{}).get('message',r)}")
        except Exception as e:
            log(f"Pipe error: {e}"); self._connected = False

    def _recv_timed(self):
        """Run the blocking pipe read on a worker with a timeout.

        If the read can't complete within PIPE_TIMEOUT_S, treat the pipe as
        dead: mark disconnected and close the pipe handle. Closing the handle
        unblocks the stuck worker thread (its read() raises) so it can't leak,
        and forces _backend to open a fresh pipe on reconnect."""
        fut = _recv_executor.submit(self._recv)
        try:
            return fut.result(timeout=self.PIPE_TIMEOUT_S)
        except _FutureTimeout:
            log("RPC pipe read timed out — forcing reconnect")
            self._connected = False
            # Close the pipe to unblock the stuck worker thread; its read()
            # will raise and the worker becomes available again.
            try:
                if self.pipe:
                    self.pipe.close()
            except Exception:
                pass
            return None
        except Exception:
            return None
    def _activity(self, title, artist, lines, art, position_ms=None, duration_ms=None):
        label = f"{title} — {artist}"[:128]
        # type 2 = "Listening" activity. Discord shows Playing/game presence and
        # Listening presence in SEPARATE slots, so tagging this as Listening lets
        # it coexist with a running game instead of fighting it for the single
        # "Playing" slot — exactly how Spotify stays visible while you game.
        # large_text is the hover text on the cover art: the album, as on
        # Spotify's own presence. Discord rejects the whole activity if a
        # text field is shorter than 2 characters, so a one-letter album
        # falls back to the artist.
        try:
            album = (album_fn() or "").strip()
        except Exception:
            album = ""
        hover = next((t for t in (album, artist, label) if t and len(t.strip()) >= 2), label)
        act = {"type": 2, "details": label,
               "status_display_type": status_display_type,
               "assets": {"large_image": art or "spotify", "large_text": hover[:128]}}
        page = track_url(uri_fn())
        url = page if link_track else None
        if url:
            act["details_url"] = url
            act["assets"]["large_url"] = url
        # Buttons ride in the same SET_ACTIVITY payload, so they cost nothing
        # against the rate budget. Discord shows them to other people only.
        if listen_button and page:
            act["buttons"] = [{"label": LISTEN_LABEL[:32], "url": page}]
        f = [l for l in lines if l]
        act["state"] = join_lines(f)[:MAX_STATE] if f else "— "
        # Add elapsed/remaining timer — this is part of the activity payload,
        # NOT a separate RPC call, so it does not count against the rate limit.
        # Milliseconds: whole seconds let the progress bar drift up to 1 s.
        if position_ms is not None and duration_ms and duration_ms > 0:
            start_ms = int(time.time() * 1000) - int(position_ms)
            act["timestamps"] = {"start": start_ms, "end": start_ms + int(duration_ms)}
        return act
    async def set_activity(self, title, artist, lines, art, position_ms=None, duration_ms=None):
        if not self._connected: return
        act = self._activity(title, artist, lines, art, position_ms, duration_ms)
        self._enqueue_send({"cmd":"SET_ACTIVITY","args":{"pid":os.getpid(),"activity":act},"nonce":self._nxt()})
    async def clear_activity(self):
        if not self._connected: return
        self._enqueue_send({"cmd":"SET_ACTIVITY","args":{"pid":os.getpid(),"activity":None},"nonce":self._nxt()})

    def _enqueue_send(self, payload):
        """Submit a send to the worker, dropping stale queued sends first.

        Without this guard, every lyric tick queues another SET_ACTIVITY on the
        single-worker executor. If the worker stalls (busy pipe / Discord held
        by a game), the backlog grows without bound and the app freezes. We keep
        only the newest pending send — older lyric lines are stale by the time
        they'd be sent anyway."""
        # Keep literally the newest send, and queue at most one drain task.
        # This used to keep every send from the last second and submit a
        # drain task per call, so while the pipe was stalled — the very case
        # this guards — the executor's queue kept growing (tests/test_rpc.py).
        with self._lock:
            self._pending = [(time.monotonic(), payload)]
            if self._drain_queued:
                return
            self._drain_queued = True
        # A ThreadPoolExecutor accepts submissions from any thread, no event
        # loop required.
        fut = executor.submit(self._drain_sends)
        # Don't await — fire-and-forget, but consume exceptions so they can't
        # surface as "exception never retrieved" warnings.
        def _done(f):
            try: f.result()
            except Exception: pass
        fut.add_done_callback(_done)

    def _drain_sends(self):
        """Worker entry: take the newest pending send and dispatch it.

        Runs on the bounded executor so even if every call here were to block,
        only a fixed number of threads are ever tied up — never an unbounded
        queue of fire-and-forget tasks."""
        with self._lock:
            # Cleared before sending, so a send arriving mid-write queues a
            # fresh drain instead of being lost.
            self._drain_queued = False
            if not self._pending:
                return
            _, payload = self._pending.pop()      # the newest (and only) send
            self._pending.clear()
        self._send(payload)
