//! Data models for samples, sessions, and market metadata.

use serde::{Deserialize, Serialize};

/// A single polling sample taken during a session.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Sample {
    pub timestamp_utc: Option<String>,
    #[serde(default)]
    pub elapsed_seconds: f64,
    pub btc_price_usd: Option<f64>,
    pub up_token_midpoint: Option<f64>,
    pub down_token_midpoint: Option<f64>,
    pub up_token_best_bid: Option<f64>,
    pub up_token_best_ask: Option<f64>,
    pub down_token_best_bid: Option<f64>,
    pub down_token_best_ask: Option<f64>,
    /// Server-side timestamp from the CLOB /book endpoint. Used for accurate
    /// chronological ordering when samples are collected with pipelined I/O.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub server_timestamp: Option<String>,
}

impl Sample {
    /// Midpoint for a leg index. Leg 0 is `up`, leg 1 is `down`; the crypto
    /// up/down market has no other legs.
    pub fn leg_midpoint(&self, leg: usize) -> Option<f64> {
        match leg {
            0 => self.up_token_midpoint,
            1 => self.down_token_midpoint,
            _ => None,
        }
    }

    pub fn leg_best_ask(&self, leg: usize) -> Option<f64> {
        match leg {
            0 => self.up_token_best_ask,
            1 => self.down_token_best_ask,
            _ => None,
        }
    }

    pub fn leg_best_bid(&self, leg: usize) -> Option<f64> {
        match leg {
            0 => self.up_token_best_bid,
            1 => self.down_token_best_bid,
            _ => None,
        }
    }

    /// Get a field by name (for backward compatibility with dict-style access).
    pub fn get_f64(&self, key: &str) -> Option<f64> {
        match key {
            "elapsed_seconds" => Some(self.elapsed_seconds),
            "btc_price_usd" => self.btc_price_usd,
            "up_token_midpoint" => self.up_token_midpoint,
            "down_token_midpoint" => self.down_token_midpoint,
            "up_token_best_bid" => self.up_token_best_bid,
            "up_token_best_ask" => self.up_token_best_ask,
            "down_token_best_bid" => self.down_token_best_bid,
            "down_token_best_ask" => self.down_token_best_ask,
            _ => None,
        }
    }
}

/// Static info about a single market session.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MarketMetadata {
    pub event_id: Option<String>,
    pub event_slug: Option<String>,
    pub event_title: Option<String>,
    pub condition_id: Option<String>,
    pub up_token_id: Option<String>,
    pub down_token_id: Option<String>,
    pub window_start_utc: Option<String>,
    pub window_end_utc: Option<String>,
    pub resolution_source: Option<String>,
}

/// Complete data for one session (loaded from JSON files).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SessionData {
    pub metadata: MarketMetadata,
    pub initial_btc_price_usd: Option<f64>,
    pub final_btc_price_usd: Option<f64>,
    pub outcome: Option<String>,
    pub outcome_prices: Option<Vec<String>>,
    #[serde(default)]
    pub samples: Vec<Sample>,
    #[serde(default)]
    pub collection_started_utc: String,
    #[serde(default)]
    pub collection_ended_utc: String,
    #[serde(default)]
    pub error_count: usize,
    #[serde(default)]
    pub errors: Vec<String>,
    /// Internal: source file path
    #[serde(skip)]
    pub source_file: String,
    /// Internal: pre-sliced prior session summaries (set by engine)
    #[serde(skip)]
    pub _prior_summaries: Option<Vec<SessionSummary>>,
}

/// Lightweight session summary for lookback queries.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SessionSummary {
    pub window_start: String,
    pub initial_btc: f64,
    pub final_btc: f64,
    pub outcome: String,
}

impl SessionSummary {
    pub fn get_f64(&self, key: &str) -> Option<f64> {
        match key {
            "initial_btc" => Some(self.initial_btc),
            "final_btc" => Some(self.final_btc),
            _ => None,
        }
    }

    pub fn get_str(&self, key: &str) -> Option<&str> {
        match key {
            "outcome" => Some(&self.outcome),
            "window_start" => Some(&self.window_start),
            _ => None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_sample_get() {
        let s = Sample {
            timestamp_utc: Some("2026-01-01T00:00:00Z".to_string()),
            elapsed_seconds: 1.5,
            btc_price_usd: Some(60000.0),
            up_token_midpoint: Some(0.55),
            down_token_midpoint: Some(0.45),
            up_token_best_bid: Some(0.01),
            up_token_best_ask: Some(0.99),
            down_token_best_bid: Some(0.01),
            down_token_best_ask: Some(0.99),
            server_timestamp: None,
        };
        assert_eq!(s.get_f64("btc_price_usd"), Some(60000.0));
        assert_eq!(s.get_f64("elapsed_seconds"), Some(1.5));
        assert_eq!(s.get_f64("nonexistent"), None);
    }

    #[test]
    fn test_session_data_deserialize() {
        let json = r#"{
            "metadata": {
                "event_id": "e1",
                "event_slug": "btc-updown-5m-123",
                "event_title": "BTC Up/Down",
                "condition_id": "c1",
                "up_token_id": "t1",
                "down_token_id": "t2",
                "window_start_utc": "2026-01-01T00:00:00Z",
                "window_end_utc": "2026-01-01T00:05:00Z",
                "resolution_source": "chainlink"
            },
            "initial_btc_price_usd": 60000.0,
            "final_btc_price_usd": 60100.0,
            "outcome": "Up",
            "outcome_prices": ["1.00", "0.00"],
            "samples": [
                {
                    "timestamp_utc": "2026-01-01T00:00:00Z",
                    "elapsed_seconds": 0.0,
                    "btc_price_usd": 60000.0,
                    "up_token_midpoint": 0.50,
                    "down_token_midpoint": 0.50,
                    "up_token_best_bid": 0.01,
                    "up_token_best_ask": 0.99,
                    "down_token_best_bid": 0.01,
                    "down_token_best_ask": 0.99
                }
            ],
            "error_count": 0,
            "errors": []
        }"#;
        let sd: SessionData = serde_json::from_str(json).unwrap();
        assert_eq!(sd.outcome.as_deref(), Some("Up"));
        assert_eq!(sd.samples.len(), 1);
        assert_eq!(sd.initial_btc_price_usd, Some(60000.0));
    }
}
