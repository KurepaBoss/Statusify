//! lrclib.net search — the third lyric source after Spicy and Spotify.

use crate::lyrics::{clean_title, pick_lrclib, Lyrics};
use std::time::Duration;

pub const URL: &str = "https://lrclib.net/api/search";

#[derive(Debug)]
pub enum Error {
    /// The lookup failed (network, HTTP error, unreadable answer): the track
    /// is forgotten so a later trigger may try again (main.py _lrclib_task).
    Failed(String),
    /// Answered, but nothing usable.
    NoMatch,
}

/// One search. Err((retry, message)): network trouble, a 5xx or an
/// unreadable body are worth another attempt; a 4xx is not.
async fn search_once(client: &reqwest::Client, base: &str, artist: &str, title: &str) -> Result<serde_json::Value, (bool, String)> {
    let r = client
        .get(base)
        .query(&[("track_name", clean_title(title)), ("artist_name", artist.to_string())])
        .timeout(Duration::from_secs(8))
        .send()
        .await
        .map_err(|e| (true, e.to_string()))?;
    let code = r.status();
    if !code.is_success() {
        return Err((code.is_server_error(), format!("HTTP {code}")));
    }
    r.json().await.map_err(|e| (true, format!("bad JSON: {e}")))
}

/// Search with up to three attempts; LRCLIB answers 503 under load,
/// sometimes several times in a row.
#[allow(dead_code)] // the engine passes its own backoff
pub async fn fetch(client: &reqwest::Client, base: &str, artist: &str, title: &str, duration_ms: i64) -> Result<Lyrics, Error> {
    fetch_with_backoff(client, base, artist, title, duration_ms, Duration::from_secs(3)).await
}

pub async fn fetch_with_backoff(client: &reqwest::Client, base: &str, artist: &str, title: &str, duration_ms: i64, step: Duration) -> Result<Lyrics, Error> {
    for attempt in 1..=3u32 {
        match search_once(client, base, artist, title).await {
            Ok(results) => return pick_lrclib(&results, duration_ms).ok_or(Error::NoMatch),
            Err((retry, msg)) => {
                if !retry || attempt == 3 {
                    return Err(Error::Failed(msg));
                }
                tokio::time::sleep(step * attempt).await;
            }
        }
    }
    unreachable!()
}

pub fn client() -> reqwest::Client {
    reqwest::Client::builder()
        // LRCLIB asks clients to identify themselves.
        .user_agent(format!("Statusify/{} (https://github.com/KurepaBoss/Statusify)", crate::engine::maint::APP_VERSION))
        .build()
        .expect("http client")
}
