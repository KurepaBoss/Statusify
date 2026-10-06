//! The planner against vectors produced by the Python original
//! (statusify_presence_plan.py + _calc_instrumental_gaps): same units, same
//! gaps, same publishes for random sheets, positions, ledgers and screens.

use super::*;
use crate::lyrics::{instrumental_gaps, Line, Lyrics};
use serde_json::Value;

fn kind(s: &str) -> Kind {
    match s {
        "line" => Kind::Line,
        "gap" => Kind::Gap,
        "title" => Kind::Title,
        _ => Kind::Blank,
    }
}

#[test]
fn matches_python_planner() {
    let cases: Vec<Value> = serde_json::from_str(include_str!("core_plan_vectors.json")).unwrap();
    let mut checked = 0;
    for (ci, c) in cases.iter().enumerate() {
        let synced: Vec<Line> = serde_json::from_value(c["synced"].clone()).unwrap();
        let plain: Vec<String> = serde_json::from_value(c["plain"].clone()).unwrap();
        let l = Lyrics { mode: c["mode"].as_str().unwrap().into(), synced, plain, source: String::new() };
        let dur = c["duration"].as_i64().unwrap();
        let gaps = if l.mode == "synced" { instrumental_gaps(&l.synced, dur) } else { vec![] };
        let want_gaps: Vec<Value> = c["gaps"].as_array().unwrap().clone();
        assert_eq!(serde_json::to_value(&gaps).unwrap(), Value::Array(want_gaps), "case {ci}: gaps");
        let units = build_units(&l, dur, &gaps);
        let want: Vec<Unit> = c["units"]
            .as_array()
            .unwrap()
            .iter()
            .map(|u| Unit {
                kind: kind(u["kind"].as_str().unwrap()),
                start: u["start"].as_i64().unwrap(),
                end: u["end"].as_i64().unwrap(),
                text: u["text"].as_str().unwrap().into(),
                idx: u["idx"].as_u64().map(|x| x as usize),
            })
            .collect();
        assert_eq!(units, want, "case {ci}: units");
        for (pi, p) in c["plans"].as_array().unwrap().iter().enumerate() {
            let hist: Vec<f64> = p["hist"].as_array().unwrap().iter().map(|x| x.as_f64().unwrap()).collect();
            let shown: Vec<String> = serde_json::from_value(p["shown"].clone()).unwrap();
            let got = plan(&units, p["pos"].as_f64().unwrap(), &hist, &shown, p["gate"].as_f64().unwrap(), Limits::default());
            let exp = p["out"].as_array().unwrap();
            assert_eq!(got.len(), exp.len(), "case {ci} plan {pi}: count\n{got:#?}\n{exp:#?}");
            for (g, e) in got.iter().zip(exp) {
                assert!((g.t - e["t"].as_f64().unwrap()).abs() < 1e-6, "case {ci} plan {pi}: t");
                assert_eq!(g.kind, kind(e["kind"].as_str().unwrap()));
                let lines: Vec<String> = serde_json::from_value(e["lines"].clone()).unwrap();
                assert_eq!(g.lines, lines, "case {ci} plan {pi}");
                assert_eq!((g.start, g.end), (e["start"].as_i64().unwrap(), e["end"].as_i64().unwrap()));
                assert_eq!(g.first, e["first"].as_u64().map(|x| x as usize));
                assert_eq!(g.last, e["last"].as_u64().map(|x| x as usize));
                checked += 1;
            }
        }
    }
    assert!(checked > 300, "only {checked} publishes compared");
}

fn sheet(lines: &[(i64, &str)]) -> Lyrics {
    Lyrics {
        mode: "synced".into(),
        synced: lines.iter().map(|&(s, w)| Line { start_ms: s, words: w.into() }).collect(),
        plain: vec![],
        source: String::new(),
    }
}

#[test]
fn slow_lines_go_out_one_at_a_time_on_their_start() {
    let l = sheet(&[(1000, "a"), (6000, "b"), (11000, "c")]);
    let units = build_units(&l, 20000, &[]);
    assert_eq!(units[0].kind, Kind::Title);
    let p = plan(&units, 1000.0, &[], &[], 1000.0, Limits::default());
    let firsts: Vec<(f64, Vec<String>)> = p.iter().map(|e| (e.t, e.lines.clone())).collect();
    assert_eq!(firsts[0], (1000.0, vec!["a".to_string()]));
    assert_eq!(firsts[1], (6000.0, vec!["b".to_string()]));
}

#[test]
fn a_full_ledger_packs_lines_or_waits() {
    // Five frames in the last second: nothing can go out for ~19 s, so the
    // first update carries several lines at once.
    let l = sheet(&[(0, "one"), (2000, "two"), (4000, "three"), (6000, "four"), (30000, "five")]);
    let units = build_units(&l, 40000, &[]);
    let hist = [-1000.0, -800.0, -600.0, -400.0, -200.0];
    let p = plan(&units, 0.0, &hist, &[], 0.0, Limits::default());
    assert!(p[0].t >= 19000.0 - 1e-9);
    assert!(p.iter().all(|e| e.kind != Kind::Line || e.t < e.end as f64));
}

#[test]
fn whats_on_screen_is_not_resent() {
    let l = sheet(&[(0, "same"), (5000, "same")]);
    let units = build_units(&l, 10000, &[]);
    let p = plan(&units, 0.0, &[], &["same".to_string()], 0.0, Limits::default());
    assert!(p.is_empty());
}

#[test]
fn no_lyrics_is_one_title_publish() {
    let units = build_units(&Lyrics::none(), 100000, &[]);
    assert_eq!(units.len(), 1);
    let p = plan(&units, 0.0, &[], &[], 1500.0, Limits::default());
    assert_eq!(p.len(), 1);
    assert_eq!(p[0].kind, Kind::Title);
    assert_eq!(p[0].t, 1500.0);
    assert_eq!(shown_for(&p[0]), vec![TITLE.to_string()]);
}
