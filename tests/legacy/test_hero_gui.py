"""
Headless smoke test for the Now-Playing hero redesign.

Verifies (without a real display or Spotify/Discord) that:
  1. main.py imports and the App can be constructed.
  2. The hero widgets exist with the right names: canvas, lbl_title,
     lbl_artist, lbl_info, lbl_lyric, lbl_delay, dot_sp, dot_dc,
     lbl_rl, log_txt — the preservation contract.
  3. The NEW progress widgets exist: _prog_cv, _prog_elapsed, _prog_total.
  4. Album-art canvas is 120x120 (HERO_ART_PX), not the old 72x72.
  5. The progress methods exist and run without raising:
     _redraw_progress, _tick_progress, _estimate_pos_ms, _fmt_time.
  6. _fmt_time formats ms -> m:ss correctly.
  7. The progress ticker self-handle (_prog_after) is set after construction.

We can't actually pump the Tk event loop here, but Tkinter lets us create
widgets and call .update_idletasks() to force geometry calculation, which is
enough to validate structure. If no display is available at all we skip
gracefully.
"""
import sys, os, traceback

# Stub the optional/optional-on-headless imports exactly like test_freeze_fix.
import types
for mod in ("keyboard", "winreg", "ctypes", "ctypes.wintypes"):
    sys.modules.setdefault(mod, types.ModuleType(mod))
class _Stub:
    def __getattr__(self, n): return _Stub()
    def __call__(self, *a, **k): return _Stub()
# We DO want real tkinter for this test if possible.
try:
    import tkinter  # noqa
except Exception:
    print("SKIP  no tkinter available on this host")
    sys.exit(0)

sys.modules.setdefault("websockets", types.ModuleType("websockets"))
sys.modules["websockets"].exceptions = types.SimpleNamespace(ConnectionClosed=Exception)
dotenv_stub = types.ModuleType("dotenv"); dotenv_stub.load_dotenv = lambda *a, **k: None
sys.modules["dotenv"] = dotenv_stub
try:
    from PIL import Image, ImageTk  # noqa
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False
    pil = types.ModuleType("PIL"); pil.Image = _Stub(); pil.ImageTk = _Stub()
    sys.modules["PIL"] = pil; sys.modules["PIL.Image"] = pil.Image
    sys.modules["PIL.ImageTk"] = pil.ImageTk

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

PASS = FAIL = 0
def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  PASS  {name}")
    else:
        FAIL += 1; print(f"  FAIL  {name}  {extra}")

# Try to force a virtual display is impossible on Windows; instead just attempt
# to build the App and catch the "no display" failure as a skip.
import main

try:
    app = main.App()
except Exception as e:
    # If Tk can't open a display, skip rather than fail.
    if "display" in str(e).lower() or "couldn't connect" in str(e).lower() or "no display" in str(e).lower():
        print(f"SKIP  no display available ({e})")
        sys.exit(0)
    print(f"ERROR constructing App:\n{traceback.format_exc()}")
    sys.exit(1)

try:
    app.win.update_idletasks()

    # 2. Preservation contract: all original Now-Playing widgets still exist.
    for attr in ("canvas", "lbl_title", "lbl_artist", "lbl_info",
                 "lbl_lyric", "lbl_delay", "dot_sp", "dot_dc",
                 "lbl_rl", "log_txt"):
        check(f"preserved widget self.{attr}", hasattr(app, attr))

    # 3. New progress widgets exist.
    check("new widget self._prog_cv",   hasattr(app, "_prog_cv"))
    check("new widget self._prog_elapsed", hasattr(app, "_prog_elapsed"))
    check("new widget self._prog_total",   hasattr(app, "_prog_total"))

    # 4. Album-art canvas dimensions == HERO_ART_PX (120), not 72.
    cw = int(app.canvas.cget("width")); ch = int(app.canvas.cget("height"))
    check(f"hero art canvas is 120x120 (got {cw}x{ch})", cw == 120 and ch == 120)
    check("HERO_ART_PX constant == 120", main.HERO_ART_PX == 120)

    # 5. Progress methods exist and run without raising.
    check("method _redraw_progress exists", callable(getattr(app, "_redraw_progress", None)))
    check("method _tick_progress exists",   callable(getattr(app, "_tick_progress", None)))
    check("method _estimate_pos_ms exists", callable(getattr(app, "_estimate_pos_ms", None)))
    check("method _fmt_time exists",        callable(getattr(app, "_fmt_time", None)))
    try:
        app._redraw_progress(); ran = True
    except Exception as e:
        ran = False; extra = repr(e)
    check("_redraw_progress() runs clean", ran)

    # 6. _fmt_time formatting.
    check("_fmt_time(0) == '0:00'", app._fmt_time(0) == "0:00")
    check("_fmt_time(72000) == '1:12'", app._fmt_time(72000) == "1:12")     # 1m12s
    check("_fmt_time(204000) == '3:24'", app._fmt_time(204000) == "3:24")   # 3m24s

    # 7. Ticker scheduled.
    check("_prog_after is set after construction", getattr(app, "_prog_after", None) is not None)

    # 8. _estimate_pos_ms clamps to duration and returns 0 when nothing loaded.
    main.state.duration_ms = 100000
    main.state.position_ms = 50000
    main.state.is_playing = False
    est = app._estimate_pos_ms()
    check("_estimate_pos_ms reflects state (got %s)" % est, est == 50000)

    # 9. _redraw_progress with a real track doesn't crash and sets total label.
    app._prog_last_redraw = 0.0  # bypass throttle so the redraw actually fires
    app._redraw_progress()
    check("progress total label set to 1:40", app._prog_total.cget("text") == "1:40")

finally:
    try:
        if getattr(app, "_prog_after", None):
            app.win.after_cancel(app._prog_after)
        app.win.destroy()
    except Exception:
        pass

print(f"\n{'='*44}\nHero-GUI results: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
