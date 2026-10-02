//! Decide which lyric lines reach Discord, and when, inside the rate limit.
//! A faithful port of statusify_presence_plan.py (same constants, same
//! search, same tie-breaking order).
//!
//! Discord accepts 5 SET_ACTIVITY frames per 20 s. A dense verse wants more
//! than that, so frames spent greedily on slow lines early in the window are
//! missing when a fast passage arrives. The synced sheet tells us the future:
//! every publish decision re-plans the next PLAN_HORIZON_MS of song with a
//! small beam search over (which lines go in each update, when it is sent)
//! under the sliding-window limit, minimising the sung time the presence
//! spends showing the wrong line. On the Python app's replay of a real
//! history.db this halved the off-screen time of the greedy loop (2.5 % ->
//! 1.1 %).
//!
//! Pure: handed the units, the position, the ledger of recent frames and
//! what Discord is showing.

use crate::lyrics::{Gap, Lyrics};
use std::rc::Rc;

/// Sentinel "texts" for the non-lyric presences, so what is on screen can be
/// tracked as one set of strings. They can never collide with a lyric.
pub const TITLE: &str = "\u{0}title";
pub const GAP: &str = "\u{0}gap";

pub const PLAN_HORIZON_MS: f64 = 30000.0;
/// Cost of each extra line packed into an update, in ms of wrong-line time.
/// Just enough to prefer one line at a time whenever the budget allows.
pub const EXTRA_LINE_COST: f64 = 50.0;
/// Cost of a line never shown at all, on top of the time it spent missing.
pub const MISSED_LINE_COST: f64 = 1000.0;
/// A late or missing instrumental marker / title-only presence matters less
/// than a wrong lyric: the old lyric merely lingers.
pub const GAP_WEIGHT: f64 = 0.5;
pub const TITLE_WEIGHT: f64 = 1.0;
pub const MAX_GROUP: usize = 6;
pub const BEAM: usize = 8;
/// Don't raise the instrumental marker in the gap's last second.
pub const GAP_TAIL_MS: f64 = 1000.0;
const NEG: f64 = -1e18;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    Line,
    Gap,
    Title,
    /// An empty lyric line: nothing to publish, whatever shows may stay.
    Blank,
}

/// A stretch of song and what the presence should say during it.
#[derive(Clone, Debug, PartialEq)]
pub struct Unit {
    pub kind: Kind,
    pub start: i64,
    pub end: i64,
    pub text: String,
    /// The line's index in the synced/plain list.
    pub idx: Option<usize>,
}

/// One planned SET_ACTIVITY. `t` is when to send it (lyric-time ms);
/// start..end the stretch of song it is for (past `end` it is pointless).
#[derive(Clone, Debug, PartialEq)]
pub struct Publish {
    pub t: f64,
    pub kind: Kind,
    pub lines: Vec<String>,
    pub start: i64,
    pub end: i64,
    pub first: Option<usize>,
    pub last: Option<usize>,
}

/// What the presence displays once `p` has been sent.
pub fn shown_for(p: &Publish) -> Vec<String> {
    match p.kind {
        Kind::Line => {
            let mut v = p.lines.clone();
            v.sort();
            v.dedup();
            v
        }
        Kind::Gap => vec![GAP.to_string()],
        _ => vec![TITLE.to_string()],
    }
}

fn unit(kind: Kind, start: i64, end: i64, text: &str, idx: Option<usize>) -> Unit {
    Unit { kind, start, end, text: text.to_string(), idx }
}

/// Cut the track into Units, in order, covering it without overlap: a synced
/// line from its start to the next line's; a plain line over its equal share
/// of the track; instrumental gaps cut into the line they start in; and
/// title-only wherever there is no lyric.
pub fn build_units(l: &Lyrics, duration_ms: i64, gaps: &[Gap]) -> Vec<Unit> {
    let dur = duration_ms.max(0);
    let mut units = Vec::new();
    if l.mode == "synced" && !l.synced.is_empty() {
        let s = &l.synced;
        for (i, e) in s.iter().enumerate() {
            let end = if i + 1 < s.len() { s[i + 1].start_ms } else { dur.max(e.start_ms + 5000) };
            let k = if e.words.is_empty() { Kind::Blank } else { Kind::Line };
            units.push(unit(k, e.start_ms, end, &e.words, Some(i)));
        }
        if units[0].start > 0 {
            let first = units[0].start;
            units.insert(0, unit(Kind::Title, 0, first, TITLE, None));
        }
    } else if l.mode == "plain" && !l.plain.is_empty() && dur > 0 {
        let n = l.plain.len() as i64;
        for (i, w) in l.plain.iter().enumerate() {
            let i = i as i64;
            let k = if w.is_empty() { Kind::Blank } else { Kind::Line };
            units.push(unit(k, i * dur / n, (i + 1) * dur / n, w, Some(i as usize)));
        }
    } else {
        return vec![unit(Kind::Title, 0, if dur > 0 { dur } else { 1_000_000_000_000 }, TITLE, None)];
    }

    let mut gs: Vec<&Gap> = gaps.iter().collect();
    gs.sort_by_key(|g| g.start_ms);
    for g in gs {
        let (a, b) = (g.start_ms, g.end_ms);
        let mut out = Vec::new();
        for u in units {
            if u.end <= a || u.start >= b {
                out.push(u);
                continue;
            }
            // A unit overlapping the gap keeps only what lies outside it.
            if u.start < a {
                out.push(Unit { end: a, ..u.clone() });
            }
            if u.end > b {
                out.push(Unit { start: b, ..u });
            }
        }
        out.push(unit(Kind::Gap, a, b, GAP, None));
        out.sort_by_key(|u| u.start); // stable, like Python's sorted
        units = out;
    }
    units.retain(|u| u.end > u.start);
    units
}

/// Limits for `plan`.
#[derive(Clone, Copy, Debug)]
pub struct Limits {
    pub calls: usize,
    pub window_ms: f64,
    pub max_state: usize,
    pub horizon_ms: f64,
}

impl Default for Limits {
    fn default() -> Self {
        Limits { calls: 5, window_ms: 20000.0, max_state: 128, horizon_ms: PLAN_HORIZON_MS }
    }
}

struct Node {
    prev: Option<Rc<Node>>,
    ev: Publish,
}

#[derive(Clone)]
struct St {
    cost: f64,
    h: Rc<Vec<f64>>,
    d: Rc<Vec<u32>>, // sorted text ids on screen
    back: Option<Rc<Node>>,
}

type Layer = Vec<(Vec<u32>, Vec<St>)>; // insertion-ordered, like a Python dict

fn intersect(d: &[u32], ahead: &[u32]) -> Vec<u32> {
    d.iter().copied().filter(|x| ahead.binary_search(x).is_ok()).collect()
}

fn le_all(a: &[f64], b: &[f64]) -> bool {
    a.iter().zip(b).all(|(x, y)| x <= y)
}

fn push(layers: &mut [Option<Layer>], ahead: &[Vec<u32>], u: usize, st: St) {
    let key = intersect(&st.d, &ahead[u]);
    let layer = layers[u].get_or_insert_with(Vec::new);
    let Some(pos) = layer.iter().position(|(k, _)| *k == key) else {
        layer.push((key, vec![st]));
        return;
    };
    let lst = &mut layer[pos].1;
    let (c, h) = (st.cost, st.h.clone());
    if lst.iter().any(|o| o.cost <= c && le_all(&o.h, &h)) {
        return;
    }
    lst.retain(|o| !(c <= o.cost && le_all(&h, &o.h)));
    lst.push(st);
    if lst.len() > BEAM {
        lst.sort_by(|a, b| a.cost.partial_cmp(&b.cost).unwrap());
        lst.truncate(BEAM);
    }
}

/// Plan the presence updates for the next `horizon_ms` of song.
///
/// pos_ms      current lyric-time position
/// history_ms  send times of recent frames, same clock (may be < 0)
/// shown       what Discord displays now (texts; TITLE / GAP sentinels)
/// gate_ms     nothing may be sent before this (the track-change settle time)
///
/// Returns Publishes in send order. The caller acts on the first and
/// re-plans after it: the future is only a forecast.
///
/// The search walks the units in order; at each one a state either leaves
/// the display alone or sends an update starting there, packing 1..MAX_GROUP
/// following lines into the 128-character state. An update goes out as soon
/// as its first line starts, or when the oldest of the last `calls` frames
/// leaves the window, whichever is later. States are kept per (next unit,
/// on-screen texts that can still recur); within one, a state that is no
/// costlier and has spent no later frames dominates; at most BEAM survive.
pub fn plan(units: &[Unit], pos_ms: f64, history_ms: &[f64], shown: &[String], gate_ms: f64, lim: Limits) -> Vec<Publish> {
    let mut u0 = 0;
    while u0 < units.len() && (units[u0].end as f64) <= pos_ms {
        u0 += 1;
    }
    let horizon_end = pos_ms + lim.horizon_ms;
    let mut n = u0;
    while n < units.len() && (units[n].start as f64) < horizon_end {
        n += 1;
    }
    if u0 >= n {
        return vec![];
    }

    // Intern the texts so on-screen sets are small sorted id lists.
    let mut names: Vec<&str> = Vec::new();
    let tid: Vec<u32> = units[..n]
        .iter()
        .map(|u| match names.iter().position(|x| *x == u.text) {
            Some(i) => i as u32,
            None => {
                names.push(&u.text);
                (names.len() - 1) as u32
            }
        })
        .collect();
    let start: Vec<f64> = units[..n].iter().map(|u| (u.start as f64).max(pos_ms)).collect();
    let end: Vec<f64> = units[..n].iter().map(|u| u.end as f64).collect();
    let kind: Vec<Kind> = units[..n].iter().map(|u| u.kind).collect();
    let tlen: Vec<i64> = units[..n].iter().map(|u| u.text.chars().count() as i64).collect();
    // join_lines separator in front of line i when it follows line i-1.
    let mut sep = vec![0i64; n];
    for i in 1..n {
        let p = &units[i - 1].text;
        sep[i] = if p.ends_with(['.', '!', '?', ';', ',']) { 1 } else { 2 };
    }
    // Texts still to come from unit i on.
    let mut ahead: Vec<Vec<u32>> = vec![Vec::new(); n + 1];
    for i in (u0..n).rev() {
        let mut a = ahead[i + 1].clone();
        if kind[i] != Kind::Blank {
            if let Err(p) = a.binary_search(&tid[i]) {
                a.insert(p, tid[i]);
            }
        }
        ahead[i] = a;
    }

    let mut hist: Vec<f64> = history_ms.to_vec();
    hist.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let keep = hist.len().saturating_sub(lim.calls);
    let hist = &hist[keep..];
    let mut h0 = vec![NEG; lim.calls - hist.len()];
    h0.extend_from_slice(hist);
    let mut d0: Vec<u32> = shown
        .iter()
        .filter_map(|s| names.iter().position(|x| x == s).map(|i| i as u32))
        .collect();
    d0.sort();
    d0.dedup();

    let mut layers: Vec<Option<Layer>> = (0..=n).map(|_| None).collect();
    layers[u0] = Some(vec![(intersect(&d0, &ahead[u0]), vec![St { cost: 0.0, h: Rc::new(h0), d: Rc::new(d0), back: None }])]);

    for u in u0..n {
        let Some(layer) = layers[u].take() else { continue };
        let (k, su, eu) = (kind[u], start[u], end[u]);
        for (_, lst) in layer {
            for st in lst {
                let on = st.d.binary_search(&tid[u]).is_ok();
                // 1) Leave the display alone through unit u.
                let stay = if k == Kind::Blank || on {
                    0.0
                } else if k == Kind::Line {
                    (eu - su) + MISSED_LINE_COST
                } else if k == Kind::Gap {
                    GAP_WEIGHT * (eu - su).min(8000.0)
                } else {
                    TITLE_WEIGHT * (eu - su)
                };
                push(&mut layers, &ahead, u + 1, St { cost: st.cost + stay, ..st.clone() });
                if k == Kind::Blank {
                    continue;
                }
                // 2) Send an update that starts with unit u. It must still be
                // current when it lands.
                let h = &st.h;
                let t = su.max(h[0] + lim.window_ms).max(h[h.len() - 1]).max(gate_ms);
                if t >= eu - if k == Kind::Gap { GAP_TAIL_MS } else { 0.0 } {
                    continue;
                }
                let mut h2v = h[1..].to_vec();
                h2v.push(t);
                let h2 = Rc::new(h2v);
                if k != Kind::Line {
                    let w = if k == Kind::Gap { GAP_WEIGHT } else { TITLE_WEIGHT };
                    let ev = Publish { t, kind: k, lines: vec![], start: units[u].start, end: units[u].end, first: None, last: None };
                    let d = Rc::new(vec![tid[u]]);
                    let back = Some(Rc::new(Node { prev: st.back.clone(), ev }));
                    push(&mut layers, &ahead, u + 1, St { cost: st.cost + w * (t - su), h: h2, d, back });
                    continue;
                }
                // Only the first line can be late: the rest have not started.
                let late = if on { 0.0 } else { t - su };
                let mut length = -sep[u];
                let mut group: Vec<String> = Vec::new();
                let mut gids: Vec<u32> = Vec::new();
                for b in (u + 1)..=n.min(u + MAX_GROUP) {
                    let j = b - 1;
                    if kind[j] != Kind::Line {
                        break;
                    }
                    length += tlen[j] + sep[j];
                    if length > lim.max_state as i64 {
                        break;
                    }
                    group.push(units[j].text.clone());
                    if let Err(p) = gids.binary_search(&tid[j]) {
                        gids.insert(p, tid[j]);
                    }
                    let ev = Publish {
                        t,
                        kind: Kind::Line,
                        lines: group.clone(),
                        start: units[u].start,
                        end: units[j].end,
                        first: units[u].idx,
                        last: units[j].idx,
                    };
                    let back = Some(Rc::new(Node { prev: st.back.clone(), ev }));
                    let cost = st.cost + late + EXTRA_LINE_COST * (b - u - 1) as f64;
                    push(&mut layers, &ahead, b, St { cost, h: h2.clone(), d: Rc::new(gids.clone()), back });
                }
            }
        }
    }

    let Some(fin) = layers[n].take() else { return vec![] };
    let mut best: Option<&St> = None;
    for (_, lst) in &fin {
        for st in lst {
            if best.is_none_or(|b| st.cost < b.cost) {
                best = Some(st);
            }
        }
    }
    let mut out = Vec::new();
    let mut node = best.and_then(|b| b.back.clone());
    while let Some(nd) = node {
        out.push(nd.ev.clone());
        node = nd.prev.clone();
    }
    out.reverse();
    out
}

#[cfg(test)]
#[path = "core_plan_tests.rs"]
mod tests;
