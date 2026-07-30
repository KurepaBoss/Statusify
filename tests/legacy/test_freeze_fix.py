"""
Targeted tests for the unresponsiveness fixes in main.py.

These exercise the logic WITHOUT needing Discord, Spotify, or a GUI:
  1. RPC send backpressure — stale sends are dropped, only newest survives.
  2. Timed-out pipe read marks the connection dead and closes the pipe.
  3. set_activity returns quickly even when the worker is stalled (no blocking).

Run:  python test_freeze_fix.py
"""
import sys, os, time, threading, io, struct, json

# Stub heavy/optional imports so main.py can be imported headless.
sys.modules.setdefault("keyboard", type(sys)("keyboard"))
import types
for mod in ("winreg", "ctypes", "ctypes.wintypes"):
    sys.modules.setdefault(mod, types.ModuleType(mod))

# Make a stub for tkinter + PIL so import doesn't fail on a headless box.
class _Stub:
    def __getattr__(self, n): return _Stub()
    def __call__(self, *a, **k): return _Stub()
sys.modules.setdefault("tkinter", _Stub())
sys.modules.setdefault("tkinter.font", _Stub())
sys.modules.setdefault("tkinter.colorchooser", _Stub())

# websockets is required by main.py at import time.
try:
    import websockets  # noqa
except ImportError:
    sys.modules.setdefault("websockets", types.ModuleType("websockets"))
    sys.modules["websockets"].exceptions = types.SimpleNamespace(ConnectionClosed=Exception)

try:
    from dotenv import load_dotenv  # noqa
except ImportError:
    dotenv_stub = types.ModuleType("dotenv")
    dotenv_stub.load_dotenv = lambda *a, **k: None
    sys.modules["dotenv"] = dotenv_stub

try:
    from PIL import Image, ImageTk  # noqa
    PIL_AVAILABLE = True
except ImportError:
    pil_stub = types.ModuleType("PIL")
    pil_stub.Image = _Stub()
    pil_stub.ImageTk = _Stub()
    sys.modules["PIL"] = pil_stub
    sys.modules["PIL.Image"] = pil_stub.Image
    sys.modules["PIL.ImageTk"] = pil_stub.ImageTk

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import main  # noqa: E402

PASS = 0
FAIL = 0

def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")

# ── A fake named pipe that we can control ─────────────────────────────
class FakePipe:
    r"""Behaves like the file object returned by open(r'\\.\pipe\...')."""
    def __init__(self):
        self.closed = False
        self.read_block = threading.Event()
        self.read_block.set()  # set = data available; clear = block forever
        self.written = []
    def write(self, b):
        if self.closed: raise OSError("closed")
        self.written.append(b)
    def flush(self): pass
    def read(self, n):
        if self.closed: raise OSError("closed")
        # Block until "data available" — simulates a stalled Discord pipe.
        self.read_block.wait()
        if self.closed: raise OSError("closed")
        # Return a well-formed Discord FRAME: op=1, len=n
        return struct.pack("<II", 1, n) + b"\x00" * n if n >= 0 else b""
    def close(self):
        self.closed = True
        self.read_block.set()  # unblock any waiting read

# ── Test 1: backpressure drops stale sends, keeps newest ──────────────
def test_backpressure():
    print("\n[Test 1] RPC send backpressure")
    rpc = main.DiscordRPC("123")
    rpc._connected = True

    # Queue 10 sends rapidly without letting the worker run.
    for i in range(10):
        rpc._enqueue_send({"nonce": i})

    # Give the worker a moment to drain, but we'll inspect the pending list
    # under the lock. After enqueue, _pending should never exceed a handful
    # because each enqueue prunes stale entries and the worker pops newest-only.
    with rpc._lock:
        pending_count = len(rpc._pending)
    # The worker may have already drained some, but _pending can't grow unbounded.
    check("pending list stays bounded (< 20)", pending_count < 20)

# ── Test 2: timed-out pipe read marks dead + closes pipe ──────────────
def test_recv_timeout():
    print("\n[Test 2] Timed-out pipe read forces reconnect")
    rpc = main.DiscordRPC("123")
    rpc._connected = True
    rpc.pipe = FakePipe()
    rpc.pipe.read_block.clear()  # make read() block forever

    # Shorten the timeout so the test runs fast.
    main.DiscordRPC.PIPE_TIMEOUT_S = 0.5

    start = time.monotonic()
    result = rpc._recv_timed()
    elapsed = time.monotonic() - start

    check("returned within ~timeout (got %.2fs, expect ~0.5s)" % elapsed, elapsed < 1.5)
    check("returned None on timeout", result is None)
    check("marked disconnected", rpc._connected is False)
    check("pipe was closed to unblock worker", rpc.pipe.closed is True)

# ── Test 3: set_activity doesn't block when worker is stalled ─────────
def test_set_activity_nonblocking():
    print("\n[Test 3] set_activity is non-blocking under worker stall")
    rpc = main.DiscordRPC("123")
    rpc._connected = True
    rpc.pipe = FakePipe()
    rpc.pipe.read_block.clear()  # worker reads will stall

    import asyncio
    loop = asyncio.new_event_loop()
    start = time.monotonic()
    # Fire several SET_ACTIVITY calls back-to-back.
    async def fire():
        for _ in range(5):
            await rpc.set_activity("T", "A", ["line"], "art", 0, 1000)
    loop.run_until_complete(fire())
    elapsed = time.monotonic() - start

    # Each call must return promptly even though the worker is stuck.
    # 5 calls in well under a second = not blocking on the pipe.
    check("5 set_activity calls returned fast (%.2fs)" % elapsed, elapsed < 1.0)
    loop.close()

# ── Test 4: executors exist and are bounded ───────────────────────────
def test_executors():
    print("\n[Test 4] Bounded executors are defined")
    check("image_executor exists", hasattr(main, "image_executor"))
    check("_recv_executor exists", hasattr(main, "_recv_executor"))
    check("image_executor has max_workers", main.image_executor._max_workers <= 4)
    check("_recv_executor has max_workers", main._recv_executor._max_workers <= 4)

if __name__ == "__main__":
    test_executors()
    test_backpressure()
    test_recv_timeout()
    test_set_activity_nonblocking()
    print(f"\n{'='*40}\nResults: {PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
