//! Cover colours: palette (median cut), album tint and the tinted accent.
//! Ported from statusify_fluid.py (palette_from_image, normalise) and
//! statusify_colors.py (tint_from_pixels, tinted_palette).

pub type Rgb = [u8; 3];

// ── colorsys ─────────────────────────────────────────────────────

pub fn rgb_to_hls(r: f64, g: f64, b: f64) -> (f64, f64, f64) {
    let maxc = r.max(g).max(b);
    let minc = r.min(g).min(b);
    let l = (minc + maxc) / 2.0;
    if minc == maxc {
        return (0.0, l, 0.0);
    }
    let s = if l <= 0.5 { (maxc - minc) / (maxc + minc) } else { (maxc - minc) / (2.0 - maxc - minc) };
    let rc = (maxc - r) / (maxc - minc);
    let gc = (maxc - g) / (maxc - minc);
    let bc = (maxc - b) / (maxc - minc);
    let h = if r == maxc {
        bc - gc
    } else if g == maxc {
        2.0 + rc - bc
    } else {
        4.0 + gc - rc
    };
    ((h / 6.0).rem_euclid(1.0), l, s)
}

fn hls_v(m1: f64, m2: f64, hue: f64) -> f64 {
    let hue = hue.rem_euclid(1.0);
    if hue < 1.0 / 6.0 {
        m1 + (m2 - m1) * hue * 6.0
    } else if hue < 0.5 {
        m2
    } else if hue < 2.0 / 3.0 {
        m1 + (m2 - m1) * (2.0 / 3.0 - hue) * 6.0
    } else {
        m1
    }
}

pub fn hls_to_rgb(h: f64, l: f64, s: f64) -> (f64, f64, f64) {
    if s == 0.0 {
        return (l, l, l);
    }
    let m2 = if l <= 0.5 { l * (1.0 + s) } else { l + s - l * s };
    let m1 = 2.0 * l - m2;
    (hls_v(m1, m2, h + 1.0 / 3.0), hls_v(m1, m2, h), hls_v(m1, m2, h - 1.0 / 3.0))
}

fn to_u8(v: f64) -> u8 {
    (v * 255.0) as i64 as u8
}

pub fn hex(c: Rgb) -> String {
    format!("#{:02x}{:02x}{:02x}", c[0], c[1], c[2])
}

// ── Palette (median cut) ─────────────────────────────────────────

/// Up to `n` representative colours, most common first; near-duplicates
/// (Manhattan distance <= 60 from an earlier pick) are skipped so five blobs
/// don't all come out the same shade of the dominant colour.
pub fn palette_from_pixels(pixels: &[Rgb], n: usize) -> Vec<Rgb> {
    if pixels.is_empty() {
        return vec![];
    }
    // 10 boxes, as PIL's quantize(colors=10, MEDIANCUT).
    let mut boxes: Vec<Vec<Rgb>> = vec![pixels.to_vec()];
    while boxes.len() < 10 {
        // Split the box with the widest channel range among those still splittable.
        let mut best: Option<(usize, usize, u32)> = None; // (box, channel, range*count weight)
        for (bi, b) in boxes.iter().enumerate() {
            if b.len() < 2 {
                continue;
            }
            for ch in 0..3 {
                let (lo, hi) = b.iter().fold((255u8, 0u8), |(lo, hi), p| (lo.min(p[ch]), hi.max(p[ch])));
                let range = (hi - lo) as u32;
                if range == 0 {
                    continue;
                }
                let w = range * 1000 + (b.len() as u32).min(999);
                if best.map_or(true, |(_, _, bw)| w > bw) {
                    best = Some((bi, ch, w));
                }
            }
        }
        let Some((bi, ch, _)) = best else { break };
        let mut b = boxes.swap_remove(bi);
        b.sort_by_key(|p| p[ch]);
        let mid = b.len() / 2;
        let hi = b.split_off(mid);
        boxes.push(b);
        boxes.push(hi);
    }
    let mut counted: Vec<(usize, Rgb)> = boxes
        .iter()
        .filter(|b| !b.is_empty())
        .map(|b| {
            let mut acc = [0u64; 3];
            for p in b {
                for i in 0..3 {
                    acc[i] += p[i] as u64;
                }
            }
            let k = b.len() as u64;
            (b.len(), [(acc[0] / k) as u8, (acc[1] / k) as u8, (acc[2] / k) as u8])
        })
        .collect();
    counted.sort_by(|a, b| b.0.cmp(&a.0).then(b.1.cmp(&a.1)));
    let mut out: Vec<Rgb> = Vec::new();
    for (_, c) in counted {
        let far = out.iter().all(|o| (0..3).map(|i| (c[i] as i32 - o[i] as i32).abs()).sum::<i32>() > 60);
        if far {
            out.push(c);
        }
        if out.len() >= n {
            break;
        }
    }
    out
}

// ── Normalising for the theme ────────────────────────────────────

struct Band {
    base: (f64, f64),
    blob: (f64, f64),
    sat: f64,
}

fn band(dark: bool) -> Band {
    if dark {
        Band { base: (0.10, 0.17), blob: (0.20, 0.40), sat: 1.15 }
    } else {
        Band { base: (0.86, 0.91), blob: (0.70, 0.84), sat: 0.95 }
    }
}

pub fn rel_lum(c: Rgb) -> f64 {
    let ch = |v: u8| {
        let v = v as f64 / 255.0;
        if v <= 0.03928 {
            v / 12.92
        } else {
            ((v + 0.055) / 1.055).powf(2.4)
        }
    };
    0.2126 * ch(c[0]) + 0.7152 * ch(c[1]) + 0.0722 * ch(c[2])
}

const LUM_MAX_DARK: f64 = 0.16;
const LUM_MIN_LIGHT: f64 = 0.52;

fn clamp_l(c: Rgb, lo: f64, hi: f64, sat_mul: f64, dark: bool) -> Rgb {
    let (h, l0, s0) = rgb_to_hls(c[0] as f64 / 255.0, c[1] as f64 / 255.0, c[2] as f64 / 255.0);
    let mut l = l0.max(lo).min(hi);
    let s = (s0 * sat_mul).min(1.0);
    let to_rgb = |l_: f64| {
        let (r, g, b) = hls_to_rgb(h, l_, s);
        [to_u8(r), to_u8(g), to_u8(b)]
    };
    let mut out = to_rgb(l);
    while dark && l > 0.02 && rel_lum(out) > LUM_MAX_DARK {
        l -= 0.01;
        out = to_rgb(l);
    }
    while !dark && l < 0.98 && rel_lum(out) < LUM_MIN_LIGHT {
        l += 0.01;
        out = to_rgb(l);
    }
    out
}

/// (base, five blob colours) ready to paint for the theme. `fallback` is used
/// when there is no palette at all.
pub fn normalise(palette: &[Rgb], dark: bool, fallback: Rgb) -> (Rgb, [Rgb; 5]) {
    let b = band(dark);
    let pal: Vec<Rgb> = if palette.is_empty() { vec![fallback; 3] } else { palette.to_vec() };
    let base = clamp_l(pal[0], b.base.0, b.base.1, b.sat, dark);
    let rest = if pal.len() > 1 { &pal[1..] } else { &pal[..] };
    let blobs: Vec<Rgb> = rest.iter().map(|c| clamp_l(*c, b.blob.0, b.blob.1, b.sat, dark)).collect();
    let mut out = [base; 5];
    for (i, o) in out.iter_mut().enumerate() {
        *o = blobs[i % blobs.len()];
    }
    (base, out)
}

// ── Album tint ───────────────────────────────────────────────────

/// Dominant hue of the pixels as a colour at lightness 0.5, or None for a
/// mostly grey cover. Hue is averaged on the colour circle, weighted by
/// saturation x mid-lightness.
pub fn tint_from_pixels(pixels: &[Rgb], min_sat: f64) -> Option<Rgb> {
    let (mut x, mut y, mut w_sum, mut s_sum) = (0.0f64, 0.0f64, 0.0f64, 0.0f64);
    let n = pixels.len();
    for p in pixels {
        let (h, l, s) = rgb_to_hls(p[0] as f64 / 255.0, p[1] as f64 / 255.0, p[2] as f64 / 255.0);
        if s < min_sat || l < 0.12 || l > 0.9 {
            continue;
        }
        let w = s * (1.0 - (l - 0.5).abs() * 1.6);
        if w <= 0.0 {
            continue;
        }
        x += (h * std::f64::consts::TAU).cos() * w;
        y += (h * std::f64::consts::TAU).sin() * w;
        w_sum += w;
        s_sum += s * w;
    }
    // A real share of coloured pixels, or one small red logo tints everything.
    if n == 0 || w_sum < n as f64 * 0.06 {
        return None;
    }
    let hue = (y.atan2(x) / std::f64::consts::TAU).rem_euclid(1.0);
    let sat = (s_sum / w_sum).min(0.55);
    let (r, g, b) = hls_to_rgb(hue, 0.5, sat);
    Some([to_u8(r), to_u8(g), to_u8(b)])
}

fn hls_hex(h: f64, l: f64, s: f64) -> Rgb {
    let (r, g, b) = hls_to_rgb(h, l.clamp(0.0, 1.0), s.clamp(0.0, 1.0));
    [to_u8(r), to_u8(g), to_u8(b)]
}

const TINT_DARK: [(&str, f64, f64); 10] = [
    ("BG", 0.085, 0.55), ("BG2", 0.115, 0.50), ("BG3", 0.155, 0.45), ("BG4", 0.205, 0.40),
    ("BORDER", 0.180, 0.40), ("SHADOW", 0.045, 0.50), ("MUTED", 0.560, 0.22),
    ("TEXT2", 0.790, 0.20), ("TEXT", 0.955, 0.18), ("ACCENT", 0.720, 1.10),
];
const TINT_LIGHT: [(&str, f64, f64); 10] = [
    ("BG", 0.945, 0.60), ("BG2", 0.985, 0.60), ("BG3", 0.905, 0.45), ("BG4", 0.860, 0.40),
    ("BORDER", 0.880, 0.35), ("SHADOW", 0.820, 0.30), ("MUTED", 0.470, 0.25),
    ("TEXT2", 0.300, 0.25), ("TEXT", 0.085, 0.30), ("ACCENT", 0.360, 1.10),
];

pub fn contrast_ratio(a: Rgb, b: Rgb) -> f64 {
    let (la, lb) = (rel_lum(a), rel_lum(b));
    let (hi, lo) = if la >= lb { (la, lb) } else { (lb, la) };
    (hi + 0.05) / (lo + 0.05)
}

/// Surface/text/accent colours built from the tint's hue; each text-ish token
/// steps away from the surfaces until it meets its minimum contrast.
pub fn tinted_palette(tint: Rgb, dark: bool) -> Vec<(&'static str, Rgb)> {
    let (h, _l, s0) = rgb_to_hls(tint[0] as f64 / 255.0, tint[1] as f64 / 255.0, tint[2] as f64 / 255.0);
    let s = s0.max(0.18);
    let table = if dark { &TINT_DARK } else { &TINT_LIGHT };
    let mut out: Vec<(&'static str, Rgb)> = table.iter().map(|(k, l, f)| (*k, hls_hex(h, *l, s * f))).collect();
    let get = |o: &Vec<(&'static str, Rgb)>, k: &str| o.iter().find(|(n, _)| *n == k).unwrap().1;
    let surfaces = [get(&out, "BG"), get(&out, "BG2"), get(&out, "BG3")];
    let step = if dark { 0.01 } else { -0.01 };
    for (tok, need) in [("TEXT", 7.0), ("TEXT2", 4.5), ("MUTED", 3.0), ("ACCENT", 3.0)] {
        let (_, l0, f) = *table.iter().find(|(k, _, _)| *k == tok).unwrap();
        let mut l = l0;
        let mut c = get(&out, tok);
        while surfaces.iter().map(|bg| contrast_ratio(c, *bg)).fold(f64::INFINITY, f64::min) < need && 0.0 < l && l < 1.0 {
            l += step;
            c = hls_hex(h, l, s * f);
        }
        out.iter_mut().find(|(k, _)| *k == tok).unwrap().1 = c;
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hls_round_trip() {
        for c in [[200u8, 30, 90], [10, 200, 10], [255, 255, 0], [128, 128, 128]] {
            let (h, l, s) = rgb_to_hls(c[0] as f64 / 255.0, c[1] as f64 / 255.0, c[2] as f64 / 255.0);
            let (r, g, b) = hls_to_rgb(h, l, s);
            let back = [(r * 255.0).round() as i32, (g * 255.0).round() as i32, (b * 255.0).round() as i32];
            for i in 0..3 {
                assert!((back[i] - c[i] as i32).abs() <= 1, "{c:?} -> {back:?}");
            }
        }
    }

    #[test]
    fn palette_leads_with_dominant_and_skips_near_duplicates() {
        let mut px = vec![[200u8, 20, 20]; 600];
        px.extend(vec![[205u8, 22, 18]; 100]); // near-duplicate of red
        px.extend(vec![[20u8, 20, 200]; 300]);
        px.extend(vec![[240u8, 240, 240]; 100]);
        let p = palette_from_pixels(&px, 5);
        assert!(p.len() >= 3);
        assert!(p[0][0] > 150 && p[0][2] < 80, "dominant red first: {:?}", p[0]);
        for i in 0..p.len() {
            for j in 0..i {
                let d: i32 = (0..3).map(|k| (p[i][k] as i32 - p[j][k] as i32).abs()).sum();
                assert!(d > 60);
            }
        }
        assert!(palette_from_pixels(&[], 5).is_empty());
    }

    #[test]
    fn tint_follows_hue_and_ignores_grey() {
        let blue = vec![[30u8, 60, 220]; 100];
        let t = tint_from_pixels(&blue, 0.22).unwrap();
        assert!(t[2] > t[0] && t[2] > t[1]);
        assert!(tint_from_pixels(&vec![[120u8, 120, 120]; 100], 0.22).is_none());
        // a single small red logo on grey is not enough
        let mut mixed = vec![[120u8, 120, 120]; 100];
        mixed[0] = [220, 20, 20];
        assert!(tint_from_pixels(&mixed, 0.22).is_none());
    }

    #[test]
    fn tinted_accent_meets_contrast() {
        for tint in [[30u8, 60, 220], [200, 200, 20], [220, 30, 30]] {
            for dark in [true, false] {
                let pal = tinted_palette(tint, dark);
                let g = |k: &str| pal.iter().find(|(n, _)| *n == k).unwrap().1;
                for bg in ["BG", "BG2", "BG3"] {
                    assert!(contrast_ratio(g("ACCENT"), g(bg)) >= 3.0 - 0.15, "{tint:?} dark={dark}");
                    assert!(contrast_ratio(g("TEXT"), g(bg)) >= 7.0 - 0.3, "{tint:?} dark={dark}");
                }
            }
        }
    }

    #[test]
    fn normalise_keeps_dark_blobs_readable() {
        let (base, blobs) = normalise(&[[255, 255, 0], [250, 250, 250], [0, 0, 255]], true, [26, 31, 38]);
        assert!(rel_lum(base) <= 0.16 + 0.01);
        for b in blobs {
            assert!(rel_lum(b) <= 0.16 + 0.01);
        }
        // empty palette falls back to a quiet field
        let (b2, bl2) = normalise(&[], true, [26, 31, 38]);
        assert!(rel_lum(b2) < 0.2 && bl2.len() == 5);
        let (lb, lbl) = normalise(&[[10, 10, 10]], false, [250, 250, 250]);
        assert!(rel_lum(lb) >= 0.5 && rel_lum(lbl[0]) >= 0.5);
    }

    #[test]
    fn hex_round_trip() {
        assert_eq!(hex([30, 215, 96]), "#1ed760");
    }
}
