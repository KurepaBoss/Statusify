//! Discord Rich Presence over the local IPC named pipe.
//!
//! Frames are `<op:u32 LE><len:u32 LE><json>`. The pipe is opened overlapped
//! (tokio), so a stalled read can never block a write the way the Python
//! client's synchronous pipe could; writes and the handshake still time out
//! after PIPE_TIMEOUT, so a Discord that stops reading forces a reconnect.
//! Failures are logged and kept in `Link::error` for the UI.

use serde_json::{json, Value};
use std::time::{Duration, Instant};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::windows::named_pipe::{ClientOptions, NamedPipeClient};
use futures_util::FutureExt;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use tokio::sync::{mpsc, Notify};

const OP_HANDSHAKE: u32 = 0;
const OP_FRAME: u32 = 1;

pub fn frame(op: u32, body: &Value) -> Vec<u8> {
    let p = serde_json::to_vec(body).unwrap();
    let mut out = Vec::with_capacity(8 + p.len());
    out.extend_from_slice(&op.to_le_bytes());
    out.extend_from_slice(&(p.len() as u32).to_le_bytes());
    out.extend_from_slice(&p);
    out
}

async fn read_frame<R: AsyncReadExt + Unpin>(r: &mut R) -> std::io::Result<Value> {
    let mut h = [0u8; 8];
    r.read_exact(&mut h).await?;
    let n = u32::from_le_bytes(h[4..8].try_into().unwrap()) as usize;
    let mut body = vec![0u8; n];
    r.read_exact(&mut body).await?;
    serde_json::from_slice(&body).map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidData, e))
}

pub const PIPE_PREFIX: &str = r"\\.\pipe\discord-ipc-";

/// A write or the handshake reply taking longer than this means Discord has
/// stopped reading (a hung client, or a game holding the pipe): drop the
/// pipe and reconnect (statusify_rpc.PIPE_TIMEOUT_S).
pub const PIPE_TIMEOUT: Duration = Duration::from_secs(5);
/// When to try again after a failed connect, a failed handshake or a
/// connection that dropped (main.py _backend waited a flat 5 s / 15 s): `first`,
/// doubling up to `cap_missing` while there is no pipe to open (opening one is
/// a cheap syscall, and Discord restarts and updates are common) and up to
/// `cap_other` while there is a pipe that does not work. A connection that
/// lasted `stable` starts the sequence over; a shorter one does not, so a
/// Discord that hangs up at once is not redialled in a tight loop.
#[derive(Clone, Copy, Debug)]
pub struct Retry {
    pub first: Duration,
    pub cap_missing: Duration,
    pub cap_other: Duration,
    pub stable: Duration,
}

pub const RETRY: Retry = Retry {
    first: Duration::from_millis(250),
    cap_missing: Duration::from_secs(2),
    cap_other: Duration::from_secs(5),
    stable: Duration::from_secs(10),
};
/// While the same failure goes on it is logged this often.
const LOG_EVERY: Duration = Duration::from_secs(30);

fn backoff(first: Duration, cap: Duration, attempt: u32) -> Duration {
    first.saturating_mul(1 << attempt.min(16)).min(cap)
}

/// True when something may be logged now (and notes that it was).
fn due(last: &mut Option<Instant>, every: Duration) -> bool {
    let now = last.is_none_or(|t| t.elapsed() >= every);
    if now {
        *last = Some(Instant::now());
    }
    now
}

fn open_pipe(prefix: &str) -> Option<NamedPipeClient> {
    (0..10).find_map(|i| ClientOptions::new().open(format!("{prefix}{i}")).ok())
}

/// Presence updates for the connection task: Some(activity) or None to clear.
pub type Update = Option<Value>;

/// The connection's control and status, shared with the UI side.
pub struct Link {
    reconnect: Notify,
    connected: AtomicBool,
    error: Mutex<String>,
    retry: Retry,
    timeout: Duration,
}

impl Link {
    pub const fn new(retry: Retry, timeout: Duration) -> Link {
        Link {
            reconnect: Notify::const_new(),
            connected: AtomicBool::new(false),
            error: Mutex::new(String::new()),
            retry,
            timeout,
        }
    }

    /// Why the presence is not reaching Discord, "" when it is (or no
    /// attempt has failed yet). Shown in the UI as extras.core.discord_error.
    pub fn error(&self) -> String {
        self.error.lock().unwrap().clone()
    }

    fn set_error(&self, e: &str) {
        *self.error.lock().unwrap() = e.to_string();
    }

    pub fn clear_error(&self) {
        self.set_error("");
    }

    #[allow(dead_code)] // status API (tests, UI)
    pub fn is_connected(&self) -> bool {
        self.connected.load(Ordering::Relaxed)
    }

    /// Drop the current pipe and re-handshake (Discord restarted, or a game
    /// grabbed the IPC pipe and left the presence dead); while not
    /// connected, retry at once instead of waiting out the retry delay.
    /// A permit is stored, so a request made mid-write or mid-wait is never
    /// lost; one left over from before a fresh handshake is discarded.
    pub fn request_reconnect(&self) {
        self.reconnect.notify_one();
    }
}

/// The app's one Discord connection.
pub static LINK: Link = Link::new(RETRY, PIPE_TIMEOUT);

pub enum Status {
    Connected(String),
    Disconnected,
}

enum Ended {
    /// There is no Discord pipe to open.
    Missing,
    /// The pipe opened, but the handshake failed.
    Failed(String),
    /// The connection worked, then dropped.
    Dropped,
    /// The user asked for a fresh handshake.
    Requested,
}

async fn timed<T>(limit: Duration, what: &str, f: impl std::future::Future<Output = std::io::Result<T>>) -> std::io::Result<T> {
    tokio::time::timeout(limit, f)
        .await
        .map_err(|_| std::io::Error::new(std::io::ErrorKind::TimedOut, format!("{what} timed out")))?
}

/// Keep a connection to Discord alive forever, sending the newest update.
/// Nothing is replayed after a reconnect: the presence loop starts over
/// (fresh rate ledger, empty screen) the moment it sees the connection, and
/// a stale replay would spend a frame the ledger never saw.
pub async fn run(app_id: String, rx: mpsc::UnboundedReceiver<Update>, status: impl Fn(Status) + Send + 'static) {
    run_on(&LINK, PIPE_PREFIX, app_id, rx, status).await
}

pub async fn run_on(link: &Link, prefix: &str, app_id: String, mut rx: mpsc::UnboundedReceiver<Update>, status: impl Fn(Status) + Send + 'static) {
    let mut nonce: u64 = 0;
    // Failed tries since the last stable connection; when a failure was last logged.
    let mut attempt: u32 = 0;
    let mut logged: Option<Instant> = None;
    loop {
        let mut up_since = None;
        let ended = match open_pipe(prefix) {
            None => Ended::Missing,
            Some(pipe) => {
                let (mut rd, mut wr) = tokio::io::split(pipe);
                let hs = frame(OP_HANDSHAKE, &json!({"v": 1, "client_id": app_id}));
                let ready = async {
                    timed(link.timeout, "handshake", wr.write_all(&hs)).await?;
                    timed(link.timeout, "handshake", read_frame(&mut rd)).await
                }
                .await;
                match ready {
                    Ok(v) if v.get("evt").and_then(|e| e.as_str()) == Some("READY") => {
                        let user = v["data"]["user"]["username"].as_str().unwrap_or("?").to_string();
                        // A reconnect asked for while we were handshaking is
                        // already satisfied.
                        let _ = link.reconnect.notified().now_or_never();
                        link.connected.store(true, Ordering::Relaxed);
                        link.clear_error();
                        up_since = Some(Instant::now());
                        logged = None;
                        status(Status::Connected(user));
                        // Reader: drain replies; log errors; ends when the pipe dies.
                        let mut reader = tokio::spawn(async move {
                            while let Ok(v) = read_frame(&mut rd).await {
                                if v.get("evt").and_then(|e| e.as_str()) == Some("ERROR") {
                                    crate::log(&format!("RPC error: {}", v["data"]["message"]));
                                }
                            }
                        });
                        let mut pending: Option<Update> = None;
                        let ended = loop {
                            if let Some(act) = pending.take() {
                                nonce += 1;
                                let msg = json!({"cmd": "SET_ACTIVITY",
                                    "args": {"pid": std::process::id(), "activity": act},
                                    "nonce": nonce.to_string()});
                                // A write Discord never takes must not wedge
                                // the loop: no reconnect could ever run then.
                                if let Err(e) = timed(link.timeout, "write", wr.write_all(&frame(OP_FRAME, &msg))).await {
                                    crate::log(&format!("Pipe error: {e}"));
                                    break Ended::Dropped;
                                }
                            }
                            tokio::select! {
                                u = rx.recv() => match u {
                                    Some(mut u) => {
                                        // Only the newest update matters.
                                        while let Ok(n) = rx.try_recv() { u = n; }
                                        pending = Some(u);
                                    }
                                    None => {
                                        reader.abort();
                                        link.connected.store(false, Ordering::Relaxed);
                                        return;
                                    }
                                },
                                _ = &mut reader => break Ended::Dropped,
                                _ = link.reconnect.notified() => break Ended::Requested,
                            }
                        };
                        reader.abort();
                        link.connected.store(false, Ordering::Relaxed);
                        status(Status::Disconnected);
                        ended
                    }
                    Ok(v) => Ended::Failed(format!("Handshake failed: {v}")),
                    Err(e) => Ended::Failed(format!("Handshake failed: {e}")),
                }
            }
        };
        if up_since.is_some_and(|t| t.elapsed() >= link.retry.stable) {
            attempt = 0;
        }
        let r = &link.retry;
        let wait = match ended {
            Ended::Requested => {
                crate::log("Reconnect requested — re-handshaking");
                Duration::ZERO
            }
            ended => {
                let (cap, failure) = match ended {
                    Ended::Missing => (r.cap_missing, Some("Discord IPC pipe not found".to_string())),
                    Ended::Failed(e) => (r.cap_other, Some(e)),
                    _ => (r.cap_other, None),
                };
                match failure {
                    Some(e) => {
                        if due(&mut logged, LOG_EVERY) {
                            crate::log(&format!("RPC unavailable: {e}  — retrying"));
                        }
                        link.set_error(&format!("Discord unreachable: {e} (retrying)"));
                    }
                    None => {
                        if due(&mut logged, LOG_EVERY) {
                            crate::log("RPC disconnected — reconnecting");
                        }
                        link.set_error("Discord pipe closed (reconnecting)");
                    }
                }
                let wait = backoff(r.first, cap, attempt);
                attempt = attempt.saturating_add(1);
                wait
            }
        };
        // Not connected: drop updates meanwhile; retry after `wait`
        // (sooner when a reconnect is asked for).
        let sleep = tokio::time::sleep(wait);
        tokio::pin!(sleep);
        loop {
            tokio::select! {
                _ = &mut sleep => break,
                _ = link.reconnect.notified() => {
                    crate::log("Reconnect requested — retrying Discord now");
                    break;
                }
                u = rx.recv() => if u.is_none() { return },
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::net::windows::named_pipe::{NamedPipeServer, ServerOptions};

    fn ms(n: u64) -> Duration {
        Duration::from_millis(n)
    }

    fn link_with(retry: Retry, timeout_ms: u64) -> &'static Link {
        Box::leak(Box::new(Link::new(retry, ms(timeout_ms))))
    }

    /// A link retrying after `first_ms`, doubling up to `cap_ms`.
    fn link(first_ms: u64, cap_ms: u64, timeout_ms: u64) -> &'static Link {
        link_with(Retry { first: ms(first_ms), cap_missing: ms(cap_ms), cap_other: ms(cap_ms), stable: Duration::from_secs(10) }, timeout_ms)
    }

    fn prefix(tag: &str) -> String {
        let n = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos();
        format!(r"\\.\pipe\statusify-test-{tag}-{}-{n}-", std::process::id())
    }

    fn server(prefix: &str, first: bool) -> NamedPipeServer {
        ServerOptions::new().first_pipe_instance(first).create(format!("{prefix}0")).unwrap()
    }

    type StatusRx = mpsc::UnboundedReceiver<bool>;

    fn start(link: &'static Link, prefix: &str) -> (mpsc::UnboundedSender<Update>, StatusRx) {
        let (tx, rx) = mpsc::unbounded_channel();
        let (st_tx, st_rx) = mpsc::unbounded_channel();
        let p = prefix.to_string();
        tokio::spawn(async move {
            run_on(link, &p, "123".into(), rx, move |s| {
                let _ = st_tx.send(matches!(s, Status::Connected(_)));
            })
            .await
        });
        (tx, st_rx)
    }

    async fn ready(server: NamedPipeServer) -> (tokio::io::ReadHalf<NamedPipeServer>, tokio::io::WriteHalf<NamedPipeServer>) {
        server.connect().await.unwrap();
        let (mut rd, mut wr) = tokio::io::split(server);
        let hs = read_frame(&mut rd).await.unwrap();
        assert_eq!(hs["client_id"], "123");
        wr.write_all(&frame(OP_FRAME, &json!({"evt": "READY", "data": {"user": {"username": "kurepa"}}}))).await.unwrap();
        (rd, wr)
    }

    async fn wait_for(mut f: impl FnMut() -> bool) {
        for _ in 0..200 {
            if f() {
                return;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        panic!("condition never became true");
    }

    /// A fake Discord: handshake, then record SET_ACTIVITY frames.
    #[tokio::test]
    async fn handshake_then_newest_activity_is_sent() {
        let p = prefix("hs");
        let srv = server(&p, true);
        let l = link(5000, 5000, 5000);
        let (tx, mut st_rx) = start(l, &p);
        let (mut rd, wr) = ready(srv).await;
        assert_eq!(st_rx.recv().await, Some(true));
        assert!(l.is_connected());
        assert_eq!(l.error(), "");

        tx.send(Some(json!({"state": "old"}))).unwrap();
        let f = read_frame(&mut rd).await.unwrap();
        assert_eq!(f["cmd"], "SET_ACTIVITY");
        assert_eq!(f["args"]["activity"]["state"], "old");
        tx.send(None).unwrap();
        let f = read_frame(&mut rd).await.unwrap();
        assert!(f["args"]["activity"].is_null());

        // Discord quits: the client notices, reports it and says why.
        drop(rd);
        drop(wr);
        assert_eq!(st_rx.recv().await, Some(false));
        assert!(!l.is_connected());
        wait_for(|| l.error().starts_with("Discord pipe closed")).await;
    }

    #[tokio::test]
    async fn missing_pipe_is_reported_and_retried_slowly() {
        let p = prefix("none");
        let l = link(60_000, 60_000, 5000);
        let (_tx, _st) = start(l, &p);
        wait_for(|| l.error().contains("Discord IPC pipe not found")).await;
        assert!(l.error().starts_with("Discord unreachable:"));
        // A reconnect request skips the long wait: Discord appears, and the
        // client connects without waiting 60 s.
        let srv = server(&p, true);
        l.request_reconnect();
        let _io = tokio::time::timeout(Duration::from_secs(5), ready(srv)).await.expect("retried at once");
        wait_for(|| l.is_connected()).await;
        assert_eq!(l.error(), "");
    }

    #[tokio::test]
    async fn rejected_handshake_is_reported() {
        let p = prefix("bad");
        let srv = server(&p, true);
        let l = link(60_000, 60_000, 5000);
        let (_tx, _st) = start(l, &p);
        srv.connect().await.unwrap();
        let (mut rd, mut wr) = tokio::io::split(srv);
        read_frame(&mut rd).await.unwrap();
        wr.write_all(&frame(2, &json!({"code": 4000, "message": "Invalid Client ID"}))).await.unwrap();
        wait_for(|| l.error().contains("Handshake failed")).await;
        assert!(l.error().contains("Invalid Client ID"));
        assert!(!l.is_connected());
    }

    #[tokio::test]
    async fn a_discord_that_stops_reading_forces_a_reconnect() {
        let p = prefix("stall");
        let srv = ServerOptions::new().first_pipe_instance(true).in_buffer_size(64).create(format!("{p}0")).unwrap();
        let l = link(60_000, 60_000, 200);
        let (tx, mut st_rx) = start(l, &p);
        let (_rd, _wr) = ready(srv).await; // never read again
        assert_eq!(st_rx.recv().await, Some(true));
        let big = "x".repeat(256 * 1024);
        for _ in 0..4 {
            let _ = tx.send(Some(json!({"state": big})));
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
        let r = tokio::time::timeout(Duration::from_secs(5), st_rx.recv()).await.expect("write timed out and dropped");
        assert_eq!(r, Some(false));
        wait_for(|| l.error().starts_with("Discord pipe closed")).await;
    }

    #[tokio::test]
    async fn reconnect_request_rehandshakes() {
        let p = prefix("re");
        let srv = server(&p, true);
        let l = link(60_000, 60_000, 5000);
        let (_tx, mut st_rx) = start(l, &p);
        let _io = ready(srv).await;
        assert_eq!(st_rx.recv().await, Some(true));
        let srv2 = server(&p, false);
        l.request_reconnect();
        assert_eq!(st_rx.recv().await, Some(false));
        let _io2 = tokio::time::timeout(Duration::from_secs(5), ready(srv2)).await.expect("re-handshake at once");
        assert_eq!(st_rx.recv().await, Some(true));
        assert_eq!(l.error(), "");
    }

    #[test]
    fn the_retry_delay_doubles_up_to_its_cap() {
        let d = |n| backoff(ms(250), Duration::from_secs(2), n).as_millis();
        assert_eq!((0..6).map(d).collect::<Vec<_>>(), vec![250, 500, 1000, 2000, 2000, 2000]);
        assert_eq!(d(100_000), 2000, "no overflow");
    }

    #[test]
    fn a_failure_is_logged_once_and_then_only_now_and_then() {
        let mut last = None;
        assert!(due(&mut last, Duration::from_secs(30)));
        assert!(!due(&mut last, Duration::from_secs(30)));
        assert!(!due(&mut last, Duration::from_secs(30)));
        assert!(due(&mut last, Duration::ZERO));
    }

    /// Discord coming back is found within the retry cap, not after the old
    /// flat 5 s / 15 s.
    #[tokio::test]
    async fn a_returning_discord_is_found_within_the_cap() {
        let p = prefix("back");
        let l = link(20, 60, 5000);
        let (_tx, mut st_rx) = start(l, &p);
        wait_for(|| l.error().contains("pipe not found")).await;
        tokio::time::sleep(ms(300)).await; // several tries fail meanwhile
        let srv = server(&p, true);
        let t = Instant::now();
        let _io = tokio::time::timeout(Duration::from_secs(2), ready(srv)).await.expect("found again");
        assert_eq!(st_rx.recv().await, Some(true));
        assert!(t.elapsed() < ms(400), "took {:?} with a 60 ms cap", t.elapsed());
        assert_eq!(l.error(), "");
    }

    /// A Discord that hangs up right after every handshake is not redialled
    /// in a tight loop: the delay keeps growing until a connection lasts.
    #[tokio::test]
    async fn a_discord_that_hangs_up_at_once_is_redialled_ever_more_slowly() {
        let p = prefix("flap");
        let l = link_with(Retry { first: ms(40), cap_missing: ms(2000), cap_other: ms(2000), stable: Duration::from_secs(60) }, 5000);
        let mut srv = server(&p, true);
        let (_tx, _st) = start(l, &p);
        let mut gaps = vec![];
        let mut hung_up: Option<Instant> = None;
        for _ in 0..6 {
            let (rd, wr) = ready(srv).await;
            if let Some(t) = hung_up {
                gaps.push(t.elapsed());
            }
            let next = server(&p, false); // there before the client comes back
            hung_up = Some(Instant::now());
            drop(rd);
            drop(wr);
            srv = next;
        }
        assert!(gaps[0] < ms(400), "the first redial is quick: {gaps:?}");
        assert!(gaps[4] > gaps[0] * 4, "and they get slower: {gaps:?}");
    }

    #[tokio::test]
    async fn a_connection_that_lasted_starts_the_delays_over() {
        let p = prefix("stable");
        let l = link_with(Retry { first: ms(40), cap_missing: ms(1000), cap_other: ms(1000), stable: ms(150) }, 5000);
        let (_tx, _st) = start(l, &p);
        // No Discord for a while: the delay has grown to hundreds of ms.
        tokio::time::sleep(ms(700)).await;
        let srv = server(&p, true);
        let (rd, wr) = tokio::time::timeout(Duration::from_secs(3), ready(srv)).await.expect("connected");
        tokio::time::sleep(ms(300)).await; // longer than `stable`
        let next = server(&p, false);
        let t = Instant::now();
        drop(rd);
        drop(wr);
        let _io = tokio::time::timeout(Duration::from_secs(3), ready(next)).await.expect("back");
        assert!(t.elapsed() < ms(300), "redialled after {:?}, not the grown delay", t.elapsed());
    }

    #[tokio::test]
    async fn frames_round_trip() {
        let f = frame(OP_FRAME, &json!({"a": 1}));
        assert_eq!(&f[..4], &1u32.to_le_bytes());
        let mut cur = std::io::Cursor::new(f);
        assert_eq!(read_frame(&mut cur).await.unwrap(), json!({"a": 1}));
    }
}
