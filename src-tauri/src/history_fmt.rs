//! Text and grouping for the History and Stats pages: the pure helpers of
//! statusify_ui_history.py / statusify_ui_stats.py / statusify_history.py.
//! Every function takes `now`, so tests never depend on the clock.

use chrono::{Datelike, Duration, NaiveDate, NaiveDateTime, Timelike};

/// A longer pause between two plays starts a new listening session.
pub const SESSION_GAP_MIN: i64 = 30;

/// Parse a stored local ISO-8601 timestamp (as Python's fromisoformat does).
pub fn parse(played_at: &str) -> Option<NaiveDateTime> {
    let s = played_at.trim();
    for f in ["%Y-%m-%dT%H:%M:%S%.f", "%Y-%m-%d %H:%M:%S%.f", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"] {
        if let Ok(t) = NaiveDateTime::parse_from_str(s, f) {
            return Some(t);
        }
    }
    NaiveDate::parse_from_str(s, "%Y-%m-%d").ok().and_then(|d| d.and_hms_opt(0, 0, 0))
}

fn hm(t: &NaiveDateTime) -> String {
    format!("{:02}:{:02}", t.hour(), t.minute())
}

/// "Mon 21 Sep" for a date.
fn short_date(d: NaiveDate) -> String {
    format!("{} {} {}", d.format("%a"), d.day(), d.format("%b"))
}

/// "14:40" for today, "Sep 21 · 14:40" this year, else "2025-09-21 · 14:40".
pub fn display_time(played_at: &str, now: NaiveDateTime) -> String {
    let Some(t) = parse(played_at) else { return played_at.to_string() };
    if t.date() == now.date() {
        hm(&t)
    } else if t.year() == now.year() {
        format!("{} {} · {}", t.format("%b"), t.day(), hm(&t))
    } else {
        format!("{} · {}", t.format("%Y-%m-%d"), hm(&t))
    }
}

/// "Today", "Yesterday", "Mon 21 Sep", "Mon 21 Sep 2025".
pub fn day_label(day: NaiveDate, today: NaiveDate) -> String {
    match (today - day).num_days() {
        0 => "Today".into(),
        1 => "Yesterday".into(),
        _ => {
            let s = short_date(day);
            if day.year() == today.year() { s } else { format!("{s} {}", day.year()) }
        }
    }
}

/// "52 min", "1 h 12 min", "2 h".
pub fn fmt_total(ms: i64) -> String {
    let m = ms.max(0) / 60_000;
    if m < 60 {
        return format!("{m} min");
    }
    let (h, m) = (m / 60, m % 60);
    if m > 0 { format!("{h} h {m} min") } else { format!("{h} h") }
}

/// Listening sessions among play start times: a gap over SESSION_GAP_MIN
/// between two plays starts a new one.
pub fn count_sessions(times: &[Option<NaiveDateTime>]) -> usize {
    let mut ts: Vec<NaiveDateTime> = times.iter().flatten().copied().collect();
    ts.sort();
    if ts.is_empty() {
        return 0;
    }
    1 + ts.windows(2).filter(|w| w[1] - w[0] > Duration::minutes(SESSION_GAP_MIN)).count()
}

/// "2 sessions · 1 h 12 min" (the time only once some was recorded).
pub fn day_summary(sessions: usize, listened_ms: i64) -> String {
    let mut s = format!("{sessions} session{}", if sessions != 1 { "s" } else { "" });
    if listened_ms >= 60_000 {
        s.push_str(&format!(" · {}", fmt_total(listened_ms)));
    }
    s
}

/// "Synced", "Plain" or None for a play's lyrics.
pub fn lyric_badge(synced_n: i64, plain_n: i64) -> Option<&'static str> {
    if synced_n > 0 {
        Some("Synced")
    } else if plain_n > 0 {
        Some("Plain")
    } else {
        None
    }
}

fn with_commas(n: i64) -> String {
    let s = n.abs().to_string();
    let mut out = String::new();
    for (i, c) in s.chars().enumerate() {
        if i > 0 && (s.len() - i) % 3 == 0 {
            out.push(',');
        }
        out.push(c);
    }
    if n < 0 { format!("-{out}") } else { out }
}

/// "1,284 plays · since 3 Aug".
pub fn plays_caption(n: i64, first_played: Option<&str>, today: NaiveDate) -> String {
    let mut s = format!("{} play{}", with_commas(n), if n != 1 { "s" } else { "" });
    if let (Some(t), true) = (first_played.and_then(parse), n > 0) {
        let d = t.date();
        s.push_str(&format!(" · since {} {}", d.day(), d.format("%b")));
        if d.year() != today.year() {
            s.push_str(&format!(" {}", d.year()));
        }
    }
    s
}

/// "Just now", "3 min ago", "Today 14:05", "Yesterday 21:14", "Mon 21:14",
/// "14 Sep", "14 Sep 2025".
pub fn rel_time(played_at: &str, now: NaiveDateTime) -> String {
    let Some(t) = parse(played_at) else { return played_at.to_string() };
    let secs = (now - t).num_seconds();
    if (0..60).contains(&secs) {
        return "Just now".into();
    }
    if (0..3600).contains(&secs) {
        return format!("{} min ago", secs / 60);
    }
    let days = (now.date() - t.date()).num_days();
    match days {
        0 => format!("Today {}", hm(&t)),
        1 => format!("Yesterday {}", hm(&t)),
        2..=6 => format!("{} {}", t.format("%a"), hm(&t)),
        _ if t.year() == now.year() => format!("{} {}", t.day(), t.format("%b")),
        _ => format!("{} {} {}", t.day(), t.format("%b"), t.year()),
    }
}

/// "3:07" for a play's listened time; "" when nothing was recorded.
pub fn fmt_duration(ms: i64) -> String {
    let s = ms.max(0) / 1000;
    if s <= 0 {
        return String::new();
    }
    let (h, rem) = (s / 3600, s % 3600);
    let (m, s) = (rem / 60, rem % 60);
    if h > 0 { format!("{h}:{m:02}:{s:02}") } else { format!("{m}:{s:02}") }
}

/// "September 2026".
pub fn month_name(year: i32, month: u32) -> String {
    NaiveDate::from_ymd_opt(year, month.clamp(1, 12), 1)
        .map(|d| format!("{} {year}", d.format("%B")))
        .unwrap_or_default()
}

/// A day of plays for the History list.
#[derive(Debug, PartialEq)]
pub struct Group<T> {
    pub day: NaiveDate,
    pub label: String,
    pub entries: Vec<T>,
    pub sessions: usize,
    pub listened_ms: i64,
}

/// Entries (newest first) grouped by local day, newest day first. Sorted by
/// when it was played, whatever order the rows came in (an imported
/// history's ids needn't follow its timestamps).
pub fn group_by_day<T>(
    entries: Vec<T>,
    now: NaiveDateTime,
    played_at: impl Fn(&T) -> &str,
    listened_ms: impl Fn(&T) -> i64,
) -> Vec<Group<T>> {
    let mut items: Vec<(NaiveDateTime, Option<NaiveDateTime>, T)> = entries
        .into_iter()
        .map(|e| {
            let t = parse(played_at(&e));
            (t.unwrap_or(now), t, e)
        })
        .collect();
    items.sort_by(|a, b| b.0.cmp(&a.0)); // stable: ties keep the query order
    let mut groups: Vec<(Group<T>, Vec<Option<NaiveDateTime>>)> = Vec::new();
    for (key, t, e) in items {
        let day = key.date();
        let ms = listened_ms(&e).max(0);
        match groups.iter_mut().find(|(g, _)| g.day == day) {
            Some((g, times)) => {
                g.entries.push(e);
                g.listened_ms += ms;
                times.push(t);
            }
            None => groups.push((
                Group { day, label: day_label(day, now.date()), entries: vec![e], sessions: 0, listened_ms: ms },
                vec![t],
            )),
        }
    }
    groups
        .into_iter()
        .map(|(mut g, times)| {
            g.sessions = count_sessions(&times);
            g
        })
        .collect()
}

/// The sheet's meta line: "Played Today 14:05 · Synced · 42 lines".
pub fn meta_line(played_at: &str, synced_n: i64, plain_n: i64, today: NaiveDate) -> String {
    let mut parts: Vec<String> = Vec::new();
    if let Some(t) = parse(played_at) {
        parts.push(format!("Played {} {}", day_label(t.date(), today), hm(&t)));
    }
    parts.push(lyric_badge(synced_n, plain_n).unwrap_or("No lyrics").to_string());
    let n = if synced_n > 0 { synced_n } else { plain_n };
    if n > 0 {
        parts.push(format!("{n} line{}", if n != 1 { "s" } else { "" }));
    }
    parts.join(" · ")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn dt(y: i32, mo: u32, d: u32, h: u32, mi: u32) -> NaiveDateTime {
        NaiveDate::from_ymd_opt(y, mo, d).unwrap().and_hms_opt(h, mi, 0).unwrap()
    }
    fn iso(t: NaiveDateTime) -> String {
        t.format("%Y-%m-%dT%H:%M:%S").to_string()
    }
    const NOW: fn() -> NaiveDateTime = || dt(2026, 9, 23, 20, 0);

    #[test]
    fn display_time_variants() {
        let now = dt(2026, 9, 22, 20, 0);
        assert_eq!(display_time("2026-09-22T14:40:00", now), "14:40");
        assert_eq!(display_time("2026-09-21T14:40:00", now), "Sep 21 · 14:40");
        assert_eq!(display_time("2025-09-21T14:40:00", now), "2025-09-21 · 14:40");
        assert_eq!(display_time("14:40", now), "14:40");
    }

    #[test]
    fn day_labels() {
        let today = NOW().date();
        assert_eq!(day_label(today, today), "Today");
        assert_eq!(day_label(today - Duration::days(1), today), "Yesterday");
        assert_eq!(day_label(NaiveDate::from_ymd_opt(2026, 9, 21).unwrap(), today), "Mon 21 Sep");
        assert_eq!(day_label(NaiveDate::from_ymd_opt(2025, 9, 21).unwrap(), today), "Sun 21 Sep 2025");
    }

    struct E(String, i64);
    fn ago(minutes: i64) -> E {
        E(iso(NOW() - Duration::minutes(minutes)), 180_000)
    }
    fn group(es: Vec<E>) -> Vec<Group<E>> {
        group_by_day(es, NOW(), |e| &e.0, |e| e.1)
    }

    #[test]
    fn group_by_day_newest_first_with_sessions_and_time() {
        let g = group(vec![ago(5), ago(10), ago(120), ago(60 * 24), ago(60 * 24 + 5)]);
        assert_eq!(g.iter().map(|x| x.label.as_str()).collect::<Vec<_>>(), ["Today", "Yesterday"]);
        assert_eq!(g[0].entries.len(), 3);
        assert_eq!(g[0].sessions, 2); // 110 minutes between 120 and 10
        assert_eq!(g[0].listened_ms, 3 * 180_000);
        assert_eq!(day_summary(g[0].sessions, g[0].listened_ms), "2 sessions · 9 min");
        assert_eq!(day_summary(g[1].sessions, g[1].listened_ms), "1 session · 6 min");
    }

    #[test]
    fn group_by_day_sorts_by_time_not_input_order() {
        let g = group(vec![ago(60 * 24), ago(5), ago(60 * 24 * 3), ago(1)]);
        assert_eq!(g.iter().map(|x| x.label.as_str()).collect::<Vec<_>>(), ["Today", "Yesterday", "Sun 20 Sep"]);
        let mins: Vec<_> = g[0].entries.iter().map(|e| e.0.clone()).collect();
        assert_eq!(mins, [iso(NOW() - Duration::minutes(1)), iso(NOW() - Duration::minutes(5))]);
    }

    #[test]
    fn sessions_and_totals() {
        assert_eq!(count_sessions(&[]), 0);
        let t = NOW();
        assert_eq!(count_sessions(&[Some(t), Some(t + Duration::minutes(29))]), 1);
        assert_eq!(count_sessions(&[Some(t), Some(t + Duration::minutes(31))]), 2);
        assert_eq!(fmt_total(52 * 60_000), "52 min");
        assert_eq!(fmt_total(72 * 60_000), "1 h 12 min");
        assert_eq!(fmt_total(120 * 60_000), "2 h");
    }

    #[test]
    fn badge_and_caption() {
        assert_eq!(lyric_badge(3, 0), Some("Synced"));
        assert_eq!(lyric_badge(0, 2), Some("Plain"));
        assert_eq!(lyric_badge(0, 0), None);
        let today = NOW().date();
        assert_eq!(plays_caption(1284, Some("2026-08-03T10:00:00"), today), "1,284 plays · since 3 Aug");
        assert_eq!(plays_caption(1, Some("2025-08-03T10:00:00"), today), "1 play · since 3 Aug 2025");
        assert_eq!(plays_caption(0, None, today), "0 plays");
        assert_eq!(plays_caption(1_234_567, None, today), "1,234,567 plays");
    }

    #[test]
    fn relative_time() {
        let now = dt(2026, 9, 23, 15, 0);
        let ago = |s: i64| iso(now - Duration::seconds(s));
        assert_eq!(rel_time(&ago(20), now), "Just now");
        assert_eq!(rel_time(&ago(180), now), "3 min ago");
        assert_eq!(rel_time(&ago(5 * 3600), now), "Today 10:00");
        assert_eq!(rel_time("2026-09-22T21:14:00", now), "Yesterday 21:14");
        assert_eq!(rel_time("2026-09-19T08:05:00", now), "Sat 08:05");
        assert_eq!(rel_time("2026-09-14T08:05:00", now), "14 Sep");
        assert_eq!(rel_time("2025-09-14T08:05:00", now), "14 Sep 2025");
        assert_eq!(rel_time("garbage", now), "garbage");
    }

    #[test]
    fn durations_and_names() {
        assert_eq!(fmt_duration(0), "");
        assert_eq!(fmt_duration(187_000), "3:07");
        assert_eq!(fmt_duration(3_725_000), "1:02:05");
        assert_eq!(month_name(2026, 9), "September 2026");
    }

    #[test]
    fn meta() {
        let today = NOW().date();
        assert_eq!(meta_line("2026-09-23T14:05:00", 42, 0, today), "Played Today 14:05 · Synced · 42 lines");
        assert_eq!(meta_line("2026-09-22T09:00:00", 0, 1, today), "Played Yesterday 09:00 · Plain · 1 line");
        assert_eq!(meta_line("2026-09-22T09:00:00", 0, 0, today), "Played Yesterday 09:00 · No lyrics");
    }
}
