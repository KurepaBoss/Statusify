"""Tests for the Discord IPC client's freeze protections (statusify_rpc).

Ported from tests/legacy/test_freeze_fix.py, a standalone script from the
"Statusify becomes unresponsive" investigation that had stopped running (it
could no longer even import main). The behaviours it guarded:

1. Presence sends can't pile up: each enqueue prunes stale entries and the
   worker sends only the newest, so a stalled pipe can't build a backlog.
2. A pipe read that hangs (Discord frozen, pipe half-dead) times out, marks
   the connection dead and closes the pipe, instead of wedging the worker
   thread forever.
3. set_activity never blocks the caller on the pipe, even with the worker
   stuck, so the backend loop keeps running.
"""
import asyncio
import os
import struct
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import statusify_rpc as rpc_mod


class FakePipe:
    r"""Stands in for open(r'\\.\pipe\discord-ipc-N'); reads can be stalled."""

    def __init__(self):
        self.closed = False
        self.read_block = threading.Event()
        self.read_block.set()           # set = data available, clear = hang
        self.written = []

    def write(self, b):
        if self.closed:
            raise OSError("closed")
        self.written.append(b)

    def flush(self):
        pass

    def read(self, n):
        if self.closed:
            raise OSError("closed")
        self.read_block.wait()
        if self.closed:
            raise OSError("closed")
        return struct.pack("<II", 1, n) + b"\x00" * n

    def close(self):
        self.closed = True
        self.read_block.set()


@pytest.fixture(autouse=True)
def wired(monkeypatch):
    recv, send = ThreadPoolExecutor(2), ThreadPoolExecutor(1)
    rpc_mod.configure(lambda *_: None, lambda *_: None, recv, send, 128)
    monkeypatch.setattr(rpc_mod.DiscordRPC, "PIPE_TIMEOUT_S", 0.5)
    yield
    recv.shutdown(wait=False, cancel_futures=True)
    send.shutdown(wait=False, cancel_futures=True)


def test_pending_sends_stay_bounded_while_worker_is_stuck():
    """The legacy script queued 10 sends and asserted "< 20", which could never
    fail. Stall the worker for real and send a burst: only the newest payload
    may be kept, and only one drain task may be queued behind the stuck one.
    (The old code kept every send from the last second and submitted a new
    drain task per call, so a stalled pipe grew the executor's queue.)"""
    rpc = rpc_mod.DiscordRPC("123")
    rpc._connected = True
    rpc.pipe = FakePipe()
    gate = threading.Event()
    rpc_mod.executor.submit(gate.wait)          # occupy the single send worker
    try:
        for i in range(50):
            rpc._enqueue_send({"nonce": i})
        with rpc._lock:
            assert [p["nonce"] for _, p in rpc._pending] == [49]
        assert rpc_mod.executor._work_queue.qsize() <= 1
    finally:
        gate.set()


def test_stalled_read_times_out_and_forces_reconnect():
    rpc = rpc_mod.DiscordRPC("123")
    rpc._connected = True
    rpc.pipe = FakePipe()
    rpc.pipe.read_block.clear()        # read() hangs
    start = time.monotonic()
    assert rpc._recv_timed() is None
    assert time.monotonic() - start < 1.5
    assert rpc._connected is False
    assert rpc.pipe.closed is True     # closing it is what frees the worker


def test_set_activity_never_blocks_on_a_stalled_pipe():
    rpc = rpc_mod.DiscordRPC("123")
    rpc._connected = True
    rpc.pipe = FakePipe()
    rpc.pipe.read_block.clear()

    async def fire():
        for _ in range(5):
            await rpc.set_activity("T", "A", ["line"], "art", 0, 1000)

    start = time.monotonic()
    asyncio.run(fire())
    assert time.monotonic() - start < 1.0
