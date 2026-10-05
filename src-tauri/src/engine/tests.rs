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

fn stored_engine(dir: &std::path::Path) -> Arc<Engine> {
    let mut e = Engine::new(Some(Store::open(dir).unwrap()), |_| {});
    Arc::get_mut(&mut e).unwrap().lrclib_enabled = AtomicBool::new(false);
    e
}

fn pos(e: &Arc<Engine>, p: i64, playing: bool) {
    e.handle(&json!({"type":"position","position_ms":p,"duration_ms":100000,"is_playing":playing}));
}

#[tokio::test]
async fn plays_commit_after_twenty_seconds_listened() {
    let dir = tmpdir("commit");
    let e = stored_engine(&dir);
    track(&e, "u1");
    for p in (1000..=25_000).step_by(1000) {
        e.advance(1000);
        pos(&e, p, true);
        if p == 15_000 {
            assert!(e.recent_plays(5).is_empty());
        }
        if p == 25_000 {
            // already saved mid-song, before any track change
            assert_eq!(e.recent_plays(5)[0].listened_ms, 25_000);
        }
    }
    // a seek forward is not listening: only the wall-clock time counts
    e.advance(500);
    pos(&e, 90_000, true);
    track(&e, "u2");
    let plays = e.recent_plays(5);
    assert_eq!(plays.len(), 1);
    assert_eq!(plays[0].track_uri, "u1");
    assert_eq!(plays[0].listened_ms, 25_500);
    let _ = std::fs::remove_dir_all(dir);
}

#[tokio::test]
async fn rare_position_pings_still_count_listening_time() {
    // Spotify minimised: the bridge pings about once a minute.
    let dir = tmpdir("rare");
    let e = stored_engine(&dir);
    track(&e, "u1");
    e.advance(60_000);
    pos(&e, 60_000, true);
    let plays = e.recent_plays(5);
    assert_eq!(plays.len(), 1, "committed on the first ping after 20 s");
    assert_eq!(plays[0].listened_ms, 60_000);
    e.advance(30_000);
    track(&e, "u2");
    assert_eq!(e.recent_plays(5)[0].listened_ms, 90_000);
    let _ = std::fs::remove_dir_all(dir);
}

#[tokio::test]
async fn paused_time_is_not_listening() {
    let dir = tmpdir("paused");
    let e = stored_engine(&dir);
    track(&e, "u1");
    e.advance(15_000);
    pos(&e, 15_000, false); // paused at 15 s
    e.advance(600_000); // ten minutes away
    pos(&e, 15_000, true); // resumed
    assert!(e.recent_plays(5).is_empty());
    e.advance(6_000);
    pos(&e, 21_000, true);
    assert_eq!(e.recent_plays(5)[0].listened_ms, 21_000);
    let _ = std::fs::remove_dir_all(dir);
}

#[tokio::test]
async fn opening_on_a_paused_song_is_not_a_play() {
    let dir = tmpdir("openpaused");
    let e = stored_engine(&dir);
    track(&e, "u1");
    pos(&e, 5_000, false);
    e.advance(120_000);
    pos(&e, 5_000, false);
    track(&e, "u2");
    assert!(e.recent_plays(5).is_empty());
    let _ = std::fs::remove_dir_all(dir);
}

#[tokio::test]
async fn repeated_track_change_keeps_position_and_play() {
    // The bridge re-sends track_change on reconnect / request_state.
    let dir = tmpdir("same");
    let e = stored_engine(&dir);
    track(&e, "u1");
    e.advance(30_000);
    pos(&e, 30_000, true);
    assert_eq!(e.recent_plays(5).len(), 1);
    track(&e, "u1");
    let s = e.snapshot();
    assert!(s.position_ms >= 30_000, "position kept, not reset to 0");
    e.advance(10_000);
    pos(&e, 40_000, true);
    track(&e, "u2");
    let plays = e.recent_plays(5);
    assert_eq!(plays.len(), 1, "no duplicate play");
    assert_eq!(plays[0].listened_ms, 40_000);
    let _ = std::fs::remove_dir_all(dir);
}

#[tokio::test]
async fn a_song_starting_over_is_a_new_play() {
    let dir = tmpdir("repeat");
    let e = stored_engine(&dir);
    track(&e, "u1");
    e.advance(99_000);
    pos(&e, 99_000, true);
    e.advance(1_500);
    pos(&e, 500, true); // repeat one: back to the start
    e.advance(25_000);
    pos(&e, 25_500, true);
    let plays = e.recent_plays(5);
    assert_eq!(plays.len(), 2);
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
    let e = stored_engine(&dir);
    std::fs::write(dir.join("statusify.cfg"), "[preferences]\nsave_history = true\n").unwrap();
    let cfg = Arc::new(crate::config::Config::open(&dir));
    e.set_config(cfg.clone());
    track(&e, "u1");
    for p in (1000..=23_000).step_by(1000) {
        e.advance(1000);
        pos(&e, p, true);
    }
    e.advance(400);
    e.handle(&json!({"type":"paused"}));
    assert_eq!(e.recent_plays(5)[0].listened_ms, 23_400);
    // history off: nothing new is recorded
    cfg.set("preferences", "save_history", "false");
    e.config_changed();
    track(&e, "u2");
    e.advance(25_000);
    pos(&e, 25_000, true);
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

#[tokio::test]
async fn uri_less_lyrics_never_replace_lyrics_already_showing() {
    let e = engine("http://127.0.0.1:9/none");
    e.lrclib_enabled.store(false, Ordering::SeqCst);
    track(&e, "u1");
    // an old bridge (no track_uri) while nothing shows: accepted
    e.handle(&json!({"type":"lyrics","mode":"plain","plain":["first"]}));
    assert_eq!(e.snapshot().lyrics.plain, vec!["first".to_string()]);
    // a late URI-less answer must not replace them
    e.handle(&json!({"type":"lyrics","mode":"plain","plain":["late"]}));
    assert_eq!(e.snapshot().lyrics.plain, vec!["first".to_string()]);
    // this track's own lyrics still do
    e.handle(&json!({"type":"lyrics","track_uri":"u1","mode":"plain","plain":["real"]}));
    assert_eq!(e.snapshot().lyrics.plain, vec!["real".to_string()]);
}

#[tokio::test]
async fn bridge_none_verdict_is_cached_and_tries_lrclib() {
    let (url, hits) = fake_lrclib(200, "[]").await;
    let dir = tmpdir("none");
    let mut e = Engine::new(Some(Store::open(&dir).unwrap()), |_| {});
    let m = Arc::get_mut(&mut e).unwrap();
    m.lrclib_url = url;
    m.lrclib_early = Duration::from_secs(60);
    track(&e, "u1");
    e.handle(&json!({"type":"lyrics","track_uri":"u1","mode":"none"}));
    settle(300).await;
    assert_eq!(hits.load(Ordering::SeqCst), 1);
    let mode = e.store.as_ref().unwrap().with_conn(|db| {
        db.query_row("SELECT mode FROM lyrics WHERE track_uri='u1'", [], |r| r.get::<_, String>(0)).ok()
    });
    assert_eq!(mode.as_deref(), Some("none"));
    // an empty answer is final: no second lookup on another "none"
    e.handle(&json!({"type":"lyrics","track_uri":"u1","mode":"none"}));
    settle(200).await;
    assert_eq!(hits.load(Ordering::SeqCst), 1);
    drop(e);
    let _ = std::fs::remove_dir_all(dir);
}

fn quick_engine(url: &str) -> Arc<Engine> {
    let mut e = engine(url);
    let m = Arc::get_mut(&mut e).unwrap();
    m.lrclib_early = Duration::from_secs(60);
    m.lrclib_backoff = Duration::from_millis(10);
    e
}

#[tokio::test]
async fn lrclib_client_errors_are_retryable_later() {
    let (url, hits) = fake_lrclib(404, "{}").await;
    let e = quick_engine(&url);
    track(&e, "u1");
    e.handle(&json!({"type":"lyrics","track_uri":"u1","mode":"none"}));
    settle(300).await;
    assert_eq!(hits.load(Ordering::SeqCst), 1, "a 4xx is not retried at once");
    assert!(!e.tried("u1"), "but the track is forgotten so it may be looked up again");
    e.handle(&json!({"type":"lyrics","track_uri":"u1","mode":"none"}));
    settle(300).await;
    assert_eq!(hits.load(Ordering::SeqCst), 2);
}

#[tokio::test]
async fn lrclib_bad_json_is_retried_then_forgotten() {
    let (url, hits) = fake_lrclib(200, "not json").await;
    let e = quick_engine(&url);
    track(&e, "u1");
    e.handle(&json!({"type":"lyrics","track_uri":"u1","mode":"none"}));
    settle(500).await;
    assert_eq!(hits.load(Ordering::SeqCst), 3);
    assert!(!e.tried("u1"));
}

#[tokio::test]
async fn beats_follow_the_current_track() {
    let e = engine("http://127.0.0.1:9/none");
    track(&e, "u1");
    e.handle(&json!({"type":"beats","track_uri":"u1","beats":[900, 300],"tempo":120}));
    assert_eq!(e.beats(), (vec![300, 900], 120.0));
    e.handle(&json!({"type":"beats","track_uri":"other","beats":[1],"tempo":60}));
    assert_eq!(e.beats().0, vec![300, 900]);
    track(&e, "u2");
    assert_eq!(e.beats(), (vec![], 0.0));
}

#[tokio::test]
async fn quitting_banks_the_play_in_progress() {
    // The periodic save writes every 5 s of listening; quitting must not lose the rest.
    let dir = tmpdir("quit-bank");
    let e = stored_engine(&dir);
    track(&e, "u1");
    for p in (1000..=25_000).step_by(1000) {
        e.advance(1000);
        pos(&e, p, true);
    }
    assert_eq!(e.recent_plays(5)[0].listened_ms, 25_000, "the last periodic save");
    e.advance(3_500); // quit 3.5 s after the last position update
    e.finish_play();
    let plays = e.recent_plays(5);
    assert_eq!(plays.len(), 1);
    assert_eq!(plays[0].listened_ms, 28_500);
    // Quit paths overlap (the window's close, then the runtime's exit event): calling it again changes nothing.
    e.finish_play();
    assert_eq!(e.recent_plays(5)[0].listened_ms, 28_500);
    assert_eq!(e.recent_plays(5).len(), 1);
    let _ = std::fs::remove_dir_all(dir);
}

#[tokio::test]
async fn quitting_commits_a_play_that_just_passed_twenty_seconds() {
    let dir = tmpdir("quit-commit");
    let e = stored_engine(&dir);
    track(&e, "u1");
    pos(&e, 1_000, true);
    e.advance(21_000); // no position update since: the 20 s mark passed unnoticed
    assert!(e.recent_plays(5).is_empty());
    e.finish_play();
    let plays = e.recent_plays(5);
    assert_eq!(plays.len(), 1, "it counts as a play");
    assert_eq!(plays[0].track_uri, "u1");
    assert_eq!(plays[0].listened_ms, 21_000);
    let _ = std::fs::remove_dir_all(dir);
}

#[tokio::test]
async fn quitting_early_in_a_song_or_while_paused_records_nothing_new() {
    let dir = tmpdir("quit-short");
    let e = stored_engine(&dir);
    track(&e, "u1");
    pos(&e, 1_000, true);
    e.advance(15_000);
    e.finish_play();
    assert!(e.recent_plays(5).is_empty(), "under 20 s is not a play");
    // Paused at 30 s, then the app is closed ten minutes later: the paused time is not listening.
    e.advance(15_000);
    pos(&e, 30_000, false);
    e.advance(600_000);
    e.finish_play();
    let plays = e.recent_plays(5);
    assert_eq!(plays.len(), 1);
    assert_eq!(plays[0].listened_ms, 30_000);
    // Nothing playing at all
    let none = stored_engine(&tmpdir("quit-none"));
    none.finish_play();
    assert!(none.recent_plays(5).is_empty());
    let _ = std::fs::remove_dir_all(dir);
}

#[tokio::test]
async fn quitting_with_history_off_writes_nothing() {
    let dir = tmpdir("quit-off");
    std::fs::write(dir.join("statusify.cfg"), "[preferences]\nsave_history = false\n").unwrap();
    let mut e = Engine::new(Some(Store::open(&dir).unwrap()), |_| {});
    Arc::get_mut(&mut e).unwrap().lrclib_enabled = AtomicBool::new(false);
    e.set_config(Arc::new(crate::config::Config::open(&dir)));
    track(&e, "u1");
    pos(&e, 1_000, true);
    e.advance(40_000);
    e.finish_play();
    assert!(e.store.as_ref().unwrap().recent_plays(5).unwrap().is_empty());
    let _ = std::fs::remove_dir_all(dir);
}
