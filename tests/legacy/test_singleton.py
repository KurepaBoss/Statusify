r"""Failing test: bind conflict must not silently kill the backend thread.

Reproduces the root cause of the unresponsiveness bug:
  - `_backend()` calls `await websockets.serve(ws_handler, WS_HOST, WS_PORT)`.
  - When the port is already bound (orphaned previous instance), this raises
    OSError(10048). The exception propagates uncaught through the daemon
    backend thread, silently killing the WS server. The Tk UI keeps drawing
    but no track updates ever arrive → "unresponsive" symptom.
  - Re-launching hits the same bind failure → "can't be opened again".

Expected after fix:
  - `_is_port_in_use(WS_PORT)` returns True when something holds the port.
  - `_backend()` catches the bind OSError and surfaces a clear, user-visible
    error via event_queue instead of dying silently.

Headless: no Tk, no Discord. Pure logic.
"""
import socket
import threading
import importlib.util
import sys
import os

HERE = os.path.dirname(os.path.abspath(__file__))


def _load_main_module():
    """Import main.py as a module so we can test its helpers headlessly."""
    spec = importlib.util.spec_from_file_location("statusify_main", os.path.join(HERE, "main.py"))
    mod = importlib.util.module_from_spec(spec)
    # Block the module from launching its Tk app by faking __name__ guard.
    # The module's import-time code runs, but __main__ block is skipped because
    # we import it under a different name.
    spec.loader.exec_module(mod)
    return mod


def _hold_port(port):
    """Bind a socket and hold it open until the returned stop() is called."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    s.bind(("127.0.0.1", port))
    s.listen(1)

    def stop():
        try:
            s.close()
        except Exception:
            pass
    return stop


def test_port_in_use_detector():
    """`_is_port_in_use` must detect a port that's actively bound."""
    mod = _load_main_module()
    port = mod.WS_PORT

    # Sanity: detector exists
    assert hasattr(mod, "_is_port_in_use"), (
        "main.py must define _is_port_in_use(port) for singleton enforcement"
    )

    # When port is free, detector returns False
    assert mod._is_port_in_use(port) is False, (
        f"Port {port} should be free before we bind it"
    )

    # Hold the port
    stop = _hold_port(port)
    try:
        assert mod._is_port_in_use(port) is True, (
            f"_is_port_in_use({port}) must return True when something holds it"
        )
    finally:
        stop()

    # After release, free again
    assert mod._is_port_in_use(port) is False, (
        f"Port {port} should be free again after release"
    )
    print("  PASS  _is_port_in_use correctly detects bind state")


def test_bind_conflict_surfaced_not_silent():
    """_backend must catch OSError from websockets.serve, not die silently."""
    mod = _load_main_module()
    port = mod.WS_PORT

    # Hold the port so websockets.serve will fail with 10048
    stop = _hold_port(port)
    surfaced_error = []
    try:
        import asyncio

        # Drain the event_queue after running _backend so we can inspect
        original_put = mod.event_queue.put
        captured = []
        mod.event_queue.put = lambda *a, **k: captured.append(a)

        # _backend also needs DISCORD_APP_ID set to not early-return.
        # We stub it. The bind happens BEFORE Discord connect, so we expect
        # the OSError to be caught and surfaced before we ever reach Discord.
        original_app_id = mod.DISCORD_APP_ID
        mod.DISCORD_APP_ID = "test-id"

        async def _run():
            # _backend is an infinite loop on success; on bind failure it
            # must surface the error and RETURN (not loop forever).
            try:
                await asyncio.wait_for(mod._backend(), timeout=5.0)
            except asyncio.TimeoutError:
                pass  # acceptable — means it didn't crash, just ran forever

        try:
            asyncio.run(_run())
        except Exception as e:
            assert False, (
                f"_backend() raised uncaught {type(e).__name__}: {e} — "
                "it must catch the bind OSError and surface via event_queue, "
                "not propagate to crash the daemon thread"
            )

        mod.DISCORD_APP_ID = original_app_id
        mod.event_queue.put = original_put

        # The error must have been surfaced to the GUI via event_queue.
        # Look for a tuple whose first element indicates an error/bind failure.
        has_error_event = any(
            isinstance(c, tuple) and c and "bind" in str(c[0]).lower()
            for c in captured
        ) or any(
            isinstance(c, tuple) and c and "error" in str(c).lower()
            for c in captured
        )
        assert captured, (
            "_backend() must put at least one event on event_queue when bind "
            "fails, so the GUI can show the error. Got: empty queue."
        )
        assert has_error_event, (
            f"_backend() must surface a bind-error event. Captured: {captured}"
        )
        print("  PASS  bind conflict surfaced via event_queue (not silent)")
    finally:
        stop()


def test_orphan_kill_path_exists():
    """There must be a helper to kill an orphaned previous instance."""
    mod = _load_main_module()
    # Either a kill-orphan helper or a documented graceful path.
    assert hasattr(mod, "_kill_orphan_instance") or hasattr(mod, "_force_take_port"), (
        "main.py must define a way to handle an orphaned instance holding the "
        "WS port (e.g. _kill_orphan_instance or _force_take_port)"
    )
    print("  PASS  orphan-instance handling helper exists")


if __name__ == "__main__":
    failures = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"\n[{name}]")
            try:
                fn()
            except AssertionError as e:
                print(f"  FAIL  {e}")
                failures += 1
            except Exception as e:
                print(f"  ERROR {type(e).__name__}: {e}")
                failures += 1
    print(f"\n========================================")
    print(f"Singleton-port results: {3 - failures} passed, {failures} failed")
    sys.exit(1 if failures else 0)
