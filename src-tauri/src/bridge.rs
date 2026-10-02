//! WebSocket server the Spicetify extension (lyrics-bridge.js) connects to.
//! Same protocol as the Python app: ws://127.0.0.1:8765, JSON messages.

use crate::engine::Engine;
use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tokio::net::TcpListener;
use tokio::sync::mpsc;
use tokio_tungstenite::tungstenite::Message;

/// Sends to whichever bridge is connected (None when none is).
#[derive(Clone, Default)]
pub struct Outbox(Arc<Mutex<Option<mpsc::UnboundedSender<Value>>>>);

impl Outbox {
    pub fn send(&self, v: Value) -> bool {
        self.0.lock().unwrap().as_ref().is_some_and(|tx| tx.send(v).is_ok())
    }
}

pub async fn bind(port: u16) -> std::io::Result<TcpListener> {
    TcpListener::bind(("127.0.0.1", port)).await
}

pub async fn serve(listener: TcpListener, engine: Arc<Engine>, outbox: Outbox) {
    loop {
        let Ok((sock, _)) = listener.accept().await else { continue };
        let Ok(ws) = tokio_tungstenite::accept_async(sock).await else { continue };
        crate::log("[Bridge] connected");
        let (mut tx, mut rx) = ws.split();
        let (out_tx, mut out_rx) = mpsc::unbounded_channel::<Value>();
        *outbox.0.lock().unwrap() = Some(out_tx.clone());
        engine.update(|s| s.bridge_connected = true);

        // Give the extension's onmessage handler a turn to wire up first.
        let req = out_tx.clone();
        tokio::spawn(async move {
            tokio::time::sleep(Duration::from_millis(300)).await;
            let _ = req.send(json!({"type": "request_state"}));
        });
        let writer = tokio::spawn(async move {
            while let Some(v) = out_rx.recv().await {
                if tx.send(Message::text(v.to_string())).await.is_err() {
                    break;
                }
            }
        });
        while let Some(Ok(m)) = rx.next().await {
            if let Message::Text(t) = m {
                if let Ok(v) = serde_json::from_str::<Value>(&t) {
                    engine.handle(&v);
                }
            }
        }
        writer.abort();
        *outbox.0.lock().unwrap() = None;
        engine.update(|s| s.bridge_connected = false);
        crate::log("[Bridge] disconnected");
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn bridge_messages_reach_engine_and_commands_reach_bridge() {
        let mut e = Engine::new(None, |_| {});
        Arc::get_mut(&mut e).unwrap().lrclib_enabled = false;
        let l = bind(0).await.unwrap();
        let port = l.local_addr().unwrap().port();
        let out = Outbox::default();
        tokio::spawn(serve(l, e.clone(), out.clone()));

        let (mut ws, _) = tokio_tungstenite::connect_async(format!("ws://127.0.0.1:{port}")).await.unwrap();
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
}
