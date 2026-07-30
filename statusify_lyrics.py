"""Pure lyric selection, formatting and instrumental-gap detection.

Split out of main.py because this is the logic that decides *what text ends
up on your Discord profile* — and it touches no globals, no Tk, no sockets and
no Windows APIs, so it can be imported and unit-tested on any platform.
Everything else in main.py is entangled with module-level palette globals or
Win32 calls and was left in place rather than risk a cosmetic refactor.
"""

def offset_key(uri):
    """Config-safe option name for a track URI.

    A Spotify URI looks like "spotify:track:<id>", and ':' is one of
    configparser's key/value delimiters — so the raw URI could never survive a
    round trip through statusify.cfg:

      * Python 3.13+ refuses to write it at all (InvalidWriteError). That
        propagated out of _save_config, which only caught OSError, so a single
        click on "+250" raised inside the Tk callback AND left the in-memory
        config permanently unwritable — every later save (theme, hotkeys,
        blacklist, window geometry, lyric delay) silently failed for the rest
        of the session.
      * Older versions wrote it happily, then parsed it back as the option
        "spotify" with "track:<id> = 250" smeared into its value, so the
        offset was never read again either.

    Spotify ids are base62, which is safe as an option name.
    """
    if not uri:
        return ""
    return uri.rsplit(":", 1)[-1]


def resolve_offset_ms(raw, global_ms):
    """Resolve a stored per-track offset string against the global default.

    `raw` is whatever came out of the [offsets] config section: "" (or None)
    when the track has no override, otherwise a signed integer as text. A
    value that won't parse falls back to the global rather than raising —
    a hand-edited statusify.cfg must not be able to break playback.
    """
    if raw is None or raw == "":
        return global_ms
    try:
        return int(raw)
    except (TypeError, ValueError):
        return global_ms


def select_line(mode, synced, plain, pos_ms, duration_ms):
    """Return (current_line, next_line) for playback position `pos_ms`.

    `pos_ms` must ALREADY have the lyric offset applied by the caller — that
    is the whole point of taking it as a parameter. main.py previously
    inlined this and hardcoded the *global* delay, so the per-track offset
    never influenced which line was chosen.

    Synced mode picks the last line whose startMs has been reached. Plain
    mode has no timings at all, so it interpolates linearly across the track
    duration — crude, but it is the only signal available.
    """
    if mode == "synced" and synced:
        idx, found = 0, False
        for i, e in enumerate(synced):
            if e["startMs"] <= pos_ms:
                idx, found = i, True
            else:
                break
        if not found or synced[idx]["startMs"] > pos_ms:
            return "", ""
        cur = synced[idx]["words"]
        nxt = ""
        # Skip past immediate repeats so "next" is genuinely different text.
        for j in range(idx + 1, len(synced)):
            if synced[j]["words"] != cur:
                nxt = synced[j]["words"]
                break
        return cur, nxt

    if mode == "plain" and plain and duration_ms > 0:
        ratio = max(0.0, min(pos_ms / duration_ms, 1.0))
        i = min(int(ratio * len(plain)), len(plain) - 1)
        return plain[i], plain[min(i + 1, len(plain) - 1)]

    return "", ""


def join_lines(lines):
    parts = []
    for i, ln in enumerate(lines):
        if i == 0: parts.append(ln)
        else:
            sep = " " if parts[-1][-1] in ".!?;," else ", "
            parts.append(sep + ln)
    return "".join(parts)

def _calc_instrumental_gaps(synced, duration_ms):
    """Pre-calculate all instrumental gaps from a synced lyrics list.
    Returns a list of dicts sorted by startMs, ready for the RPC loop."""
    if not synced or duration_ms <= 0:
        return []
    gaps = []
    n = len(synced)

    # Whole-song average line duration — used as the outlier baseline.
    # Excludes the last line (no next line to measure against) and any
    # candidate gaps we're about to test so the baseline isn't polluted.
    all_gaps = []
    for i in range(n - 1):
        all_gaps.append(synced[i + 1]["startMs"] - synced[i]["startMs"])
    # Also include the intro gap and outro gap in the distribution
    if synced[0]["startMs"] > 0:
        all_gaps.append(synced[0]["startMs"])
    all_gaps.append(duration_ms - synced[-1]["startMs"])

    if not all_gaps:
        return []

    # Use median rather than mean — more robust to a few very long gaps
    # (which are the outliers we're trying to detect)
    sorted_gaps = sorted(all_gaps)
    mid = len(sorted_gaps) // 2
    if len(sorted_gaps) % 2 == 0:
        song_median = (sorted_gaps[mid - 1] + sorted_gaps[mid]) / 2
    else:
        song_median = sorted_gaps[mid]

    # A gap is instrumental if it is >= 2x the song's median line duration
    # AND at least 8 seconds long absolutely.
    THRESHOLD_MULT = 2.0
    THRESHOLD_ABS  = 8000

    # INTRO gap
    intro_gap = synced[0]["startMs"]
    if intro_gap >= THRESHOLD_ABS and intro_gap >= song_median * THRESHOLD_MULT:
        gaps.append({
            "startMs": 0,
            "endMs":   intro_gap,
            "gap_ms":  intro_gap,
            "key":     -2,
        })

    # MID gaps
    for i in range(n - 1):
        cur     = synced[i]
        nxt     = synced[i + 1]
        mid_gap = nxt["startMs"] - cur["startMs"]
        if mid_gap < THRESHOLD_ABS:
            continue
        if mid_gap < song_median * THRESHOLD_MULT:
            continue
        # Silence window: require at least 4s of actual silence after line is sung.
        # Approximate line duration as song_median (fair since we use whole-song stats).
        sung_end = cur["startMs"] + min(int(song_median), mid_gap - 1000)
        silence  = nxt["startMs"] - sung_end
        if silence < 4000:
            continue
        # Start showing 🎵 after 3s of the current line (enough to read it)
        gap_start = cur["startMs"] + 3000
        if gap_start >= nxt["startMs"]:
            continue
        gaps.append({
            "startMs": gap_start,
            "endMs":   nxt["startMs"],
            "gap_ms":  mid_gap,
            "key":     i,
        })

    # OUTRO gap
    last_start = synced[-1]["startMs"]
    outro_gap  = duration_ms - last_start
    if outro_gap >= THRESHOLD_ABS and outro_gap >= song_median * THRESHOLD_MULT:
        gap_start = last_start + min(int(song_median), outro_gap - 1000)
        if gap_start < duration_ms:
            gaps.append({
                "startMs": gap_start,
                "endMs":   duration_ms,
                "gap_ms":  outro_gap,
                "key":     -3,
            })

    gaps.sort(key=lambda g: g["startMs"])
    return gaps
