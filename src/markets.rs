//! Market-specific constants for known Polymarket up/down markets.

use std::collections::HashMap;

#[derive(Debug, Clone)]
pub struct MarketConfig {
    pub slug_prefix: String,
    pub window_duration_seconds: u64,
    pub max_session_gap_seconds: u64,
    pub poll_interval_seconds: Option<f64>,
}

impl MarketConfig {
    pub fn new(
        slug_prefix: &str,
        window_duration_seconds: u64,
        max_session_gap_seconds: Option<u64>,
        poll_interval_seconds: Option<f64>,
    ) -> Self {
        Self {
            slug_prefix: slug_prefix.to_string(),
            window_duration_seconds,
            max_session_gap_seconds: max_session_gap_seconds
                .unwrap_or(window_duration_seconds * 2),
            poll_interval_seconds,
        }
    }
}

pub fn known_markets() -> HashMap<String, MarketConfig> {
    let mut m = HashMap::new();
    m.insert(
        "btc-updown-5m-".to_string(),
        MarketConfig::new("btc-updown-5m-", 300, None, Some(0.055)),
    );
    m.insert(
        "btc-updown-15m-".to_string(),
        MarketConfig::new("btc-updown-15m-", 900, None, None),
    );
    m
}

pub fn get_market_config(slug_prefix: &str) -> Option<MarketConfig> {
    known_markets().get(slug_prefix).cloned()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_known_markets() {
        let m = known_markets();
        let cfg = m.get("btc-updown-5m-").unwrap();
        assert_eq!(cfg.window_duration_seconds, 300);
        assert_eq!(cfg.max_session_gap_seconds, 600);
    }

    #[test]
    fn test_15m_market() {
        let cfg = get_market_config("btc-updown-15m-").unwrap();
        assert_eq!(cfg.window_duration_seconds, 900);
        assert_eq!(cfg.max_session_gap_seconds, 1800);
    }

    #[test]
    fn test_unknown_market() {
        assert!(get_market_config("eth-updown-1m-").is_none());
    }
}
