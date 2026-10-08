//! WebSocket server the Spicetify extension (lyrics-bridge.js) connects to.
//! Same protocol as the Python app: ws://127.0.0.1:8765, JSON messages.
//!
//! Every connection runs on its own task, so a client that stalls (a stuck
//! handshake, a half-open socket) cannot keep the real bridge out. A
//! connection becomes the bridge with its first message; the newest one feeds
//! the engine and receives the app's commands. Older ones are kept (a reload
//! can leave a dead socket behind) and take over again if the newest goes
//! away. Every connection is pinged, and dropped once nothing at all, not
//! even a pong, has arrived for IDLE_TIMEOUT (the browser answers pings from
//! its network stack, so a quiet but alive Spotify is never mistaken for dead).
//! A connection that takes over from the newest one is asked for the state and
//! has TAKEOVER_ANSWER to say anything: a half-open socket left behind by a
//! reload is dropped in seconds, not after the idle timeout, and never keeps
//! the app showing a bridge that is not there.

use crate::engine::Engine;
use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use std::panic::AssertUnwindSafe;
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::mpsc;
use tokio::time::{Instant, MissedTickBehavior};
use tokio_tungstenite::tungstenite::Message;
use tokio_tungstenite::WebSocketStream;

/// A client has this long to complete the WebSocket handshake.
pub const HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(3);
pub const PING_EVERY: Duration = Duration::from_secs(5);
/// A connection that has sent nothing for this long (checked at every ping)
/// is dead.
pub const IDLE_TIMEOUT: Duration = Duration::from_secs(15);
/// A write the peer does not take within this long ends the connection.
const WRITE_TIMEOUT: Duration = Duration::from_secs(5);
/// Give the extension's onmessage handler a turn to wire up first.
const REQUEST_STATE_DELAY: Duration = Duration::from_millis(300);
/// A connection that has just become the bridge again (the newest one left)
/// is asked for the state; a live bridge answers at once. One that says
/// nothing within this long is a dead socket.
pub const TAKEOVER_ANSWER: Duration = Duration::from_secs(3);

#[derive(Clone, Copy)]
pub struct Timing {
    pub handshake: Duration,
    pub ping: Duration,
    pub idle: Duration,
    pub answer: Duration,
}

impl Default for Timing {
    fn default() -> Self {
        Timing { handshake: HANDSHAKE_TIMEOUT, ping: PING_EVERY, idle: IDLE_TIMEOUT, answer: TAKEOVER_ANSWER }
    }
}

/// What a connection task does with a message from its peer.
type Handler = Arc<dyn Fn(&Arc<Engine>, &Value) + Send + Sync>;

struct Slot {
    id: u64,
    tx: mpsc::UnboundedSender<Value>,
}

/// Sends to whichever bridge is connected (nothing when none is). The
/// connections that have spoken, oldest first; the last one is the bridge.
#[derive(Clone, Default)]
pub struct Outbox(Arc<Mutex<Vec<Slot>>>);

impl Outbox {
    pub fn send(&self, v: Value) -> bool {
        self.0.lock().unwrap().last().is_some_and(|s| s.tx.send(v).is_ok())
    }

    fn connected(&self) -> bool {
        !self.0.lock().unwrap().is_empty()
    }

    /// Make connection `id` the bridge; how many connections there are now.
    fn install(&self, id: u64, tx: mpsc::UnboundedSender<Value>) -> usize {
        let mut g = self.0.lock().unwrap();
        g.push(Slot { id, tx });
        g.len()
    }

    fn is_current(&self, id: u64) -> bool {
        self.0.lock().unwrap().last().is_some_and(|s| s.id == id)
    }

    /// Forget connection `id`. If it was the bridge the next newest one takes
    /// over and is asked for the state. True when no connection is left.
    fn release(&self, id: u64) -> bool {
        let mut g = self.0.lock().unwrap();
        let was_current = g.last().is_some_and(|s| s.id == id);
        g.retain(|s| s.id != id);
        if was_current {
            if let Some(s) = g.last() {
                let _ = s.tx.send(json!({"type": "request_state"}));
            }
        }
        g.is_empty()
    }
}

pub async fn bind(port: u16) -> std::io::Result<TcpListener> {
    TcpListener::bind(("127.0.0.1", port)).await
}

pub async fn serve(listener: TcpListener, engine: Arc<Engine>, outbox: Outbox) {
    serve_with(listener, engine, outbox, Timing::default()).await
}

pub async fn serve_with(listener: TcpListener, engine: Arc<Engine>, outbox: Outbox, timing: Timing) {
    serve_handling(listener, engine, outbox, timing, Arc::new(|e, v| e.handle(v))).await
}

async fn serve_handling(listener: TcpListener, engine: Arc<Engine>, outbox: Outbox, timing: Timing, handler: Handler) {
    let mut id = 0;
    loop {
        let sock = match listener.accept().await {
            Ok((s, _)) => s,
            Err(_) => {
                tokio::time::sleep(Duration::from_millis(100)).await;
                continue;
            }
        };
        id += 1;
        tokio::spawn(connection(sock, id, engine.clone(), outbox.clone(), timing, handler.clone()));
    }
}

async fn send(ws: &mut WebSocketStream<TcpStream>, m: Message) -> bool {
    matches!(tokio::time::timeout(WRITE_TIMEOUT, ws.send(m)).await, Ok(Ok(())))
}

/// A connection's place in the outbox. Dropping it, however the task ends (a
/// panic in the handler included), frees the slot, so a dead connection can
/// never stay the bridge or leave `bridge_connected` set.
struct Lease {
    outbox: Outbox,
    engine: Arc<Engine>,
    id: u64,
}

impl Drop for Lease {
    fn drop(&mut self) {
        if std::thread::panicking() {
            crate::log("[Bridge] a connection task panicked, releasing its slot");
        }
        // Never panic in here: during an unwind that would abort the process.
        let _ = std::panic::catch_unwind(AssertUnwindSafe(|| {
            if self.outbox.release(self.id) {
                self.engine.update(|s| s.bridge_connected = false);
                // One that spoke up meanwhile has already set it.
                if self.outbox.connected() {
                    self.engine.update(|s| s.bridge_connected = true);
                }
            }
        }));
    }
}

async fn connection(sock: TcpStream, id: u64, engine: Arc<Engine>, outbox: Outbox, t: Timing, handler: Handler) {
    let Ok(Ok(mut ws)) = tokio::time::timeout(t.handshake, tokio_tungstenite::accept_async(sock)).await else { return };
    let (out_tx, mut out_rx) = mpsc::unbounded_channel::<Value>();
    // Handed to the outbox with the first message.
    let mut pending_tx = Some(out_tx);
    let mut ping = tokio::time::interval_at(Instant::now() + t.ping, t.ping);
    ping.set_missed_tick_behavior(MissedTickBehavior::Delay);
    let request = tokio::time::sleep(REQUEST_STATE_DELAY);
    tokio::pin!(request);
    let mut requested = false;
    let mut last_rx = Instant::now();
    // Set when the state is requested of a connection that took over: it must
    // answer by then.
    let mut answer_by: Option<Instant> = None;
    let mut lease: Option<Lease> = None;
    let why = loop {
        tokio::select! {
            out = out_rx.recv() => match out {
                Some(v) => {
                    let asks = v.get("type").and_then(|x| x.as_str()) == Some("request_state");
                    if !send(&mut ws, Message::text(v.to_string())).await {
                        break "write failed";
                    }
                    if asks {
                        answer_by = Some(Instant::now() + t.answer);
                    }
                }
                None => break "gone",
            },
            m = ws.next() => {
                let Some(Ok(m)) = m else { break "closed" };
                last_rx = Instant::now();
                answer_by = None;
                let Message::Text(text) = m else { continue };
                let Ok(v) = serde_json::from_str::<Value>(&text) else { continue };
                if let Some(tx) = pending_tx.take() {
                    let n = outbox.install(id, tx);
                    lease = Some(Lease { outbox: outbox.clone(), engine: engine.clone(), id });
                    crate::log(&if n > 1 { format!("[Bridge] connected ({n} connections, the newest is used)") } else { "[Bridge] connected".into() });
                    engine.update(|s| s.bridge_connected = true);
                }
                if outbox.is_current(id) {
                    handler(&engine, &v);
                }
            }
            _ = async { tokio::time::sleep_until(answer_by.unwrap()).await }, if answer_by.is_some() => {
                break "no answer after taking over";
            }
            _ = &mut request, if !requested => {
                requested = true;
                if !send(&mut ws, Message::text(json!({"type": "request_state"}).to_string())).await {
                    break "write failed";
                }
            }
            _ = ping.tick() => {
                if last_rx.elapsed() >= t.idle {
                    break "silent";
                }
                if !send(&mut ws, Message::Ping(Vec::new())).await {
                    break "write failed";
                }
            }
        }
    };
    if lease.is_some() {
        crate::log(&format!("[Bridge] disconnected ({why})"));
        // (the lease frees the slot as it goes out of scope)
    } else if why == "silent" {
        crate::log("[Bridge] dropped a connection that never spoke");
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::AtomicBool;
    use tokio::io::AsyncReadExt;
    use tokio_tungstenite::MaybeTlsStream;

    type Client = WebSocketStream<MaybeTlsStream<TcpStream>>;

    async fn up(timing: Timing) -> (u16, Arc<Engine>, Outbox) {
        let mut e = Engine::new(None, |_, _| {});
        Arc::get_mut(&mut e).unwrap().lrclib_enabled = AtomicBool::new(false);
        let l = bind(0).await.unwrap();
        let port = l.local_addr().unwrap().port();
        let out = Outbox::default();
        tokio::spawn(serve_with(l, e.clone(), out.clone(), timing));
        (port, e, out)
    }

    async fn client(port: u16) -> Client {
        tokio_tungstenite::connect_async(format!("ws://127.0.0.1:{port}")).await.unwrap().0
    }

    fn hello() -> Message {
        Message::text(json!({"type": "hello", "version": "t"}).to_string())
    }

    fn track(title: &str) -> Message {
        Message::text(json!({"type": "track_change", "track_uri": "u", "artist": "A", "title": title, "duration_ms": 1000}).to_string())
    }

    async fn until(what: &str, mut f: impl FnMut() -> bool) {
        for _ in 0..300 {
            if f() {
                return;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        panic!("never happened: {what}");
    }

    fn title(e: &Arc<Engine>) -> String {
        e.snapshot().track.map(|t| t.title).unwrap_or_default()
    }

    /// Every text message `c` receives within `ms` (answers pings meanwhile).
    async fn read_for(c: &mut Client, ms: u64) -> Vec<String> {
        let mut seen = vec![];
        let end = tokio::time::Instant::now() + Duration::from_millis(ms);
        while let Ok(Some(Ok(m))) = tokio::time::timeout_at(end, c.next()).await {
            if let Message::Text(t) = m {
                seen.push(t);
            }
        }
        seen
    }

    #[tokio::test]
    async fn bridge_messages_reach_engine_and_commands_reach_bridge() {
        let (port, e, out) = up(Timing::default()).await;

        let mut ws = client(port).await;
        // The app asks for state first.
        let first = ws.next().await.unwrap().unwrap();
        assert!(first.to_text().unwrap().contains("request_state"));
        ws.send(Message::text(json!({"type":"track_change","track_uri":"u","artist":"A","title":"T","duration_ms":1000}).to_string())).await.unwrap();
        tokio::time::sleep(Duration::from_millis(100)).await;
        let s = e.snapshot();
        assert!(s.bridge_connected);
        assert_eq!(s.track.unwrap().title, "T");

        assert!(out.send(json!({"type":"player","action":"next"})));
        let m = ws.next().await.unwrap().unwrap();
        assert!(m.to_text().unwrap().contains("\"next\""));

        drop(ws);
        tokio::time::sleep(Duration::from_millis(100)).await;
        assert!(!e.snapshot().bridge_connected);
        assert!(!out.send(json!({})));
    }

    #[tokio::test]
    async fn port_in_use_is_reported() {
        let a = bind(0).await.unwrap();
        let port = a.local_addr().unwrap().port();
        assert!(bind(port).await.is_err());
    }

    #[tokio::test]
    async fn a_second_client_is_served_while_the_first_is_connected_and_silent() {
        let (port, e, out) = up(Timing::default()).await;
        let mut a = client(port).await;
        a.send(hello()).await.unwrap();
        a.send(track("A")).await.unwrap();
        until("A is the bridge", || title(&e) == "A").await;

        // A never says anything else, and is not read from: B must not wait.
        let mut b = tokio::time::timeout(Duration::from_secs(2), client(port)).await.expect("B is served at once");
        b.send(hello()).await.unwrap();
        b.send(track("B")).await.unwrap();
        until("B is the bridge", || title(&e) == "B").await;

        // A's words stop counting, commands go to B only.
        a.send(track("A again")).await.unwrap();
        tokio::time::sleep(Duration::from_millis(100)).await;
        assert_eq!(title(&e), "B");
        assert!(out.send(json!({"type": "player", "action": "next"})));
        let to_b = read_for(&mut b, 500).await;
        assert!(to_b.iter().any(|m| m.contains("\"next\"")), "{to_b:?}");
        let to_a = read_for(&mut a, 300).await;
        assert!(!to_a.iter().any(|m| m.contains("\"next\"")), "{to_a:?}");
        assert_eq!(to_a.iter().filter(|m| m.contains("request_state")).count(), 1, "A has had its one request_state");
        assert!(e.snapshot().bridge_connected);

        // B leaves: A is the bridge again, asked for the state, still connected.
        drop(b);
        let asked = read_for(&mut a, 500).await;
        assert_eq!(asked.iter().filter(|m| m.contains("request_state")).count(), 1, "{asked:?}");
        assert!(e.snapshot().bridge_connected, "an older connection leaving or taking over is not a disconnect");
        a.send(track("A2")).await.unwrap();
        until("A is the bridge again", || title(&e) == "A2").await;
        assert!(out.send(json!({"type": "player", "action": "prev"})));
        assert!(read_for(&mut a, 300).await.iter().any(|m| m.contains("\"prev\"")));

        drop(a);
        until("disconnected", || !e.snapshot().bridge_connected).await;
        assert!(!out.send(json!({})));
    }

    #[tokio::test]
    async fn an_older_connection_leaving_does_not_disconnect_the_newer_one() {
        let (port, e, out) = up(Timing::default()).await;
        let mut a = client(port).await;
        a.send(hello()).await.unwrap();
        until("A connected", || e.snapshot().bridge_connected).await;
        let mut b = client(port).await;
        b.send(hello()).await.unwrap();
        b.send(track("B")).await.unwrap();
        until("B is the bridge", || title(&e) == "B").await;
        drop(a);
        tokio::time::sleep(Duration::from_millis(200)).await;
        assert!(e.snapshot().bridge_connected);
        assert!(out.send(json!({"type": "player", "action": "next"})));
        assert!(read_for(&mut b, 300).await.iter().any(|m| m.contains("\"next\"")));
    }

    #[tokio::test]
    async fn a_peer_that_never_finishes_the_handshake_blocks_nobody_and_is_dropped() {
        let timing = Timing { handshake: Duration::from_millis(200), ..Timing::default() };
        let (port, e, _out) = up(timing).await;
        let mut stuck = TcpStream::connect(("127.0.0.1", port)).await.unwrap();
        let mut ws = tokio::time::timeout(Duration::from_secs(2), client(port)).await.expect("served behind a stuck peer");
        ws.send(hello()).await.unwrap();
        until("connected", || e.snapshot().bridge_connected).await;
        let mut buf = [0u8; 16];
        let n = tokio::time::timeout(Duration::from_secs(3), stuck.read(&mut buf)).await.expect("the stuck peer is closed").unwrap_or(0);
        assert_eq!(n, 0);
    }

    #[tokio::test]
    async fn a_connection_that_never_speaks_does_not_replace_the_bridge() {
        let (port, e, out) = up(Timing::default()).await;
        let mut a = client(port).await;
        a.send(hello()).await.unwrap();
        a.send(track("A")).await.unwrap();
        until("A is the bridge", || title(&e) == "A").await;
        let _probe = client(port).await; // a port probe: handshake, then nothing
        tokio::time::sleep(Duration::from_millis(100)).await;
        assert!(out.send(json!({"type": "player", "action": "next"})));
        assert!(read_for(&mut a, 300).await.iter().any(|m| m.contains("\"next\"")));
    }

    #[tokio::test]
    async fn a_dead_peer_is_noticed_and_a_quiet_one_that_answers_pings_is_kept() {
        let timing = Timing { handshake: Duration::from_secs(3), ping: Duration::from_millis(100), idle: Duration::from_millis(400), ..Timing::default() };
        let (port, e, out) = up(timing).await;

        // Alive but quiet: it reads (so pings are answered) and says nothing.
        let mut live = client(port).await;
        live.send(hello()).await.unwrap();
        until("connected", || e.snapshot().bridge_connected).await;
        read_for(&mut live, 1500).await;
        assert!(e.snapshot().bridge_connected, "a peer that answers pings stays");
        assert!(out.send(json!({"type": "player", "action": "next"})));
        drop(live);
        until("disconnected", || !e.snapshot().bridge_connected).await;

        // Dead: spoke once, then never reads again, so no pong ever comes.
        let mut dead = client(port).await;
        dead.send(hello()).await.unwrap();
        until("connected", || e.snapshot().bridge_connected).await;
        until("the silent peer is dropped", || !e.snapshot().bridge_connected).await;
        assert!(!out.send(json!({})));
        drop(dead);
    }

    #[tokio::test]
    async fn a_half_open_older_connection_is_dropped_in_seconds_when_it_takes_over() {
        // Pings and the idle timeout are far off: only the takeover deadline
        // can drop A here.
        let timing = Timing { ping: Duration::from_secs(30), idle: Duration::from_secs(60), answer: Duration::from_millis(300), ..Timing::default() };
        let (port, e, out) = up(timing).await;
        let mut a = client(port).await;
        a.send(hello()).await.unwrap();
        a.send(track("A")).await.unwrap();
        until("A is the bridge", || title(&e) == "A").await;
        let mut b = client(port).await;
        b.send(hello()).await.unwrap();
        b.send(track("B")).await.unwrap();
        until("B is the bridge", || title(&e) == "B").await;
        // B leaves (a reload). A is asked for the state, and is not read from
        // by this test: a half-open socket. It must not stay the bridge.
        drop(b);
        until("the app knows nobody is there", || !e.snapshot().bridge_connected).await;
        assert!(!out.send(json!({})));
        drop(a);
    }

    #[tokio::test]
    async fn an_older_connection_that_answers_after_taking_over_is_kept() {
        let timing = Timing { ping: Duration::from_secs(30), idle: Duration::from_secs(60), answer: Duration::from_millis(600), ..Timing::default() };
        let (port, e, out) = up(timing).await;
        let mut a = client(port).await;
        a.send(hello()).await.unwrap();
        a.send(track("A")).await.unwrap();
        until("A is the bridge", || title(&e) == "A").await;
        let mut b = client(port).await;
        b.send(hello()).await.unwrap();
        b.send(track("B")).await.unwrap();
        until("B is the bridge", || title(&e) == "B").await;
        drop(b);
        // A hears the request and answers like the extension does
        let asked = read_for(&mut a, 300).await;
        assert!(asked.iter().any(|m| m.contains("request_state")), "{asked:?}");
        a.send(track("A again")).await.unwrap();
        until("A is the bridge again", || title(&e) == "A again").await;
        tokio::time::sleep(Duration::from_millis(900)).await;
        assert!(e.snapshot().bridge_connected, "it answered, so it stays");
        assert!(out.send(json!({"type": "player", "action": "next"})));
        assert!(read_for(&mut a, 300).await.iter().any(|m| m.contains("\"next\"")));
    }

    #[tokio::test]
    async fn a_panic_in_the_handler_still_frees_the_connection() {
        let mut e = Engine::new(None, |_, _| {});
        Arc::get_mut(&mut e).unwrap().lrclib_enabled = AtomicBool::new(false);
        let l = bind(0).await.unwrap();
        let port = l.local_addr().unwrap().port();
        let out = Outbox::default();
        let handler: Handler = Arc::new(|eng, v| {
            if v["title"] == "boom" {
                panic!("a bug in the handler");
            }
            eng.handle(v);
        });
        tokio::spawn(serve_handling(l, e.clone(), out.clone(), Timing::default(), handler));
        let mut a = client(port).await;
        a.send(hello()).await.unwrap();
        until("A is the bridge", || e.snapshot().bridge_connected).await;
        assert!(out.send(json!({"type": "player", "action": "next"})));
        a.send(track("boom")).await.unwrap();
        until("the slot is freed and the app knows", || !e.snapshot().bridge_connected).await;
        assert!(!out.send(json!({})), "no stale slot is left to send to");
        // and the next connection is the bridge as if nothing happened
        let mut b = client(port).await;
        b.send(hello()).await.unwrap();
        b.send(track("fine")).await.unwrap();
        until("B is the bridge", || title(&e) == "fine").await;
        assert!(e.snapshot().bridge_connected);
    }

    #[tokio::test]
    async fn a_bridge_that_connects_and_drops_over_and_over_leaves_nothing_behind() {
        let (port, e, out) = up(Timing::default()).await;
        for i in 0..30 {
            let mut c = client(port).await;
            c.send(hello()).await.unwrap();
            c.send(track(&format!("T{i}"))).await.unwrap();
            until("it is the bridge", || title(&e) == format!("T{i}")).await;
            drop(c);
            // every third one is replaced before the server has noticed it go
            if i % 3 != 0 {
                until("noticed gone", || !e.snapshot().bridge_connected).await;
            }
        }
        until("all gone", || !e.snapshot().bridge_connected).await;
        assert!(!out.send(json!({})), "no slot is left behind");
    }
}
