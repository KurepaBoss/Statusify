use super::*;
use std::sync::atomic::AtomicBool;
use serde_json::json;
use std::sync::atomic::{AtomicUsize, Ordering};
use tokio::io::{AsyncReadExt, AsyncWriteExt};

/// A fake lrclib.net: answers `status` with `body`, counting requests.
async fn fake_lrclib(status: u16, body: &'static str) -> (String, Arc<AtomicUsize>) {
    let l = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}/api/search", l.local_addr().unwrap());
    let hits = Arc::new(AtomicUsize::new(0));
    let h = hits.clone();
    tokio::spawn(async move {
        loop {
            let (mut c, _) = l.accept().await.unwrap();
            h.fetch_add(1, Ordering::SeqCst);
            let mut buf = [0u8; 4096];
            let _ = c.read(&mut buf).await;
            let resp = format!(
                "HTTP/1.1 {status} X\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{body}",
                body.len()
            );
            let _ = c.write_all(resp.as_bytes()).await;
        }
    });
    (url, hits)
}

const HIT: &str = r#"[{"duration":100,"syncedLyrics":"[00:01.00]hello\n[00:02.00]world"}]"#;

fn engine(url: &str) -> Arc<Engine> {
    let mut e = Engine::new(None, |_| {});
    let m = Arc::get_mut(&mut e).unwrap();
    m.lrclib_url = url.into();
    m.lrclib_early = Duration::from_millis(50);
    e
}

fn track(e: &Arc<Engine>, uri: &str) {
    e.handle(&json!({"type":"track_change","track_uri":uri,"artist":"A","title":"T","duration_ms":100000}));
}

async fn settle(ms: u64) {
    tokio::time::sleep(Duration::from_millis(ms)).await;
}

#[tokio::test]
async fn lrclib_starts_early_without_waiting_for_bridge() {
    let (url, hits) = fake_lrclib(200, HIT).await;
    let e = engine(&url);
    track(&e, "u1");
    settle(400).await;
    let s = e.snapshot();
    assert_eq!(hits.load(Ordering::SeqCst), 1);
    assert_eq!(s.lyrics.source, "LRCLIB");
    assert_eq!(s.lyrics.synced[1].words, "world");
}

#[tokio::test]
async fn spicy_in_time_means_no_lrclib() {
    let (url, hits) = fake_lrclib(200, HIT).await;
    let e = engine(&url);
    track(&e, "u1");
    e.handle(&json!({"type":"lyrics","track_uri":"u1","mode":"synced","source":"Spicy",
                     "synced":[{"startMs":0,"words":"spicy"}]}));
    settle(300).await;
    assert_eq!(hits.load(Ordering::SeqCst), 0);
    assert_eq!(e.snapshot().lyrics.source, "Spicy");
}

#[tokio::test]
async fn late_bridge_lyrics_replace_lrclib() {
    let (url, _) = fake_lrclib(200, HIT).await;
    let e = engine(&url);
    track(&e, "u1");
    settle(300).await;
    e.handle(&json!({"type":"lyrics","track_uri":"u1","mode":"synced","source":"Spotify",
                     "synced":[{"startMs":0,"words":"real"}]}));
    assert_eq!(e.snapshot().lyrics.source, "Spotify");
    // and a "none" verdict afterwards keeps them
    e.handle(&json!({"type":"lyrics","track_uri":"u1","mode":"none"}));
    assert_eq!(e.snapshot().lyrics.source, "Spotify");
}

#[tokio::test]
async fn previous_songs_lyrics_are_ignored() {
    let (url, _) = fake_lrclib(404, "[]").await;
    let e = engine(&url);
    track(&e, "u2");
    e.handle(&json!({"type":"lyrics","track_uri":"u1","mode":"synced","synced":[{"startMs":0,"words":"old"}]}));
    assert!(e.snapshot().lyrics.is_none());
}

#[tokio::test]
async fn server_errors_stay_retryable() {
    let (url, hits) = fake_lrclib(503, "busy").await;
    let e = engine(&url);
    // skip the 3 s / 6 s backoff: start paused clock is overkill; just check
    // the first attempt and that the track is not permanently marked.
    track(&e, "u1");
    settle(200).await;
    assert!(hits.load(Ordering::SeqCst) >= 1);
    assert!(e.tried("u1")); // still in flight (backing off)
}

#[tokio::test]
async fn skipping_cancels_early_lookup() {
    let (url, hits) = fake_lrclib(200, HIT).await;
    let e = engine(&url);
    track(&e, "u1");
    track(&e, "u2");
    e.handle(&json!({"type":"lyrics","track_uri":"u2","mode":"plain","plain":["x"]}));
    settle(300).await;
    // u1's early timer fired after the skip and did nothing; u2 had lyrics.
    assert_eq!(hits.load(Ordering::SeqCst), 0);
}

#[tokio::test]
async fn prefetched_lyrics_are_used_on_track_start() {
    let (url, hits) = fake_lrclib(200, HIT).await;
    let e = engine(&url);
    track(&e, "u1");
    e.handle(&json!({"type":"lyrics_prefetch","track_uri":"u2","mode":"synced","source":"Spicy",
                     "synced":[{"startMs":0,"words":"pre"}]}));
    track(&e, "u2");
    settle(200).await;
    assert_eq!(e.snapshot().lyrics.source, "Spicy · preloaded");
    // u1 had no lyrics for 50 ms → one early lookup for u1 only, at most
    assert!(hits.load(Ordering::SeqCst) <= 1);
}

#[tokio::test]
async fn plays_commit_after_twenty_seconds_listened() {
    let dir = std::env::temp_dir().join(format!("statusify-rs-eng-{}", now_ms()));
    std::fs::create_dir_all(&dir).unwrap();
    let mut e = Engine::new(Some(Store::open(&dir).unwrap()), |_| {});
    Arc::get_mut(&mut e).unwrap().lrclib_enabled = AtomicBool::new(false);
    track(&e, "u1");
    for p in (0..=25_000).step_by(1000) {
        e.handle(&json!({"type":"position","position_ms":p,"duration_ms":100000,"is_playing":true}));
        if p == 15_000 {
            assert!(e.recent_plays(5).is_empty());
        }
        if p == 25_000 {
            // already saved mid-song, before any track change
            assert_eq!(e.recent_plays(5)[0].listened_ms, 25_000);
        }
    }
    // a seek forward must not count as listening
    e.handle(&json!({"type":"position","position_ms":90_000,"is_playing":true}));
    track(&e, "u2");
    let plays = e.recent_plays(5);
    assert_eq!(plays.len(), 1);
    assert_eq!(plays[0].track_uri, "u1");
    assert_eq!(plays[0].listened_ms, 25_000);
    let _ = std::fs::remove_dir_all(dir);
}

fn tmpdir(tag: &str) -> std::path::PathBuf {
    let d = std::env::temp_dir().join(format!("statusify-rs-{tag}-{}", std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos()));
    std::fs::create_dir_all(&d).unwrap();
    d
}

#[tokio::test]
async fn pause_saves_listening_time_and_history_switch_is_honoured() {
    let dir = tmpdir("pause");
    let mut e = Engine::new(Some(Store::open(&dir).unwrap()), |_| {});
    Arc::get_mut(&mut e).unwrap().lrclib_enabled = AtomicBool::new(false);
    std::fs::write(dir.join("statusify.cfg"), "[preferences]\nsave_history = true\n").unwrap();
    let cfg = Arc::new(crate::config::Config::open(&dir));
    e.set_config(cfg.clone());
    track(&e, "u1");
    for p in (0..=22_000).step_by(1000) {
        e.handle(&json!({"type":"position","position_ms":p,"is_playing":true}));
    }
    e.handle(&json!({"type":"position","position_ms":23_000,"is_playing":true}));
    e.handle(&json!({"type":"paused"}));
    assert_eq!(e.recent_plays(5)[0].listened_ms, 23_000);
    // history off: nothing new is recorded
    cfg.set("preferences", "save_history", "false");
    e.config_changed();
    track(&e, "u2");
    for p in (0..=25_000).step_by(1000) {
        e.handle(&json!({"type":"position","position_ms":p,"is_playing":true}));
    }
    track(&e, "u3");
    assert_eq!(e.recent_plays(5).len(), 1);
    let _ = std::fs::remove_dir_all(dir);
}

#[tokio::test]
async fn bridge_disconnect_means_not_playing() {
    let e = engine("http://127.0.0.1:9/none");
    e.update(|s| s.bridge_connected = true);
    track(&e, "u1");
    assert!(e.snapshot().is_playing);
    let mut rx = e.subscribe();
    e.update(|s| s.bridge_connected = false);
    assert!(!e.snapshot().is_playing);
    assert!(matches!(rx.try_recv(), Ok(Event::Paused)));
}

#[tokio::test]
async fn blacklist_and_offsets_come_from_config() {
    let dir = tmpdir("bl");
    std::fs::write(dir.join("statusify.cfg"), "[preferences]\nblacklist = peep\nzzz\nlyric_delay_ms = 120\n\n[offsets]\nabc = -300\n").unwrap();
    let e = engine("http://127.0.0.1:9/none");
    e.set_config(Arc::new(crate::config::Config::open(&dir)));
    e.handle(&json!({"type":"track_change","track_uri":"spotify:track:abc","artist":"Lil Peep","title":"x","duration_ms":1000}));
    assert!(e.snapshot().track.unwrap().blacklisted);
    assert_eq!(e.offset_ms(), -300);
    assert_eq!(e.offset_ms_for("spotify:track:other"), 120);
    let _ = std::fs::remove_dir_all(dir);
}
