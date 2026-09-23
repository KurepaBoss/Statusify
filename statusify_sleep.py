"""Sleep timer: pause Spotify after N minutes, or when the current song ends.

Pausing is all it does — the bridge gives no guaranteed volume control, so
there's no fade. Nothing is persisted: a restart forgets the timer.

SleepTimer is plain logic with an injectable clock and pause function so the
tests can drive it without Tk or Spotify. install(App) adds the App methods
the UI uses:
    App._sleep_timer_set(minutes | "eos" | None)
    App._sleep_timer_remaining() -> seconds left, or None when off
    App._sleep_timer_label()     -> short status text ("" when off)
The App methods tick on the Tk thread with self._schedule, so there's no
extra thread and nothing to shut down.
"""
import time

M = None   # the main module, bound by main

EOS = "eos"
# Pause this long before the song's end, so Spotify doesn't start the next
# one. The track-change check below catches it if the ping comes in late.
EOS_LEAD_S = 0.6

# The Settings control's fixed choices (SleepTimer.value() keys). Any other
# minute count was typed into its Custom field.
PRESETS = ("off", "15", "30", "60", EOS)
CUSTOM_MIN, CUSTOM_MAX = 1, 600


def parse_minutes(text):
    """A typed custom duration: whole minutes, CUSTOM_MIN..CUSTOM_MAX, with
    an optional "m"/"min" suffix. None when it isn't one."""
    s = str(text or "").strip().lower()
    for suffix in ("minutes", "min", "m"):
        if s.endswith(suffix):
            s = s[:-len(suffix)].strip()
            break
    if not s.isdigit():
        return None
    m = int(s)
    return m if CUSTOM_MIN <= m <= CUSTOM_MAX else None


class SleepTimer:
    def __init__(self, clock=time.monotonic, pause=None, state=None):
        self.clock = clock
        self.pause = pause or (lambda: None)
        self._state = state            # callable -> object with track_uri etc.
        self.mode = None               # None, "min" or EOS
        self.minutes = None
        self.deadline = None
        self.uri = None

    def state(self):
        return self._state() if self._state else None

    def set(self, spec):
        """spec: minutes (int), "eos", or None/"off"/0 to cancel."""
        if spec in (None, "off", 0, "0", ""):
            self.mode = self.minutes = self.deadline = self.uri = None
            return
        if spec == EOS:
            st = self.state()
            self.mode, self.minutes, self.deadline = EOS, None, None
            self.uri = getattr(st, "track_uri", "") if st else ""
            return
        m = float(spec)
        if m <= 0:
            return self.set(None)
        self.mode, self.minutes, self.uri = "min", int(m) if m == int(m) else m, None
        self.deadline = self.clock() + m * 60.0

    @property
    def active(self):
        return self.mode is not None

    def value(self):
        """"off", "eos" or the minutes as a string ("15", or a custom "45")."""
        if self.mode == EOS:
            return EOS
        if self.mode == "min":
            return str(self.minutes)
        return "off"

    def remaining(self):
        """Seconds left, or None when off (or, for end-of-song, unknown)."""
        if self.mode == "min":
            return max(0.0, self.deadline - self.clock())
        if self.mode == EOS:
            st = self.state()
            if st is None:
                return None
            dur = getattr(st, "duration_ms", 0) or 0
            if not dur:
                return None
            return max(0.0, (dur - getattr(st, "position_ms", 0)) / 1000.0)
        return None

    def due(self):
        if self.mode == "min":
            return self.clock() >= self.deadline
        if self.mode == EOS:
            st = self.state()
            if st is None:
                return False
            if self.uri and getattr(st, "track_uri", "") not in ("", self.uri):
                return True                           # the next song already started
            if not getattr(st, "is_playing", True):
                return False
            left = self.remaining()
            return left is not None and left <= EOS_LEAD_S
        return False

    def tick(self):
        """Pause and switch off if the timer is due. Returns True if it fired."""
        if not self.active or not self.due():
            return False
        self.set(None)
        try:
            self.pause()
        except Exception:
            pass
        return True

    def label(self):
        if self.mode == "min":
            return f"Sleep in {fmt(self.remaining())}"
        if self.mode == EOS:
            left = self.remaining()
            return "Sleep at end of song" + (f" · {fmt(left)}" if left is not None else "")
        return ""


def fmt(seconds):
    s = int(round(seconds or 0))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# ── App methods ──────────────────────────────────────────────────
def _timer(self):
    t = self.__dict__.get("_sleep_timer")
    if t is None:
        t = SleepTimer(pause=lambda: M.player_command("pause"),
                       state=lambda: M.state)
        self._sleep_timer = t
    return t


def _sleep_timer_set(self, spec):
    """Start (minutes or "eos") or cancel (None) the sleep timer."""
    t = _timer(self)
    t.set(spec)
    if M is not None:
        M.log(f"Sleep timer: {t.label() or 'off'}")
    self._sleep_timer_tick()


def _sleep_timer_remaining(self):
    return _timer(self).remaining()


def _sleep_timer_label(self):
    return _timer(self).label()


def _sleep_timer_tick(self):
    t = _timer(self)
    if t.tick() and M is not None:
        M.log("Sleep timer: paused Spotify")
    try:
        self._sleep_timer_changed()
    except Exception:
        pass
    if not t.active:
        try:
            self._cancel("sleeptick")
        except Exception:
            pass
        return
    left = t.remaining()
    ms = 200 if (t.mode == EOS and (left is None or left < 5)) else 1000
    self._schedule("sleeptick", ms, self._sleep_timer_tick)


def _sleep_timer_changed(self):
    """Refresh the Settings countdown and control (if built)."""
    lbl = getattr(self, "lbl_sleep", None)
    if lbl is not None:
        t = _timer(self)
        left = t.remaining()
        if t.mode == "min":
            text = fmt(left)
        elif t.mode == EOS:
            text = "End of song" if left is None else fmt(left)
        else:
            text = "Off"
        lbl.config(text=text, fg=M.ACCENT if t.active else M.TEXT2)
    seg = getattr(self, "_sleep_seg", None)
    v = _timer(self).value()
    prev = self.__dict__.get("_sleep_seg_val")
    changed = v != prev
    if seg is not None and getattr(seg, "item", None) is not None and changed:
        self._sleep_seg_val = v
        if v == "off" and prev not in (None, "off") and getattr(self, "_sleep_custom_open", False):
            # The timer went off (it fired) with the Custom field still open:
            # fold it away so the control reads Off, like the timer.
            close = getattr(self, "_sleep_custom_close", None)
            if close is not None:
                close()
        seg.slide()


_METHODS = ("_sleep_timer_set", "_sleep_timer_remaining", "_sleep_timer_label",
            "_sleep_timer_tick", "_sleep_timer_changed")


def install(cls):
    """Add the sleep-timer methods to App (kept out of its base-class list
    so the other feature branches' mixins merge cleanly)."""
    for name in _METHODS:
        if not hasattr(cls, name):
            setattr(cls, name, globals()[name])
