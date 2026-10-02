//! Pure geometry for the mini player and the desktop overlay (no Tauri, no
//! Win32), ported from statusify_ui_mini.py / statusify_ui_overlay.py so it
//! can be tested headless. All values are physical pixels unless noted.

use regex::Regex;
use std::sync::LazyLock;

/// A monitor work area: (left, top, right, bottom).
pub type Area = (i32, i32, i32, i32);

pub const SNAP_THRESHOLD: i32 = 24; // magnetic range of an edge
pub const SNAP_MARGIN: i32 = 12; // gap kept from a snapped edge

pub const DEFAULT_OVERLAY_SIZE: i64 = 30;
pub const MIN_OVERLAY_SIZE: i64 = 16;
pub const MAX_OVERLAY_SIZE: i64 = 72;

pub fn ease_out(p: f64) -> f64 {
    let p = p.clamp(0.0, 1.0);
    1.0 - (1.0 - p).powi(3)
}

/// Where a w x h window dropped at (x, y) should settle inside `area`.
///
/// Each axis snaps on its own to the near edge, the far edge or (x only) the
/// centre when within `threshold` px of it, so a drop near a corner lands in
/// the corner. Being dragged past an edge counts as near it. Whatever
/// happens, the window ends up fully on the monitor.
pub fn snap_position(x: i32, y: i32, w: i32, h: i32, area: Area, threshold: i32, margin: i32) -> (i32, i32) {
    let (left, top, right, bottom) = area;

    fn axis(p: i32, size: i32, lo: i32, hi: i32, centre: bool, threshold: i32, margin: i32) -> i32 {
        let (near, far) = (lo + margin, hi - margin - size);
        let mut cands = vec![(near, p < near), (far, p > far)];
        if centre {
            // Python: (lo + hi - size) // 2 (floor division)
            cands.push(((lo + hi - size).div_euclid(2), false));
        }
        let (mut best, mut dist): (i32, Option<i32>) = (p, None);
        for (c, past) in cands {
            let d = (p - c).abs();
            if (d <= threshold || past) && dist.is_none_or(|b| d < b) {
                best = c;
                dist = Some(d);
            }
        }
        if hi - lo <= size {
            return lo;
        }
        // max(lo, min(best, hi - size))
        lo.max(best.min(hi - size))
    }

    (
        axis(x, w, left, right, true, threshold, margin),
        axis(y, h, top, bottom, false, threshold, margin),
    )
}

/// (x, y) from a Tk geometry string ("560x76+10+-5", "+3+4"), or None.
pub fn parse_position(geometry: &str) -> Option<(i32, i32)> {
    static RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\+(-?\d+)\+(-?\d+)\s*$").unwrap());
    let c = RE.captures(geometry)?;
    Some((c[1].parse().ok()?, c[2].parse().ok()?))
}

/// "WxH+X+Y" -> (w, h, x, y), or None. X/Y may be negative written Tk's way
/// ("+-1280+0"), which is how monitors left of the primary one come out.
pub fn parse_geometry(s: &str) -> Option<(i32, i32, i32, i32)> {
    static RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^\s*(\d+)x(\d+)\+(-?\d+)\+(-?\d+)\s*$").unwrap());
    let c = RE.captures(s)?;
    let (w, h): (i32, i32) = (c[1].parse().ok()?, c[2].parse().ok()?);
    if w <= 0 || h <= 0 {
        return None;
    }
    Some((w, h, c[3].parse().ok()?, c[4].parse().ok()?))
}

/// Always '+', never '-': in Tk "-X" means "X from the right edge".
pub fn format_geometry(w: i32, h: i32, x: i32, y: i32) -> String {
    format!("{w}x{h}+{x}+{y}")
}

/// Keep a w x h rect inside the work area.
pub fn clamp_rect(x: i32, y: i32, w: i32, h: i32, work: Area) -> (i32, i32) {
    let (l, t, r, b) = work;
    let nx = if r - l >= w { l.max(x.min(r - w)) } else { l + (r - l - w).div_euclid(2) };
    let ny = if b - t >= h { t.max(y.min(b - h)) } else { t };
    (nx, ny)
}

/// (centre x, edge y, top?) for a strip at x,y,w,h: pinned by its top edge in
/// the upper half of the monitor's work area, else by its bottom edge.
pub fn anchor_for(x: i32, y: i32, w: i32, h: i32, work: Option<Area>) -> (i32, i32, bool) {
    let cx = x + w.div_euclid(2);
    let top = match work {
        Some(wk) => (y as f64 + h as f64 / 2.0) < (wk.1 + wk.3) as f64 / 2.0,
        None => false,
    };
    if top {
        (cx, y, true)
    } else {
        (cx, y + h, false)
    }
}

/// The overlay strip's measurements for a text size and monitor.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct OverlayMetrics {
    /// Window size, physical px.
    pub w: i32,
    pub h: i32,
    /// DPI scale (dpi / 96).
    pub s: f64,
    /// Everything below is physical px.
    pub px: i32,
    pub npx: i32,
    pub lh: i32,
    pub nlh: i32,
    pub pad: i32,
    pub bar: i32,
    pub gap: i32,
}

/// Python's text-renderer line heights depended on the font; here the line
/// box is a fixed 1.3 x the font size (the overlay CSS uses the same ratio).
pub const LINE_RATIO: f64 = 1.3;

fn round(v: f64) -> i32 {
    // Python's round() is banker's, but these are never exact .5 in practice;
    // f64::round (half away from zero) is fine and deterministic.
    v.round() as i32
}

/// Size the strip for the monitor it is on and the text size.
pub fn overlay_metrics(size: i64, dpi: u32, work_w: i32, show_next: bool) -> OverlayMetrics {
    let s = dpi.max(1) as f64 / 96.0;
    let size = size.clamp(MIN_OVERLAY_SIZE, MAX_OVERLAY_SIZE) as f64;
    let px = round(size * s).max(8);
    let npx = round(px as f64 * 0.62).max(8);
    let lh = round(px as f64 * LINE_RATIO);
    let nlh = round(npx as f64 * LINE_RATIO);
    let pad = round(px as f64 * 0.40).max(8);
    let bar = round(26.0 * s);
    let gap = round(px as f64 * 0.16);
    let w = (work_w - round(16.0 * s)).min(round(560.0 * s).max(px * 30));
    let h = bar + pad + 2 * lh + if show_next { gap + nlh } else { 0 } + pad;
    OverlayMetrics { w, h, s, px, npx, lh, nlh, pad, bar, gap }
}

/// The overlay's default anchor: bottom centre of the primary work area.
pub fn default_overlay_anchor(work: Area, dpi: u32) -> (i32, i32, bool) {
    let s = dpi.max(1) as f64 / 96.0;
    ((work.0 + work.2).div_euclid(2), work.3 - (36.0 * s) as i32, false)
}

#[cfg(test)]
mod tests {
    use super::*;

    const AREA: Area = (0, 0, 1920, 1040);

    #[test]
    fn snaps_to_near_edges_and_corners() {
        // Dropped a few px from the top-left corner -> margin from both.
        assert_eq!(snap_position(5, 8, 440, 68, AREA, 24, 12), (12, 12));
        // Near the bottom-right.
        assert_eq!(snap_position(1920 - 440 - 3, 1040 - 68 - 5, 440, 68, AREA, 24, 12), (1920 - 440 - 12, 1040 - 68 - 12));
        // Near horizontal centre snaps x only.
        assert_eq!(snap_position(745, 400, 440, 68, AREA, 24, 12), ((1920 - 440) / 2, 400));
        // Far from everything: untouched.
        assert_eq!(snap_position(300, 400, 440, 68, AREA, 24, 12), (300, 400));
    }

    #[test]
    fn dragged_past_an_edge_counts_as_near_it() {
        assert_eq!(snap_position(-300, -50, 440, 68, AREA, 24, 12), (12, 12));
        assert_eq!(snap_position(5000, 5000, 440, 68, AREA, 24, 12), (1920 - 440 - 12, 1040 - 68 - 12));
    }

    #[test]
    fn zero_threshold_zero_margin_only_clamps_onto_the_monitor() {
        assert_eq!(snap_position(100, 100, 440, 68, AREA, 0, 0), (100, 100));
        assert_eq!(snap_position(-100, 5000, 440, 68, AREA, 0, 0), (0, 1040 - 68));
        // window bigger than the area pins to the near edge
        assert_eq!(snap_position(50, 10, 3000, 68, AREA, 0, 0).0, 0);
    }

    #[test]
    fn works_on_monitors_left_of_primary() {
        let left: Area = (-1920, 0, 0, 1080);
        assert_eq!(snap_position(-1915, 4, 440, 68, left, 24, 12), (-1920 + 12, 12));
        assert_eq!(snap_position(-100, 100, 440, 68, left, 0, 0), (-440, 100));
    }

    #[test]
    fn parses_tk_geometry_strings() {
        assert_eq!(parse_position("560x76+10+-5"), Some((10, -5)));
        assert_eq!(parse_position("+3+4"), Some((3, 4)));
        assert_eq!(parse_position(""), None);
        assert_eq!(parse_position("garbage"), None);
        assert_eq!(parse_geometry("800x120+-1280+0"), Some((800, 120, -1280, 0)));
        assert_eq!(parse_geometry(" 800x120+5+6 "), Some((800, 120, 5, 6)));
        assert_eq!(parse_geometry("0x120+5+6"), None);
        assert_eq!(parse_geometry("800x120"), None);
        assert_eq!(format_geometry(800, 120, -3, 4), "800x120+-3+4");
        assert_eq!(parse_geometry(&format_geometry(800, 120, -3, 4)), Some((800, 120, -3, 4)));
    }

    #[test]
    fn clamp_keeps_rect_inside_work_area() {
        assert_eq!(clamp_rect(-50, -50, 500, 100, AREA), (0, 0));
        assert_eq!(clamp_rect(1900, 1000, 500, 100, AREA), (1420, 940));
        assert_eq!(clamp_rect(100, 100, 500, 100, AREA), (100, 100));
        // wider than the monitor: centred on it
        assert_eq!(clamp_rect(0, 0, 2000, 100, AREA).0, -40);
    }

    #[test]
    fn anchor_pins_the_edge_nearest_the_screen_edge() {
        // Lower half -> pinned by bottom edge.
        assert_eq!(anchor_for(100, 900, 600, 100, Some(AREA)), (400, 1000, false));
        // Upper half -> pinned by top edge.
        assert_eq!(anchor_for(100, 20, 600, 100, Some(AREA)), (400, 20, true));
        // Unknown monitor behaves like the lower half.
        assert_eq!(anchor_for(100, 20, 600, 100, None), (400, 120, false));
    }

    #[test]
    fn overlay_metrics_scale_with_size_dpi_and_next_line() {
        let a = overlay_metrics(30, 96, 1920, true);
        assert_eq!((a.px, a.npx, a.pad, a.bar), (30, 19, 12, 26));
        assert_eq!(a.w, 900); // max(560, 30*30)
        let no_next = overlay_metrics(30, 96, 1920, false);
        assert_eq!(a.h - no_next.h, a.gap + a.nlh);
        assert_eq!(no_next.h, a.bar + a.pad + 2 * a.lh + a.pad);
        // Small monitors cap the width.
        assert_eq!(overlay_metrics(72, 96, 800, true).w, 784);
        // DPI scales everything.
        let hi = overlay_metrics(30, 192, 3840, true);
        assert_eq!((hi.px, hi.bar), (60, 52));
        // Out-of-range sizes clamp.
        assert_eq!(overlay_metrics(500, 96, 3000, true).px, 72);
        assert_eq!(overlay_metrics(1, 96, 3000, true).px, 16);
    }

    #[test]
    fn default_anchor_is_bottom_centre_of_primary() {
        assert_eq!(default_overlay_anchor((0, 0, 1920, 1040), 96), (960, 1004, false));
        assert_eq!(default_overlay_anchor((0, 0, 3840, 2080), 192), (1920, 2008, false));
    }

    #[test]
    fn ease_out_is_monotonic_and_clamped() {
        assert_eq!(ease_out(-1.0), 0.0);
        assert_eq!(ease_out(2.0), 1.0);
        assert!(ease_out(0.3) < ease_out(0.6));
    }
}
