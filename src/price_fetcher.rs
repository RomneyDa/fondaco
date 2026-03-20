//! Price fetching utilities for BTC price and CLOB token data.

use crate::clob_ws::ClobBookClient;
use crate::defaults::{API_TIMEOUT_SHORT, CLOB_API_BASE, COINBASE_BTC_URL, COINGECKO_BTC_URL};
use crate::models::Sample;
use crate::rtds::RtdsClient;
use chrono::{DateTime, Utc};
use log::{info, warn};
use reqwest::blocking::Client;
use serde_json::Value;
use std::sync::Mutex;
use std::thread;
use std::time::Duration;

pub fn get_btc_price(client: &Client, rtds: &RtdsClient) -> Option<f64> {
    // Priority: RTDS -> Coinbase -> CoinGecko
    if let Some(price) = rtds.get_price() {
        return Some(price);
    }

    // Coinbase fallback
    if let Ok(resp) = client
        .get(COINBASE_BTC_URL)
        .timeout(Duration::from_secs(API_TIMEOUT_SHORT))
        .send()
    {
        if resp.status().is_success() {
            if let Ok(json) = resp.json::<Value>() {
                if let Some(amount) = json
                    .get("data")
                    .and_then(|d| d.get("amount"))
                    .and_then(|a| a.as_str())
                {
                    if let Ok(price) = amount.parse::<f64>() {
                        return Some(price);
                    }
                }
            }
        }
    }

    // CoinGecko fallback
    if let Ok(resp) = client
        .get(COINGECKO_BTC_URL)
        .timeout(Duration::from_secs(API_TIMEOUT_SHORT))
        .send()
    {
        if resp.status().is_success() {
            if let Ok(json) = resp.json::<Value>() {
                if let Some(price) = json
                    .get("bitcoin")
                    .and_then(|b| b.get("usd"))
                    .and_then(|u| u.as_f64())
                {
                    return Some(price);
                }
            }
        }
    }

    warn!("Failed to fetch BTC price from all sources");
    None
}

pub fn get_token_midpoint(client: &Client, token_id: &str) -> Option<f64> {
    let mut backoff_ms = 250u64;
    for attempt in 0..3 {
        let resp = client
            .get(format!("{}/midpoint", CLOB_API_BASE))
            .query(&[("token_id", token_id)])
            .timeout(Duration::from_secs(API_TIMEOUT_SHORT))
            .send()
            .ok()?;
        if resp.status().as_u16() == 429 {
            warn!("HTTP 429 on CLOB midpoint (attempt {}/3), backing off {}ms", attempt + 1, backoff_ms);
            thread::sleep(Duration::from_millis(backoff_ms));
            backoff_ms *= 2;
            continue;
        }
        if !resp.status().is_success() {
            return None;
        }
        let json: Value = resp.json().ok()?;
        let mid = json.get("mid")?;
        return if let Some(f) = mid.as_f64() {
            Some(f)
        } else if let Some(s) = mid.as_str() {
            s.parse::<f64>().ok()
        } else {
            None
        };
    }
    None
}

/// Book snapshot returned by [`get_token_book`].
pub struct BookSnapshot {
    pub best_bid: Option<f64>,
    pub best_ask: Option<f64>,
    /// Server-side timestamp from the CLOB `/book` response.
    pub timestamp: Option<String>,
}

impl BookSnapshot {
    /// Midpoint derived from best bid/ask: (bid + ask) / 2.
    pub fn midpoint(&self) -> Option<f64> {
        match (self.best_bid, self.best_ask) {
            (Some(b), Some(a)) => Some((b + a) / 2.0),
            _ => None,
        }
    }
}

pub fn get_token_book(client: &Client, token_id: &str) -> BookSnapshot {
    let empty = BookSnapshot {
        best_bid: None,
        best_ask: None,
        timestamp: None,
    };

    let url = format!("{}/book", CLOB_API_BASE);
    info!("[CLOB] GET {} token_id={}", url, token_id);

    let mut backoff_ms = 250u64;
    let resp = 'retry: {
        for attempt in 0..3 {
            match client
                .get(&url)
                .query(&[("token_id", token_id)])
                .timeout(Duration::from_secs(API_TIMEOUT_SHORT))
                .send()
            {
                Ok(r) if r.status().as_u16() == 429 => {
                    warn!(
                        "HTTP 429 on CLOB book (attempt {}/3), backing off {}ms",
                        attempt + 1,
                        backoff_ms
                    );
                    thread::sleep(Duration::from_millis(backoff_ms));
                    backoff_ms *= 2;
                    continue;
                }
                Ok(r) if r.status().is_success() => {
                    info!("[CLOB] book 200 token_id={}..{}", &token_id[..8.min(token_id.len())], &token_id[token_id.len().saturating_sub(6)..]);
                    break 'retry r;
                }
                Ok(r) => {
                    warn!("[CLOB] book HTTP {} token_id={}..{}", r.status(), &token_id[..8.min(token_id.len())], &token_id[token_id.len().saturating_sub(6)..]);
                    return empty;
                }
                Err(e) => {
                    warn!("[CLOB] book request failed: {} token_id={}..{}", e, &token_id[..8.min(token_id.len())], &token_id[token_id.len().saturating_sub(6)..]);
                    return empty;
                }
            }
        }
        return empty;
    };

    let json: Value = match resp.json() {
        Ok(j) => j,
        Err(e) => {
            warn!("[CLOB] book JSON parse failed: {}", e);
            return empty;
        }
    };

    let timestamp = json
        .get("timestamp")
        .and_then(|t| t.as_str())
        .map(|s| s.to_string());

    let num_bids = json.get("bids").and_then(|b| b.as_array()).map(|a| a.len()).unwrap_or(0);
    let num_asks = json.get("asks").and_then(|a| a.as_array()).map(|a| a.len()).unwrap_or(0);

    let best_bid = json
        .get("bids")
        .and_then(|b| b.as_array())
        .and_then(|bids| {
            bids.iter()
                .filter_map(|b| {
                    b.get("price").and_then(|p| {
                        p.as_str()
                            .map(|s| s.parse::<f64>().ok())
                            .unwrap_or(p.as_f64())
                    })
                })
                .fold(None, |max: Option<f64>, p| {
                    Some(max.map_or(p, |m| m.max(p)))
                })
        });

    let best_ask = json
        .get("asks")
        .and_then(|a| a.as_array())
        .and_then(|asks| {
            asks.iter()
                .filter_map(|a| {
                    a.get("price").and_then(|p| {
                        p.as_str()
                            .map(|s| s.parse::<f64>().ok())
                            .unwrap_or(p.as_f64())
                    })
                })
                .fold(None, |min: Option<f64>, p| {
                    Some(min.map_or(p, |m| m.min(p)))
                })
        });

    info!(
        "[CLOB] book result: bid={:?} ask={:?} mid={:?} bids={} asks={} ts={:?}",
        best_bid, best_ask,
        match (best_bid, best_ask) { (Some(b), Some(a)) => Some((b + a) / 2.0), _ => None },
        num_bids, num_asks, timestamp,
    );

    BookSnapshot {
        best_bid,
        best_ask,
        timestamp,
    }
}

/// Take a complete price sample.
///
/// Primary path: read latest book from the CLOB WebSocket client (zero latency).
/// Fallback: fire 2 CLOB `/book` HTTP requests in parallel.
pub fn take_sample(
    client: &Client,
    rtds: &RtdsClient,
    up_token_id: &str,
    down_token_id: &str,
    window_start_utc: &str,
    last_btc_price: &Mutex<Option<f64>>,
    clob_ws: Option<&ClobBookClient>,
) -> Sample {
    let now: DateTime<Utc> = Utc::now();
    let window_start_dt = DateTime::parse_from_rfc3339(
        &window_start_utc
            .replace("Z", "+00:00")
            .replace(" ", "T"),
    )
    .unwrap_or_else(|_| {
        DateTime::parse_from_rfc3339(window_start_utc).unwrap_or_else(|_| {
            warn!("Could not parse window_start_utc: {}", window_start_utc);
            now.fixed_offset()
        })
    });
    let elapsed = (now - window_start_dt.with_timezone(&Utc))
        .num_milliseconds() as f64
        / 1000.0;

    // BTC price: RTDS primary, then cached, then HTTP fallback
    let mut btc_price = rtds.get_price();
    if let Some(p) = btc_price {
        *last_btc_price.lock().unwrap() = Some(p);
    } else {
        btc_price = *last_btc_price.lock().unwrap();
        if btc_price.is_none() {
            btc_price = get_btc_price(client, rtds);
            if let Some(p) = btc_price {
                *last_btc_price.lock().unwrap() = Some(p);
            }
        }
    }

    // Try WebSocket first — instant read from shared state
    let ws_up = clob_ws.and_then(|ws| ws.get_up_book());
    let ws_down = clob_ws.and_then(|ws| ws.get_down_book());

    let (up_book, down_book) = if ws_up.is_some() && ws_down.is_some() {
        (ws_up.unwrap(), ws_down.unwrap())
    } else {
        // Fallback: fire 2 HTTP book requests in parallel
        let up_tid = up_token_id.to_string();
        let down_tid = down_token_id.to_string();
        let c1 = client.clone();
        let c2 = client.clone();

        let h_up = thread::spawn(move || get_token_book(&c1, &up_tid));
        let h_down = thread::spawn(move || get_token_book(&c2, &down_tid));

        let http_up = ws_up.unwrap_or_else(|| {
            h_up.join().unwrap_or(BookSnapshot {
                best_bid: None,
                best_ask: None,
                timestamp: None,
            })
        });
        let http_down = ws_down.unwrap_or_else(|| {
            h_down.join().unwrap_or(BookSnapshot {
                best_bid: None,
                best_ask: None,
                timestamp: None,
            })
        });
        (http_up, http_down)
    };

    // Use the earlier of the two server timestamps (most conservative)
    let server_timestamp = match (&up_book.timestamp, &down_book.timestamp) {
        (Some(a), Some(b)) => Some(std::cmp::min(a, b).clone()),
        (Some(a), None) => Some(a.clone()),
        (None, Some(b)) => Some(b.clone()),
        (None, None) => None,
    };

    Sample {
        timestamp_utc: Some(now.to_rfc3339()),
        elapsed_seconds: (elapsed * 100.0).round() / 100.0,
        btc_price_usd: btc_price,
        up_token_midpoint: up_book.midpoint(),
        down_token_midpoint: down_book.midpoint(),
        up_token_best_bid: up_book.best_bid,
        up_token_best_ask: up_book.best_ask,
        down_token_best_bid: down_book.best_bid,
        down_token_best_ask: down_book.best_ask,
        server_timestamp,
    }
}
