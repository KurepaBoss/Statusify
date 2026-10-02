//! lrclib.net search — the third lyric source after Spicy and Spotify.

use crate::lyrics::{clean_title, pick_lrclib, Lyrics};
use std::time::Duration;

pub const URL: &str = "https://lrclib.net/api/search";

#[derive(Debug)]
pub enum Error {
    /// Network trouble or a 5xx: worth trying again later.
    Retryable(String),
    /// Answered, but nothing usable.
    NoMatch,
}

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
    r.json().await.map_err(|e| (false, e.to_string()))
}

/// Search with up to three attempts; LRCLIB answers 503 under load,
/// sometimes several times in a row.
pub async fn fetch(client: &reqwest::Client, base: &str, artist: &str, title: &str, duration_ms: i64) -> Result<Lyrics, Error> {
    for attempt in 1..=3u64 {
        match search_once(client, base, artist, title).await {
            Ok(results) => return pick_lrclib(&results, duration_ms).ok_or(Error::NoMatch),
            Err((retry, msg)) => {
                if !retry {
                    return Err(Error::NoMatch);
                }
                if attempt == 3 {
                    return Err(Error::Retryable(msg));
                }
                tokio::time::sleep(Duration::from_secs(3 * attempt)).await;
            }
        }
    }
    unreachable!()
}

pub fn client() -> reqwest::Client {
    reqwest::Client::builder()
        // LRCLIB asks clients to identify themselves.
        .user_agent(concat!("Statusify/", env!("CARGO_PKG_VERSION"), " (https://github.com/KurepaBoss/Statusify)"))
        .build()
        .expect("http client")
}
