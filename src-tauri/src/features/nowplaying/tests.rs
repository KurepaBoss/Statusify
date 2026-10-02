use super::*;
use std::io::Cursor;
use std::sync::atomic::AtomicBool;
use std::time::Duration;
use tokio::io::{AsyncReadExt, AsyncWriteExt};

fn tmp(tag: &str) -> std::path::PathBuf {
    let d = std::env::temp_dir().join(format!("sfy-np-{tag}-{}", crate::state::now_ms()));
    std::fs::create_dir_all(&d).unwrap();
    d
}

fn engine() -> Arc<Engine> {
    let mut e = Engine::new(None, |_| {});
    Arc::get_mut(&mut e).unwrap().lrclib_enabled = AtomicBool::new(false);
    e
}

fn send(st: &Arc<State>, e: &Arc<Engine>, m: Value) {
    e.handle(&m);
    st.on_bridge(e, &m);
}

fn track(st: &Arc<State>, e: &Arc<Engine>, uri: &str, art: &str) {
    send(st, e, json!({"type":"track_change","track_uri":uri,"artist":"A","title":"T","album_art":art,"duration_ms":100000}));
}

/// A fake CDN serving one PNG.
async fn fake_art(png: Vec<u8>) -> String {
    let l = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}/cover.png", l.local_addr().unwrap());
    tokio::spawn(async move {
        loop {
            let (mut c, _) = l.accept().await.unwrap();
            let mut buf = [0u8; 2048];
            let _ = c.read(&mut buf).await;
            let head = format!("HTTP/1.1 200 OK\r\ncontent-type: image/png\r\ncontent-length: {}\r\nconnection: close\r\n\r\n", png.len());
            let _ = c.write_all(head.as_bytes()).await;
            let _ = c.write_all(&png).await;
        }
    });
    url
}

fn blue_png() -> Vec<u8> {
    let img = image::DynamicImage::ImageRgb8(image::RgbImage::from_pixel(80, 80, image::Rgb([30, 60, 220])));
    let mut out = Vec::new();
    img.write_to(&mut Cursor::new(&mut out), image::ImageFormat::Png).unwrap();
    out
}

#[tokio::test]
async fn player_queue_and_beats_reach_the_snapshot() {
    let d = tmp("pq");
    let st = State::new(&d);
    let e = engine();
    track(&st, &e, "u1", "");
    send(&st, &e, json!({"type":"player_state","volume":0.3,"shuffle":true,"repeat":1,"liked":true}));
    let ex = e.snapshot().extras;
    assert_eq!(ex["player"]["volume"], 0.3);
    assert_eq!(ex["player"]["shuffle"], true);
    assert_eq!(ex["player"]["repeat"], 1);
    send(&st, &e, json!({"type":"queue","tracks":[{"uri":"spotify:track:9","title":"Next","duration_ms":1000},{"junk":1}]}));
    assert_eq!(e.snapshot().extras["queue"].as_array().unwrap().len(), 1);
    // beats for another track are ignored; the current track's are sorted
    send(&st, &e, json!({"type":"beats","track_uri":"other","tempo":90,"beats":[1,2]}));
    assert!(e.snapshot().extras.get("beats").is_none());
    send(&st, &e, json!({"type":"beats","track_uri":"u1","tempo":120.5,"beats":[900,100,500]}));
    assert_eq!(e.snapshot().extras["beats"]["beats"], json!([100, 500, 900]));
    assert_eq!(e.snapshot().extras["beats"]["tempo"], 120.5);
    // a new track forgets the beats
    track(&st, &e, "u2", "");
    st.on_track(&e);
    assert!(e.snapshot().extras["beats"].is_null());
    let _ = std::fs::remove_dir_all(d);
}

#[tokio::test]
async fn cover_colours_come_from_the_art_and_stale_covers_are_dropped() {
    let d = tmp("pal");
    let st = State::new(&d);
    let e = engine();
    let url = fake_art(blue_png()).await;
    track(&st, &e, "u1", &url);
    st.on_track(&e);
    tokio::time::sleep(Duration::from_millis(600)).await;
    let p = e.snapshot().extras["palette"].clone();
    assert_eq!(p["uri"], "u1");
    assert!(p["accent"].as_str().unwrap().starts_with('#'));
    assert!(!p["colors"].as_array().unwrap().is_empty());
    assert_eq!(p["blobs"].as_array().unwrap().len(), 5);
    assert_eq!(p["light"]["blobs"].as_array().unwrap().len(), 5);
    // no art: an empty palette, not a stale one
    track(&st, &e, "u2", "");
    st.on_track(&e);
    tokio::time::sleep(Duration::from_millis(200)).await;
    let p = e.snapshot().extras["palette"].clone();
    assert_eq!(p["uri"], "u2");
    assert!(p["accent"].is_null());
    assert!(p["colors"].as_array().unwrap().is_empty());
    let _ = std::fs::remove_dir_all(d);
}

#[tokio::test]
async fn syllable_timing_follows_the_sheet_on_screen() {
    let d = tmp("tm");
    let st = State::new(&d);
    let e = engine();
    track(&st, &e, "u1", "");
    let msg = json!({"type":"lyrics","track_uri":"u1","mode":"synced","source":"Spicy",
        "synced":[{"startMs":1000,"words":"hello world","endMs":3000,"syl":[[1000,2000,"hello"],[2000,3000,"world"]]},
                  {"startMs":6000,"words":"bye"}]});
    send(&st, &e, msg);
    st.on_lyrics(&e);
    let t = e.snapshot().extras["np_timing"].clone();
    assert_eq!(t["uri"], "u1");
    assert_eq!(t["n"], 2);
    assert_eq!(t["lines"][0]["syl"][0][2], "hello");
    assert!(t["lines"][1].is_null());
    // a different sheet (a pinned LRCLIB pick, say) gets no timing
    e.set_lyrics(Lyrics { mode: "synced".into(), synced: vec![crate::lyrics::Line { start_ms: 1000, words: "other".into() }], plain: vec![], source: "LRCLIB".into() });
    st.on_lyrics(&e);
    assert!(e.snapshot().extras["np_timing"]["lines"].is_null());
    let _ = std::fs::remove_dir_all(d);
}

fn fake_result(name: &str, dur: f64) -> Value {
    json!({"trackName": name, "artistName": "A", "albumName": "", "duration": dur, "syncedLyrics": "[00:01.00]one\n[00:02.00]two", "plainLyrics": "one\ntwo"})
}

#[tokio::test]
async fn pin_then_unpin_restores_the_previous_lyrics() {
    let d = tmp("pin");
    let st = State::new(&d);
    let e = engine();
    track(&st, &e, "u1", "");
    send(&st, &e, json!({"type":"lyrics","track_uri":"u1","mode":"synced","source":"Spicy","synced":[{"startMs":0,"words":"spicy"}]}));
    // search results live in the backend; the page picks by index
    *st.results.lock().unwrap() = vec![json!({"raw": fake_result("Song", 100.0)})];
    let r = st.pin_result(&e, 0).unwrap();
    assert_eq!(r["ok"], true);
    let s = e.snapshot();
    assert_eq!(s.lyrics.source, PIN_SOURCE);
    assert_eq!(s.lyrics.synced[1].words, "two");
    assert!(e.is_pinned());
    // the bridge answering later does not displace the pick (engine rule) ...
    send(&st, &e, json!({"type":"lyrics","track_uri":"u1","mode":"synced","source":"Spotify","synced":[{"startMs":0,"words":"spot"}]}));
    assert_eq!(e.snapshot().lyrics.source, PIN_SOURCE);
    // ... and unpinning goes back to what was showing when the pick was made
    let u = st.unpin(&e);
    assert_eq!(u, json!({"had": true, "restored": true}));
    assert!(!e.is_pinned());
    assert_eq!(e.snapshot().lyrics.synced[0].words, "spicy");
    // unpin with nothing pinned changes nothing
    assert_eq!(st.unpin(&e), json!({"had": false, "restored": false}));
    // a bad index is an error, an unusable result is reported
    assert!(st.pin_result(&e, 5).is_err());
    *st.results.lock().unwrap() = vec![json!({"raw": {"duration": 100.0, "syncedLyrics": "", "plainLyrics": ""}})];
    assert_eq!(st.pin_result(&e, 0).unwrap()["ok"], false);
    let _ = std::fs::remove_dir_all(d);
}

#[tokio::test]
async fn unpin_falls_back_to_the_bridges_own_lyrics() {
    let d = tmp("pin2");
    let st = State::new(&d);
    let e = engine();
    track(&st, &e, "u1", "");
    e.set_lyrics(Lyrics { mode: "plain".into(), synced: vec![], plain: vec!["x".into()], source: "cache".into() });
    e.pin_lyrics(Lyrics { mode: "plain".into(), synced: vec![], plain: vec!["chosen".into()], source: PIN_SOURCE.into() });
    // the bridge's lyrics arrive only while pinned
    send(&st, &e, json!({"type":"lyrics","track_uri":"u1","mode":"synced","source":"Spotify","synced":[{"startMs":0,"words":"theirs"}]}));
    assert_eq!(e.snapshot().lyrics.plain[0], "chosen");
    let u = st.unpin(&e);
    assert_eq!(u["restored"], true);
    assert_eq!(e.snapshot().lyrics.synced[0].words, "theirs");
    let _ = std::fs::remove_dir_all(d);
}

#[test]
fn np_json_reads_the_preferences_section() {
    let d = tmp("cfg");
    std::fs::write(
        d.join("statusify.cfg"),
        "[preferences]\nanimations = false\nrender_quality = LOW\nlyric_font_boost = 40\nlyric_subline = Both\naccent_color = #ff0000\n\n[offsets]\n4uLU6hMCjMI75M1A2tKUQC = 250\n",
    )
    .unwrap();
    let cfg = Config::open(&d);
    let j = np_json(&cfg, "spotify:track:4uLU6hMCjMI75M1A2tKUQC", true);
    assert_eq!(j["animations"], false);
    assert_eq!(j["render_quality"], "low");
    assert_eq!(j["lyric_font_boost"], 10); // clamped
    assert_eq!(j["lyric_subline"], "both");
    assert_eq!(j["accent"], "#ff0000");
    assert_eq!(j["song_offset"], true);
    assert_eq!(j["pinned"], true);
    assert_eq!(j["album_tint"], true); // default
    let k = np_json(&cfg, "spotify:track:other", false);
    assert_eq!(k["song_offset"], false);
    assert_eq!(np_json(&cfg, "", false)["song_offset"], false);
    let _ = std::fs::remove_dir_all(d);
}

#[test]
fn palette_json_shapes() {
    let c = np_art::CoverColors { palette: vec![[200, 30, 30], [30, 30, 200]], tint: Some([200, 40, 40]) };
    let j = palette_json("u", "http://a", Some(&c));
    assert!(j["accent"].as_str().unwrap().starts_with('#'));
    assert_eq!(j["tokens"]["TEXT"].as_str().map(|s| s.len()), Some(7));
    assert_eq!(j["blobs"].as_array().unwrap().len(), 5);
    let grey = np_art::CoverColors { palette: vec![[100, 100, 100]], tint: None };
    let g = palette_json("u", "a", Some(&grey));
    assert!(g["accent"].is_null() && g["tokens"].is_null());
    assert_eq!(g["blobs"].as_array().unwrap().len(), 5);
    assert!(palette_json("u", "a", None)["blobs"].as_array().unwrap().is_empty());
}

#[test]
fn filenames_are_made_safe() {
    assert_eq!(clean_filename("AC/DC: Back In Black?"), "ACDC Back In Black");
    assert_eq!(clean_filename(" - .. "), "");
    assert_eq!(clean_filename(&"x".repeat(300)).len(), 100);
}
