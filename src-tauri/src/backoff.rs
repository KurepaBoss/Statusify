//! What every outbound HTTP client here shares: reading a Retry-After, a
//! jittered wait, and a `Limiter` that keeps a server's "not before" for
//! every request to it, not only the one that was told.
use std::sync::Mutex;
use std::time::{Duration, Instant};

/// Retry-After in seconds (the form the services here use; an HTTP-date
/// is left to the caller's default). None when absent or unreadable.
pub fn retry_after(headers: &reqwest::header::HeaderMap) -> Option<Duration> {
    let v = headers.get("retry-after")?.to_str().ok()?.trim();
    let secs: f64 = v.parse().ok()?;
    (secs.is_finite() && secs >= 0.0).then(|| Duration::from_secs_f64(secs))
}

/// `d` scaled by a factor in [0.75, 1.25), so clients that failed together
/// do not come back together. No dependency on a random crate: the clock's
/// low bits are plenty for this.
pub fn jitter(d: Duration) -> Duration {
    let nanos = Instant::now().elapsed().as_nanos() as u64 ^ (std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|x| x.as_nanos() as u64).unwrap_or(0));
    let mut x = nanos.wrapping_mul(0x9E37_79B9_7F4A_7C15) ^ (nanos >> 29);
    x ^= x << 13;
    x ^= x >> 7;
    x ^= x << 17;
    let f = 0.75 + (x % 10_000) as f64 / 20_000.0;
    d.mul_f64(f)
}

/// A server's "not before": set from a 429 (or a Retry-After on a 503),
/// read by every request to that server before it goes out.
#[derive(Default, Debug)]
pub struct Limiter {
    not_before: Mutex<Option<Instant>>,
}

impl Limiter {
    /// Hold every request for `d` from now (a later, longer hold wins).
    pub fn hold(&self, d: Duration) {
        let until = Instant::now() + d;
        let mut g = self.not_before.lock().unwrap();
        if g.is_none_or(|t| t < until) {
            *g = Some(until);
        }
    }

    /// How much longer to wait, if a hold is on.
    pub fn remaining(&self) -> Option<Duration> {
        let now = Instant::now();
        self.not_before.lock().unwrap().filter(|t| *t > now).map(|t| t - now)
    }

    /// Wait the hold out (nothing when there is none).
    pub async fn wait(&self) {
        if let Some(d) = self.remaining() {
            tokio::time::sleep(d).await;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use reqwest::header::{HeaderMap, HeaderValue};

    fn h(v: &str) -> HeaderMap {
        let mut m = HeaderMap::new();
        m.insert("retry-after", HeaderValue::from_str(v).unwrap());
        m
    }

    #[test]
    fn retry_after_reads_seconds_and_ignores_the_rest() {
        assert_eq!(retry_after(&h("20")), Some(Duration::from_secs(20)));
        assert_eq!(retry_after(&h(" 1.5 ")), Some(Duration::from_millis(1500)));
        assert_eq!(retry_after(&h("Wed, 21 Oct 2015 07:28:00 GMT")), None);
        assert_eq!(retry_after(&h("-3")), None);
        assert_eq!(retry_after(&HeaderMap::new()), None);
    }

    #[test]
    fn jitter_stays_within_a_quarter_either_way_and_varies() {
        let d = Duration::from_secs(4);
        let mut seen = std::collections::HashSet::new();
        for _ in 0..200 {
            let j = jitter(d);
            assert!(j >= Duration::from_secs(3) && j < Duration::from_secs(5), "{j:?}");
            seen.insert(j.as_micros());
        }
        assert!(seen.len() > 10, "{} distinct values", seen.len());
    }

    #[test]
    fn a_limiter_holds_for_the_longest_asked_and_then_lets_go() {
        let l = Limiter::default();
        assert_eq!(l.remaining(), None);
        l.hold(Duration::from_millis(200));
        l.hold(Duration::from_millis(50)); // shorter: does not cut the hold
        let r = l.remaining().unwrap();
        assert!(r > Duration::from_millis(150) && r <= Duration::from_millis(200), "{r:?}");
        std::thread::sleep(Duration::from_millis(220));
        assert_eq!(l.remaining(), None);
    }
}
