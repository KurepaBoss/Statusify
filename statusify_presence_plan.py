"""Decide which lyric lines reach Discord, and when, inside the rate limit.

Discord accepts RATE_LIMIT_CALLS SET_ACTIVITY frames per RATE_LIMIT_WINDOW
(5 per 20 s). A dense verse wants more than that: 3.8 s lines of 70-90
characters cannot share the 128-character status, so each needs a frame of
its own, a little faster than the budget refills.

rpc_loop used to spend frames greedily — publish the moment a line starts,
pad the group out to ~4.75 s of song — and when the ledger ran dry it slept
and published whatever line was current by then. Replaying that loop over
every lyric sheet in a real history.db put the sung line off-screen for 2.5 %
of all lyric time. Part of that was a de-dupe bug (see rpc_loop). The rest was
the greed itself: a frame spent on a slow line early in the window is a frame
missing when a fast passage arrives ten seconds later.

The synced sheet tells us the future, so plan against it. Every publish
decision re-plans the next PLAN_HORIZON_MS of song with a small beam search
over (which lines go in each update, when it is sent) under the sliding-window
limit, minimising the sung time the presence spends showing the wrong line.
On the same replay that halves the off-screen time again (1.1 %).

Pure: no Tk, no sockets, no globals — it is handed the lyric sheet, the
playback position, the ledger of recent frames and what Discord is showing.
"""
from collections import namedtuple

from statusify_lyrics import join_lines

# Sentinel "texts" for the non-lyric presences, so what-is-on-screen can be
# tracked as one set of strings. They can never collide with a lyric.
TITLE = "\x00title"     # title/artist only, no lyric line
GAP   = "\x00gap"       # the instrumental marker

PLAN_HORIZON_MS = 30000
# Cost of each extra line packed into an update, in ms of wrong-line time.
# Just enough to prefer one line at a time whenever the budget allows it;
# the replay found larger values starve dense passages and smaller ones
# pack lines that did not need packing.
EXTRA_LINE_COST = 50.0
# Cost of a line never shown at all, on top of the time it spent missing.
# Without it the planner happily drops a whole line to be punctual on the
# next; with it, missed lines fell from 109 to 91 on the replay for the same
# overall punctuality.
MISSED_LINE_COST = 1000.0
# An instrumental marker or title-only presence that is late or missing
# matters less than a wrong lyric: the old lyric merely lingers.
GAP_WEIGHT   = 0.5
TITLE_WEIGHT = 1.0
MAX_GROUP    = 6
BEAM         = 8
# Don't raise the instrumental marker in the gap's last second.
GAP_TAIL_MS  = 1000

_NEG = -1e18

Unit = namedtuple("Unit", "kind start end text idx")
Unit.__doc__ = """A stretch of song and what the presence should say during it.

kind is "line", "gap", "title" or "blank" (an empty lyric line: nothing to
publish, whatever is showing may stay). idx is the line's index in the
synced/plain list, or None."""

Publish = namedtuple("Publish", "t kind lines start end first last")
Publish.__doc__ = """One planned SET_ACTIVITY.

t is when to send it (lyric-time ms, same clock as the units); lines the
lyric lines it carries; start..end the stretch of song it is for (so t-start
is how late the plan had to make it, and past `end` it is pointless);
first/last the line indices covered, None for a marker or title-only
presence."""


def shown_for(p):
    """What the presence displays once `p` has been sent."""
    if p.kind == "line":
        return frozenset(p.lines)
    return frozenset({GAP if p.kind == "gap" else TITLE})


def build_units(mode, synced, plain, duration_ms, gaps):
    """Cut the track into Units, in order, covering it without overlap.

    Mirrors what select_line and rpc_loop would show at each moment: a synced
    line from its startMs to the next line's; a plain line over its equal
    share of the track; instrumental gaps (from _calc_instrumental_gaps) cut
    into the line they start in; and title-only wherever there is no lyric
    — before the first line, or for the whole track when there are none."""
    dur = duration_ms if duration_ms and duration_ms > 0 else 0
    units = []
    if mode == "synced" and synced:
        for i, e in enumerate(synced):
            s = e["startMs"]
            end = synced[i + 1]["startMs"] if i + 1 < len(synced) else max(dur, s + 5000)
            units.append(Unit("line" if e["words"] else "blank", s, end, e["words"], i))
        if units[0].start > 0:
            units.insert(0, Unit("title", 0, units[0].start, TITLE, None))
    elif mode == "plain" and plain and dur:
        n = len(plain)
        for i, w in enumerate(plain):
            units.append(Unit("line" if w else "blank", i * dur // n, (i + 1) * dur // n, w, i))
    else:
        return [Unit("title", 0, dur or 10 ** 12, TITLE, None)]

    for g in sorted(gaps or (), key=lambda g: g["startMs"]):
        gs, ge = g["startMs"], g["endMs"]
        out = []
        for u in units:
            if u.end <= gs or u.start >= ge:
                out.append(u)
                continue
            # A unit overlapping the gap keeps only what lies outside it.
            if u.start < gs:
                out.append(u._replace(end=gs))
            if u.end > ge:
                out.append(u._replace(start=ge))
        out.append(Unit("gap", gs, ge, GAP, None))
        units = sorted(out, key=lambda u: u.start)
    return [u for u in units if u.end > u.start]


def plan(units, pos_ms, history_ms, shown, gate_ms, *, calls=5, window_ms=20000,
         max_state=128, horizon_ms=PLAN_HORIZON_MS):
    """Plan the presence updates for the next `horizon_ms` of song.

    pos_ms      current lyric-time position
    history_ms  send times of recent frames, on the same clock (may be < 0)
    shown       what Discord is displaying now (texts; TITLE / GAP sentinels)
    gate_ms     nothing may be sent before this (the track-change settle time)

    Returns Publishes in send order. The caller acts on the first and
    re-plans after it — the future is only a forecast; the ledger and the
    playback position are the truth.

    The search walks the units in order; at each one a state either leaves
    the display alone or sends an update starting there, packing 1..MAX_GROUP
    following lines into the 128-character state. An update goes out as soon
    as its first line starts, or when the oldest of the last `calls` frames
    leaves the window, whichever is later. Cost is the time a sung line spends
    off-screen (a repeated line counts as on-screen if the same words are
    showing), plus MISSED_LINE_COST per line never shown and EXTRA_LINE_COST
    per extra packed line. States are kept per (next unit, on-screen texts
    that can still recur); within one, a state that is no costlier and has
    spent no later frames dominates, and at most BEAM survive."""
    n = len(units)
    u0 = 0
    while u0 < n and units[u0].end <= pos_ms:
        u0 += 1
    lim = pos_ms + horizon_ms
    n = u0
    while n < len(units) and units[n].start < lim:
        n += 1
    if u0 >= n:
        return []

    start = [max(units[i].start, pos_ms) for i in range(n)]
    end = [units[i].end for i in range(n)]
    kind = [units[i].kind for i in range(n)]
    text = [units[i].text for i in range(n)]
    # join_lines separator in front of line i when it follows line i-1.
    sep = [0] * n
    for i in range(1, n):
        p = text[i - 1]
        sep[i] = 1 if (p and p[-1] in ".!?;,") else 2
    # Texts still to come from unit i on: what is on screen matters only
    # insofar as one of them may be sung while it is still showing.
    ahead = [frozenset()] * (n + 1)
    for i in range(n - 1, u0 - 1, -1):
        ahead[i] = ahead[i + 1] | {text[i]} if kind[i] != "blank" else ahead[i + 1]

    hist = sorted(history_ms)[-calls:]
    H0 = tuple([_NEG] * (calls - len(hist)) + hist)
    D0 = frozenset(shown or ())
    layers = [None] * (n + 1)
    # state: (cost, H = last `calls` send times, D = on-screen texts, back)
    layers[u0] = {D0 & ahead[u0]: [(0.0, H0, D0, None)]}

    def push(u, st):
        key = st[2] & ahead[u]
        layer = layers[u]
        if layer is None:
            layer = layers[u] = {}
        lst = layer.get(key)
        if lst is None:
            layer[key] = [st]
            return
        c, H = st[0], st[1]
        for o in lst:
            if o[0] <= c and all(a <= b for a, b in zip(o[1], H)):
                return
        lst[:] = [o for o in lst if not (c <= o[0] and all(a <= b for a, b in zip(H, o[1])))]
        lst.append(st)
        if len(lst) > BEAM:
            lst.sort(key=lambda x: x[0])
            del lst[BEAM:]

    for u in range(u0, n):
        layer, layers[u] = layers[u], None
        if not layer:
            continue
        k, su, eu = kind[u], start[u], end[u]
        for lst in layer.values():
            for cost, H, D, back in lst:
                # 1) Leave the display alone through unit u.
                if k == "blank" or text[u] in D:
                    stay = 0.0
                elif k == "line":
                    stay = (eu - su) + MISSED_LINE_COST
                else:
                    stay = (GAP_WEIGHT * min(eu - su, 8000) if k == "gap"
                            else TITLE_WEIGHT * (eu - su))
                push(u + 1, (cost + stay, H, D, back))
                if k == "blank":
                    continue
                # 2) Send an update that starts with unit u. It must still be
                # current when it lands: leading with lines that have already
                # been sung would dodge MISSED_LINE_COST without anyone ever
                # seeing them in time.
                t = max(su, H[0] + window_ms, H[-1], gate_ms)
                if t >= eu - (GAP_TAIL_MS if k == "gap" else 0):
                    continue
                H2 = H[1:] + (t,)
                if k != "line":
                    w = GAP_WEIGHT if k == "gap" else TITLE_WEIGHT
                    ev = Publish(t, k, (), units[u].start, eu, None, None)
                    push(u + 1, (cost + w * (t - su), H2, shown_for(ev), (back, ev)))
                    continue
                # Only the first line can be late: the rest have not started.
                late = 0.0 if text[u] in D else t - su
                length = -sep[u]
                group = []
                for b in range(u + 1, min(n, u + MAX_GROUP) + 1):
                    j = b - 1
                    if kind[j] != "line":
                        break
                    length += len(text[j]) + sep[j]
                    if length > max_state:
                        break
                    group.append(text[j])
                    ev = Publish(t, "line", tuple(group), units[u].start, end[j],
                                 units[u].idx, units[j].idx)
                    push(b, (cost + late + EXTRA_LINE_COST * (b - u - 1), H2,
                             frozenset(group), (back, ev)))

    final = layers[n]
    if not final:
        return []
    best = min((st for lst in final.values() for st in lst), key=lambda st: st[0])
    out = []
    node = best[3]
    while node:
        node, ev = node
        out.append(ev)
    out.reverse()
    return out
