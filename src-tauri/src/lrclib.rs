//! lrclib.net search — the third lyric source after Spicy and Spotify.

use crate::backoff::{jitter, retry_after, Limiter};
use crate::lyrics::{clean_title, pick_lrclib, Lyrics};
use std::time::Duration;

/// The longest a Retry-After is honoured for; longer ones are read as
/// "come back later" and the lookup gives up for now (the track is
/// retried on a later trigger).
pub const MAX_RETRY_AFTER: Duration = Duration::from_secs(60);
/// A 429 without a Retry-After.
pub const DEFAULT_429_HOLD: Duration = Duration::from_secs(30);

pub const URL: &str = "https://lrclib.net/api/search";

#[derive(Debug)]
pub enum Error {
    /// The lookup failed (network, HTTP error, unreadable answer): the track
    /// is forgotten so a later trigger may try again (main.py _lrclib_task).
    Failed(String),
    /// Answered, but nothing usable.
    NoMatch,
}

/// Why one search failed, and what to do about it.
struct Fail {
    msg: String,
    /// Network trouble, a 5xx, a 429 or an unreadable body are worth
    /// another attempt; any other 4xx is not.
    retry: bool,
    /// What the server asked: a Retry-After, or the default for a 429.
    hold: Option<Duration>,
}

async fn search_once(client: &reqwest::Client, base: &str, artist: &str, title: &str) -> Result<serde_json::Value, Fail> {
    let r = client
        .get(base)
        .query(&[("track_name", clean_title(title)), ("artist_name", artist.to_string())])
        .timeout(Duration::from_secs(8))
        .send()
        .await
        .map_err(|e| Fail { msg: e.to_string(), retry: true, hold: None })?;
    let code = r.status();
    if !code.is_success() {
        let asked = retry_after(r.headers());
        let hold = if code.as_u16() == 429 { Some(asked.unwrap_or(DEFAULT_429_HOLD)) } else { asked.filter(|_| code.is_server_error()) };
        return Err(Fail { msg: format!("HTTP {code}"), retry: code.is_server_error() || code.as_u16() == 429, hold });
    }
    r.json().await.map_err(|e| Fail { msg: format!("bad JSON: {e}"), retry: true, hold: None })
}

/// Search with up to three attempts; LRCLIB answers 503 under load,
/// sometimes several times in a row.
#[allow(dead_code)] // the engine passes its own backoff
pub async fn fetch(client: &reqwest::Client, lim: &Limiter, base: &str, artist: &str, title: &str, duration_ms: i64) -> Result<Lyrics, Error> {
    fetch_with_backoff(client, lim, base, artist, title, duration_ms, Duration::from_secs(3)).await
}

/// Up to three attempts. A wait the server asked for (Retry-After, or the
/// default after a 429) is honoured, and goes into `lim` so every other
/// lookup waits too; otherwise the wait is `step`, `2 step`, with a quarter
/// of jitter so clients that failed together do not return together. A
/// Retry-After past MAX_RETRY_AFTER is not slept on: the lookup fails and
/// is retried on a later trigger.
pub async fn fetch_with_backoff(client: &reqwest::Client, lim: &Limiter, base: &str, artist: &str, title: &str, duration_ms: i64, step: Duration) -> Result<Lyrics, Error> {
    lim.wait().await;
    for attempt in 1..=3u32 {
        match search_once(client, base, artist, title).await {
            Ok(results) => return pick_lrclib(&results, duration_ms).ok_or(Error::NoMatch),
            Err(f) => {
                if let Some(h) = f.hold {
                    lim.hold(h.min(MAX_RETRY_AFTER));
                }
                let too_long = f.hold.is_some_and(|h| h > MAX_RETRY_AFTER);
                if !f.retry || attempt == 3 || too_long {
                    return Err(Error::Failed(f.msg));
                }
                match f.hold {
                    Some(h) => tokio::time::sleep(h).await,
                    None => tokio::time::sleep(jitter(step * attempt)).await,
                }
            }
        }
    }
    unreachable!()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Arc;
    use std::time::Instant;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    /// Answers each connection with the next (status, extra headers, body); the last one repeats.
    async fn server(answers: Vec<(u16, &'static str, &'static str)>) -> (String, Arc<AtomicUsize>) {
        let l = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}/api/search", l.local_addr().unwrap());
        let hits = Arc::new(AtomicUsize::new(0));
        let h = hits.clone();
        tokio::spawn(async move {
            loop {
                let (mut c, _) = l.accept().await.unwrap();
                let n = h.fetch_add(1, Ordering::SeqCst);
                let (status, extra, body) = answers[n.min(answers.len() - 1)];
                let mut buf = [0u8; 4096];
                let _ = c.read(&mut buf).await;
                let resp = format!("HTTP/1.1 {status} X\r\ncontent-type: application/json\r\n{extra}content-length: {}\r\nconnection: close\r\n\r\n{body}", body.len());
                let _ = c.write_all(resp.as_bytes()).await;
            }
        });
        (url, hits)
    }

    const HIT: &str = r#"[{"duration":100,"syncedLyrics":"[00:01.00]hello\n[00:02.00]world"}]"#;

    #[tokio::test]
    async fn a_429_is_retried_after_its_retry_after_and_holds_other_lookups() {
        let (url, hits) = server(vec![(429, "retry-after: 1\r\n", "slow down"), (200, "", HIT)]).await;
        let c = reqwest::Client::new();
        let lim = Limiter::default();
        let t0 = Instant::now();
        let l = fetch_with_backoff(&c, &lim, &url, "A", "T", 100_000, Duration::from_millis(10)).await.unwrap();
        assert_eq!(l.mode, "synced");
        assert_eq!(hits.load(Ordering::SeqCst), 2);
        let took = t0.elapsed();
        assert!(took >= Duration::from_millis(950) && took < Duration::from_millis(2500), "{took:?}: waited the Retry-After, not the step");
        // The second lookup got the same limiter: the hold had already passed.
        assert!(lim.remaining().is_none());
    }

    #[tokio::test]
    async fn a_503_without_retry_after_backs_off_by_the_step_and_a_4xx_gives_up() {
        let (url, hits) = server(vec![(503, "", "busy"), (503, "", "busy"), (200, "", HIT)]).await;
        let c = reqwest::Client::new();
        let lim = Limiter::default();
        let t0 = Instant::now();
        fetch_with_backoff(&c, &lim, &url, "A", "T", 100_000, Duration::from_millis(100)).await.unwrap();
        assert_eq!(hits.load(Ordering::SeqCst), 3);
        let took = t0.elapsed();
        // 100 ms and 200 ms, each within a quarter.
        assert!(took >= Duration::from_millis(225) && took < Duration::from_millis(900), "{took:?}");
        assert!(lim.remaining().is_none(), "no hold without a Retry-After");

        let (url, hits) = server(vec![(404, "", "{}")]).await;
        assert!(matches!(fetch_with_backoff(&c, &lim, &url, "A", "T", 100_000, Duration::from_millis(10)).await, Err(Error::Failed(_))));
        assert_eq!(hits.load(Ordering::SeqCst), 1, "a 4xx is not retried");
    }

    #[tokio::test]
    async fn a_retry_after_past_the_cap_is_not_slept_on_but_still_holds() {
        let (url, hits) = server(vec![(429, "retry-after: 3600\r\n", "later")]).await;
        let c = reqwest::Client::new();
        let lim = Limiter::default();
        let t0 = Instant::now();
        assert!(matches!(fetch_with_backoff(&c, &lim, &url, "A", "T", 100_000, Duration::from_millis(10)).await, Err(Error::Failed(_))));
        assert!(t0.elapsed() < Duration::from_millis(500));
        assert_eq!(hits.load(Ordering::SeqCst), 1);
        let r = lim.remaining().unwrap();
        assert!(r > Duration::from_secs(50) && r <= MAX_RETRY_AFTER, "{r:?}: held, for the cap at most");
    }
}

pub fn client() -> reqwest::Client {
    reqwest::Client::builder()
        // LRCLIB asks clients to identify themselves.
        .user_agent(format!("Statusify/{} (https://github.com/KurepaBoss/Statusify)", crate::engine::maint::APP_VERSION))
        .build()
        .expect("http client")
}
