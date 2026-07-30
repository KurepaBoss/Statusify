"""Tests for process teardown.

THE BUG these cover: _quit() ended with `self._root.destroy(); sys.exit(0)`.

Since Python 3.9, ThreadPoolExecutor worker threads are NON-daemon and are
joined by an interpreter-exit hook. Statusify runs three pools, and
_recv_executor's workers sit in a blocking read() on the Discord named pipe —
a read that returns only when Discord sends something or the handle closes.
sys.exit() therefore handed control to a shutdown that waited forever on a
worker nothing was ever going to wake.

Observed: closing Statusify left pythonw.exe running with no window. That
zombie still held the single-instance mutex AND port 8765, so the next launch
hit "Statusify is already running" and refused. The only recovery was ending
the task by hand.

The first test below is the mechanism, run as a real subprocess — it is the
one that fails against the old code. The rest pin the ordering that makes the
fix work: the pipe must be closed BEFORE the pools are abandoned, because
closing the handle is what unblocks the stuck reader.
"""
import os
import subprocess
import sys
import textwrap

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import main


# The shutdown shape, reduced to its essentials. Parameterised on which exit
# call is used so the test demonstrates the difference rather than asserting it.
_EXIT_PROGRAM = textwrap.dedent("""
    import sys, os, threading, time
    from concurrent.futures import ThreadPoolExecutor

    pool = ThreadPoolExecutor(max_workers=1)
    started = threading.Event()

    def blocked_like_a_pipe_read():
        started.set()
        time.sleep(600)

    pool.submit(blocked_like_a_pipe_read)
    started.wait(5)
    {exit_call}
""")


def _exits_within(exit_call, seconds=10):
    """Run the shutdown shape in a subprocess; True if it actually exited."""
    proc = subprocess.Popen(
        [sys.executable, "-c", _EXIT_PROGRAM.format(exit_call=exit_call)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        proc.wait(timeout=seconds)
        return True
    except subprocess.TimeoutExpired:
        return False
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


class TestExitMechanism:
    def test_sys_exit_hangs_with_a_blocked_pool_worker(self):
        """The original bug, demonstrated. If this ever starts passing,
        CPython changed and the workaround can be revisited."""
        assert _exits_within("sys.exit(0)") is False

    def test_os_exit_terminates_regardless(self):
        """The property _teardown_and_exit relies on."""
        assert _exits_within("os._exit(0)") is True

    def test_shutdown_wait_false_alone_is_not_enough(self):
        """Worth pinning: shutdown(wait=False) does NOT unregister the
        interpreter's join hook, so it cannot rescue a blocked worker on its
        own. The pipe close and os._exit are both load-bearing."""
        assert _exits_within(
            "pool.shutdown(wait=False, cancel_futures=True); sys.exit(0)"
        ) is False


class FakePipe:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeRPC:
    def __init__(self):
        self.pipe = FakePipe()
        self._connected = True


class TestTeardownOrdering:
    """_teardown_and_exit must do its cleanup before os._exit, and must not
    let any single failure skip the rest."""

    @pytest.fixture
    def captured(self, monkeypatch):
        events = []
        monkeypatch.setattr(main.os, "_exit",
                            lambda code: events.append(("exit", code)))
        for name in ("executor", "image_executor", "_recv_executor"):
            pool = getattr(main, name)
            monkeypatch.setattr(
                pool, "shutdown",
                lambda wait=True, cancel_futures=False, _n=name:
                    events.append(("shutdown", _n, wait, cancel_futures)))
        return events

    def test_closes_the_discord_pipe_before_abandoning_the_pools(self, captured,
                                                                 monkeypatch):
        rpc = FakeRPC()
        monkeypatch.setitem(main._ACTIVE_RPC, "rpc", rpc)
        main._teardown_and_exit()
        assert rpc.pipe.closed, "stuck reader was never unblocked"
        assert rpc._connected is False

    def test_abandons_all_three_pools_without_waiting(self, captured, monkeypatch):
        monkeypatch.setitem(main._ACTIVE_RPC, "rpc", FakeRPC())
        main._teardown_and_exit()
        shutdowns = {e[1]: e for e in captured if e[0] == "shutdown"}
        assert set(shutdowns) == {"executor", "image_executor", "_recv_executor"}
        for name, ev in shutdowns.items():
            assert ev[2] is False, f"{name} would block on a stuck worker"
            assert ev[3] is True,  f"{name} left queued work to start"

    def test_always_reaches_the_exit(self, captured, monkeypatch):
        monkeypatch.setitem(main._ACTIVE_RPC, "rpc", FakeRPC())
        main._teardown_and_exit()
        assert ("exit", 0) in captured

    def test_exits_even_with_no_rpc_connection(self, captured, monkeypatch):
        """Quitting before Discord ever connected must still exit."""
        monkeypatch.setitem(main._ACTIVE_RPC, "rpc", None)
        main._teardown_and_exit()
        assert ("exit", 0) in captured

    def test_a_failing_pipe_close_does_not_prevent_exit(self, captured,
                                                        monkeypatch):
        class ExplodingRPC:
            _connected = True

            @property
            def pipe(self):
                raise OSError("handle already invalid")

        monkeypatch.setitem(main._ACTIVE_RPC, "rpc", ExplodingRPC())
        main._teardown_and_exit()
        assert ("exit", 0) in captured, "a cleanup error stranded the process"

    def test_propagates_a_nonzero_exit_code(self, captured, monkeypatch):
        monkeypatch.setitem(main._ACTIVE_RPC, "rpc", FakeRPC())
        main._teardown_and_exit(3)
        assert ("exit", 3) in captured
