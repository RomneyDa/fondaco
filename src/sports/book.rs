//! Multi-token CLOB book tracking for sports.
//!
//! `clob_ws.rs` is hardwired to exactly two tokens (`Inner { up, down }`),
//! which is correct for a crypto session but useless when ~15 games are live
//! at once with 2-3 legs each. The wire protocol already accepts
//! `assets_ids: [...]` as an array, so this is the same client generalised to
//! a `HashMap<token_id, BookState>`.
//!
//! Also provides the batch REST fallback. `POST /midpoints` and `POST /books`
//! cover every token in a single request, which is what makes polling many
//! concurrent games cheap when the socket is unavailable.

use log::{debug, info, warn};
use serde_json::{json, Value};
use std::collections::{HashMap, HashSet};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};
use tungstenite::Message;

use crate::defaults::{API_TIMEOUT_DEFAULT, CLOB_API_BASE};
use crate::sports::models::LegBook;

pub const CLOB_WS_URL: &str = "wss://ws-subscriptions-clob.polymarket.com/ws/market";
const PING_INTERVAL_S: f64 = 10.0;
const RECONNECT_DELAY_S: f64 = 1.0;
const MAX_RECONNECT_DELAY_S: f64 = 10.0;
pub const STALE_THRESHOLD_S: f64 = 20.0;
/// Levels of depth retained per side. Depth is the input the backtest fill
/// model needs and cannot be reconstructed later.
pub const DEPTH_LEVELS: usize = 10;

#[derive(Debug, Clone)]
pub struct TokenBook {
    pub book: LegBook,
    pub updated_at: Instant,
}

impl TokenBook {
    pub fn is_stale(&self) -> bool {
        self.updated_at.elapsed().as_secs_f64() > STALE_THRESHOLD_S
    }
}

struct Inner {
    books: HashMap<String, TokenBook>,
    subscribed: HashSet<String>,
}

/// Streams order books for an arbitrary set of tokens.
#[derive(Clone)]
pub struct MultiBookClient {
    inner: Arc<Mutex<Inner>>,
    running: Arc<AtomicBool>,
    needs_resubscribe: Arc<AtomicBool>,
    update_count: Arc<AtomicU64>,
}

impl MultiBookClient {
    pub fn new() -> Self {
        Self {
            inner: Arc::new(Mutex::new(Inner {
                books: HashMap::new(),
                subscribed: HashSet::new(),
            })),
            running: Arc::new(AtomicBool::new(false)),
            needs_resubscribe: Arc::new(AtomicBool::new(false)),
            update_count: Arc::new(AtomicU64::new(0)),
        }
    }

    pub fn start(&self) {
        if self.running.swap(true, Ordering::SeqCst) {
            return;
        }
        let inner = Arc::clone(&self.inner);
        let running = Arc::clone(&self.running);
        let resub = Arc::clone(&self.needs_resubscribe);
        let count = Arc::clone(&self.update_count);

        thread::Builder::new()
            .name("clob-multibook-ws".into())
            .spawn(move || run_loop(&inner, &running, &resub, &count))
            .expect("failed to spawn multi-book WS thread");

        info!("[BOOK-WS] client started");
    }

    pub fn stop(&self) {
        self.running.store(false, Ordering::SeqCst);
    }

    /// Replace the subscription set. Tokens no longer present are dropped so a
    /// finished game's books cannot be mistaken for live ones.
    pub fn set_tokens(&self, tokens: &[String]) {
        let wanted: HashSet<String> = tokens.iter().filter(|t| !t.is_empty()).cloned().collect();
        let mut changed = false;
        if let Ok(mut inner) = self.inner.lock() {
            if inner.subscribed != wanted {
                inner.books.retain(|k, _| wanted.contains(k));
                inner.subscribed = wanted;
                changed = true;
            }
        }
        if changed {
            self.needs_resubscribe.store(true, Ordering::SeqCst);
            debug!("[BOOK-WS] subscription set updated ({} tokens)", tokens.len());
        }
    }

    pub fn subscribed_count(&self) -> usize {
        self.inner.lock().map(|i| i.subscribed.len()).unwrap_or(0)
    }

    pub fn update_count(&self) -> u64 {
        self.update_count.load(Ordering::SeqCst)
    }

    /// Latest book for a token, if fresh.
    pub fn book(&self, token_id: &str) -> Option<LegBook> {
        let inner = self.inner.lock().ok()?;
        let entry = inner.books.get(token_id)?;
        if entry.is_stale() {
            return None;
        }
        Some(entry.book.clone())
    }

    /// Merge REST-sourced books into the shared state, so the socket and the
    /// batch fallback populate one place.
    pub fn apply_rest_books(&self, books: HashMap<String, LegBook>) {
        if let Ok(mut inner) = self.inner.lock() {
            for (token, book) in books {
                if inner.subscribed.contains(&token) || inner.subscribed.is_empty() {
                    inner.books.insert(
                        token,
                        TokenBook {
                            book,
                            updated_at: Instant::now(),
                        },
                    );
                }
            }
        }
        self.update_count.fetch_add(1, Ordering::Relaxed);
    }

    #[doc(hidden)]
    pub fn inject_for_test(&self, token_id: &str, book: LegBook) {
        if let Ok(mut inner) = self.inner.lock() {
            inner.subscribed.insert(token_id.to_string());
            inner.books.insert(
                token_id.to_string(),
                TokenBook {
                    book,
                    updated_at: Instant::now(),
                },
            );
        }
    }
}

impl Default for MultiBookClient {
    fn default() -> Self {
        Self::new()
    }
}

fn run_loop(
    inner: &Arc<Mutex<Inner>>,
    running: &AtomicBool,
    resub: &AtomicBool,
    count: &AtomicU64,
) {
    let mut delay = RECONNECT_DELAY_S;
    while running.load(Ordering::SeqCst) {
        match connect_once(inner, running, resub, count) {
            Ok(()) => delay = RECONNECT_DELAY_S,
            Err(e) => warn!("[BOOK-WS] connection error: {e}"),
        }
        if !running.load(Ordering::SeqCst) {
            break;
        }
        let deadline = Instant::now() + Duration::from_secs_f64(delay);
        while Instant::now() < deadline && running.load(Ordering::SeqCst) {
            thread::sleep(Duration::from_millis(200));
        }
        delay = (delay * 1.5).min(MAX_RECONNECT_DELAY_S);
    }
}

fn connect_once(
    inner: &Arc<Mutex<Inner>>,
    running: &AtomicBool,
    resub: &AtomicBool,
    count: &AtomicU64,
) -> Result<(), Box<dyn std::error::Error>> {
    let (mut ws, _resp) = tungstenite::connect(CLOB_WS_URL)?;
    info!("[BOOK-WS] connected");

    if let tungstenite::stream::MaybeTlsStream::Plain(tcp) = ws.get_ref() {
        let _ = tcp.set_read_timeout(Some(Duration::from_secs(2)));
    }

    send_subscribe(&mut ws, inner)?;
    resub.store(false, Ordering::SeqCst);

    let mut last_ping = Instant::now();

    loop {
        if !running.load(Ordering::SeqCst) {
            let _ = ws.close(None);
            break;
        }
        if resub.swap(false, Ordering::SeqCst) {
            send_subscribe(&mut ws, inner)?;
        }
        if last_ping.elapsed().as_secs_f64() >= PING_INTERVAL_S {
            ws.send(Message::Text("PING".into()))?;
            last_ping = Instant::now();
        }

        let msg = match ws.read() {
            Ok(m) => m,
            Err(tungstenite::Error::Io(ref e))
                if e.kind() == std::io::ErrorKind::WouldBlock
                    || e.kind() == std::io::ErrorKind::TimedOut =>
            {
                continue;
            }
            Err(e) => {
                warn!("[BOOK-WS] read error: {e}");
                break;
            }
        };

        match msg {
            Message::Text(text) => {
                if text == "PONG" || text.is_empty() {
                    continue;
                }
                if let Ok(v) = serde_json::from_str::<Value>(&text) {
                    let items: Vec<Value> = match v {
                        Value::Array(a) => a,
                        other => vec![other],
                    };
                    for item in items {
                        handle_message(&item, inner, count);
                    }
                }
            }
            Message::Ping(d) => {
                let _ = ws.send(Message::Pong(d));
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
    let tokens: Vec<String> = inner
        .lock()
        .map(|i| i.subscribed.iter().cloned().collect())
        .unwrap_or_default();
    if tokens.is_empty() {
        debug!("[BOOK-WS] nothing to subscribe to yet");
        return Ok(());
    }
    let msg = json!({
        "assets_ids": tokens,
        "type": "market",
        "initial_dump": true,
    });
    ws.send(Message::Text(msg.to_string()))?;
    info!("[BOOK-WS] subscribed to {} tokens", tokens.len());
    Ok(())
}

fn handle_message(data: &Value, inner: &Arc<Mutex<Inner>>, count: &AtomicU64) {
    let event_type = match data.get("event_type").and_then(|e| e.as_str()) {
        Some(e) => e,
        None => return,
    };
    let token_id = match data
        .get("asset_id")
        .or_else(|| data.get("token_id"))
        .and_then(|v| v.as_str())
    {
        Some(t) => t.to_string(),
        None => return,
    };

    match event_type {
        "book" => {
            let book = parse_book_levels(data);
            store(inner, token_id, book, count);
        }
        "price_change" | "best_bid_ask" => {
            // Incremental updates only carry tops; merge them into whatever
            // depth we already hold rather than discarding it.
            let bid = num_field(data, &["best_bid", "bestBid"]);
            let ask = num_field(data, &["best_ask", "bestAsk"]);
            if bid.is_none() && ask.is_none() {
                return;
            }
            if let Ok(mut guard) = inner.lock() {
                if !guard.subscribed.contains(&token_id) {
                    return;
                }
                let entry = guard.books.entry(token_id).or_insert_with(|| TokenBook {
                    book: LegBook::default(),
                    updated_at: Instant::now(),
                });
                if let Some(b) = bid {
                    entry.book.best_bid = Some(b);
                }
                if let Some(a) = ask {
                    entry.book.best_ask = Some(a);
                }
                entry.book.midpoint = match (entry.book.best_bid, entry.book.best_ask) {
                    (Some(b), Some(a)) => Some((b + a) / 2.0),
                    _ => entry.book.midpoint,
                };
                entry.updated_at = Instant::now();
            }
            count.fetch_add(1, Ordering::Relaxed);
        }
        _ => {}
    }
}

fn store(inner: &Arc<Mutex<Inner>>, token_id: String, book: LegBook, count: &AtomicU64) {
    if let Ok(mut guard) = inner.lock() {
        if !guard.subscribed.contains(&token_id) {
            return;
        }
        guard.books.insert(
            token_id,
            TokenBook {
                book,
                updated_at: Instant::now(),
            },
        );
    }
    count.fetch_add(1, Ordering::Relaxed);
}

fn num_field(v: &Value, names: &[&str]) -> Option<f64> {
    for n in names {
        if let Some(f) = v.get(*n) {
            if let Some(x) = f.as_f64() {
                return Some(x);
            }
            if let Some(s) = f.as_str() {
                if let Ok(x) = s.parse::<f64>() {
                    return Some(x);
                }
            }
        }
    }
    None
}

/// Parse a full `book` payload into tops plus retained depth.
pub fn parse_book_levels(data: &Value) -> LegBook {
    let side = |key: &str| -> Vec<(f64, f64)> {
        data.get(key)
            .and_then(|v| v.as_array())
            .map(|arr| {
                arr.iter()
                    .filter_map(|lvl| {
                        let p = num_field(lvl, &["price"])?;
                        let s = num_field(lvl, &["size"]).unwrap_or(0.0);
                        Some((p, s))
                    })
                    .collect()
            })
            .unwrap_or_default()
    };

    let mut bids = side("bids");
    let mut asks = side("asks");

    // Best bid is the highest price; best ask the lowest.
    bids.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap_or(std::cmp::Ordering::Equal));
    asks.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap_or(std::cmp::Ordering::Equal));

    let best_bid = bids.first().map(|l| l.0);
    let best_ask = asks.first().map(|l| l.0);

    bids.truncate(DEPTH_LEVELS);
    asks.truncate(DEPTH_LEVELS);

    LegBook {
        midpoint: match (best_bid, best_ask) {
            (Some(b), Some(a)) => Some((b + a) / 2.0),
            _ => None,
        },
        best_bid,
        best_ask,
        bid_levels: bids,
        ask_levels: asks,
    }
}

// ---------------------------------------------------------------------------
// Batch REST fallback
// ---------------------------------------------------------------------------

/// Fetch full books for many tokens in one request.
///
/// This is the reason many concurrent games are affordable: one HTTP round trip
/// covers every leg of every live game instead of two per game per tick.
pub fn fetch_books_batch(
    client: &reqwest::blocking::Client,
    token_ids: &[String],
) -> HashMap<String, LegBook> {
    let mut out = HashMap::new();
    if token_ids.is_empty() {
        return out;
    }

    let body: Vec<Value> = token_ids
        .iter()
        .map(|t| json!({ "token_id": t }))
        .collect();

    let resp = match client
        .post(format!("{CLOB_API_BASE}/books"))
        .json(&body)
        .timeout(Duration::from_secs(API_TIMEOUT_DEFAULT))
        .send()
    {
        Ok(r) => r,
        Err(e) => {
            warn!("[BOOK-REST] batch books request failed: {e}");
            return out;
        }
    };

    if !resp.status().is_success() {
        warn!("[BOOK-REST] batch books HTTP {}", resp.status());
        return out;
    }

    let value: Value = match resp.json() {
        Ok(v) => v,
        Err(e) => {
            warn!("[BOOK-REST] batch books parse error: {e}");
            return out;
        }
    };

    let items = match value.as_array() {
        Some(a) => a.clone(),
        None => vec![value],
    };

    for item in items {
        let token = item
            .get("asset_id")
            .or_else(|| item.get("token_id"))
            .and_then(|v| v.as_str())
            .map(|s| s.to_string());
        if let Some(t) = token {
            out.insert(t, parse_book_levels(&item));
        }
    }

    out
}

/// Fetch just midpoints for many tokens — cheaper than full books, used for the
/// idle in-play cadence where depth is not needed every tick.
pub fn fetch_midpoints_batch(
    client: &reqwest::blocking::Client,
    token_ids: &[String],
) -> HashMap<String, f64> {
    let mut out = HashMap::new();
    if token_ids.is_empty() {
        return out;
    }

    let body: Vec<Value> = token_ids
        .iter()
        .map(|t| json!({ "token_id": t }))
        .collect();

    let resp = match client
        .post(format!("{CLOB_API_BASE}/midpoints"))
        .json(&body)
        .timeout(Duration::from_secs(API_TIMEOUT_DEFAULT))
        .send()
    {
        Ok(r) => r,
        Err(e) => {
            warn!("[BOOK-REST] batch midpoints request failed: {e}");
            return out;
        }
    };

    if !resp.status().is_success() {
        return out;
    }

    let value: Value = match resp.json() {
        Ok(v) => v,
        Err(_) => return out,
    };

    // Response may be a map of token -> mid, or an array of objects.
    if let Some(obj) = value.as_object() {
        for (k, v) in obj {
            if let Some(m) = v
                .as_f64()
                .or_else(|| v.as_str().and_then(|s| s.parse().ok()))
            {
                out.insert(k.clone(), m);
            }
        }
        if !out.is_empty() {
            return out;
        }
    }

    if let Some(arr) = value.as_array() {
        for item in arr {
            let token = item
                .get("asset_id")
                .or_else(|| item.get("token_id"))
                .and_then(|v| v.as_str());
            let mid = num_field(item, &["mid", "midpoint"]);
            if let (Some(t), Some(m)) = (token, mid) {
                out.insert(t.to_string(), m);
            }
        }
    }

    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn parses_depth_and_orders_tops_correctly() {
        let data = json!({
            "asset_id": "t1",
            "bids": [
                {"price": "0.55", "size": "100"},
                {"price": "0.58", "size": "40"},
                {"price": "0.52", "size": "250"}
            ],
            "asks": [
                {"price": "0.63", "size": "80"},
                {"price": "0.61", "size": "30"}
            ]
        });

        let b = parse_book_levels(&data);
        assert_eq!(b.best_bid, Some(0.58), "best bid is the highest price");
        assert_eq!(b.best_ask, Some(0.61), "best ask is the lowest price");
        assert!((b.midpoint.unwrap() - 0.595).abs() < 1e-9);

        // Depth retained and sorted away from the touch.
        assert_eq!(b.bid_levels.len(), 3);
        assert_eq!(b.bid_levels[0], (0.58, 40.0));
        assert_eq!(b.bid_levels[2], (0.52, 250.0));
        assert_eq!(b.ask_levels[0], (0.61, 30.0));
    }

    #[test]
    fn depth_is_truncated_to_the_retained_level_count() {
        let bids: Vec<Value> = (0..30)
            .map(|i| json!({"price": format!("0.{:02}", 70 - i), "size": "10"}))
            .collect();
        let data = json!({ "asset_id": "t", "bids": bids, "asks": [] });
        let b = parse_book_levels(&data);
        assert_eq!(b.bid_levels.len(), DEPTH_LEVELS);
        assert_eq!(b.best_bid, Some(0.70));
    }

    #[test]
    fn one_sided_book_has_no_midpoint() {
        let data = json!({ "asset_id": "t", "bids": [{"price": "0.4", "size": "5"}], "asks": [] });
        let b = parse_book_levels(&data);
        assert_eq!(b.best_bid, Some(0.4));
        assert_eq!(b.best_ask, None);
        assert_eq!(b.midpoint, None, "must not invent a midpoint");
    }

    #[test]
    fn set_tokens_drops_books_for_unsubscribed_tokens() {
        let c = MultiBookClient::new();
        c.inject_for_test("a", LegBook { midpoint: Some(0.5), ..Default::default() });
        c.inject_for_test("b", LegBook { midpoint: Some(0.6), ..Default::default() });
        assert!(c.book("a").is_some());

        // Game "a" finished; its token is no longer subscribed.
        c.set_tokens(&["b".to_string()]);
        assert!(
            c.book("a").is_none(),
            "a finished game's book must not linger and read as live"
        );
        assert!(c.book("b").is_some());
    }

    #[test]
    fn stale_books_are_not_returned() {
        let c = MultiBookClient::new();
        c.inject_for_test("t", LegBook { midpoint: Some(0.5), ..Default::default() });
        assert!(c.book("t").is_some());

        // Force staleness.
        if let Ok(mut inner) = c.inner.lock() {
            if let Some(e) = inner.books.get_mut("t") {
                e.updated_at = Instant::now() - Duration::from_secs_f64(STALE_THRESHOLD_S + 1.0);
            }
        }
        assert!(c.book("t").is_none());
    }

    #[test]
    fn price_change_merges_into_existing_depth() {
        let c = MultiBookClient::new();
        c.inject_for_test(
            "t",
            LegBook {
                midpoint: Some(0.50),
                best_bid: Some(0.49),
                best_ask: Some(0.51),
                bid_levels: vec![(0.49, 100.0)],
                ask_levels: vec![(0.51, 100.0)],
            },
        );

        let count = AtomicU64::new(0);
        handle_message(
            &json!({"event_type": "price_change", "asset_id": "t", "best_bid": "0.52"}),
            &c.inner,
            &count,
        );

        let b = c.book("t").unwrap();
        assert_eq!(b.best_bid, Some(0.52));
        assert_eq!(b.best_ask, Some(0.51), "untouched side is preserved");
        assert_eq!(
            b.bid_levels.len(),
            1,
            "incremental update must not wipe retained depth"
        );
    }

    #[test]
    fn updates_for_unsubscribed_tokens_are_ignored() {
        let c = MultiBookClient::new();
        c.set_tokens(&["known".to_string()]);
        let count = AtomicU64::new(0);
        handle_message(
            &json!({"event_type": "book", "asset_id": "stranger", "bids": [], "asks": []}),
            &c.inner,
            &count,
        );
        assert!(c.book("stranger").is_none());
    }
}
