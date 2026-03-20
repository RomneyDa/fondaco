//! Market discovery: finding active Polymarket events by slug prefix + window timestamp.

use crate::defaults::{API_TIMEOUT_DEFAULT, GAMMA_API_BASE};
use crate::models::MarketMetadata;
use log::debug;
use reqwest::blocking::Client;
use serde_json::Value;
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

pub fn compute_window_timestamp(window_duration: u64) -> u64 {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs();
    (now / window_duration) * window_duration
}

pub fn get_event_slug(slug_prefix: &str, window_start_ts: u64) -> String {
    format!("{}{}", slug_prefix, window_start_ts)
}

/// Parse a single Gamma API event JSON object into MarketMetadata.
/// Extracted from fetch_market_metadata for testability.
pub(crate) fn parse_event_json(event: &Value) -> Option<MarketMetadata> {
    let markets = event.get("markets")?.as_array()?;
    let market = markets.first()?;

    if market
        .get("closed")
        .and_then(|c| c.as_bool())
        .unwrap_or(false)
    {
        return None;
    }

    let clob_token_ids_str = market.get("clobTokenIds")?.as_str()?;
    let clob_token_ids: Vec<String> = serde_json::from_str(clob_token_ids_str).ok()?;
    if clob_token_ids.len() < 2 {
        return None;
    }

    let window_start_utc = market
        .get("eventStartTime")
        .or_else(|| market.get("startDate"))
        .and_then(|v| v.as_str())
        .map(|s| s.to_string());
    let window_end_utc = market
        .get("endDate")
        .and_then(|v| v.as_str())
        .map(|s| s.to_string());
    let resolution_source = market
        .get("resolutionSource")
        .and_then(|v| v.as_str())
        .map(|s| s.to_string());

    Some(MarketMetadata {
        event_id: event
            .get("id")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string()),
        event_slug: event
            .get("slug")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string()),
        event_title: event
            .get("title")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string()),
        condition_id: market
            .get("conditionId")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string()),
        up_token_id: Some(clob_token_ids[0].clone()),
        down_token_id: Some(clob_token_ids[1].clone()),
        window_start_utc,
        window_end_utc,
        resolution_source,
    })
}

pub fn fetch_market_metadata(
    client: &Client,
    slug_prefix: &str,
    window_start_ts: u64,
) -> Option<MarketMetadata> {
    let slug = get_event_slug(slug_prefix, window_start_ts);
    let mut backoff_ms = 500u64;
    for attempt in 0..3 {
        let resp = match client
            .get(format!("{}/events", GAMMA_API_BASE))
            .query(&[("slug", &slug)])
            .timeout(Duration::from_secs(API_TIMEOUT_DEFAULT))
            .send()
        {
            Ok(r) => r,
            Err(_) => return None,
        };
        if resp.status().as_u16() == 429 {
            debug!("HTTP 429 on Gamma API (attempt {}/3), backing off {}ms", attempt + 1, backoff_ms);
            thread::sleep(Duration::from_millis(backoff_ms));
            backoff_ms *= 2;
            continue;
        }
        if !resp.status().is_success() {
            return None;
        }
        let events: Vec<Value> = resp.json().ok()?;
        let event = events.first()?;
        return parse_event_json(event);
    }
    None
}

pub fn fetch_metadata_with_retry(
    client: &Client,
    slug_prefix: &str,
    window_ts: u64,
    running: &AtomicBool,
) -> Option<MarketMetadata> {
    for attempt in 0..30 {
        if !running.load(Ordering::SeqCst) {
            return None;
        }
        match fetch_market_metadata(client, slug_prefix, window_ts) {
            Some(md) => return Some(md),
            None => {
                if attempt < 29 {
                    debug!(
                        "Market not found for window {} (attempt {}/30), retrying...",
                        window_ts,
                        attempt + 1
                    );
                    thread::sleep(Duration::from_secs(1));
                }
            }
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_get_event_slug() {
        let slug = get_event_slug("btc-updown-5m-", 1700000000);
        assert_eq!(slug, "btc-updown-5m-1700000000");
    }

    #[test]
    fn test_get_event_slug_with_trailing_dash() {
        let slug = get_event_slug("eth-updown-15m-", 1700000300);
        assert_eq!(slug, "eth-updown-15m-1700000300");
    }

    #[test]
    fn test_get_event_slug_no_trailing_dash() {
        let slug = get_event_slug("btc-updown-5m", 1700000000);
        assert_eq!(slug, "btc-updown-5m1700000000");
    }

    #[test]
    fn test_parse_event_json_valid() {
        let event: Value = serde_json::json!({
            "id": "event-123",
            "slug": "btc-updown-5m-1700000000",
            "title": "Will BTC go up or down?",
            "markets": [{
                "conditionId": "cond-abc",
                "clobTokenIds": "[\"token-up-1\",\"token-down-2\"]",
                "closed": false,
                "eventStartTime": "2026-01-01T00:00:00Z",
                "endDate": "2026-01-01T00:05:00Z",
                "resolutionSource": "chainlink"
            }]
        });

        let md = parse_event_json(&event).expect("should parse valid event");
        assert_eq!(md.event_id.as_deref(), Some("event-123"));
        assert_eq!(md.event_slug.as_deref(), Some("btc-updown-5m-1700000000"));
        assert_eq!(md.event_title.as_deref(), Some("Will BTC go up or down?"));
        assert_eq!(md.condition_id.as_deref(), Some("cond-abc"));
        assert_eq!(md.up_token_id.as_deref(), Some("token-up-1"));
        assert_eq!(md.down_token_id.as_deref(), Some("token-down-2"));
        assert_eq!(md.window_start_utc.as_deref(), Some("2026-01-01T00:00:00Z"));
        assert_eq!(md.window_end_utc.as_deref(), Some("2026-01-01T00:05:00Z"));
        assert_eq!(md.resolution_source.as_deref(), Some("chainlink"));
    }

    #[test]
    fn test_parse_event_json_closed_market() {
        let event: Value = serde_json::json!({
            "id": "event-123",
            "slug": "btc-updown-5m-1700000000",
            "title": "Will BTC go up or down?",
            "markets": [{
                "conditionId": "cond-abc",
                "clobTokenIds": "[\"token-up-1\",\"token-down-2\"]",
                "closed": true,
                "eventStartTime": "2026-01-01T00:00:00Z",
                "endDate": "2026-01-01T00:05:00Z"
            }]
        });

        assert!(parse_event_json(&event).is_none(), "closed market should return None");
    }

    #[test]
    fn test_parse_event_json_missing_markets() {
        let event: Value = serde_json::json!({
            "id": "event-123",
            "slug": "btc-updown-5m-1700000000",
            "title": "Will BTC go up or down?"
        });

        assert!(parse_event_json(&event).is_none(), "missing markets should return None");
    }

    #[test]
    fn test_parse_event_json_insufficient_token_ids() {
        let event: Value = serde_json::json!({
            "id": "event-123",
            "slug": "btc-updown-5m-1700000000",
            "title": "Title",
            "markets": [{
                "conditionId": "cond-abc",
                "clobTokenIds": "[\"only-one\"]",
                "closed": false
            }]
        });

        assert!(parse_event_json(&event).is_none(), "single token ID should return None");
    }

    #[test]
    fn test_parse_event_json_uses_start_date_fallback() {
        let event: Value = serde_json::json!({
            "id": "event-123",
            "slug": "btc-updown-5m-1700000000",
            "title": "Title",
            "markets": [{
                "conditionId": "cond-abc",
                "clobTokenIds": "[\"t1\",\"t2\"]",
                "closed": false,
                "startDate": "2026-02-01T00:00:00Z",
                "endDate": "2026-02-01T00:05:00Z"
            }]
        });

        let md = parse_event_json(&event).expect("should parse with startDate fallback");
        assert_eq!(md.window_start_utc.as_deref(), Some("2026-02-01T00:00:00Z"));
    }
}
