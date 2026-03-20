//! WebSocket client for real-time CLOB order book updates.
//!
//! Connects to `wss://ws-subscriptions-clob.polymarket.com/ws/market`
//! and streams `price_change` / `book` events so the collector gets
//! live best-bid/ask instead of polling the REST `/book` endpoint.

use crate::price_fetcher::BookSnapshot;
use log::{debug, info, warn};
use serde_json::Value;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};
use tungstenite::Message;

pub const CLOB_WS_URL: &str = "wss://ws-subscriptions-clob.polymarket.com/ws/market";
const PING_INTERVAL_S: f64 = 10.0;
const RECONNECT_DELAY_S: f64 = 1.0;
const MAX_RECONNECT_DELAY_S: f64 = 10.0;
const STALE_THRESHOLD_S: f64 = 15.0;

struct BookState {
    best_bid: f64,
    best_ask: f64,
    timestamp: String,
    updated_at: Instant,
}

/// Shared interior: the WS thread writes, `get_book` reads.
struct Inner {
    up: Option<BookState>,
    down: Option<BookState>,
    up_token_id: String,
    down_token_id: String,
}

#[derive(Clone)]
pub struct ClobBookClient {
    inner: Arc<Mutex<Inner>>,
    running: Arc<AtomicBool>,
    /// Set to true to tell the WS thread to re-subscribe (new token IDs).
    needs_resubscribe: Arc<AtomicBool>,
}

impl ClobBookClient {
    pub fn new() -> Self {
        Self {
            inner: Arc::new(Mutex::new(Inner {
                up: None,
                down: None,
                up_token_id: String::new(),
                down_token_id: String::new(),
            })),
            running: Arc::new(AtomicBool::new(false)),
            needs_resubscribe: Arc::new(AtomicBool::new(false)),
        }
    }

    /// Start the background WS thread. Safe to call multiple times.
    pub fn start(&self) {
        if self.running.load(Ordering::SeqCst) {
            return;
        }
        self.running.store(true, Ordering::SeqCst);

        let inner = Arc::clone(&self.inner);
        let running = Arc::clone(&self.running);
        let needs_resub = Arc::clone(&self.needs_resubscribe);

        thread::Builder::new()
            .name("clob-ws".into())
            .spawn(move || ws_run_loop(&inner, &running, &needs_resub))
            .expect("failed to spawn CLOB WS thread");

        info!("[CLOB-WS] client started");
    }

    pub fn stop(&self) {
        self.running.store(false, Ordering::SeqCst);
    }

    /// Update which token IDs the WS should subscribe to.
    /// Called at the start of each session with the new market's tokens.
    pub fn subscribe(&self, up_token_id: &str, down_token_id: &str) {
        let mut guard = self.inner.lock().unwrap();
        guard.up_token_id = up_token_id.to_string();
        guard.down_token_id = down_token_id.to_string();
        guard.up = None;
        guard.down = None;
        drop(guard);
        self.needs_resubscribe.store(true, Ordering::SeqCst);
        info!(
            "[CLOB-WS] subscribe requested: up={}..{} down={}..{}",
            &up_token_id[..8.min(up_token_id.len())],
            &up_token_id[up_token_id.len().saturating_sub(6)..],
            &down_token_id[..8.min(down_token_id.len())],
            &down_token_id[down_token_id.len().saturating_sub(6)..],
        );
    }

    /// Read the latest book state for the up token.
    pub fn get_up_book(&self) -> Option<BookSnapshot> {
        let guard = self.inner.lock().unwrap();
        book_state_to_snapshot(guard.up.as_ref())
    }

    /// Read the latest book state for the down token.
    pub fn get_down_book(&self) -> Option<BookSnapshot> {
        let guard = self.inner.lock().unwrap();
        book_state_to_snapshot(guard.down.as_ref())
    }
}

fn book_state_to_snapshot(state: Option<&BookState>) -> Option<BookSnapshot> {
    let s = state?;
    if s.updated_at.elapsed().as_secs_f64() > STALE_THRESHOLD_S {
        return None;
    }
    Some(BookSnapshot {
        best_bid: Some(s.best_bid),
        best_ask: Some(s.best_ask),
        timestamp: Some(s.timestamp.clone()),
    })
}

// ---------------------------------------------------------------------------
// WS background thread
// ---------------------------------------------------------------------------

fn ws_run_loop(
    inner: &Arc<Mutex<Inner>>,
    running: &AtomicBool,
    needs_resub: &AtomicBool,
) {
    let mut delay = RECONNECT_DELAY_S;

    while running.load(Ordering::SeqCst) {
        match ws_connect(inner, running, needs_resub) {
            Ok(()) => {
                delay = RECONNECT_DELAY_S;
            }
            Err(e) => {
                warn!("[CLOB-WS] connection error: {}", e);
            }
        }

        if !running.load(Ordering::SeqCst) {
            break;
        }

        info!("[CLOB-WS] reconnecting in {:.1}s...", delay);
        let deadline = Instant::now() + Duration::from_secs_f64(delay);
        while Instant::now() < deadline && running.load(Ordering::SeqCst) {
            thread::sleep(Duration::from_millis(200));
        }
        delay = (delay * 1.5).min(MAX_RECONNECT_DELAY_S);
    }
}

fn ws_connect(
    inner: &Arc<Mutex<Inner>>,
    running: &AtomicBool,
    needs_resub: &AtomicBool,
) -> Result<(), Box<dyn std::error::Error>> {
    let (mut ws, _resp) = tungstenite::connect(CLOB_WS_URL)?;
    info!("[CLOB-WS] connected to {}", CLOB_WS_URL);

    // Set read timeout so we can check running/needs_resub flags
    match ws.get_ref() {
        tungstenite::stream::MaybeTlsStream::Plain(tcp) => {
            let _ = tcp.set_read_timeout(Some(Duration::from_secs(2)));
        }
        _ => {}
    }

    // Send initial subscription if we already have token IDs
    send_subscribe(&mut ws, inner)?;
    needs_resub.store(false, Ordering::SeqCst);

    let mut last_ping = Instant::now();

    loop {
        if !running.load(Ordering::SeqCst) {
            let _ = ws.close(None);
            break;
        }

        // Re-subscribe if token IDs changed (new session)
        if needs_resub.load(Ordering::SeqCst) {
            needs_resub.store(false, Ordering::SeqCst);
            send_subscribe(&mut ws, inner)?;
        }

        // Send PING keepalive every 10s
        if last_ping.elapsed().as_secs_f64() >= PING_INTERVAL_S {
            ws.send(Message::Text("PING".into()))?;
            last_ping = Instant::now();
        }

        // Read next message
        let msg = match ws.read() {
            Ok(m) => m,
            Err(tungstenite::Error::Io(ref e))
                if e.kind() == std::io::ErrorKind::WouldBlock
                    || e.kind() == std::io::ErrorKind::TimedOut =>
            {
                continue;
            }
            Err(e) => {
                warn!("[CLOB-WS] read error: {}", e);
                break;
            }
        };

        match msg {
            Message::Text(text) => {
                if text == "PONG" || text.is_empty() {
                    continue;
                }
                if let Ok(data) = serde_json::from_str::<Value>(&text) {
                    handle_message(&data, inner);
                }
            }
            Message::Ping(data) => {
                let _ = ws.send(Message::Pong(data));
            }
            Message::Close(_) => break,
            _ => {}
        }
    }

    Ok(())
}

fn send_subscribe(
    ws: &mut tungstenite::WebSocket<tungstenite::stream::MaybeTlsStream<std::net::TcpStream>>,
    inner: &Arc<Mutex<Inner>>,
) -> Result<(), Box<dyn std::error::Error>> {
    let guard = inner.lock().unwrap();
    let up = guard.up_token_id.clone();
    let down = guard.down_token_id.clone();
    drop(guard);

    if up.is_empty() || down.is_empty() {
        debug!("[CLOB-WS] no token IDs yet, skipping subscribe");
        return Ok(());
    }

    let msg = serde_json::json!({
        "assets_ids": [up, down],
        "type": "market",
        "initial_dump": true,
        "custom_feature_enabled": true,
    });
    ws.send(Message::Text(msg.to_string()))?;
    info!("[CLOB-WS] subscribed to up + down tokens");
    Ok(())
}

fn handle_message(data: &Value, inner: &Arc<Mutex<Inner>>) {
    let event_type = match data.get("event_type").and_then(|e| e.as_str()) {
        Some(et) => et,
        None => return,
    };

    match event_type {
        "price_change" => handle_price_change(data, inner),
        "book" => handle_book(data, inner),
        "best_bid_ask" => handle_best_bid_ask(data, inner),
        _ => {}
    }
}

fn handle_price_change(data: &Value, inner: &Arc<Mutex<Inner>>) {
    let timestamp = data
        .get("timestamp")
        .and_then(|t| t.as_str())
        .unwrap_or("")
        .to_string();

    let changes = match data.get("price_changes").and_then(|p| p.as_array()) {
        Some(arr) => arr,
        None => return,
    };

    for change in changes {
        let asset_id = match change.get("asset_id").and_then(|a| a.as_str()) {
            Some(id) => id,
            None => continue,
        };
        let best_bid = parse_f64_field(change, "best_bid");
        let best_ask = parse_f64_field(change, "best_ask");

        if let (Some(bid), Some(ask)) = (best_bid, best_ask) {
            update_book(inner, asset_id, bid, ask, &timestamp);
        }
    }
}

fn handle_book(data: &Value, inner: &Arc<Mutex<Inner>>) {
    let asset_id = match data.get("asset_id").and_then(|a| a.as_str()) {
        Some(id) => id,
        None => return,
    };
    let timestamp = data
        .get("timestamp")
        .and_then(|t| t.as_str())
        .unwrap_or("")
        .to_string();

    // Extract best bid (max price in bids)
    let best_bid = data
        .get("bids")
        .and_then(|b| b.as_array())
        .and_then(|bids| {
            bids.iter()
                .filter_map(|b| parse_f64_field(b, "price"))
                .fold(None, |max: Option<f64>, p| Some(max.map_or(p, |m| m.max(p))))
        });

    // Extract best ask (min price in asks)
    let best_ask = data
        .get("asks")
        .and_then(|a| a.as_array())
        .and_then(|asks| {
            asks.iter()
                .filter_map(|a| parse_f64_field(a, "price"))
                .fold(None, |min: Option<f64>, p| Some(min.map_or(p, |m| m.min(p))))
        });

    if let (Some(bid), Some(ask)) = (best_bid, best_ask) {
        update_book(inner, asset_id, bid, ask, &timestamp);
    }
}

fn handle_best_bid_ask(data: &Value, inner: &Arc<Mutex<Inner>>) {
    let asset_id = match data.get("asset_id").and_then(|a| a.as_str()) {
        Some(id) => id,
        None => return,
    };
    let timestamp = data
        .get("timestamp")
        .and_then(|t| t.as_str())
        .unwrap_or("")
        .to_string();

    let best_bid = parse_f64_field(data, "best_bid");
    let best_ask = parse_f64_field(data, "best_ask");

    if let (Some(bid), Some(ask)) = (best_bid, best_ask) {
        update_book(inner, asset_id, bid, ask, &timestamp);
    }
}

fn update_book(
    inner: &Arc<Mutex<Inner>>,
    asset_id: &str,
    best_bid: f64,
    best_ask: f64,
    timestamp: &str,
) {
    let mut guard = inner.lock().unwrap();
    let state = BookState {
        best_bid,
        best_ask,
        timestamp: timestamp.to_string(),
        updated_at: Instant::now(),
    };

    if asset_id == guard.up_token_id {
        let is_first = guard.up.is_none();
        guard.up = Some(state);
        if is_first {
            info!(
                "[CLOB-WS] first up book: bid={} ask={} ts={}",
                best_bid, best_ask, timestamp
            );
        }
    } else if asset_id == guard.down_token_id {
        let is_first = guard.down.is_none();
        guard.down = Some(state);
        if is_first {
            info!(
                "[CLOB-WS] first down book: bid={} ask={} ts={}",
                best_bid, best_ask, timestamp
            );
        }
    }
}

fn parse_f64_field(obj: &Value, field: &str) -> Option<f64> {
    let v = obj.get(field)?;
    if let Some(f) = v.as_f64() {
        Some(f)
    } else if let Some(s) = v.as_str() {
        s.parse::<f64>().ok()
    } else {
        None
    }
}
