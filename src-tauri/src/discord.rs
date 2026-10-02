//! Discord Rich Presence over the local IPC named pipe.
//!
//! Frames are `<op:u32 LE><len:u32 LE><json>`. The pipe is opened overlapped
//! (tokio), so a stalled read can never block a write the way the Python
//! client's synchronous pipe could.

use serde_json::{json, Value};
use std::time::Duration;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::windows::named_pipe::{ClientOptions, NamedPipeClient};
use tokio::sync::mpsc;

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

fn open_pipe(prefix: &str) -> Option<NamedPipeClient> {
    (0..10).find_map(|i| ClientOptions::new().open(format!("{prefix}{i}")).ok())
}

/// Presence updates for the connection task: Some(activity) or None to clear.
pub type Update = Option<Value>;

pub enum Status {
    Connected(String),
    Disconnected,
}

/// Keep a connection to Discord alive forever, sending the newest update.
/// After a reconnect the last update is re-sent so the profile catches up.
pub async fn run(app_id: String, rx: mpsc::UnboundedReceiver<Update>, status: impl Fn(Status) + Send + 'static) {
    run_on(PIPE_PREFIX, app_id, rx, status).await
}

pub async fn run_on(prefix: &str, app_id: String, mut rx: mpsc::UnboundedReceiver<Update>, status: impl Fn(Status) + Send + 'static) {
    let mut last: Update = None;
    let mut nonce: u64 = 0;
    loop {
        if let Some(pipe) = open_pipe(prefix) {
            let (mut rd, mut wr) = tokio::io::split(pipe);
            let hs = frame(OP_HANDSHAKE, &json!({"v": 1, "client_id": app_id}));
            let ready = async {
                wr.write_all(&hs).await?;
                tokio::time::timeout(Duration::from_secs(5), read_frame(&mut rd))
                    .await
                    .map_err(|_| std::io::Error::new(std::io::ErrorKind::TimedOut, "handshake"))?
            }
            .await;
            match ready {
                Ok(v) if v.get("evt").and_then(|e| e.as_str()) == Some("READY") => {
                    let user = v["data"]["user"]["username"].as_str().unwrap_or("?").to_string();
                    status(Status::Connected(user));
                    // Reader: drain replies; log errors; ends when the pipe dies.
                    let mut reader = tokio::spawn(async move {
                        while let Ok(v) = read_frame(&mut rd).await {
                            if v.get("evt").and_then(|e| e.as_str()) == Some("ERROR") {
                                crate::log(&format!("RPC error: {}", v["data"]["message"]));
                            }
                        }
                    });
                    let mut pending = last.clone().map(Some);
                    loop {
                        if let Some(act) = pending.take() {
                            nonce += 1;
                            let msg = json!({"cmd": "SET_ACTIVITY",
                                "args": {"pid": std::process::id(), "activity": act},
                                "nonce": nonce.to_string()});
                            if wr.write_all(&frame(OP_FRAME, &msg)).await.is_err() {
                                break;
                            }
                        }
                        tokio::select! {
                            u = rx.recv() => match u {
                                Some(mut u) => {
                                    // Only the newest update matters.
                                    while let Ok(n) = rx.try_recv() { u = n; }
                                    last = u.clone();
                                    pending = Some(u);
                                }
                                None => return,
                            },
                            _ = &mut reader => break,
                        }
                    }
                    reader.abort();
                    status(Status::Disconnected);
                }
                _ => {}
            }
        }
        // Not running / not connected: absorb updates so `last` stays current.
        let wait = tokio::time::sleep(Duration::from_secs(5));
        tokio::pin!(wait);
        loop {
            tokio::select! {
                _ = &mut wait => break,
                u = rx.recv() => match u { Some(u) => last = u, None => return },
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::net::windows::named_pipe::ServerOptions;

    /// A fake Discord: handshake, then record SET_ACTIVITY frames.
    #[tokio::test]
    async fn handshake_then_newest_activity_is_sent() {
        let prefix = format!(r"\\.\pipe\statusify-test-{}-", std::process::id());
        let server = ServerOptions::new().first_pipe_instance(true).create(format!("{prefix}0")).unwrap();
        let (tx, rx) = mpsc::unbounded_channel();
        let (st_tx, mut st_rx) = mpsc::unbounded_channel();
        let p = prefix.clone();
        tokio::spawn(async move {
            run_on(&p, "123".into(), rx, move |s| {
                let _ = st_tx.send(matches!(s, Status::Connected(_)));
            }).await
        });
        server.connect().await.unwrap();
        let (mut rd, mut wr) = tokio::io::split(server);
        let hs = read_frame(&mut rd).await.unwrap();
        assert_eq!(hs["client_id"], "123");
        wr.write_all(&frame(OP_FRAME, &json!({"evt": "READY", "data": {"user": {"username": "kurepa"}}}))).await.unwrap();
        assert_eq!(st_rx.recv().await, Some(true));

        tx.send(Some(json!({"state": "old"}))).unwrap();
        let f = read_frame(&mut rd).await.unwrap();
        assert_eq!(f["cmd"], "SET_ACTIVITY");
        assert_eq!(f["args"]["activity"]["state"], "old");
        tx.send(None).unwrap();
        let f = read_frame(&mut rd).await.unwrap();
        assert!(f["args"]["activity"].is_null());

        // Discord quits: the client notices and reports it.
        drop(rd); drop(wr);
        assert_eq!(st_rx.recv().await, Some(false));
    }

    #[tokio::test]
    async fn frames_round_trip() {
        let f = frame(OP_FRAME, &json!({"a": 1}));
        assert_eq!(&f[..4], &1u32.to_le_bytes());
        let mut cur = std::io::Cursor::new(f);
        assert_eq!(read_frame(&mut cur).await.unwrap(), json!({"a": 1}));
    }
}
