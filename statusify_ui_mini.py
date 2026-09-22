"""Mini mode and the system-tray icon.

Moved verbatim out of main.App (which inherits MiniTrayMixin). Names that
belong to main are reached through M, the live main module, bound by
main at import time: palette colours and settings are rebound at
runtime, so they must be read from main on every use, never copied.
"""
import os
import threading
import tkinter as tk

M = None   # the main module


class MiniTrayMixin:

    # ── Mini mode ─────────────────────────────────────────────────
    def _toggle_mini(self, _e=None):
        if getattr(self, "_mini", None) is not None:
            self._close_mini()
        else:
            self._open_mini()

    def _open_mini(self):
        """A compact always-on-top strip showing just the current lyric.

        The main window is 520x720 — far too big to leave floating over a game
        or a video. Mini mode is the form this app actually wants most of the
        time: one line of text, always visible, out of the way."""
        try:
            m = tk.Toplevel(self._root)
            m.overrideredirect(True)
            m.attributes("-topmost", True)
            m.configure(bg=M.BG)
            sw = m.winfo_screenwidth()
            w, h = 560, 76
            saved = M._cfg_get("window", "mini_geometry", "")
            m.geometry(saved if saved else f"{w}x{h}+{(sw - w) // 2}+40")

            frame = tk.Frame(m, bg=M.BG2, highlightbackground=M.BORDER, highlightthickness=1)
            frame.pack(fill="both", expand=True)

            top = tk.Frame(frame, bg=M.BG2); top.pack(fill="x", padx=M.SP_MD, pady=(M.SP_SM, 0))
            self._mini_track = tk.Label(top, text="—", fg=M.MUTED, bg=M.BG2,
                                        font=self._f(M.FS_MICRO), anchor="w")
            self._mini_track.pack(side="left", fill="x", expand=True)
            close = tk.Label(top, text="✕", fg=M.MUTED, bg=M.BG2, font=self._f(M.FS_SMALL),
                             cursor="hand2", padx=M.SP_XS)
            close.pack(side="right")
            close.bind("<Button-1>", lambda e: self._close_mini())
            self._hoverable(close, fg=lambda: M.MUTED, hover_fg=lambda: M.DANGER, duration_ms=90)

            self._mini_lyric = tk.Label(frame, text="—", fg=M.ACCENT, bg=M.BG2,
                                        font=self._f(M.FS_TITLE, True), anchor="w",
                                        wraplength=520, justify="left")
            self._mini_lyric.pack(fill="x", padx=M.SP_MD, pady=(0, M.SP_SM))

            # Drag anywhere on the strip to move it.
            for w_ in (frame, top, self._mini_lyric, self._mini_track):
                w_.bind("<ButtonPress-1>", lambda e: (
                    setattr(self, "_mox", e.x_root - m.winfo_x()),
                    setattr(self, "_moy", e.y_root - m.winfo_y())))
                w_.bind("<B1-Motion>", lambda e: m.geometry(
                    f"+{e.x_root - self._mox}+{e.y_root - self._moy}"))

            self._mini = m
            self._refresh_mini()
            M.log("Mini mode on  ·  Ctrl+M to close")
        except tk.TclError as e:
            self._mini = None
            M.log(f"Mini mode failed: {e}")

    def _close_mini(self):
        m = getattr(self, "_mini", None)
        if m is None:
            return
        try:
            M._cfg_set("window", "mini_geometry", m.geometry())
            m.destroy()
        except (tk.TclError, ValueError):
            pass
        self._mini = None
        self._cancel("mini")
        M.log("Mini mode off")

    def _refresh_mini(self):
        """Mirror the current lyric into the mini strip."""
        m = getattr(self, "_mini", None)
        if m is None:
            return
        try:
            self._mini_lyric.config(text=self.lbl_lyric.cget("text") or "—")
            artist = getattr(M.state, "artist", ""); title = getattr(M.state, "title", "")
            self._mini_track.config(text=f"{artist} — {title}" if title else "Waiting for Spotify…")
        except (AttributeError, tk.TclError):
            pass

    # ── System tray (#11, #12) ────────────────────────────────────
    def _tray_start(self):
        """Create the tray icon, if pystray is available.

        A tray icon is the idiomatic home for a background presence app:
        closing to it keeps RPC running without a taskbar button."""
        self._tray = None
        if not M.TRAY_AVAILABLE:
            M.log("Tray unavailable (pystray/Pillow not installed) — window-only mode")
            return
        try:
            image = None
            try:
                image = M.Image.open(M._ensure_icon_path())
            except Exception:
                image = M.Image.new("RGB", (64, 64), M.ACCENT)

            def _do(fn):
                # pystray callbacks run on the tray's own thread; every Tk
                # call must be marshalled back to the main loop.
                return lambda *_: self._root.after(0, fn)

            menu = M.pystray.Menu(
                M.pystray.MenuItem("Show Statusify", _do(self._tray_show), default=True),
                M.pystray.MenuItem("Hide to tray",   _do(self._hide_to_tray)),
                M.pystray.Menu.SEPARATOR,
                M.pystray.MenuItem("Mini mode",          _do(self._toggle_mini)),
                M.pystray.MenuItem("Always on top",      _do(self._toggle_topmost)),
                M.pystray.Menu.SEPARATOR,
                M.pystray.MenuItem("Toggle Discord RPC", _do(self._tray_toggle_rpc)),
                M.pystray.MenuItem("Reconnect RPC",      _do(self._reconnect_rpc)),
                M.pystray.Menu.SEPARATOR,
                M.pystray.MenuItem("Quit", _do(self._quit)),
            )
            self._tray = M.pystray.Icon("Statusify", image, "Statusify", menu)
            threading.Thread(target=self._tray.run, name="tray", daemon=True).start()
            M.log("Tray icon started")
        except Exception as e:
            self._tray = None
            M.log(f"Tray icon failed: {e}")

    def _tray_stop(self):
        tray = getattr(self, "_tray", None)
        if tray is not None:
            try:
                tray.stop()
            except Exception:
                pass
            self._tray = None

    def _tray_show(self):
        try:
            self._root.deiconify()
            self._root.lift()
            self._root.focus_force()
            self._hidden = False
        except Exception as e:
            M.log(f"Tray show failed: {e}")

    def _watch_show_request(self):
        """Restore the window when another launch asks us to.

        Double-clicking Statusify.exe while a copy is already running used to
        hit the single-instance guard and do nothing but show an 'already
        running' box. If that instance was hidden in the tray, the app was
        effectively unopenable — you had to hunt for the tray icon or kill the
        process. The second launch now drops a sentinel file and exits; this
        picks it up and brings the window back."""
        try:
            if os.path.exists(M._SHOW_FLAG):
                try:
                    os.remove(M._SHOW_FLAG)
                except OSError:
                    pass
                M.log("Second launch detected — restoring window")
                self._tray_show()
        except OSError:
            pass
        self._schedule("showwatch", 1000, self._watch_show_request)

    def _hide_to_tray(self):
        """Withdraw the window but keep the backend and RPC running."""
        self._save_geometry()   # capture position before it becomes unreadable
        if not getattr(self, "_tray", None):
            # No tray icon means no way to get the window back — minimise
            # instead of withdrawing, or the app becomes unreachable.
            self._minimize()
            return
        try:
            self._root.withdraw()
            self._hidden = True
        except Exception as e:
            M.log(f"Hide to tray failed: {e}")
