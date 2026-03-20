//! Global constants and path helpers.

use std::path::PathBuf;

// API endpoints
pub const GAMMA_API_BASE: &str = "https://gamma-api.polymarket.com";
pub const CLOB_API_BASE: &str = "https://clob.polymarket.com";
pub const COINBASE_BTC_URL: &str = "https://api.coinbase.com/v2/prices/BTC-USD/spot";
pub const COINGECKO_BTC_URL: &str =
    "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd";

// Timing
pub const POLL_INTERVAL_SECONDS: f64 = 0.055;
pub const RESOLUTION_POLL_INTERVAL: f64 = 5.0;
pub const RESOLUTION_TIMEOUT_SECONDS: f64 = 300.0;

// Collection
pub const TARGET_SESSIONS: usize = 100;

// Trading guardrails
pub const CLOB_MIN_ORDER_TOKENS: f64 = 5.0;
pub const TRADE_CUTOFF_SECONDS: f64 = 5.0;
pub const DEFAULT_RATIO_MAX: f64 = 100.0;
pub const OPTIMIZER_MAX_RATIO: f64 = 0.12;

// Price/token precision
pub const PRICE_DECIMALS: u32 = 2;
pub const TOKEN_DECIMALS: u32 = 4;
pub const TOKEN_FLOOR_DIVISOR: f64 = 10_000.0; // 10^TOKEN_DECIMALS
pub const MAX_TOKEN_PRICE: f64 = 0.99;
pub const TICK_SIZE: &str = "0.01";
pub const TOKEN_DUST_BUFFER: f64 = 0.0001;
pub const WEI_DIVISOR: f64 = 1_000_000.0;

// Basis points
pub const BPS_DIVISOR: f64 = 10_000.0;

// HTTP timeouts
pub const API_TIMEOUT_SHORT: u64 = 5;
pub const API_TIMEOUT_DEFAULT: u64 = 10;
pub const API_TIMEOUT_LONG: u64 = 30;

// Regime filter
pub const MIN_BTC_MOVE_PCT: f64 = 0.06;

// Latency simulation
pub const DEFAULT_LATENCY_MS: u64 = 200;

// Flatline detection — block trading when midpoints are stale
pub const FLATLINE_THRESHOLD_SECONDS: f64 = 15.0;

// Default order expiration
pub const DEFAULT_EXPIRATION_SECONDS: f64 = 120.0;

// CLOB taker fee
pub const CRYPTO_FEE_RATE: f64 = 0.25;
pub const CRYPTO_FEE_EXPONENT: i32 = 2;

pub fn compute_taker_fee(price: f64, tokens: f64, enabled: bool) -> f64 {
    if !enabled || price <= 0.0 || price >= 1.0 || tokens <= 0.0 {
        return 0.0;
    }
    tokens * price * CRYPTO_FEE_RATE * (price * (1.0 - price)).powi(CRYPTO_FEE_EXPONENT)
}

// App
pub const DEFAULT_CONTROL_PORT: u16 = 8420;

/// Get the base directory of the project (parent of src/).
pub fn base_dir() -> PathBuf {
    // At runtime, use the manifest dir or current dir
    std::env::var("EROS_PM_BASE_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|_| {
            std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."))
        })
}

pub fn slug_key(slug_prefix: &str) -> String {
    slug_prefix.trim_end_matches('-').to_string()
}

pub fn markets_dir() -> PathBuf {
    base_dir().join("data").join("markets")
}

pub fn market_data_dir(sk: &str) -> PathBuf {
    markets_dir().join(sk)
}

pub fn market_sessions_dir(sk: &str) -> PathBuf {
    markets_dir().join(sk).join("sessions")
}

pub fn market_backtest_log(sk: &str) -> PathBuf {
    markets_dir().join(sk).join("backtest_log.jsonl")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_slug_key() {
        assert_eq!(slug_key("btc-updown-5m-"), "btc-updown-5m");
        assert_eq!(slug_key("btc-updown-5m"), "btc-updown-5m");
    }

    #[test]
    fn test_compute_taker_fee() {
        // Disabled
        assert_eq!(compute_taker_fee(0.5, 100.0, false), 0.0);
        // Edge cases
        assert_eq!(compute_taker_fee(0.0, 100.0, true), 0.0);
        assert_eq!(compute_taker_fee(1.0, 100.0, true), 0.0);
        assert_eq!(compute_taker_fee(0.5, 0.0, true), 0.0);
        // Normal: fee = 100 * 0.5 * 0.25 * (0.5 * 0.5)^2 = 50 * 0.25 * 0.0625 = 0.78125
        let fee = compute_taker_fee(0.5, 100.0, true);
        assert!((fee - 0.78125).abs() < 1e-10);
        // At price 0.90: fee = 100 * 0.9 * 0.25 * (0.9*0.1)^2 = 22.5 * 0.0081 = 0.1822...
        let fee2 = compute_taker_fee(0.90, 100.0, true);
        assert!((fee2 - 0.18225).abs() < 1e-5);
    }
}
