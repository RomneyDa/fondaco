//! RTDS WebSocket client for real-time Chainlink price feeds.

use log::{debug, info, warn};
use serde_json::Value;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};
use tungstenite::Message;

pub const RTDS_WS_URL: &str = "wss://ws-live-data.polymarket.com";
pub const RECONNECT_DELAY_S: f64 = 2.0;
pub const MAX_RECONNECT_DELAY_S: f64 = 30.0;
pub const STALE_THRESHOLD_S: f64 = 10.0;
pub const WATCHDOG_TIMEOUT_S: f64 = 30.0;

pub struct RtdsPrice {
    pub price: f64,
    pub updated_at: Instant,
}

#[derive(Clone)]
pub struct RtdsClient {
    pub symbol: String,
    pub latest: Arc<Mutex<Option<RtdsPrice>>>,
    pub tick_count: Arc<AtomicU64>,
    pub running: Arc<AtomicBool>,
    pub force_reconnect: Arc<AtomicBool>,
}

impl RtdsClient {
    pub fn new(symbol: &str) -> Self {
        Self {
            symbol: symbol.to_lowercase(),
            latest: Arc::new(Mutex::new(None)),
            tick_count: Arc::new(AtomicU64::new(0)),
            running: Arc::new(AtomicBool::new(false)),
            force_reconnect: Arc::new(AtomicBool::new(false)),
        }
    }

    pub fn start(&self) {
        if self.running.load(Ordering::SeqCst) {
            return;
        }
        self.running.store(true, Ordering::SeqCst);

        // WS thread
        let running = Arc::clone(&self.running);
        let latest = Arc::clone(&self.latest);
        let tick_count = Arc::clone(&self.tick_count);
        let force_reconnect = Arc::clone(&self.force_reconnect);
        let symbol = self.symbol.clone();

        thread::Builder::new()
            .name("rtds-ws".into())
            .spawn(move || {
                rtds_run_loop(&symbol, &running, &latest, &tick_count, &force_reconnect);
            })
            .expect("failed to spawn RTDS thread");

        // Watchdog thread
        let running_w = Arc::clone(&self.running);
        let latest_w = Arc::clone(&self.latest);
        let force_reconnect_w = Arc::clone(&self.force_reconnect);

        thread::Builder::new()
            .name("rtds-watchdog".into())
            .spawn(move || {
                rtds_watchdog_loop(&running_w, &latest_w, &force_reconnect_w);
            })
            .expect("failed to spawn RTDS watchdog thread");

        info!("RTDS client started for {}", self.symbol);
    }

    pub fn stop(&self) {
        self.running.store(false, Ordering::SeqCst);
        self.force_reconnect.store(true, Ordering::SeqCst);
    }

    pub fn get_price(&self) -> Option<f64> {
        let guard = self.latest.lock().unwrap();
        if let Some(ref rp) = *guard {
            let age = rp.updated_at.elapsed().as_secs_f64();
            if age > STALE_THRESHOLD_S {
                debug!("RTDS price stale ({:.1}s old)", age);
                return None;
            }
            Some(rp.price)
        } else {
            None
        }
    }
}

pub fn rtds_run_loop(
    symbol: &str,
    running: &AtomicBool,
    latest: &Arc<Mutex<Option<RtdsPrice>>>,
    tick_count: &AtomicU64,
    force_reconnect: &AtomicBool,
) {
    let mut delay = RECONNECT_DELAY_S;

    while running.load(Ordering::SeqCst) {
        let ticks_before = tick_count.load(Ordering::SeqCst);
        force_reconnect.store(false, Ordering::SeqCst);

        if let Err(e) = rtds_connect(symbol, running, latest, tick_count, force_reconnect) {
            warn!("RTDS connection error: {}", e);
        }

        if !running.load(Ordering::SeqCst) {
            break;
        }

        // Reset backoff if we received ticks
        if tick_count.load(Ordering::SeqCst) > ticks_before {
            delay = RECONNECT_DELAY_S;
        } else {
            delay = (delay * 1.5).min(MAX_RECONNECT_DELAY_S);
        }

        info!("RTDS reconnecting in {:.1}s...", delay);
        let deadline = Instant::now() + Duration::from_secs_f64(delay);
        while Instant::now() < deadline && running.load(Ordering::SeqCst) {
            thread::sleep(Duration::from_millis(200));
        }
    }
}

pub fn rtds_connect(
    symbol: &str,
    running: &AtomicBool,
    latest: &Arc<Mutex<Option<RtdsPrice>>>,
    tick_count: &AtomicU64,
    force_reconnect: &AtomicBool,
) -> Result<(), Box<dyn std::error::Error>> {
    let (mut ws, _response) = tungstenite::connect(RTDS_WS_URL)?;

    let subscribe_msg = serde_json::json!({
        "action": "subscribe",
        "subscriptions": [{
            "topic": "crypto_prices_chainlink",
            "type": "*",
            "filters": "",
        }],
    });
    ws.send(Message::Text(subscribe_msg.to_string()))?;
    info!("RTDS subscribed to Chainlink (all symbols)");

    match ws.get_ref() {
        tungstenite::stream::MaybeTlsStream::Plain(tcp) => {
            let _ = tcp.set_read_timeout(Some(Duration::from_secs(5)));
        }
        _ => {}
    }

    loop {
        if !running.load(Ordering::SeqCst) || force_reconnect.load(Ordering::SeqCst) {
            let _ = ws.close(None);
            break;
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
                warn!("RTDS read error: {}", e);
                break;
            }
        };

        match msg {
            Message::Text(text) => {
                if text.is_empty() {
                    continue;
                }
                if let Ok(data) = serde_json::from_str::<Value>(&text) {
                    if data.get("topic").and_then(|t| t.as_str())
                        != Some("crypto_prices_chainlink")
                    {
                        continue;
                    }
                    if let Some(payload) = data.get("payload") {
                        if payload.get("symbol").is_some() && payload.get("value").is_some() {
                            handle_rtds_tick(payload, symbol, latest, tick_count);
                        }
                        if let Some(arr) = payload.get("data").and_then(|d| d.as_array()) {
                            for tick in arr {
                                handle_rtds_tick(tick, symbol, latest, tick_count);
                            }
                        }
                    }
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

pub fn handle_rtds_tick(
    tick: &Value,
    symbol: &str,
    latest: &Arc<Mutex<Option<RtdsPrice>>>,
    tick_count: &AtomicU64,
) {
    let sym = tick
        .get("symbol")
        .and_then(|s| s.as_str())
        .unwrap_or("")
        .to_lowercase();
    if !sym.is_empty() && sym != symbol {
        return;
    }
    let price = match tick.get("value") {
        Some(v) => {
            if let Some(f) = v.as_f64() {
                f
            } else if let Some(s) = v.as_str() {
                match s.parse::<f64>() {
                    Ok(f) => f,
                    Err(_) => return,
                }
            } else {
                return;
            }
        }
        None => return,
    };

    {
        let mut guard = latest.lock().unwrap();
        *guard = Some(RtdsPrice {
            price,
            updated_at: Instant::now(),
        });
    }
    let count = tick_count.fetch_add(1, Ordering::SeqCst) + 1;
    if count <= 3 || count % 60 == 0 {
        debug!("RTDS btc/usd tick #{}: ${:.2}", count, price);
    }
}

pub fn rtds_watchdog_loop(
    running: &AtomicBool,
    latest: &Arc<Mutex<Option<RtdsPrice>>>,
    force_reconnect: &AtomicBool,
) {
    while running.load(Ordering::SeqCst) {
        thread::sleep(Duration::from_secs(5));
        let age = {
            let guard = latest.lock().unwrap();
            match *guard {
                Some(ref rp) => rp.updated_at.elapsed().as_secs_f64(),
                None => 0.0,
            }
        };
        if age > WATCHDOG_TIMEOUT_S {
            warn!(
                "RTDS watchdog: no ticks for {:.0}s, forcing reconnect",
                age
            );
            force_reconnect.store(true, Ordering::SeqCst);
        }
    }
}
