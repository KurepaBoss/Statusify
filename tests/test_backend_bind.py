"""Tests that a WebSocket port conflict is reported, never silent.

Ported from tests/legacy/test_singleton.py, a standalone script from the
"Statusify is unresponsive / won't reopen" investigation that no longer ran.

THE BUG it guarded: _backend() awaited websockets.serve() unguarded. When the
port was already held (usually an orphaned previous instance), the OSError
killed the daemon backend thread silently: the window kept drawing, no track
updates ever arrived, and relaunching hit the same wall.
"""
import asyncio
import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main


def test_port_conflict_is_surfaced_and_backend_returns(monkeypatch):
    # Hold a free port of our own: the real 8765 may belong to a running app.
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]

    events = []
    monkeypatch.setattr(main, "WS_PORT", port)
    monkeypatch.setattr(main, "DISCORD_APP_ID", "123")
    monkeypatch.setattr(main.event_queue, "put", lambda ev: events.append(ev))
    # The real one kills whatever owns the port — here, this test process.
    monkeypatch.setattr(main, "_kill_orphan_instance", lambda: False)
    try:
        # Must return promptly on its own, not raise and not hang.
        asyncio.run(asyncio.wait_for(main._backend(), timeout=10))
    finally:
        holder.close()

    kinds = [e[0] for e in events]
    assert "bind_conflict" in kinds
    assert "bind_error" in kinds
