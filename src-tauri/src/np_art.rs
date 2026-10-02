//! Album art: memory -> disk -> network cache, then the colours taken from it.
//! Ported from statusify_art.py. The disk tier lives in `<data>/.artcache`
//! (the Python app's folder; this app's files are `rs-<hash>.png`, so the
//! two never read each other's format).

use super::np_colors::{self, Rgb};
use base64::Engine as _;
use image::{imageops::FilterType, DynamicImage, ImageFormat};
use std::collections::HashMap;
use std::io::Cursor;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::Duration;

const MEM_MAX: usize = 40;
const DISK_MAX_FILES: usize = 400;

/// Colours of one cover, ready for the UI.
#[derive(Clone, Debug, PartialEq)]
pub struct CoverColors {
    pub palette: Vec<Rgb>,
    pub tint: Option<Rgb>,
}

pub struct ArtCache {
    dir: PathBuf,
    mem: Mutex<(Vec<(String, u32)>, HashMap<(String, u32), Arc<DynamicImage>>)>,
    http: reqwest::Client,
}

fn fnv(s: &str) -> u64 {
    let mut h: u64 = 0xcbf29ce484222325;
    for b in s.as_bytes() {
        h ^= *b as u64;
        h = h.wrapping_mul(0x100000001b3);
    }
    h
}

impl ArtCache {
    pub fn new(data_dir: &Path) -> Self {
        ArtCache {
            dir: data_dir.join(".artcache"),
            mem: Mutex::new(Default::default()),
            http: reqwest::Client::builder().timeout(Duration::from_secs(4)).build().expect("http client"),
        }
    }

    fn path(&self, url: &str, size: u32) -> PathBuf {
        self.dir.join(format!("rs-{:016x}.png", fnv(&format!("{url}@{size}"))))
    }

    /// Drop the oldest of this app's PNGs beyond DISK_MAX_FILES (run at startup).
    pub fn prune(&self) {
        let Ok(rd) = std::fs::read_dir(&self.dir) else { return };
        let mut files: Vec<(std::time::SystemTime, PathBuf)> = rd
            .flatten()
            .filter(|e| e.file_name().to_string_lossy().starts_with("rs-"))
            .filter_map(|e| Some((e.metadata().ok()?.modified().ok()?, e.path())))
            .collect();
        if files.len() <= DISK_MAX_FILES {
            return;
        }
        files.sort();
        let extra = files.len() - DISK_MAX_FILES;
        for (_, p) in files.into_iter().take(extra) {
            let _ = std::fs::remove_file(p);
        }
    }

    /// The cover as a size x size image, or None (offline, bad URL).
    pub async fn fetch(&self, url: &str, size: u32) -> Option<Arc<DynamicImage>> {
        if url.is_empty() {
            return None;
        }
        let key = (url.to_string(), size);
        if let Some(i) = self.mem.lock().unwrap().1.get(&key) {
            return Some(i.clone());
        }
        let disk = self.path(url, size);
        let mut img = std::fs::read(&disk).ok().and_then(|b| image::load_from_memory(&b).ok());
        if img.is_none() {
            let bytes = self.http.get(url).send().await.ok()?.error_for_status().ok()?.bytes().await.ok()?;
            let dec = image::load_from_memory(&bytes).ok()?;
            let resized = dec.resize_exact(size, size, FilterType::Lanczos3);
            let _ = std::fs::create_dir_all(&self.dir);
            let mut png = Vec::new();
            if resized.write_to(&mut Cursor::new(&mut png), ImageFormat::Png).is_ok() {
                let _ = std::fs::write(&disk, png);
            }
            img = Some(resized);
        }
        let img = Arc::new(img?);
        let mut m = self.mem.lock().unwrap();
        if m.0.len() >= MEM_MAX {
            for _ in 0..MEM_MAX / 4 {
                if !m.0.is_empty() {
                    let k = m.0.remove(0);
                    m.1.remove(&k);
                }
            }
        }
        m.0.push(key.clone());
        m.1.insert(key, img.clone());
        Some(img)
    }
}

/// `data:image/png;base64,...` for a canvas that must not be tainted (share image).
pub fn data_uri(img: &DynamicImage) -> Option<String> {
    let mut png = Vec::new();
    img.write_to(&mut Cursor::new(&mut png), ImageFormat::Png).ok()?;
    Some(format!("data:image/png;base64,{}", base64::engine::general_purpose::STANDARD.encode(png)))
}

/// Palette and tint of a cover (48 px for the palette, 24 px for the tint,
/// as the Python app).
pub fn colors_of(img: &DynamicImage) -> CoverColors {
    let px = |n: u32| -> Vec<Rgb> {
        img.resize_exact(n, n, FilterType::Triangle).to_rgb8().pixels().map(|p| p.0).collect()
    };
    CoverColors {
        palette: np_colors::palette_from_pixels(&px(48), 5),
        tint: np_colors::tint_from_pixels(&px(24), 0.22),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use image::{Rgb as P, RgbImage};

    fn solid(c: [u8; 3]) -> DynamicImage {
        DynamicImage::ImageRgb8(RgbImage::from_pixel(64, 64, P(c)))
    }

    #[test]
    fn colours_of_a_blue_cover() {
        let c = colors_of(&solid([20, 40, 220]));
        assert!(!c.palette.is_empty());
        assert!(c.palette[0][2] > 150);
        assert!(c.tint.is_some());
        assert!(colors_of(&solid([128, 128, 128])).tint.is_none());
    }

    #[test]
    fn data_uri_is_png() {
        let u = data_uri(&solid([1, 2, 3])).unwrap();
        assert!(u.starts_with("data:image/png;base64,"));
    }

    #[tokio::test]
    async fn disk_cache_is_used_without_network() {
        let d = std::env::temp_dir().join(format!("sfy-art-{}", crate::state::now_ms()));
        std::fs::create_dir_all(d.join(".artcache")).unwrap();
        let cache = ArtCache::new(&d);
        let url = "http://127.0.0.1:9/never-fetched.jpg";
        // Offline and uncached: nothing.
        assert!(cache.fetch(url, 32).await.is_none());
        // Seed the disk tier, then it loads with no network at all.
        let mut png = Vec::new();
        solid([9, 9, 9]).resize_exact(32, 32, FilterType::Nearest).write_to(&mut Cursor::new(&mut png), ImageFormat::Png).unwrap();
        std::fs::write(cache.path(url, 32), png).unwrap();
        let img = cache.fetch(url, 32).await.expect("from disk");
        assert_eq!((img.width(), img.height()), (32, 32));
        assert!(cache.fetch("", 32).await.is_none());
        cache.prune();
        let _ = std::fs::remove_dir_all(d);
    }
}
