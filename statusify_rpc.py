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


def configure(log_fn, emit_fn, recv_executor, send_executor, max_state=None):
    global log, emit, _recv_executor, executor, MAX_STATE
    log, emit, _recv_executor, executor = log_fn, emit_fn, recv_executor, send_executor
    if max_state is not None:
        MAX_STATE = max_state


class DiscordRPC:
    OP_HANDSHAKE = 0; OP_FRAME = 1
    # If a single pipe operation takes longer than this, consider the pipe dead.
    # Prevents a blocking read() from wedging the worker thread forever.
    PIPE_TIMEOUT_S   = 5.0
    # Drop a pending SET_ACTIVITY if the worker can't keep up (rate-limit /
    # pipe stall). Old lyric lines are stale anyway; we keep only the latest.
    SEND_STALE_S     = 1.0
    def __init__(self, app_id):
        self.app_id = app_id; self.pipe = None
        self._connected = False; self._nonce = 0
        # Pending RPC sends, newest last. We only need the newest, so older
        # unprocessed sends are dropped to avoid an unbounded queue backlog.
        self._pending = []
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
        # large_text is the smaller secondary line Discord renders under the
        # details/state — showing the full "title — artist" there just repeated
        # the top line, so use it for the creator only.
        act = {"type": 2, "details": label,
               "assets": {"large_image": art or "spotify", "large_text": (artist or label)[:128]}}
        f = [l for l in lines if l]
        act["state"] = join_lines(f)[:MAX_STATE] if f else "— "
        # Add elapsed/remaining timer — this is part of the activity payload,
        # NOT a separate RPC call, so it does not count against the rate limit.
        if position_ms is not None and duration_ms and duration_ms > 0:
            import time as _time
            now_unix   = int(_time.time())
            start_unix = now_unix - (position_ms // 1000)
            end_unix   = start_unix + (duration_ms // 1000)
            act["timestamps"] = {"start": start_unix, "end": end_unix}
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
        now = time.monotonic()
        with self._lock:
            # Drop sends that have been sitting in the queue longer than the
            # staleness window — they're no longer worth sending.
            self._pending = [(ts, p) for (ts, p) in self._pending
                             if now - ts < self.SEND_STALE_S]
            fut_payload = (now, payload)
            self._pending.append(fut_payload)
        # Submit directly to the thread pool. A ThreadPoolExecutor accepts
        # submissions from any thread — no event loop required — and is itself
        # the backpressure boundary (max_workers caps concurrent sends).
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
            if not self._pending:
                return
            _, payload = self._pending.pop()      # newest only
            self._pending.clear()                  # discard the rest (stale)
        self._send(payload)
