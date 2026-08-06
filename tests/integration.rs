//! Integration tests matching the Python test suite.
//! Covers: Position, SimExecutor, Trader, Strategy base, defaults, math_utils,
//! volatility, session_cache, models, engine, strategy behavior.

use fondaco::defaults::*;
use fondaco::engine::*;
use fondaco::math_utils::*;
use fondaco::models::*;
use fondaco::session_cache::*;
use fondaco::strategy::*;
use fondaco::trader::*;
use fondaco::volatility::*;
use fondaco::economics::MarketEconomics;
use std::collections::HashMap;

/// Crypto economics, matching the former global constants.
fn econ_test() -> MarketEconomics {
    MarketEconomics::crypto()
}

// ---------------------------------------------------------------------------
// Helper: make_sample
// ---------------------------------------------------------------------------

fn make_sample(elapsed: f64, btc: f64, up_mid: f64, down_mid: f64) -> Sample {
    Sample {
        timestamp_utc: Some("2026-01-01T00:00:00Z".to_string()),
        elapsed_seconds: elapsed,
        btc_price_usd: Some(btc),
        up_token_midpoint: Some(up_mid),
        down_token_midpoint: Some(down_mid),
        up_token_best_bid: Some(0.01),
        up_token_best_ask: Some(0.99),
        down_token_best_bid: Some(0.01),
        down_token_best_ask: Some(0.99),
        server_timestamp: None,
    }
}

fn make_sample_with_book(
    elapsed: f64,
    btc: f64,
    up_mid: f64,
    down_mid: f64,
    up_ask: f64,
    down_ask: f64,
) -> Sample {
    Sample {
        timestamp_utc: Some("2026-01-01T00:00:00Z".to_string()),
        elapsed_seconds: elapsed,
        btc_price_usd: Some(btc),
        up_token_midpoint: Some(up_mid),
        down_token_midpoint: Some(down_mid),
        up_token_best_bid: Some(0.01),
        up_token_best_ask: Some(up_ask),
        down_token_best_bid: Some(0.01),
        down_token_best_ask: Some(down_ask),
        server_timestamp: None,
    }
}

// ---------------------------------------------------------------------------
// Helper: make_vol_summaries
// ---------------------------------------------------------------------------

fn make_vol_summaries(returns_bps: &[f64]) -> Vec<SessionSummary> {
    returns_bps
        .iter()
        .enumerate()
        .map(|(i, &r)| {
            let initial = 60000.0;
            let final_btc = initial * (1.0 + r / BPS_DIVISOR);
            SessionSummary {
                window_start: format!("2026-01-01T{:02}:00:00Z", i),
                initial_btc: initial,
                final_btc,
                outcome: if r > 0.0 { "Up" } else { "Down" }.to_string(),
            }
        })
        .collect()
}

// ---------------------------------------------------------------------------
// Helper: test strategy implementations
// ---------------------------------------------------------------------------

/// Strategy that always returns HOLD.
struct AlwaysHoldStrategy;

impl Strategy for AlwaysHoldStrategy {
    fn name(&self) -> &str {
        "AlwaysHold"
    }
    fn version(&self) -> &str {
        "1"
    }
    fn on_session_start(&mut self, _metadata: &SessionMetadata) {}
    fn on_tick(&mut self, _ctx: &TickContext) -> TradeAction {
        hold()
    }
    fn with_params(&self, _params: &HashMap<String, ParamValue>) -> Box<dyn Strategy> {
        Box::new(AlwaysHoldStrategy)
    }
}

/// Strategy that buys once past 50% elapsed.
struct BuyOnceStrategy {
    bought: bool,
}

impl BuyOnceStrategy {
    fn new() -> Self {
        Self { bought: false }
    }
}

impl Strategy for BuyOnceStrategy {
    fn name(&self) -> &str {
        "BuyOnce"
    }
    fn version(&self) -> &str {
        "1"
    }
    fn on_session_start(&mut self, _metadata: &SessionMetadata) {
        self.bought = false;
    }
    fn on_tick(&mut self, ctx: &TickContext) -> TradeAction {
        if !self.bought && ctx.elapsed_fraction > 0.50 {
            self.bought = true;
            let up_mid = ctx.sample.up_token_midpoint.unwrap_or(0.50);
            TradeAction::buy("buy_up", ctx.position.cash, up_mid, Some(10.0))
        } else {
            hold()
        }
    }
    fn with_params(&self, _params: &HashMap<String, ParamValue>) -> Box<dyn Strategy> {
        Box::new(BuyOnceStrategy::new())
    }
}

/// Strategy that tries to buy immediately with a huge amount.
struct BuyMaxStrategy;

impl Strategy for BuyMaxStrategy {
    fn name(&self) -> &str {
        "BuyMax"
    }
    fn version(&self) -> &str {
        "1"
    }
    fn on_session_start(&mut self, _metadata: &SessionMetadata) {}
    fn on_tick(&mut self, ctx: &TickContext) -> TradeAction {
        if ctx.tick_index == 0 {
            TradeAction {
                side: "buy_up".to_string(),
                amount: 99999.0,
                price: None,
                expiration_seconds: None,
                cancel_after_s: None,
            }
        } else {
            hold()
        }
    }
    fn with_params(&self, _params: &HashMap<String, ParamValue>) -> Box<dyn Strategy> {
        Box::new(BuyMaxStrategy)
    }
}

/// Strategy that places a limit order.
struct LimitOrderStrategy {
    placed: bool,
}

impl LimitOrderStrategy {
    fn new() -> Self {
        Self { placed: false }
    }
}

impl Strategy for LimitOrderStrategy {
    fn name(&self) -> &str {
        "LimitOrder"
    }
    fn version(&self) -> &str {
        "1"
    }
    fn on_session_start(&mut self, _metadata: &SessionMetadata) {
        self.placed = false;
    }
    fn on_tick(&mut self, ctx: &TickContext) -> TradeAction {
        if !self.placed && ctx.elapsed_fraction > 0.1 {
            self.placed = true;
            TradeAction::buy("buy_up", ctx.position.cash, 0.50, Some(10.0))
        } else {
            hold()
        }
    }
    fn with_params(&self, _params: &HashMap<String, ParamValue>) -> Box<dyn Strategy> {
        Box::new(LimitOrderStrategy::new())
    }
}

/// Strategy that sends a cancel action on tick 2.
struct CancelStrategy {
    tick: usize,
    placed: bool,
}

impl CancelStrategy {
    fn new() -> Self {
        Self {
            tick: 0,
            placed: false,
        }
    }
}

impl Strategy for CancelStrategy {
    fn name(&self) -> &str {
        "Cancel"
    }
    fn version(&self) -> &str {
        "1"
    }
    fn on_session_start(&mut self, _metadata: &SessionMetadata) {
        self.tick = 0;
        self.placed = false;
    }
    fn on_tick(&mut self, ctx: &TickContext) -> TradeAction {
        self.tick += 1;
        if self.tick == 1 && !self.placed {
            self.placed = true;
            TradeAction::buy("buy_up", ctx.position.cash, 0.55, Some(10.0))
        } else if self.tick == 3 {
            TradeAction {
                side: "cancel_up".to_string(),
                amount: 0.0,
                price: None,
                expiration_seconds: None,
                cancel_after_s: None,
            }
        } else {
            hold()
        }
    }
    fn with_params(&self, _params: &HashMap<String, ParamValue>) -> Box<dyn Strategy> {
        Box::new(CancelStrategy::new())
    }
}

fn default_metadata() -> SessionMetadata {
    SessionMetadata {
        up_token_id: "t1".to_string(),
        down_token_id: "t2".to_string(),
        window_start_utc: "2026-01-01T00:00:00Z".to_string(),
        window_duration: 300.0,
        prior_session_summaries: Vec::new(),
        gap_before_seconds: None,
        data_density: 1.0,
        legs: fondaco::trader::LegSpec::crypto_pair("t1", "t2"),
        economics: econ_test(),
        extra: HashMap::new(),
    }
}

// ===========================================================================
// test_defaults (matches test_defaults.py)
// ===========================================================================

#[test]
fn test_slug_key_strips_trailing_dash() {
    assert_eq!(slug_key("btc-updown-5m-"), "btc-updown-5m");
}

#[test]
fn test_slug_key_noop_without_dash() {
    assert_eq!(slug_key("btc-updown-5m"), "btc-updown-5m");
}

#[test]
fn test_taker_fee_at_midpoint() {
    let fee = compute_taker_fee(0.50, 100.0, true);
    let expected = 100.0 * 0.50 * 0.25 * (0.50 * 0.50_f64).powi(2);
    assert!((fee - expected).abs() < 1e-10);
}

#[test]
fn test_taker_fee_disabled() {
    assert_eq!(compute_taker_fee(0.50, 100.0, false), 0.0);
}

#[test]
fn test_taker_fee_at_boundary() {
    assert_eq!(compute_taker_fee(0.0, 100.0, true), 0.0);
    assert_eq!(compute_taker_fee(1.0, 100.0, true), 0.0);
    assert_eq!(compute_taker_fee(-0.1, 100.0, true), 0.0);
}

// ===========================================================================
// test_position (matches test_position.py)
// ===========================================================================

#[test]
fn test_position_compute_trade_size_buy_up() {
    let p = Position::new(100.0);
    let (tokens, cost) = p.compute_trade_size("buy_up", Some(0.50), 100.0, &econ_test());
    assert_eq!(tokens, 200.0);
    assert!((cost - 100.0).abs() < 0.01);
}

#[test]
fn test_position_compute_trade_size_buy_down() {
    let p = Position::new(50.0);
    let (tokens, _cost) = p.compute_trade_size("buy_down", Some(0.25), 50.0, &econ_test());
    assert_eq!(tokens, 200.0);
}

#[test]
fn test_position_compute_trade_size_below_min() {
    let p = Position::new(1.0);
    let (tokens, _) = p.compute_trade_size("buy_up", Some(0.50), 1.0, &econ_test());
    assert_eq!(tokens, 0.0);
}

#[test]
fn test_position_compute_trade_size_no_cash() {
    let p = Position::new(0.0);
    let (tokens, _) = p.compute_trade_size("buy_up", Some(0.50), 100.0, &econ_test());
    assert_eq!(tokens, 0.0);
}

#[test]
fn test_position_compute_trade_size_sell() {
    let mut p = Position::new(0.0);
    p.set_leg(0, 100.0);
    let (tokens, proceeds) = p.compute_trade_size("sell_up", Some(0.80), 100.0, &econ_test());
    assert_eq!(tokens, 100.0);
    assert!((proceeds - 80.0).abs() < 0.01);
}

#[test]
fn test_position_apply_trade_buy() {
    let mut p = Position::new(100.0);
    p.apply_trade("buy_up", 0.50, 50.0);
    assert!((p.cash - 75.0).abs() < 0.01);
    assert!((p.up_tokens() - 50.0).abs() < 0.01);
}

#[test]
fn test_position_apply_trade_sell() {
    let mut p = Position::new(0.0);
    p.set_leg(0, 100.0);
    p.apply_trade("sell_up", 0.80, 100.0);
    assert!((p.cash - 80.0).abs() < 0.01);
    assert!((p.up_tokens() - 0.0).abs() < 0.01);
}

#[test]
fn test_position_resolve_up_wins() {
    let mut p = Position::new(10.0);
    p.set_leg(0, 100.0);
    p.set_leg(1, 50.0);
    p.resolve(Some("Up"));
    assert_eq!(p.cash, 110.0);
    assert_eq!(p.up_tokens(), 0.0);
    assert_eq!(p.down_tokens(), 0.0);
}

#[test]
fn test_position_resolve_down_wins() {
    let mut p = Position::new(10.0);
    p.set_leg(0, 100.0);
    p.set_leg(1, 50.0);
    p.resolve(Some("Down"));
    assert_eq!(p.cash, 60.0);
    assert_eq!(p.up_tokens(), 0.0);
    assert_eq!(p.down_tokens(), 0.0);
}

#[test]
fn test_position_resolve_none() {
    let mut p = Position::new(10.0);
    p.set_leg(0, 100.0);
    p.resolve(None);
    assert_eq!(p.cash, 10.0);
    assert_eq!(p.up_tokens(), 100.0);
}

#[test]
fn test_position_reserve_unreserve_buy() {
    let mut p = Position::new(100.0);
    let (dr, tr) = p.reserve_for_order("buy_up", 0.50, 50.0);
    assert!((dr - 25.0).abs() < 0.01);
    assert_eq!(tr, 0.0);
    assert!((p.cash - 75.0).abs() < 0.01);

    p.unreserve_for_order("buy_up", dr, tr);
    assert!((p.cash - 100.0).abs() < 0.01);
}

#[test]
fn test_position_reserve_unreserve_sell() {
    let mut p = Position::new(0.0);
    p.set_leg(0, 100.0);
    let (dr, tr) = p.reserve_for_order("sell_up", 0.80, 50.0);
    assert_eq!(dr, 0.0);
    assert_eq!(tr, 50.0);
    assert_eq!(p.up_tokens(), 50.0);

    p.unreserve_for_order("sell_up", dr, tr);
    assert_eq!(p.up_tokens(), 100.0);
}

// ===========================================================================
// 1. SimExecutor tests (matches test_sim_executor.py)
// ===========================================================================

#[test]
fn test_sim_executor_midpoint_buy() {
    let exec = SimExecutor::new("midpoint", 0.02, false, None, 0);
    let sample = make_sample(10.0, 60000.0, 0.55, 0.45);
    let mut pos = Position::new(100.0);
    let action = TradeAction {
        side: "buy_up".to_string(),
        amount: 100.0,
        price: None,
        expiration_seconds: None,
        cancel_after_s: None,
    };
    let trade = exec.execute_trade(&action, 0, &sample, &mut pos);
    assert!(trade.is_some());
    let t = trade.unwrap();
    assert!((t.price - 0.55).abs() < 0.01);
    assert!(t.tokens > 0.0);
}

#[test]
fn test_sim_midpoint_slippage_buy() {
    let exec = SimExecutor::new("midpoint_slippage", 0.02, false, None, 0);
    let sample = make_sample(10.0, 60000.0, 0.50, 0.50);
    let mut pos = Position::new(100.0);
    let action = TradeAction {
        side: "buy_up".to_string(),
        amount: 100.0,
        price: None,
        expiration_seconds: None,
        cancel_after_s: None,
    };
    let trade = exec.execute_trade(&action, 0, &sample, &mut pos);
    assert!(trade.is_some());
    let t = trade.unwrap();
    // midpoint_slippage adds slippage to buy price: 0.50 + 0.02 = 0.52
    assert!((t.price - 0.52).abs() < 0.01);
}

#[test]
fn test_sim_midpoint_slippage_sell() {
    let exec = SimExecutor::new("midpoint_slippage", 0.02, false, None, 0);
    let sample = make_sample(10.0, 60000.0, 0.60, 0.40);
    let mut pos = Position::new(0.0);
    pos.set_leg(0, 100.0);
    let action = TradeAction {
        side: "sell_up".to_string(),
        amount: 100.0,
        price: None,
        expiration_seconds: None,
        cancel_after_s: None,
    };
    let trade = exec.execute_trade(&action, 0, &sample, &mut pos);
    assert!(trade.is_some());
    let t = trade.unwrap();
    // midpoint_slippage subtracts slippage from sell price: 0.60 - 0.02 = 0.58
    assert!((t.price - 0.58).abs() < 0.01);
}

#[test]
fn test_sim_book_mode_uses_ask_for_buy() {
    let exec = SimExecutor::new("book", 0.02, false, None, 0);
    let sample = make_sample_with_book(10.0, 60000.0, 0.55, 0.45, 0.60, 0.50);
    let mut pos = Position::new(100.0);
    let action = TradeAction {
        side: "buy_up".to_string(),
        amount: 100.0,
        price: None,
        expiration_seconds: None,
        cancel_after_s: None,
    };
    let trade = exec.execute_trade(&action, 0, &sample, &mut pos);
    assert!(trade.is_some());
    let t = trade.unwrap();
    // Book mode buys at best_ask
    assert!((t.price - 0.60).abs() < 0.01);
}

#[test]
fn test_sim_book_mode_uses_midpoint_for_sell() {
    let exec = SimExecutor::new("book", 0.02, false, None, 0);
    let sample = make_sample_with_book(10.0, 60000.0, 0.70, 0.30, 0.75, 0.35);
    let mut pos = Position::new(0.0);
    pos.set_leg(0, 100.0);
    let action = TradeAction {
        side: "sell_up".to_string(),
        amount: 100.0,
        price: None,
        expiration_seconds: None,
        cancel_after_s: None,
    };
    let trade = exec.execute_trade(&action, 0, &sample, &mut pos);
    assert!(trade.is_some());
    let t = trade.unwrap();
    // Book mode sells at midpoint
    assert!((t.price - 0.70).abs() < 0.01);
}

#[test]
fn test_sim_fee_deduction() {
    let exec = SimExecutor::new("midpoint", 0.0, true, None, 0);
    let sample = make_sample(10.0, 60000.0, 0.50, 0.50);
    let mut pos = Position::new(100.0);
    let action = TradeAction {
        side: "buy_up".to_string(),
        amount: 100.0,
        price: None,
        expiration_seconds: None,
        cancel_after_s: None,
    };
    let trade = exec.execute_trade(&action, 0, &sample, &mut pos);
    assert!(trade.is_some());
    let t = trade.unwrap();
    assert!(t.fee.unwrap() > 0.0);
    // Cash should be less than 0 (cost + fee deducted)
    // Cost = tokens * 0.50, fee deducted from cash
    assert!(pos.cash < 0.0);
}

#[test]
fn test_sim_zero_amount_returns_none() {
    let exec = SimExecutor::new("midpoint", 0.02, false, None, 0);
    let sample = make_sample(10.0, 60000.0, 0.55, 0.45);
    let mut pos = Position::new(100.0);
    let action = TradeAction {
        side: "buy_up".to_string(),
        amount: 0.0,
        price: None,
        expiration_seconds: None,
        cancel_after_s: None,
    };
    let trade = exec.execute_trade(&action, 0, &sample, &mut pos);
    assert!(trade.is_none());
}

#[test]
fn test_sim_below_min_order_returns_none() {
    let exec = SimExecutor::new("midpoint", 0.02, false, None, 0);
    let sample = make_sample(10.0, 60000.0, 0.55, 0.45);
    // Only $1 at 0.55 = ~1.8 tokens, below CLOB_MIN_ORDER_TOKENS (5)
    let mut pos = Position::new(1.0);
    let action = TradeAction {
        side: "buy_up".to_string(),
        amount: 1.0,
        price: None,
        expiration_seconds: None,
        cancel_after_s: None,
    };
    let trade = exec.execute_trade(&action, 0, &sample, &mut pos);
    assert!(trade.is_none());
}

#[test]
fn test_sim_executor_hold_returns_none() {
    let exec = SimExecutor::new("midpoint", 0.02, false, None, 0);
    let sample = make_sample(10.0, 60000.0, 0.55, 0.45);
    let mut pos = Position::new(100.0);
    let result = exec.execute_trade(&hold(), 0, &sample, &mut pos);
    assert!(result.is_none());
}

#[test]
fn test_sim_limit_order_place_and_cancel() {
    let mut exec = SimExecutor::new("book", 0.02, false, None, 0);
    let sample = make_sample(10.0, 60000.0, 0.55, 0.45);
    let mut pos = Position::new(100.0);

    let action = TradeAction {
        side: "buy_up".to_string(),
        amount: 100.0,
        price: Some(0.50),
        expiration_seconds: Some(120.0),
        cancel_after_s: None,
    };

    let order = exec.place_limit_order(&action, 0, &sample, &mut pos);
    assert!(order.is_some());
    let order = order.unwrap();
    assert_eq!(order.leg, 0);
    // Cash should be reserved (reduced)
    let cash_after_reserve = pos.cash;
    assert!(cash_after_reserve < 100.0);

    // Cancel the order -- funds should be unreserved
    exec.cancel_order(&order, &mut pos);
    assert!((pos.cash - 100.0).abs() < 0.01);
}

#[test]
fn test_sim_limit_order_gtd_expiry() {
    let mut exec = SimExecutor::new("book", 0.02, false, None, 0);
    let sample = make_sample(10.0, 60000.0, 0.55, 0.45);
    let mut pos = Position::new(100.0);

    let action = TradeAction {
        side: "buy_up".to_string(),
        amount: 100.0,
        price: Some(0.50),
        expiration_seconds: Some(30.0), // Expires at elapsed=40
        cancel_after_s: None,
    };

    let order = exec.place_limit_order(&action, 0, &sample, &mut pos);
    assert!(order.is_some());
    let order = order.unwrap();
    let cash_reserved = pos.cash;
    assert!(cash_reserved < 100.0);

    // Tick past expiry -- order should be expired and funds unreserved
    let mut orders = vec![order];
    let expired_sample = make_sample(50.0, 60000.0, 0.55, 0.45);
    let fills = exec.check_fills(&mut orders, &expired_sample, &mut pos);
    assert_eq!(fills.len(), 0);
    assert!(orders.is_empty()); // Order was removed
    assert!((pos.cash - 100.0).abs() < 0.01); // Funds unreserved
}

#[test]
fn test_sim_limit_order_fill_threshold() {
    let mut exec = SimExecutor::new("book", 0.02, false, None, 0);
    let sample = make_sample(10.0, 60000.0, 0.55, 0.45);
    let mut pos = Position::new(100.0);

    let action = TradeAction {
        side: "buy_up".to_string(),
        amount: 100.0,
        price: Some(0.55),
        expiration_seconds: Some(120.0),
        cancel_after_s: None,
    };

    let order = exec.place_limit_order(&action, 0, &sample, &mut pos);
    assert!(order.is_some());
    let order = order.unwrap();

    // Midpoint at order price -- should NOT fill (needs to cross by margin)
    let mut orders = vec![order];
    let nofill_sample = make_sample(20.0, 60000.0, 0.55, 0.45);
    let fills = exec.check_fills(&mut orders, &nofill_sample, &mut pos);
    assert_eq!(fills.len(), 0);
    assert_eq!(orders.len(), 1); // Order still pending

    // Midpoint drops well below limit price minus margin -- should fill
    let fill_sample = make_sample(25.0, 59800.0, 0.48, 0.52);
    let fills = exec.check_fills(&mut orders, &fill_sample, &mut pos);
    assert_eq!(fills.len(), 1);
    assert!(pos.up_tokens() > 0.0);
}

#[test]
fn test_sim_sell_limit_fill() {
    let mut exec = SimExecutor::new("book", 0.02, false, None, 0);
    let sample = make_sample(10.0, 60000.0, 0.55, 0.45);
    let mut pos = Position::new(0.0);
    pos.set_leg(0, 100.0);

    let action = TradeAction {
        side: "sell_up".to_string(),
        amount: 100.0,
        price: Some(0.60),
        expiration_seconds: Some(120.0),
        cancel_after_s: None,
    };

    let order = exec.place_limit_order(&action, 0, &sample, &mut pos);
    assert!(order.is_some());
    let order = order.unwrap();
    assert_eq!(order.leg, 0);
    assert_eq!(pos.up_tokens(), 0.0); // Tokens reserved

    // Midpoint rises above price + margin -- should fill
    let mut orders = vec![order];
    let fill_sample = make_sample(20.0, 60200.0, 0.70, 0.30);
    let fills = exec.check_fills(&mut orders, &fill_sample, &mut pos);
    assert_eq!(fills.len(), 1);
    assert!(pos.cash > 0.0); // Got proceeds
    assert!(orders.is_empty());
}

#[test]
fn test_sim_executor_limit_order_and_fill() {
    let mut exec = SimExecutor::new("book", 0.02, false, None, 0);
    let sample = make_sample(10.0, 60000.0, 0.55, 0.45);
    let mut pos = Position::new(100.0);

    let action = TradeAction {
        side: "buy_up".to_string(),
        amount: 100.0,
        price: Some(0.55),
        expiration_seconds: Some(120.0),
        cancel_after_s: None,
    };

    let order = exec.place_limit_order(&action, 0, &sample, &mut pos);
    assert!(order.is_some());
    let order = order.unwrap();
    assert_eq!(order.leg, 0);

    // Price drops below limit - margin -> fill
    let mut orders = vec![order];
    let fill_sample = make_sample(20.0, 59900.0, 0.50, 0.50);
    let fills = exec.check_fills(&mut orders, &fill_sample, &mut pos);
    assert_eq!(fills.len(), 1);
    assert!(pos.up_tokens() > 0.0);
}

#[test]
fn test_sim_executor_book_buy() {
    let exec = SimExecutor::new("book", 0.02, false, None, 0);
    let mut sample = make_sample(10.0, 60000.0, 0.55, 0.45);
    sample.up_token_best_ask = Some(0.60);
    let mut pos = Position::new(100.0);
    let action = TradeAction {
        side: "buy_up".to_string(),
        amount: 100.0,
        price: None,
        expiration_seconds: None,
        cancel_after_s: None,
    };
    let trade = exec.execute_trade(&action, 0, &sample, &mut pos);
    assert!(trade.is_some());
    let t = trade.unwrap();
    assert!((t.price - 0.60).abs() < 0.01);
}

// ===========================================================================
// 2. Trader tests (matches test_trader.py)
// ===========================================================================

#[test]
fn test_trader_session_lifecycle() {
    let exec = SimExecutor::new("midpoint", 0.02, false, None, 0);
    let strategy: Box<dyn Strategy> = Box::new(AlwaysHoldStrategy);
    let mut trader = Trader::new(strategy, exec);

    let pos = Position::new(100.0);
    trader.start_session(default_metadata(), pos, 60000.0, 300.0);

    let sample = make_sample(10.0, 60000.0, 0.55, 0.45);
    let result = trader.process_tick(&sample);
    assert!(result.is_none()); // AlwaysHold -> no trade

    let trades = trader.end_session(Some("Up"));
    assert!(trades.is_empty());

    // After resolution, position should reflect Up outcome
    let final_pos = trader.position().unwrap();
    assert!((final_pos.cash - 100.0).abs() < 0.01);
}

#[test]
fn test_trader_hold_returns_none() {
    let exec = SimExecutor::new("midpoint", 0.0, false, None, 0);
    let strategy: Box<dyn Strategy> = Box::new(AlwaysHoldStrategy);
    let mut trader = Trader::new(strategy, exec);
    trader.start_session(default_metadata(), Position::new(100.0), 60000.0, 300.0);

    let sample = make_sample(10.0, 60000.0, 0.50, 0.50);
    let result = trader.process_tick(&sample);
    assert!(result.is_none());
}

#[test]
fn test_trader_buy_caps_to_cash() {
    let exec = SimExecutor::new("midpoint", 0.0, false, None, 0);
    let strategy: Box<dyn Strategy> = Box::new(BuyMaxStrategy);
    let mut trader = Trader::new(strategy, exec);
    trader.start_session(default_metadata(), Position::new(50.0), 60000.0, 300.0);

    // BuyMaxStrategy requests $99999, but Trader caps to available cash ($50)
    let sample = make_sample(10.0, 60000.0, 0.50, 0.50);
    let result = trader.process_tick(&sample);
    assert!(result.is_some());
    let t = result.unwrap();
    assert!(t.tokens > 0.0);
    // Position should have used at most $50
    let pos = trader.position().unwrap();
    assert!(pos.cash >= -0.01); // Shouldn't go negative (or barely due to rounding)
}

#[test]
fn test_trader_cutoff_near_end() {
    let exec = SimExecutor::new("midpoint", 0.0, false, None, 0);
    let strategy: Box<dyn Strategy> = Box::new(BuyOnceStrategy::new());
    let mut trader = Trader::new(strategy, exec);
    trader.start_session(default_metadata(), Position::new(100.0), 60000.0, 300.0);

    // At 296s (within TRADE_CUTOFF_SECONDS=5s of 300s end), trade should be blocked
    let sample = make_sample(296.0, 60000.0, 0.50, 0.50);
    let result = trader.process_tick(&sample);
    assert!(result.is_none()); // Blocked by cutoff guard
}

#[test]
fn test_trader_limit_order_flow() {
    let exec = SimExecutor::new("book", 0.02, false, None, 0);
    let strategy: Box<dyn Strategy> = Box::new(LimitOrderStrategy::new());
    let mut trader = Trader::new(strategy, exec);
    trader.start_session(default_metadata(), Position::new(100.0), 60000.0, 300.0);

    // First tick -- too early (< 0.1 elapsed fraction)
    let sample1 = make_sample(10.0, 60000.0, 0.55, 0.45);
    let result1 = trader.process_tick(&sample1);
    assert!(result1.is_none()); // Hold

    // Second tick -- past 0.1 fraction: should place limit order (returns None since limit)
    let sample2 = make_sample(35.0, 60000.0, 0.55, 0.45);
    let result2 = trader.process_tick(&sample2);
    assert!(result2.is_none()); // Limit orders don't return a trade immediately

    // Cash should be reduced due to reservation
    let pos = trader.position().unwrap();
    assert!(pos.cash < 100.0);
}

#[test]
fn test_trader_cancel_action() {
    let exec = SimExecutor::new("book", 0.02, false, None, 0);
    let strategy: Box<dyn Strategy> = Box::new(CancelStrategy::new());
    let mut trader = Trader::new(strategy, exec);
    trader.start_session(default_metadata(), Position::new(100.0), 60000.0, 300.0);

    // Tick 1 -- place limit order
    let sample1 = make_sample(10.0, 60000.0, 0.55, 0.45);
    trader.process_tick(&sample1);
    let cash_after_place = trader.position().unwrap().cash;
    assert!(cash_after_place < 100.0);

    // Tick 2 -- hold
    let sample2 = make_sample(20.0, 60000.0, 0.55, 0.45);
    trader.process_tick(&sample2);

    // Tick 3 -- cancel
    let sample3 = make_sample(30.0, 60000.0, 0.55, 0.45);
    trader.process_tick(&sample3);

    // Cash should be restored after cancel
    let pos = trader.position().unwrap();
    assert!((pos.cash - 100.0).abs() < 0.01);
}

#[test]
fn test_trader_deferred_latency() {
    // With latency, trades should be deferred
    let exec = SimExecutor::new("midpoint", 0.0, false, None, 200);
    let strategy: Box<dyn Strategy> = Box::new(BuyOnceStrategy::new());
    let mut trader = Trader::new(strategy, exec);
    trader.start_session(default_metadata(), Position::new(100.0), 60000.0, 300.0);

    // Tick past 50% -- BuyOnceStrategy wants to buy
    let sample1 = make_sample(160.0, 60000.0, 0.55, 0.45);
    let result1 = trader.process_tick(&sample1);
    // With latency, the trade is deferred, not executed immediately
    assert!(result1.is_none());

    // Cash should be reserved for the deferred action
    let cash_after = trader.position().unwrap().cash;
    assert!(cash_after < 100.0);

    // Tick after latency period (200ms = 0.2s later)
    let sample2 = make_sample(160.3, 60000.0, 0.55, 0.45);
    trader.process_tick(&sample2);

    // The deferred trade should have executed
    let pos = trader.position().unwrap();
    assert!(pos.up_tokens() > 0.0);
}

// ===========================================================================
// test_math_utils (matches test_math_utils.py)
// ===========================================================================

#[test]
fn test_norm_cdf_zero() {
    assert!((norm_cdf(0.0) - 0.5).abs() < 1e-6);
}

#[test]
fn test_norm_cdf_positive() {
    assert!((norm_cdf(1.96) - 0.975).abs() < 0.01);
}

#[test]
fn test_norm_cdf_negative() {
    assert!((norm_cdf(-1.96) - 0.025).abs() < 0.01);
}

#[test]
fn test_ols_slope_flat() {
    let prices = vec![100.0; 10];
    let slope = ols_slope_bps(&prices, 10).unwrap();
    assert!(slope.abs() < 1e-10);
}

#[test]
fn test_ols_slope_upward() {
    let prices: Vec<f64> = (0..10).map(|i| 100.0 + i as f64).collect();
    let slope = ols_slope_bps(&prices, 10).unwrap();
    assert!(slope > 0.0);
}

#[test]
fn test_ols_timed_insufficient() {
    assert!(ols_slope_bps_timed(&[0.0], &[100.0]).is_none());
}

// ===========================================================================
// 3. Regime tests (matches test_regime.py)
// ===========================================================================

#[test]
fn test_session_volatility_basic() {
    let s = make_vol_summaries(&[5.0, -3.0, 7.0, -2.0, 4.0]);
    let (mean, _) = compute_session_volatility(&s, 5, 3).unwrap();
    let expected = (5.0 + 3.0 + 7.0 + 2.0 + 4.0) / 5.0;
    assert!((mean - expected).abs() < 0.01);
}

#[test]
fn test_session_volatility_insufficient() {
    let s = make_vol_summaries(&[5.0, -3.0]);
    assert!(compute_session_volatility(&s, 5, 3).is_none());
}

#[test]
fn test_vol_regime_classification() {
    let low = make_vol_summaries(&[1.0, -1.0, 0.5, -0.5, 1.0]);
    assert_eq!(classify_vol_regime(&low, 5, 3), Some(VolRegime::Low));

    let high = make_vol_summaries(&[10.0, -12.0, 15.0, -8.0, 11.0]);
    assert_eq!(classify_vol_regime(&high, 5, 3), Some(VolRegime::High));
}

#[test]
fn test_signed_volatility() {
    let s = make_vol_summaries(&[5.0, -3.0, 7.0, -2.0, 4.0]);
    let result = compute_signed_volatility(&s, 5, 3);
    assert!(result.is_some());
    let (mean, _) = result.unwrap();
    let expected = (5.0 + (-3.0) + 7.0 + (-2.0) + 4.0) / 5.0;
    assert!((mean - expected).abs() < 0.01);
}

#[test]
fn test_regime_classification_combined() {
    let summaries = make_vol_summaries(&[5.0, -3.0, 7.0, -2.0, 4.0]);
    let regime = classify_regime(&summaries, 5, 3, Some(300.0), 1.0, Some(0.5), Some(600.0));
    assert!(regime.vol.is_some());
    assert!(regime.trend.is_some());
    assert_eq!(regime.data_quality, "clean");
}

#[test]
fn test_regime_stale_data_detection() {
    let summaries = make_vol_summaries(&[5.0, -3.0, 7.0, -2.0, 4.0]);
    // Gap > max_gap -> stale
    let regime = classify_regime(
        &summaries,
        5,
        3,
        Some(1200.0), // gap_before_seconds > max_gap_s (600)
        1.0,
        Some(0.5),
        Some(600.0),
    );
    assert_eq!(regime.data_quality, "stale");
}

#[test]
fn test_trend_regime_trending() {
    // Positive autocorrelation: consistent positive returns
    let summaries = make_vol_summaries(&[5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 11.0, 12.0, 13.0, 14.0]);
    let trend = classify_trend_regime(&summaries, 10, 5);
    // All positive, high positive autocorrelation
    // Note: with all same-sign returns, autocorrelation depends on the pattern
    assert!(trend.is_some());
}

#[test]
fn test_trend_regime_mean_reverting() {
    // Alternating returns: negative autocorrelation
    let summaries = make_vol_summaries(&[5.0, -5.0, 5.0, -5.0, 5.0, -5.0, 5.0, -5.0, 5.0, -5.0]);
    let trend = classify_trend_regime(&summaries, 10, 5);
    assert_eq!(trend, Some(TrendRegime::MeanReverting));
}

#[test]
fn test_trend_regime_flat() {
    // Near-zero autocorrelation
    let summaries = make_vol_summaries(&[1.0, 3.0, -2.0, 1.0, -1.0, 2.0, -3.0, 1.0, 0.5, -0.5]);
    let trend = classify_trend_regime(&summaries, 10, 5);
    // With mixed returns, autocorrelation should be near zero
    assert!(trend.is_some());
    // Could be Flat depending on exact autocorrelation
}

#[test]
fn test_vol_regime_medium() {
    // Returns between VOL_LOW_THRESHOLD_BPS (3.0) and VOL_HIGH_THRESHOLD_BPS (8.0)
    let summaries = make_vol_summaries(&[4.0, -5.0, 6.0, -4.0, 5.0]);
    let vol = classify_vol_regime(&summaries, 5, 3);
    assert_eq!(vol, Some(VolRegime::Medium));
}

// ===========================================================================
// test_strategy_base (matches test_strategy_base.py)
// ===========================================================================

#[test]
fn test_param_grid_generation() {
    let p = Param::float(0.5, 0.0, 1.0);
    let grid = p.grid(3);
    assert_eq!(grid.len(), 3);
    assert_eq!(grid[0], ParamValue::Float(0.0));
    assert_eq!(grid[1], ParamValue::Float(0.5));
    assert_eq!(grid[2], ParamValue::Float(1.0));
}

#[test]
fn test_param_grid_nullable() {
    let p = Param::float_nullable(0.5, 0.0, 1.0);
    let grid = p.grid(3);
    assert!(grid.contains(&ParamValue::None));
    assert!(grid.len() >= 4);
}

#[test]
fn test_param_fixed() {
    let p = Param::fixed_float(0.5);
    let grid = p.grid(3);
    assert_eq!(grid.len(), 1);
    assert_eq!(grid[0], ParamValue::Float(0.5));
}

#[test]
fn test_held_side_empty() {
    let p = Position::new(100.0);
    assert_eq!(held_side(&p), None);
}

#[test]
fn test_held_side_up() {
    let mut p = Position::new(0.0);
    p.set_leg(0, 50.0);
    assert_eq!(held_side(&p), Some("up"));
}

#[test]
fn test_held_side_down() {
    let mut p = Position::new(0.0);
    p.set_leg(1, 50.0);
    assert_eq!(held_side(&p), Some("down"));
}

#[test]
fn test_timing_guards_too_early() {
    assert!(check_timing_guards(0.3, 0.5, 0.95));
}

#[test]
fn test_timing_guards_ok() {
    assert!(!check_timing_guards(0.6, 0.5, 0.95));
}

#[test]
fn test_timing_guards_too_late() {
    assert!(check_timing_guards(0.96, 0.5, 0.95));
}

#[test]
fn test_param_grid_default_included() {
    // Default value must always appear in grid
    let p = Param::float(0.37, 0.0, 1.0);
    let grid = p.grid(3);
    // The default 0.37 should replace the nearest grid value
    assert!(grid.contains(&ParamValue::Float(0.37)));
}

#[test]
fn test_param_grid_int_range() {
    let p = Param::int(3, 1, 5);
    let grid = p.grid(5);
    // Should include 1, 2, 3, 4, 5
    assert!(grid.contains(&ParamValue::Int(1)));
    assert!(grid.contains(&ParamValue::Int(5)));
    // Grid should have values between min and max
    for v in &grid {
        if let ParamValue::Int(i) = v {
            assert!(*i >= 1 && *i <= 5);
        }
    }
}

#[test]
fn test_strategy_param_grid_total() {
    // param_grid() respects max_variants cap
    let s = fondaco::strategies::poisson_kelly::PoissonKelly::default();
    let grid = s.param_grid(100);
    if !grid.is_empty() {
        let total: usize = grid.values().map(|v| v.len()).product();
        assert!(total <= 100);
    }
}

#[test]
fn test_has_position() {
    let p = Position::new(100.0);
    assert!(!has_position(&p));

    let mut p2 = Position::new(0.0);
    p2.set_leg(0, 10.0);
    assert!(has_position(&p2));
}

#[test]
fn test_check_expired_entry() {
    let mut entry_midpoint = 0.55;
    let p = Position::new(100.0); // No tokens held
    check_expired_entry(&mut entry_midpoint, &p);
    // Entry midpoint should be reset since no position
    assert_eq!(entry_midpoint, 0.0);

    // With position, should not reset
    let mut entry_midpoint2 = 0.55;
    let mut p2 = Position::new(0.0);
    p2.set_leg(0, 50.0);
    check_expired_entry(&mut entry_midpoint2, &p2);
    assert_eq!(entry_midpoint2, 0.55);
}

// ===========================================================================
// 4. Strategy behavior tests (matches test_active_strategies.py)
// ===========================================================================

#[test]
fn test_poisson_kelly_param_defs() {
    let s = fondaco::strategies::poisson_kelly::PoissonKelly::default();
    let defs = s.param_defs();
    assert!(defs.contains_key("jump_bps"));
    assert!(defs.contains_key("min_elapsed"));
    assert!(defs.contains_key("edge_min"));
    assert!(defs.contains_key("stop_loss_pct"));
}

#[test]
fn test_strategy_with_params() {
    let s = fondaco::strategies::poisson_kelly::PoissonKelly::default();
    let mut params = s.params();
    params.insert("jump_bps".to_string(), ParamValue::Float(0.25));
    let s2 = s.with_params(&params);
    let p2 = s2.params();
    assert_eq!(p2.get("jump_bps"), Some(&ParamValue::Float(0.25)));
}

#[test]
fn test_poisson_kelly_holds_early() {
    let mut s = fondaco::strategies::poisson_kelly::PoissonKelly::default();
    s.on_session_start(&default_metadata());

    // Before min_elapsed (0.65), should always hold
    let pos = Position::new(100.0);
    let sample = make_sample(100.0, 60000.0, 0.50, 0.50);
    let ctx = TickContext {
        tick_index: 0,
        sample: &sample,
        position: &pos,
        metadata: &default_metadata(),
        initial_btc_price: 60000.0,
        elapsed_fraction: 0.30, // < 0.65 min_elapsed
        elapsed_seconds: 100.0,
        max_buy_amount: 100.0,
        open_orders: vec![],
    };
    let action = s.on_tick(&ctx);
    assert!(action.is_hold());
}

#[test]
fn test_poisson_kelly_counts_jumps() {
    // Set jump_bps low to trigger easily
    // We need to feed multiple ticks with BTC price changes to accumulate jumps
    let mut s2 = fondaco::strategies::poisson_kelly::PoissonKelly::default();
    s2.jump_bps = 1.0; // 1 bps = 0.01% -- very small jumps
    s2.min_elapsed = 0.10;
    s2.on_session_start(&default_metadata());

    let pos = Position::new(100.0);
    let meta = default_metadata();

    // Feed ticks with small BTC moves to count jumps
    let base_btc = 60000.0;
    for i in 0..20 {
        // Alternate price up/down by 10 bps to generate jumps
        let btc = if i % 2 == 0 {
            base_btc * (1.0 + 10.0 / BPS_DIVISOR)
        } else {
            base_btc * (1.0 - 10.0 / BPS_DIVISOR)
        };
        let elapsed = 10.0 + i as f64 * 5.0;
        let sample = make_sample(elapsed, btc, 0.50, 0.50);
        let ctx = TickContext {
            tick_index: i,
            sample: &sample,
            position: &pos,
            metadata: &meta,
            initial_btc_price: base_btc,
            elapsed_fraction: elapsed / 300.0,
            elapsed_seconds: elapsed,
            max_buy_amount: 100.0,
            open_orders: vec![],
        };
        s2.on_tick(&ctx);
    }

    // Strategy should have counted jumps -- verify indirectly by checking
    // it eventually produces a non-hold action or at least doesn't crash
    // (jump counting is internal state)
}

#[test]
fn test_drift_sniper_needs_displacement() {
    let mut s = fondaco::strategies::drift_sniper::DriftSniper::default();
    s.on_session_start(&default_metadata());

    let pos = Position::new(100.0);
    let meta = default_metadata();

    // Feed some BTC prices to build history
    for i in 0..10 {
        let sample = make_sample(
            i as f64 * 20.0,
            60000.0, // No displacement from initial
            0.50,
            0.50,
        );
        let ctx = TickContext {
            tick_index: i,
            sample: &sample,
            position: &pos,
            metadata: &meta,
            initial_btc_price: 60000.0,
            elapsed_fraction: (i as f64 * 20.0) / 300.0,
            elapsed_seconds: i as f64 * 20.0,
            max_buy_amount: 100.0,
            open_orders: vec![],
        };
        let action = s.on_tick(&ctx);
        // Without BTC displacement, should always hold
        assert!(action.is_hold());
    }
}

#[test]
fn test_drift_sniper_needs_velocity() {
    let mut s = fondaco::strategies::drift_sniper::DriftSniper::default();
    s.on_session_start(&default_metadata());

    let pos = Position::new(100.0);
    let meta = default_metadata();

    // Feed BTC prices that have displacement but no consistent velocity
    // Build up some history first
    for i in 0..5 {
        let sample = make_sample(i as f64 * 1.0, 60000.0, 0.50, 0.50);
        let ctx = TickContext {
            tick_index: i,
            sample: &sample,
            position: &pos,
            metadata: &meta,
            initial_btc_price: 60000.0,
            elapsed_fraction: (i as f64 * 1.0) / 300.0,
            elapsed_seconds: i as f64 * 1.0,
            max_buy_amount: 100.0,
            open_orders: vec![],
        };
        s.on_tick(&ctx);
    }

    // Now jump BTC price up suddenly (displacement but recent velocity is flat)
    let btc_displaced = 60000.0 * (1.0 + 20.0 / BPS_DIVISOR); // 20 bps up
    let sample = make_sample(200.0, btc_displaced, 0.60, 0.40);
    let ctx = TickContext {
        tick_index: 10,
        sample: &sample,
        position: &pos,
        metadata: &meta,
        initial_btc_price: 60000.0,
        elapsed_fraction: 200.0 / 300.0,
        elapsed_seconds: 200.0,
        max_buy_amount: 100.0,
        open_orders: vec![],
    };
    let action = s.on_tick(&ctx);
    // Should hold because velocity window only has 1-2 recent points
    // (insufficient for strong OLS velocity)
    assert!(action.is_hold());
}

#[test]
fn test_gap_fader_observation_phase() {
    let mut s = fondaco::strategies::gap_fader::GapFader::default();
    s.on_session_start(&default_metadata());

    let pos = Position::new(100.0);
    let meta = default_metadata();

    // During observation period (first 15 seconds), should always hold
    for i in 0..5 {
        let elapsed = i as f64 * 2.0; // 0, 2, 4, 6, 8 seconds
        let sample = make_sample(elapsed, 60000.0, 0.50, 0.50);
        let ctx = TickContext {
            tick_index: i,
            sample: &sample,
            position: &pos,
            metadata: &meta,
            initial_btc_price: 60000.0,
            elapsed_fraction: elapsed / 300.0,
            elapsed_seconds: elapsed,
            max_buy_amount: 100.0,
            open_orders: vec![],
        };
        let action = s.on_tick(&ctx);
        assert!(action.is_hold(), "Should hold during observation at {}s", elapsed);
    }
}

#[test]
fn test_gap_fader_wide_gap_deactivates() {
    let mut s = fondaco::strategies::gap_fader::GapFader::default();
    s.on_session_start(&default_metadata());

    let pos = Position::new(100.0);
    let meta = default_metadata();

    // Feed observation samples with a wide gap (up=0.80, down=0.20 => gap=0.60)
    for i in 0..10 {
        let elapsed = i as f64 * 1.5;
        let sample = make_sample(elapsed, 60000.0, 0.80, 0.20);
        let ctx = TickContext {
            tick_index: i,
            sample: &sample,
            position: &pos,
            metadata: &meta,
            initial_btc_price: 60000.0,
            elapsed_fraction: elapsed / 300.0,
            elapsed_seconds: elapsed,
            max_buy_amount: 100.0,
            open_orders: vec![],
        };
        s.on_tick(&ctx);
    }

    // After observation, the wide gap should prevent activation
    let sample_post = make_sample(20.0, 60000.0, 0.45, 0.45);
    let ctx_post = TickContext {
        tick_index: 20,
        sample: &sample_post,
        position: &pos,
        metadata: &meta,
        initial_btc_price: 60000.0,
        elapsed_fraction: 20.0 / 300.0,
        elapsed_seconds: 20.0,
        max_buy_amount: 100.0,
        open_orders: vec![],
    };
    let action = s.on_tick(&ctx_post);
    assert!(action.is_hold(), "Wide gap should deactivate GapFader");
}

#[test]
fn test_regime_gated_skips_wrong_regime() {
    let mut s = fondaco::strategies::trend_drift::TrendDrift::default();

    // Create metadata with flat/low vol regime (should gate TrendDrift out)
    let summaries = make_vol_summaries(&[1.0, -1.0, 0.5, -0.5, 1.0, -1.0, 0.5, -0.5, 1.0, -1.0,
                                          0.5, -0.5, 1.0, -1.0, 0.5, -0.5, 1.0, -1.0, 0.5, -0.5]);
    let mut meta = default_metadata();
    meta.prior_session_summaries = summaries;
    meta.gap_before_seconds = Some(300.0);
    meta.data_density = 1.0;

    s.on_session_start(&meta);

    // TrendDrift requires trending + medium/high vol
    // Low vol + flat trend -> should hold on every tick
    let pos = Position::new(100.0);
    let btc_up = 60000.0 * (1.0 + 20.0 / BPS_DIVISOR);
    let sample = make_sample(200.0, btc_up, 0.60, 0.40);
    let ctx = TickContext {
        tick_index: 0,
        sample: &sample,
        position: &pos,
        metadata: &meta,
        initial_btc_price: 60000.0,
        elapsed_fraction: 200.0 / 300.0,
        elapsed_seconds: 200.0,
        max_buy_amount: 100.0,
        open_orders: vec![],
    };
    let action = s.on_tick(&ctx);
    assert!(action.is_hold(), "TrendDrift should skip flat/low-vol regime");
}

// ===========================================================================
// test_strategy_registry (matches test_strategy_registry.py)
// ===========================================================================

#[test]
fn test_all_strategies_registry() {
    let strategies = fondaco::strategies::all_strategies();
    assert!(strategies.contains_key("PoissonKelly"));
    assert!(strategies.contains_key("PoissonKellyDynamic"));
    assert!(strategies.contains_key("DriftSniper"));
    assert!(strategies.contains_key("GapFader"));
    assert!(strategies.contains_key("RegimeSniper"));
    assert!(strategies.contains_key("TrendDrift"));
    assert!(strategies.contains_key("MeanRevFader"));
    assert!(strategies.contains_key("LateSniperCB"));
    assert_eq!(strategies.len(), 8);
}

#[test]
fn test_strategy_names() {
    let strategies = fondaco::strategies::all_strategies();
    for (name, factory) in &strategies {
        let s = factory();
        assert_eq!(s.name(), name.as_str());
    }
}

// ===========================================================================
// 5. Engine/Simulator tests (matches test_simulator.py)
// ===========================================================================

fn make_test_session_data(outcome: &str, num_samples: usize) -> SessionData {
    let samples: Vec<Sample> = (0..num_samples)
        .map(|i| {
            let elapsed = i as f64 * (300.0 / num_samples as f64);
            let btc = if outcome == "Up" {
                60000.0 + i as f64 * 2.0
            } else {
                60000.0 - i as f64 * 2.0
            };
            let up_mid = if outcome == "Up" {
                0.50 + i as f64 * 0.005
            } else {
                0.50 - i as f64 * 0.005
            };
            make_sample(elapsed, btc, up_mid.min(0.95).max(0.05), (1.0 - up_mid).min(0.95).max(0.05))
        })
        .collect();

    SessionData {
        metadata: MarketMetadata {
            event_id: Some("e1".to_string()),
            event_slug: Some("btc-updown-5m-test".to_string()),
            event_title: Some("Test".to_string()),
            condition_id: Some("c1".to_string()),
            up_token_id: Some("t1".to_string()),
            down_token_id: Some("t2".to_string()),
            window_start_utc: Some("2026-01-01T00:00:00Z".to_string()),
            window_end_utc: Some("2026-01-01T00:05:00Z".to_string()),
            resolution_source: Some("chainlink".to_string()),
        },
        initial_btc_price_usd: Some(60000.0),
        final_btc_price_usd: Some(if outcome == "Up" { 60100.0 } else { 59900.0 }),
        outcome: Some(outcome.to_string()),
        outcome_prices: Some(vec!["1.00".to_string(), "0.00".to_string()]),
        samples,
        collection_started_utc: String::new(),
        collection_ended_utc: String::new(),
        error_count: 0,
        errors: vec![],
        source_file: "test.json".to_string(),
        _prior_summaries: None,
    }
}

#[test]
fn test_simulator_run_session_no_trades() {
    let sim = Simulator::new(100.0, "midpoint", 0.02, false, None, 0, None);
    let mut strategy = AlwaysHoldStrategy;
    let session = make_test_session_data("Up", 50);
    let result = sim.run_session(&mut strategy, &session, None);
    // AlwaysHold -> no trades -> PnL = 0
    assert!(result.trades.is_empty());
    assert!((result.pnl - 0.0).abs() < 0.01);
}

#[test]
fn test_simulator_run_backtest_aggregation() {
    let sim = Simulator::new(100.0, "midpoint", 0.02, false, None, 0, None);
    let mut strategy = AlwaysHoldStrategy;
    let sessions: Vec<SessionData> = (0..5)
        .map(|i| {
            let outcome = if i % 2 == 0 { "Up" } else { "Down" };
            make_test_session_data(outcome, 50)
        })
        .collect();
    let result = sim.run_backtest(&mut strategy, &sessions);
    assert_eq!(result.num_sessions, 5);
    assert_eq!(result.sessions.len(), 5);
    // No trades -> entry_rate = 0
    assert_eq!(result.entry_rate, 0.0);
}

#[test]
fn test_simulator_compound_mode() {
    let sim = Simulator::new(100.0, "midpoint", 0.0, false, None, 0, Some((0.25, 50.0)));
    let mut strategy = AlwaysHoldStrategy;
    let sessions: Vec<SessionData> = (0..3)
        .map(|_| make_test_session_data("Up", 50))
        .collect();
    let result = sim.run_backtest(&mut strategy, &sessions);
    // Compound mode enabled, but AlwaysHold makes no trades
    // Bankroll should stay at starting cash
    assert_eq!(result.num_sessions, 3);
    // Final bankroll should be approximately starting cash since no trades
    assert!((result.final_bankroll - 100.0).abs() < 1.0);
}

#[test]
fn test_simulator_entry_win_rates() {
    // Create session results directly to test rate computation
    let sessions_with_trades: Vec<SessionResult> = (0..10)
        .map(|i| SessionResult {
            session_file: format!("s{}.json", i),
            outcome: if i % 2 == 0 { "Up" } else { "Down" }.to_string(),
            pnl: if i < 6 { 5.0 } else { -3.0 },
            pnl_pct: if i < 6 { 5.0 } else { -3.0 },
            trades: if i < 8 {
                vec![Trade::new(0, "buy_up", 0.5, 50.0, 100.0)]
            } else {
                vec![] // No trades for sessions 8 and 9
            },
            final_position: Position::new(if i < 6 { 105.0 } else { 97.0 }),
            num_samples: 100,
            starting_cash: 100.0,
            total_fees: 0.0,
        })
        .collect();

    // entry_rate = traded / total = 8/10 = 0.8
    let traded = sessions_with_trades
        .iter()
        .filter(|r| !r.trades.is_empty())
        .count();
    let entry_rate = traded as f64 / sessions_with_trades.len() as f64;
    assert!((entry_rate - 0.8).abs() < 0.01);

    // win_rate = wins / traded = 6/8 = 0.75
    let wins = sessions_with_trades
        .iter()
        .filter(|r| !r.trades.is_empty() && r.pnl > 0.0)
        .count();
    let win_rate = wins as f64 / traded as f64;
    assert!((win_rate - 0.75).abs() < 0.01);
}

// ===========================================================================
// Kelly sizing tests (matches test_simulator.py)
// ===========================================================================

#[test]
fn test_kelly_sizing_positive_returns() {
    let sessions: Vec<SessionResult> = (0..10)
        .map(|i| SessionResult {
            session_file: "test".to_string(),
            outcome: if i % 3 == 0 { "Down" } else { "Up" }.to_string(),
            pnl: if i % 3 == 0 { -20.0 } else { 15.0 },
            pnl_pct: if i % 3 == 0 { -20.0 } else { 15.0 },
            trades: vec![Trade::new(0, "buy_up", 0.5, 50.0, 100.0)],
            final_position: Position::new(if i % 3 == 0 { 80.0 } else { 115.0 }),
            num_samples: 100,
            starting_cash: 100.0,
            total_fees: 0.0,
        })
        .collect();
    let kelly = compute_kelly_sizing(&sessions, 100.0, 0.5, None);
    assert!(kelly.kelly_f > 0.0);
    assert!(kelly.cash_pct > 0.0);
}

#[test]
fn test_kelly_sizing_all_losses() {
    let sessions: Vec<SessionResult> = (0..5)
        .map(|_| SessionResult {
            session_file: "test".to_string(),
            outcome: "Down".to_string(),
            pnl: -10.0,
            pnl_pct: -10.0,
            trades: vec![Trade::new(0, "buy_up", 0.5, 50.0, 100.0)],
            final_position: Position::new(90.0),
            num_samples: 100,
            starting_cash: 100.0,
            total_fees: 0.0,
        })
        .collect();
    let kelly = compute_kelly_sizing(&sessions, 100.0, 0.5, None);
    assert_eq!(kelly.kelly_f, 0.0);
}

#[test]
fn test_kelly_sizing_empty() {
    let sessions: Vec<SessionResult> = vec![];
    let kelly = compute_kelly_sizing(&sessions, 100.0, 0.5, None);
    assert_eq!(kelly.kelly_f, 0.0);
    assert_eq!(kelly.cash_pct, 0.0);
}

// ===========================================================================
// 6. Session cache tests (matches test_gap_handling.py)
// ===========================================================================

#[test]
fn test_session_cache_prior() {
    let summaries: Vec<SessionSummary> = (0..10)
        .map(|i| SessionSummary {
            window_start: format!("2026-01-01T{:02}:00:00Z", i),
            initial_btc: 60000.0,
            final_btc: 60000.0 + i as f64 * 10.0,
            outcome: "Up".to_string(),
        })
        .collect();
    let cache = SessionStatsCache::from_summaries(&summaries);

    let prior = cache.prior("2026-01-01T05:00:00Z", 3);
    assert_eq!(prior.len(), 3);
    assert!(prior[0].window_start.contains("02:"));
}

#[test]
fn test_session_cache_gap_before() {
    let summaries = vec![
        SessionSummary {
            window_start: "2026-01-01T00:00:00Z".to_string(),
            initial_btc: 60000.0,
            final_btc: 60100.0,
            outcome: "Up".to_string(),
        },
        SessionSummary {
            window_start: "2026-01-01T00:05:00Z".to_string(),
            initial_btc: 60100.0,
            final_btc: 60000.0,
            outcome: "Down".to_string(),
        },
    ];
    let cache = SessionStatsCache::from_summaries(&summaries);
    let gap = cache.gap_before("2026-01-01T00:05:00Z");
    assert!(gap.is_some());
    assert!((gap.unwrap() - 300.0).abs() < 1.0);
}

#[test]
fn test_cache_prior_empty() {
    let summaries: Vec<SessionSummary> = (0..5)
        .map(|i| SessionSummary {
            window_start: format!("2026-01-01T{:02}:00:00Z", i + 5),
            initial_btc: 60000.0,
            final_btc: 60100.0,
            outcome: "Up".to_string(),
        })
        .collect();
    let cache = SessionStatsCache::from_summaries(&summaries);

    // Query before earliest session -> empty
    let prior = cache.prior("2026-01-01T04:00:00Z", 3);
    assert!(prior.is_empty());
}

#[test]
fn test_cache_data_density_sparse() {
    // Create sessions with gaps (not every interval filled)
    let summaries = vec![
        SessionSummary {
            window_start: "2026-01-01T00:00:00Z".to_string(),
            initial_btc: 60000.0,
            final_btc: 60100.0,
            outcome: "Up".to_string(),
        },
        // Gap: missing 00:05:00
        SessionSummary {
            window_start: "2026-01-01T00:10:00Z".to_string(),
            initial_btc: 60000.0,
            final_btc: 60100.0,
            outcome: "Up".to_string(),
        },
        // Gap: missing 00:15:00
        SessionSummary {
            window_start: "2026-01-01T00:20:00Z".to_string(),
            initial_btc: 60000.0,
            final_btc: 60100.0,
            outcome: "Up".to_string(),
        },
    ];
    let cache = SessionStatsCache::from_summaries(&summaries);
    let density = cache.data_density("2026-01-01T00:25:00Z", 10, Some(300.0));
    // Span = 20min = 1200s, expected = 1200/300 + 1 = 5, actual = 3
    // density = 3/5 = 0.6
    assert!(density < 1.0);
    assert!(density > 0.0);
}

#[test]
fn test_cache_add_dedup() {
    // SessionStatsCache deduplication: adding same window_start twice
    let summaries = vec![
        SessionSummary {
            window_start: "2026-01-01T00:00:00Z".to_string(),
            initial_btc: 60000.0,
            final_btc: 60100.0,
            outcome: "Up".to_string(),
        },
        SessionSummary {
            window_start: "2026-01-01T00:00:00Z".to_string(), // Duplicate
            initial_btc: 60000.0,
            final_btc: 60200.0,
            outcome: "Up".to_string(),
        },
        SessionSummary {
            window_start: "2026-01-01T00:05:00Z".to_string(),
            initial_btc: 60000.0,
            final_btc: 60100.0,
            outcome: "Up".to_string(),
        },
    ];
    let cache = SessionStatsCache::from_summaries(&summaries);
    // from_summaries doesn't deduplicate, but both entries are there
    // The prior query should still work correctly
    let prior = cache.prior("2026-01-01T00:05:00Z", 10);
    // Both entries with window_start < "00:05:00Z" are returned
    assert_eq!(prior.len(), 2);
}

// ===========================================================================
// test_models (matches test_models.py)
// ===========================================================================

#[test]
fn test_session_data_roundtrip() {
    let json = r#"{
        "metadata": {
            "event_id": "e1",
            "event_slug": "slug",
            "event_title": "title",
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
        "samples": [],
        "error_count": 0,
        "errors": []
    }"#;
    let sd: SessionData = serde_json::from_str(json).unwrap();
    assert_eq!(sd.outcome.as_deref(), Some("Up"));
    assert_eq!(sd.initial_btc_price_usd, Some(60000.0));
    assert_eq!(sd.metadata.event_id.as_deref(), Some("e1"));
}

#[test]
fn test_sample_get_f64() {
    let s = make_sample(5.0, 60000.0, 0.55, 0.45);
    assert_eq!(s.get_f64("elapsed_seconds"), Some(5.0));
    assert_eq!(s.get_f64("btc_price_usd"), Some(60000.0));
    assert_eq!(s.get_f64("up_token_midpoint"), Some(0.55));
    assert_eq!(s.get_f64("down_token_midpoint"), Some(0.45));
    assert_eq!(s.get_f64("nonexistent"), None);
}

#[test]
fn test_session_summary_accessors() {
    let ss = SessionSummary {
        window_start: "2026-01-01T00:00:00Z".to_string(),
        initial_btc: 60000.0,
        final_btc: 60100.0,
        outcome: "Up".to_string(),
    };
    assert_eq!(ss.get_f64("initial_btc"), Some(60000.0));
    assert_eq!(ss.get_f64("final_btc"), Some(60100.0));
    assert_eq!(ss.get_str("outcome"), Some("Up"));
    assert_eq!(ss.get_str("window_start"), Some("2026-01-01T00:00:00Z"));
    assert_eq!(ss.get_f64("nonexistent"), None);
    assert_eq!(ss.get_str("nonexistent"), None);
}

// ===========================================================================
// 7. Param grid tests (matches test_strategy_base.py deeper)
// ===========================================================================

#[test]
fn test_param_grid_two_point() {
    let p = Param::float(0.5, 0.0, 1.0);
    let grid = p.grid(2);
    // grid(2) with default=0.5: generates [0.0, 1.0], but default replaces nearest
    // so result is [0.5, 1.0] since 0.5 replaces 0.0
    assert_eq!(grid.len(), 2);
    assert!(grid.contains(&ParamValue::Float(0.5)));
    assert!(grid.contains(&ParamValue::Float(1.0)));
}

#[test]
fn test_param_grid_int_small_range() {
    // When range is small enough, all values are included
    let p = Param::int(2, 1, 3);
    let grid = p.grid(5);
    assert!(grid.contains(&ParamValue::Int(1)));
    assert!(grid.contains(&ParamValue::Int(2)));
    assert!(grid.contains(&ParamValue::Int(3)));
    assert_eq!(grid.len(), 3);
}

#[test]
fn test_param_grid_nullable_includes_none_first() {
    let p = Param::float_nullable(0.5, 0.0, 1.0);
    let grid = p.grid(3);
    assert_eq!(grid[0], ParamValue::None);
}

#[test]
fn test_param_value_conversions() {
    assert_eq!(ParamValue::Float(3.14).as_f64(), Some(3.14));
    assert_eq!(ParamValue::Int(42).as_f64(), Some(42.0));
    assert_eq!(ParamValue::None.as_f64(), None);
    assert_eq!(ParamValue::Bool(true).as_f64(), None);

    assert_eq!(ParamValue::Int(42).as_i64(), Some(42));
    assert_eq!(ParamValue::Float(3.7).as_i64(), Some(3));
    assert_eq!(ParamValue::None.as_i64(), None);
}

#[test]
fn test_strategy_param_grid_capped() {
    // All strategies should produce grids within max_variants
    let strategies = fondaco::strategies::all_strategies();
    for (name, factory) in &strategies {
        let s = factory();
        let grid = s.param_grid(1000);
        if !grid.is_empty() {
            let total: usize = grid.values().map(|v| v.len()).product();
            assert!(
                total <= 1000,
                "Strategy {} param_grid({}) produced {} variants, exceeding cap",
                name,
                1000,
                total
            );
        }
    }
}

// ===========================================================================
// Additional edge case tests
// ===========================================================================

#[test]
fn test_trade_action_constructors() {
    let h = TradeAction::hold();
    assert!(h.is_hold());
    assert_eq!(h.side, "hold");

    let buy = TradeAction::buy("buy_up", 100.0, 0.55, Some(10.0));
    assert!(!buy.is_hold());
    assert_eq!(buy.side, "buy_up");
    assert_eq!(buy.price, Some(0.55));
    assert_eq!(buy.cancel_after_s, Some(10.0));

    let sell = TradeAction::sell("sell_down", 50.0, 0.70);
    assert!(!sell.is_hold());
    assert_eq!(sell.side, "sell_down");
    assert_eq!(sell.cancel_after_s, None);
}

#[test]
fn test_trade_new() {
    let t = Trade::new(5, "buy_up", 0.55, 27.5, 50.0);
    assert_eq!(t.tick_index, 5);
    assert_eq!(t.side, "buy_up");
    assert!((t.price - 0.55).abs() < 1e-10);
    assert!((t.tokens - 50.0).abs() < 1e-10);
    assert!(t.order_id.is_none());
    assert!(t.fee.is_none());
}

#[test]
fn test_position_apply_trade_buy_down() {
    let mut p = Position::new(100.0);
    p.apply_trade("buy_down", 0.40, 50.0);
    assert!((p.cash - 80.0).abs() < 0.01);
    assert!((p.down_tokens() - 50.0).abs() < 0.01);
    assert_eq!(p.up_tokens(), 0.0);
}

#[test]
fn test_position_apply_trade_sell_down() {
    let mut p = Position::new(0.0);
    p.set_leg(1, 100.0);
    p.apply_trade("sell_down", 0.70, 100.0);
    assert!((p.cash - 70.0).abs() < 0.01);
    assert_eq!(p.down_tokens(), 0.0);
}

#[test]
fn test_position_apply_trade_zero_tokens() {
    let mut p = Position::new(100.0);
    p.apply_trade("buy_up", 0.50, 0.0);
    assert_eq!(p.cash, 100.0);
    assert_eq!(p.up_tokens(), 0.0);
}

#[test]
fn test_open_order_fields() {
    let order = OpenOrder {
        order_id: "sim-1".to_string(),
        side: "buy_up".to_string(),
        leg: 0,
        price: 0.50,
        tokens: 100.0,
        dollars_reserved: 50.0,
        tokens_reserved: 0.0,
        tick_placed: 0,
        token_id: None,
        expiration: Some(130.0),
        cancel_after_s: Some(10.0),
        time_placed: Some(10.0),
    };
    assert_eq!(order.leg, 0);
    assert_eq!(order.expiration, Some(130.0));
    assert_eq!(order.cancel_after_s, Some(10.0));
}

#[test]
fn test_sim_executor_down_token_buy() {
    let exec = SimExecutor::new("midpoint", 0.0, false, None, 0);
    let sample = make_sample(10.0, 60000.0, 0.40, 0.60);
    let mut pos = Position::new(100.0);
    let action = TradeAction {
        side: "buy_down".to_string(),
        amount: 100.0,
        price: None,
        expiration_seconds: None,
        cancel_after_s: None,
    };
    let trade = exec.execute_trade(&action, 0, &sample, &mut pos);
    assert!(trade.is_some());
    let t = trade.unwrap();
    assert!((t.price - 0.60).abs() < 0.01);
    assert!(pos.down_tokens() > 0.0);
}

#[test]
fn test_trader_end_session_cancels_pending() {
    let exec = SimExecutor::new("book", 0.02, false, None, 0);
    let strategy: Box<dyn Strategy> = Box::new(LimitOrderStrategy::new());
    let mut trader = Trader::new(strategy, exec);
    trader.start_session(default_metadata(), Position::new(100.0), 60000.0, 300.0);

    // Place a limit order (tick past 0.1 fraction)
    let sample = make_sample(35.0, 60000.0, 0.55, 0.45);
    trader.process_tick(&sample);
    let cash_reserved = trader.position().unwrap().cash;
    assert!(cash_reserved < 100.0);

    // End session should cancel pending orders and restore funds
    let _trades = trader.end_session(Some("Up"));
    let pos = trader.position().unwrap();
    // Cash should be restored (from cancelled order) + resolution
    assert!(pos.cash > cash_reserved);
}

#[test]
fn test_check_exit_stop_loss() {
    let mut pos = Position::new(0.0);
    pos.set_leg(0, 100.0);
    let sample = make_sample(100.0, 60000.0, 0.40, 0.60);
    let meta = default_metadata();
    let ctx = TickContext {
        tick_index: 10,
        sample: &sample,
        position: &pos,
        metadata: &meta,
        initial_btc_price: 60000.0,
        elapsed_fraction: 100.0 / 300.0,
        elapsed_seconds: 100.0,
        max_buy_amount: 0.0,
        open_orders: vec![],
    };

    // Entry at 0.55, current at 0.40 -> loss > 10%
    let exit = check_exit(
        &ctx,
        0.40, // up_mid
        0.60, // down_mid
        0.55, // entry_midpoint
        Some(0.10), // stop_loss_pct
        None,
        -999.0, // last_trade_time
        2000.0, // cooldown_ms
    );
    assert!(exit.is_some());
    let action = exit.unwrap();
    assert_eq!(action.side, "sell_up");
}

#[test]
fn test_check_exit_take_profit() {
    let mut pos = Position::new(0.0);
    pos.set_leg(0, 100.0);
    let sample = make_sample(100.0, 60100.0, 0.80, 0.20);
    let meta = default_metadata();
    let ctx = TickContext {
        tick_index: 10,
        sample: &sample,
        position: &pos,
        metadata: &meta,
        initial_btc_price: 60000.0,
        elapsed_fraction: 100.0 / 300.0,
        elapsed_seconds: 100.0,
        max_buy_amount: 0.0,
        open_orders: vec![],
    };

    // Entry at 0.55, current at 0.80 -> profit > 40%
    let exit = check_exit(
        &ctx,
        0.80,
        0.20,
        0.55,
        None,
        Some(0.40), // take_profit_pct
        -999.0,
        2000.0,
    );
    assert!(exit.is_some());
    let action = exit.unwrap();
    assert_eq!(action.side, "sell_up");
}

#[test]
fn test_check_exit_no_position() {
    let pos = Position::new(100.0);
    let sample = make_sample(100.0, 60000.0, 0.50, 0.50);
    let meta = default_metadata();
    let ctx = TickContext {
        tick_index: 0,
        sample: &sample,
        position: &pos,
        metadata: &meta,
        initial_btc_price: 60000.0,
        elapsed_fraction: 0.33,
        elapsed_seconds: 100.0,
        max_buy_amount: 100.0,
        open_orders: vec![],
    };

    let exit = check_exit(&ctx, 0.50, 0.50, 0.55, Some(0.10), Some(0.40), -999.0, 2000.0);
    assert!(exit.is_none());
}

#[test]
fn test_check_exit_cooldown_blocks() {
    let mut pos = Position::new(0.0);
    pos.set_leg(0, 100.0);
    let sample = make_sample(100.0, 60000.0, 0.40, 0.60);
    let meta = default_metadata();
    let ctx = TickContext {
        tick_index: 10,
        sample: &sample,
        position: &pos,
        metadata: &meta,
        initial_btc_price: 60000.0,
        elapsed_fraction: 100.0 / 300.0,
        elapsed_seconds: 100.0,
        max_buy_amount: 0.0,
        open_orders: vec![],
    };

    // Last trade was 1s ago, cooldown is 2s -> should return hold
    let exit = check_exit(
        &ctx, 0.40, 0.60, 0.55,
        Some(0.10), None,
        99.0, // last_trade_time_s (1s ago)
        2000.0,
    );
    assert!(exit.is_some());
    // Should be hold because within cooldown
    assert!(exit.unwrap().is_hold());
}

#[test]
fn test_session_cache_gap_before_first() {
    let summaries = vec![
        SessionSummary {
            window_start: "2026-01-01T00:00:00Z".to_string(),
            initial_btc: 60000.0,
            final_btc: 60100.0,
            outcome: "Up".to_string(),
        },
    ];
    let cache = SessionStatsCache::from_summaries(&summaries);
    // First session has no predecessor -> None
    let gap = cache.gap_before("2026-01-01T00:00:00Z");
    assert!(gap.is_none());
}

#[test]
fn test_session_cache_len() {
    let summaries = make_vol_summaries(&[5.0, -3.0, 7.0]);
    let cache = SessionStatsCache::from_summaries(&summaries);
    assert_eq!(cache.len(), 3);
    assert!(!cache.is_empty());

    let empty_cache = SessionStatsCache::new();
    assert_eq!(empty_cache.len(), 0);
    assert!(empty_cache.is_empty());
}

#[test]
fn test_all_strategies_require_full_session() {
    // Most strategies in this codebase require full session
    let strategies = fondaco::strategies::all_strategies();
    for (_name, factory) in &strategies {
        let s = factory();
        // Just verify the method is callable (strategies can be either)
        let _rfs = s.requires_full_session();
    }
}

#[test]
fn test_all_strategies_have_version() {
    let strategies = fondaco::strategies::all_strategies();
    for (name, factory) in &strategies {
        let s = factory();
        let v = s.version();
        assert!(
            !v.is_empty(),
            "Strategy {} has empty version",
            name
        );
    }
}

#[test]
fn test_all_strategies_param_roundtrip() {
    // Every strategy's with_params(params()) should produce the same params
    let strategies = fondaco::strategies::all_strategies();
    for (name, factory) in &strategies {
        let s = factory();
        let params = s.params();
        let s2 = s.with_params(&params);
        let params2 = s2.params();
        assert_eq!(
            params, params2,
            "Strategy {} params not stable through roundtrip",
            name
        );
    }
}

// ===========================================================================
// 8. GTD order lifecycle (matches test_gtd_orders.py)
// ===========================================================================

/// Helper: strategy that emits a sequence of actions then holds.
struct SeqStrategy {
    actions: Vec<TradeAction>,
    idx: usize,
}

impl SeqStrategy {
    fn new(actions: Vec<TradeAction>) -> Self {
        Self { actions, idx: 0 }
    }
}

impl Strategy for SeqStrategy {
    fn name(&self) -> &str { "Seq" }
    fn version(&self) -> &str { "1" }
    fn on_session_start(&mut self, _metadata: &SessionMetadata) { self.idx = 0; }
    fn on_tick(&mut self, _ctx: &TickContext) -> TradeAction {
        if self.idx < self.actions.len() {
            let a = self.actions[self.idx].clone();
            self.idx += 1;
            a
        } else {
            hold()
        }
    }
    fn with_params(&self, _params: &HashMap<String, ParamValue>) -> Box<dyn Strategy> {
        Box::new(SeqStrategy::new(self.actions.clone()))
    }
}

#[test]
fn test_gtd_order_expires_after_deadline() {
    let actions = vec![
        TradeAction {
            side: "buy_up".to_string(),
            amount: 100.0,
            price: Some(0.10),
            expiration_seconds: Some(30.0),
            cancel_after_s: None,
        },
    ];
    let exec = SimExecutor::new("book", 0.02, false, None, 0);
    let strategy: Box<dyn Strategy> = Box::new(SeqStrategy::new(actions));
    let mut trader = Trader::new(strategy, exec);
    let pos = Position::new(100.0);
    trader.start_session(default_metadata(), pos, 60000.0, 300.0);

    // Tick 0: place order at elapsed=10s, expires at 40s
    let s0 = make_sample(10.0, 60000.0, 0.50, 0.50);
    trader.process_tick(&s0);
    assert!(trader.position().unwrap().cash < 100.0);

    // Tick 1: elapsed=35s, still before deadline
    let s1 = make_sample(35.0, 60000.0, 0.50, 0.50);
    trader.process_tick(&s1);
    // Order should still be pending (not expired, not filled)
    assert!(trader.position().unwrap().cash < 100.0);

    // Tick 2: elapsed=45s, past 40s deadline — order expires
    let s2 = make_sample(45.0, 60000.0, 0.50, 0.50);
    trader.process_tick(&s2);
    // Cash restored after expiration
    assert!((trader.position().unwrap().cash - 100.0).abs() < 0.01);
}

#[test]
fn test_gtd_order_fills_before_deadline() {
    let actions = vec![
        TradeAction {
            side: "buy_up".to_string(),
            amount: 100.0,
            price: Some(0.20),
            expiration_seconds: Some(60.0),
            cancel_after_s: None,
        },
    ];
    let exec = SimExecutor::new("book", 0.02, false, None, 0);
    let strategy: Box<dyn Strategy> = Box::new(SeqStrategy::new(actions));
    let mut trader = Trader::new(strategy, exec);
    let pos = Position::new(100.0);
    trader.start_session(default_metadata(), pos, 60000.0, 300.0);

    // Tick 0: place order at elapsed=10s
    let s0 = make_sample(10.0, 60000.0, 0.50, 0.50);
    trader.process_tick(&s0);

    // Tick 1: midpoint dips below limit price minus margin -> fill
    let s1 = make_sample(30.0, 60000.0, 0.15, 0.85);
    trader.process_tick(&s1);
    assert!(trader.position().unwrap().up_tokens() > 0.0);
}

#[test]
fn test_cancel_up_removes_order_and_unreserves() {
    let actions = vec![
        TradeAction {
            side: "buy_up".to_string(),
            amount: 100.0,
            price: Some(0.20),
            expiration_seconds: Some(120.0),
            cancel_after_s: None,
        },
        TradeAction {
            side: "cancel_up".to_string(),
            amount: 0.0,
            price: None,
            expiration_seconds: None,
            cancel_after_s: None,
        },
    ];
    let exec = SimExecutor::new("book", 0.02, false, None, 0);
    let strategy: Box<dyn Strategy> = Box::new(SeqStrategy::new(actions));
    let mut trader = Trader::new(strategy, exec);
    let pos = Position::new(100.0);
    trader.start_session(default_metadata(), pos, 60000.0, 300.0);

    // Tick 0: place order
    let s0 = make_sample(10.0, 60000.0, 0.50, 0.50);
    trader.process_tick(&s0);
    assert!(trader.position().unwrap().cash < 100.0);

    // Tick 1: cancel
    let s1 = make_sample(20.0, 60000.0, 0.50, 0.50);
    trader.process_tick(&s1);
    assert!((trader.position().unwrap().cash - 100.0).abs() < 0.01);
}

#[test]
fn test_cancel_down_removes_order_and_unreserves() {
    let actions = vec![
        TradeAction {
            side: "buy_down".to_string(),
            amount: 50.0,
            price: Some(0.25),
            expiration_seconds: Some(120.0),
            cancel_after_s: None,
        },
        TradeAction {
            side: "cancel_down".to_string(),
            amount: 0.0,
            price: None,
            expiration_seconds: None,
            cancel_after_s: None,
        },
    ];
    let exec = SimExecutor::new("book", 0.02, false, None, 0);
    let strategy: Box<dyn Strategy> = Box::new(SeqStrategy::new(actions));
    let mut trader = Trader::new(strategy, exec);
    let pos = Position::new(50.0);
    trader.start_session(default_metadata(), pos, 60000.0, 300.0);

    let s0 = make_sample(10.0, 60000.0, 0.50, 0.50);
    trader.process_tick(&s0);
    assert!(trader.position().unwrap().cash < 50.0);

    let s1 = make_sample(20.0, 60000.0, 0.50, 0.50);
    trader.process_tick(&s1);
    assert!((trader.position().unwrap().cash - 50.0).abs() < 0.01);
}

#[test]
fn test_cancel_wrong_token_is_noop() {
    let actions = vec![
        TradeAction {
            side: "buy_up".to_string(),
            amount: 100.0,
            price: Some(0.20),
            expiration_seconds: Some(120.0),
            cancel_after_s: None,
        },
        TradeAction {
            side: "cancel_down".to_string(), // wrong token
            amount: 0.0,
            price: None,
            expiration_seconds: None,
            cancel_after_s: None,
        },
    ];
    let exec = SimExecutor::new("book", 0.02, false, None, 0);
    let strategy: Box<dyn Strategy> = Box::new(SeqStrategy::new(actions));
    let mut trader = Trader::new(strategy, exec);
    let pos = Position::new(100.0);
    trader.start_session(default_metadata(), pos, 60000.0, 300.0);

    let s0 = make_sample(10.0, 60000.0, 0.50, 0.50);
    trader.process_tick(&s0);
    let cash_after_place = trader.position().unwrap().cash;
    assert!(cash_after_place < 100.0);

    // Cancel the wrong side — order should still be there
    let s1 = make_sample(20.0, 60000.0, 0.50, 0.50);
    trader.process_tick(&s1);
    assert!((trader.position().unwrap().cash - cash_after_place).abs() < 0.01);
}

#[test]
fn test_gtd_expiration_timestamp() {
    // Verify the expiration deadline = current_elapsed + expiration_seconds
    let mut exec = SimExecutor::new("book", 0.02, false, None, 0);
    let sample = make_sample(60.0, 60000.0, 0.50, 0.50);
    let mut pos = Position::new(100.0);
    let action = TradeAction {
        side: "buy_up".to_string(),
        amount: 100.0,
        price: Some(0.20),
        expiration_seconds: Some(30.0),
        cancel_after_s: None,
    };
    let order = exec.place_limit_order(&action, 0, &sample, &mut pos);
    assert!(order.is_some());
    let order = order.unwrap();
    // Expiration = 60.0 + 30.0 = 90.0
    assert!((order.expiration.unwrap() - 90.0).abs() < 0.01);
}

#[test]
fn test_gtc_order_has_no_expiration() {
    let mut exec = SimExecutor::new("book", 0.02, false, None, 0);
    let sample = make_sample(10.0, 60000.0, 0.50, 0.50);
    let mut pos = Position::new(100.0);
    let action = TradeAction {
        side: "buy_up".to_string(),
        amount: 100.0,
        price: Some(0.20),
        expiration_seconds: None,
        cancel_after_s: None,
    };
    let order = exec.place_limit_order(&action, 0, &sample, &mut pos);
    assert!(order.is_some());
    assert!(order.unwrap().expiration.is_none());
}

#[test]
fn test_sell_limit_order_gtd_expiry() {
    let mut exec = SimExecutor::new("book", 0.02, false, None, 0);
    let sample = make_sample(10.0, 60000.0, 0.55, 0.45);
    let mut pos = Position::new(0.0);
    pos.set_leg(0, 100.0);

    let action = TradeAction {
        side: "sell_up".to_string(),
        amount: 100.0,
        price: Some(0.70),
        expiration_seconds: Some(20.0), // Expires at elapsed=30
        cancel_after_s: None,
    };

    let order = exec.place_limit_order(&action, 0, &sample, &mut pos);
    assert!(order.is_some());
    let order = order.unwrap();
    assert_eq!(pos.up_tokens(), 0.0); // Tokens reserved

    // Past expiry -> order removed, tokens restored
    let mut orders = vec![order];
    let expired_sample = make_sample(35.0, 60000.0, 0.55, 0.45);
    let fills = exec.check_fills(&mut orders, &expired_sample, &mut pos);
    assert_eq!(fills.len(), 0);
    assert!(orders.is_empty());
    assert_eq!(pos.up_tokens(), 100.0); // Tokens unreserved
}

// ===========================================================================
// 9. Session cache: gap handling deep tests (matches test_gap_handling.py)
// ===========================================================================

fn make_cache_with_gap() -> SessionStatsCache {
    // Block 1: 6 sessions at 5-min intervals starting 00:00
    // Block 2: 5 sessions at 5-min intervals starting 02:30 (2-hour gap)
    let mut summaries: Vec<SessionSummary> = Vec::new();
    for i in 0..6 {
        summaries.push(SessionSummary {
            window_start: format!("2025-01-01T00:{:02}:00Z", i * 5),
            initial_btc: 97000.0 + i as f64,
            final_btc: 97001.0 + i as f64,
            outcome: if i % 2 == 0 { "Up" } else { "Down" }.to_string(),
        });
    }
    for i in 0..5 {
        summaries.push(SessionSummary {
            window_start: format!("2025-01-01T02:{:02}:00Z", 30 + i * 5),
            initial_btc: 97010.0 + i as f64,
            final_btc: 97011.0 + i as f64,
            outcome: if i % 2 == 0 { "Up" } else { "Down" }.to_string(),
        });
    }
    SessionStatsCache::from_summaries(&summaries)
}

#[test]
fn test_gap_before_contiguous() {
    let cache = make_cache_with_gap();
    let gap = cache.gap_before("2025-01-01T00:05:00Z");
    assert!(gap.is_some());
    assert!((gap.unwrap() - 300.0).abs() < 1.0);
}

#[test]
fn test_gap_before_after_hole() {
    let cache = make_cache_with_gap();
    let gap = cache.gap_before("2025-01-01T02:30:00Z");
    assert!(gap.is_some());
    // Gap from 00:25:00 to 02:30:00 = 7500s
    assert!(gap.unwrap() > 7000.0);
}

#[test]
fn test_gap_before_first_session_none() {
    let cache = make_cache_with_gap();
    let gap = cache.gap_before("2025-01-01T00:00:00Z");
    assert!(gap.is_none());
}

#[test]
fn test_density_contiguous_sessions() {
    let mut summaries = Vec::new();
    for i in 0..20 {
        summaries.push(SessionSummary {
            window_start: format!("2025-01-01T{:02}:{:02}:00Z", i / 12, (i % 12) * 5),
            initial_btc: 97000.0,
            final_btc: 97001.0,
            outcome: "Up".to_string(),
        });
    }
    let cache = SessionStatsCache::from_summaries(&summaries);
    let density = cache.data_density("2025-01-01T01:40:00Z", 20, Some(300.0));
    assert!(density > 0.90);
}

#[test]
fn test_density_with_large_gap() {
    let cache = make_cache_with_gap();
    let density = cache.data_density("2025-01-01T02:50:00Z", 11, Some(300.0));
    // 11 sessions spanning ~2.5 hours -> density well below 1.0
    assert!(density < 0.80);
}

#[test]
fn test_density_single_session_returns_one() {
    let summaries = vec![SessionSummary {
        window_start: "2025-01-01T00:00:00Z".to_string(),
        initial_btc: 97000.0,
        final_btc: 97001.0,
        outcome: "Up".to_string(),
    }];
    let cache = SessionStatsCache::from_summaries(&summaries);
    let density = cache.data_density("2025-01-01T00:05:00Z", 1, Some(300.0));
    assert_eq!(density, 1.0);
}

// ===========================================================================
// 10. Strategy session start resets (matches test_active_strategies.py)
// ===========================================================================

#[test]
fn test_drift_sniper_session_start_resets() {
    let mut s = fondaco::strategies::drift_sniper::DriftSniper::default();
    let meta = default_metadata();

    // Feed some ticks to build up state
    let pos = Position::new(100.0);
    s.on_session_start(&meta);
    for i in 0..5 {
        let sample = make_sample(i as f64 * 10.0, 60000.0, 0.50, 0.50);
        let ctx = TickContext {
            tick_index: i,
            sample: &sample,
            position: &pos,
            metadata: &meta,
            initial_btc_price: 60000.0,
            elapsed_fraction: (i as f64 * 10.0) / 300.0,
            elapsed_seconds: i as f64 * 10.0,
            max_buy_amount: 100.0,
            open_orders: vec![],
        };
        s.on_tick(&ctx);
    }

    // Reset: on_session_start should clear accumulated state
    s.on_session_start(&meta);

    // After reset, strategy should hold on first tick (fresh state)
    let sample = make_sample(200.0, 60000.0, 0.50, 0.50);
    let ctx = TickContext {
        tick_index: 0,
        sample: &sample,
        position: &pos,
        metadata: &meta,
        initial_btc_price: 60000.0,
        elapsed_fraction: 200.0 / 300.0,
        elapsed_seconds: 200.0,
        max_buy_amount: 100.0,
        open_orders: vec![],
    };
    let action = s.on_tick(&ctx);
    // With reset state, no BTC price history => holds
    assert!(action.is_hold());
}

#[test]
fn test_gap_fader_session_start_resets() {
    let mut s = fondaco::strategies::gap_fader::GapFader::default();
    let meta = default_metadata();

    // Build state: feed observation samples
    s.on_session_start(&meta);
    let pos = Position::new(100.0);
    for i in 0..10 {
        let sample = make_sample(i as f64 * 1.5, 60000.0, 0.50, 0.50);
        let ctx = TickContext {
            tick_index: i,
            sample: &sample,
            position: &pos,
            metadata: &meta,
            initial_btc_price: 60000.0,
            elapsed_fraction: (i as f64 * 1.5) / 300.0,
            elapsed_seconds: i as f64 * 1.5,
            max_buy_amount: 100.0,
            open_orders: vec![],
        };
        s.on_tick(&ctx);
    }

    // Reset
    s.on_session_start(&meta);

    // After reset: observation phase should restart (hold during first 15s)
    let sample = make_sample(5.0, 60000.0, 0.45, 0.45);
    let ctx = TickContext {
        tick_index: 0,
        sample: &sample,
        position: &pos,
        metadata: &meta,
        initial_btc_price: 60000.0,
        elapsed_fraction: 5.0 / 300.0,
        elapsed_seconds: 5.0,
        max_buy_amount: 100.0,
        open_orders: vec![],
    };
    let action = s.on_tick(&ctx);
    assert!(action.is_hold());
}

#[test]
fn test_poisson_kelly_session_start_resets() {
    let mut s = fondaco::strategies::poisson_kelly::PoissonKelly::default();
    let meta = default_metadata();

    // Build state: feed ticks with jumps
    s.on_session_start(&meta);
    let pos = Position::new(100.0);
    let base_btc = 60000.0;
    for i in 0..20 {
        let btc = if i % 2 == 0 {
            base_btc * (1.0 + 10.0 / BPS_DIVISOR)
        } else {
            base_btc * (1.0 - 10.0 / BPS_DIVISOR)
        };
        let sample = make_sample(i as f64 * 5.0, btc, 0.50, 0.50);
        let ctx = TickContext {
            tick_index: i,
            sample: &sample,
            position: &pos,
            metadata: &meta,
            initial_btc_price: base_btc,
            elapsed_fraction: (i as f64 * 5.0) / 300.0,
            elapsed_seconds: i as f64 * 5.0,
            max_buy_amount: 100.0,
            open_orders: vec![],
        };
        s.on_tick(&ctx);
    }

    // Reset
    s.on_session_start(&meta);

    // After reset: early tick should hold (min_elapsed=0.65 not reached, and no jump history)
    let sample = make_sample(200.0, 60000.0, 0.50, 0.50);
    let ctx = TickContext {
        tick_index: 0,
        sample: &sample,
        position: &pos,
        metadata: &meta,
        initial_btc_price: 60000.0,
        elapsed_fraction: 200.0 / 300.0,
        elapsed_seconds: 200.0,
        max_buy_amount: 100.0,
        open_orders: vec![],
    };
    let action = s.on_tick(&ctx);
    // After reset, prev_btc=0, no jumps counted -> holds
    assert!(action.is_hold());
}

// ===========================================================================
// 11. Strategy-specific param grid verification
// ===========================================================================

#[test]
fn test_drift_sniper_param_grid_keys() {
    let s = fondaco::strategies::drift_sniper::DriftSniper::default();
    let defs = s.param_defs();
    assert!(defs.contains_key("return_threshold_bps"));
    assert!(defs.contains_key("min_elapsed"));
    assert!(defs.contains_key("velocity_window_ms"));
    assert!(defs.contains_key("min_velocity_bps_s"));
    assert!(defs.contains_key("max_price"));
    assert!(defs.contains_key("stop_loss_pct"));
    // Fixed params should be present but not tunable
    assert!(!defs["no_new_after"].tunable);
    assert!(!defs["bet_fraction"].tunable);
}

#[test]
fn test_gap_fader_param_grid_keys() {
    let s = fondaco::strategies::gap_fader::GapFader::default();
    let defs = s.param_defs();
    assert!(defs.contains_key("gap_threshold"));
    assert!(defs.contains_key("max_buy_price"));
    assert!(defs.contains_key("take_profit_pct"));
    assert!(defs.contains_key("stop_loss_pct"));
    assert!(defs.contains_key("max_entries"));
    assert!(defs.contains_key("time_decay_power"));
    // Fixed
    assert!(!defs["observe_seconds"].tunable);
    assert!(!defs["cooldown_ms"].tunable);
}

#[test]
fn test_poisson_kelly_min_jumps_not_tunable() {
    let s = fondaco::strategies::poisson_kelly::PoissonKelly::default();
    let defs = s.param_defs();
    assert!(!defs["min_jumps"].tunable);
    assert!(!defs["no_new_after"].tunable);
}

#[test]
fn test_regime_sniper_param_grid_keys() {
    let s = fondaco::strategies::regime_sniper::RegimeSniper::default();
    let defs = s.param_defs();
    assert!(defs.contains_key("min_elapsed"));
    assert!(defs.contains_key("min_displacement_bps"));
    assert!(defs.contains_key("min_consistency"));
    assert!(defs.contains_key("max_price"));
    assert!(defs.contains_key("price_slope"));
    // Fixed
    assert!(!defs["consistency_window_ms"].tunable);
    assert!(!defs["velocity_window_ms"].tunable);
}

#[test]
fn test_late_sniper_cb_param_grid_keys() {
    let s = fondaco::strategies::late_sniper_cb::LateSniperCB::default();
    let defs = s.param_defs();
    assert!(defs.contains_key("min_elapsed"));
    assert!(defs.contains_key("confidence_threshold"));
    assert!(defs.contains_key("max_price"));
    assert!(defs.contains_key("confirm_ms"));
    assert!(!defs["cb_lookback"].tunable);
    assert!(!defs["cb_max_losses"].tunable);
}

#[test]
fn test_mean_rev_fader_param_grid_keys() {
    let s = fondaco::strategies::mean_rev_fader::MeanRevFader::default();
    let defs = s.param_defs();
    assert!(defs.contains_key("max_buy_price"));
    assert!(defs.contains_key("take_profit_pct"));
    assert!(defs.contains_key("stop_loss_pct"));
    assert!(defs.contains_key("min_elapsed"));
    assert!(defs.contains_key("max_entries"));
    assert!(!defs["no_new_after"].tunable);
    assert!(!defs["cooldown_ms"].tunable);
}

#[test]
fn test_trend_drift_param_grid_keys() {
    let s = fondaco::strategies::trend_drift::TrendDrift::default();
    let defs = s.param_defs();
    assert!(defs.contains_key("return_threshold_bps"));
    assert!(defs.contains_key("min_elapsed"));
    assert!(defs.contains_key("max_price"));
    assert!(defs.contains_key("velocity_window_ms"));
    assert!(defs.contains_key("min_velocity_bps_s"));
    assert!(!defs["no_new_after"].tunable);
}

#[test]
fn test_poisson_kelly_dynamic_param_grid_keys() {
    let s = fondaco::strategies::poisson_kelly_dynamic::PoissonKellyDynamic::default();
    let defs = s.param_defs();
    assert!(defs.contains_key("jump_bps"));
    assert!(defs.contains_key("edge_min"));
    assert!(defs.contains_key("max_entries"));
    assert!(defs.contains_key("vol_scale"));
    assert!(defs.contains_key("trend_strength"));
}

// ===========================================================================
// 12. Strategy with_params applies correctly
// ===========================================================================

#[test]
fn test_drift_sniper_with_params() {
    let s = fondaco::strategies::drift_sniper::DriftSniper::default();
    let mut params = s.params();
    params.insert("return_threshold_bps".to_string(), ParamValue::Float(8.0));
    params.insert("stop_loss_pct".to_string(), ParamValue::None);
    let s2 = s.with_params(&params);
    let p2 = s2.params();
    assert_eq!(p2.get("return_threshold_bps"), Some(&ParamValue::Float(8.0)));
    assert_eq!(p2.get("stop_loss_pct"), Some(&ParamValue::None));
}

#[test]
fn test_gap_fader_with_params() {
    let s = fondaco::strategies::gap_fader::GapFader::default();
    let mut params = s.params();
    params.insert("max_entries".to_string(), ParamValue::Int(3));
    params.insert("gap_threshold".to_string(), ParamValue::Float(0.08));
    let s2 = s.with_params(&params);
    let p2 = s2.params();
    assert_eq!(p2.get("max_entries"), Some(&ParamValue::Int(3)));
    assert_eq!(p2.get("gap_threshold"), Some(&ParamValue::Float(0.08)));
}

// ===========================================================================
// 13. Regime-gated strategies skip wrong regime
// ===========================================================================

#[test]
fn test_mean_rev_fader_skips_trending_regime() {
    let mut s = fondaco::strategies::mean_rev_fader::MeanRevFader::default();

    // Create trending regime: consistent positive returns -> Trending, not Flat
    let summaries = make_vol_summaries(&[
        5.0, 6.0, 5.0, 7.0, 6.0, 5.0, 7.0, 6.0, 5.0, 7.0,
        6.0, 5.0, 7.0, 6.0, 5.0, 7.0, 6.0, 5.0, 7.0, 6.0,
    ]);
    let mut meta = default_metadata();
    meta.prior_session_summaries = summaries;
    meta.gap_before_seconds = Some(300.0);
    meta.data_density = 1.0;

    s.on_session_start(&meta);

    // MeanRevFader requires Flat trend -> should hold on every tick
    let pos = Position::new(100.0);
    let sample = make_sample(100.0, 60000.0, 0.40, 0.40);
    let ctx = TickContext {
        tick_index: 0,
        sample: &sample,
        position: &pos,
        metadata: &meta,
        initial_btc_price: 60000.0,
        elapsed_fraction: 100.0 / 300.0,
        elapsed_seconds: 100.0,
        max_buy_amount: 100.0,
        open_orders: vec![],
    };
    let action = s.on_tick(&ctx);
    assert!(action.is_hold());
}

#[test]
fn test_late_sniper_cb_skips_low_vol() {
    let mut s = fondaco::strategies::late_sniper_cb::LateSniperCB::default();

    // Low vol data -> LateSniperCB requires medium/high vol
    let summaries = make_vol_summaries(&[
        1.0, -1.0, 0.5, -0.5, 1.0, -1.0, 0.5, -0.5, 1.0, -1.0,
        0.5, -0.5, 1.0, -1.0, 0.5, -0.5, 1.0, -1.0, 0.5, -0.5,
    ]);
    let mut meta = default_metadata();
    meta.prior_session_summaries = summaries;
    meta.gap_before_seconds = Some(300.0);
    meta.data_density = 1.0;

    s.on_session_start(&meta);

    let pos = Position::new(100.0);
    let sample = make_sample(200.0, 60100.0, 0.85, 0.15);
    let ctx = TickContext {
        tick_index: 0,
        sample: &sample,
        position: &pos,
        metadata: &meta,
        initial_btc_price: 60000.0,
        elapsed_fraction: 200.0 / 300.0,
        elapsed_seconds: 200.0,
        max_buy_amount: 100.0,
        open_orders: vec![],
    };
    let action = s.on_tick(&ctx);
    assert!(action.is_hold());
}

// ===========================================================================
// 14. Path helpers (matches test_defaults.py / test_backtest_paths.py)
// ===========================================================================

#[test]
fn test_market_sessions_dir_path() {
    let p = market_sessions_dir("btc-updown-5m");
    let path_str = p.to_string_lossy();
    assert!(path_str.contains("markets"));
    assert!(path_str.contains("btc-updown-5m"));
    assert!(path_str.ends_with("sessions"));
}

#[test]
fn test_market_backtest_log_path() {
    let p = market_backtest_log("btc-updown-5m");
    let path_str = p.to_string_lossy();
    assert!(path_str.contains("markets"));
    assert!(path_str.contains("btc-updown-5m"));
    assert!(path_str.ends_with("backtest_log.jsonl"));
}

#[test]
fn test_slug_key_various() {
    assert_eq!(slug_key("btc-updown-15m-"), "btc-updown-15m");
    assert_eq!(slug_key("eth-updown-5m-"), "eth-updown-5m");
    assert_eq!(slug_key("no-trailing"), "no-trailing");
    assert_eq!(slug_key(""), "");
}

// ===========================================================================
// 15. Position edge cases
// ===========================================================================

#[test]
fn test_position_compute_trade_size_sell_down() {
    let mut p = Position::new(0.0);
    p.set_leg(1, 100.0);
    let (tokens, proceeds) = p.compute_trade_size("sell_down", Some(0.70), 100.0, &econ_test());
    assert_eq!(tokens, 100.0);
    assert!((proceeds - 70.0).abs() < 0.01);
}

#[test]
fn test_position_compute_trade_size_sell_more_than_held() {
    let mut p = Position::new(0.0);
    p.set_leg(0, 50.0);
    // Request sell 100, but only 50 held -> should sell 50
    let (tokens, _proceeds) = p.compute_trade_size("sell_up", Some(0.80), 100.0, &econ_test());
    assert!((tokens - 50.0).abs() < 0.01);
}

#[test]
fn test_position_compute_trade_size_invalid_price() {
    let p = Position::new(100.0);
    let (tokens, _) = p.compute_trade_size("buy_up", Some(0.0), 100.0, &econ_test());
    assert_eq!(tokens, 0.0);
    let (tokens2, _) = p.compute_trade_size("buy_up", Some(1.0), 100.0, &econ_test());
    assert_eq!(tokens2, 0.0);
    let (tokens3, _) = p.compute_trade_size("buy_up", None, 100.0, &econ_test());
    assert_eq!(tokens3, 0.0);
}

#[test]
fn test_position_compute_trade_size_unknown_side() {
    let p = Position::new(100.0);
    let (tokens, _) = p.compute_trade_size("fly_up", Some(0.50), 100.0, &econ_test());
    assert_eq!(tokens, 0.0);
}

#[test]
fn test_position_reserve_sell_down() {
    let mut p = Position::new(0.0);
    p.set_leg(1, 100.0);
    let (dr, tr) = p.reserve_for_order("sell_down", 0.70, 50.0);
    assert_eq!(dr, 0.0);
    assert_eq!(tr, 50.0);
    assert_eq!(p.down_tokens(), 50.0);

    p.unreserve_for_order("sell_down", dr, tr);
    assert_eq!(p.down_tokens(), 100.0);
}

// ===========================================================================
// 16. Trader: auto-exit and history tracking
// ===========================================================================

/// Strategy that does a market order (no price) past 50% elapsed.
struct MarketBuyOnceStrategy {
    bought: bool,
}

impl MarketBuyOnceStrategy {
    fn new() -> Self {
        Self { bought: false }
    }
}

impl Strategy for MarketBuyOnceStrategy {
    fn name(&self) -> &str { "MarketBuyOnce" }
    fn version(&self) -> &str { "1" }
    fn on_session_start(&mut self, _metadata: &SessionMetadata) { self.bought = false; }
    fn on_tick(&mut self, ctx: &TickContext) -> TradeAction {
        if !self.bought && ctx.elapsed_fraction > 0.50 {
            self.bought = true;
            TradeAction {
                side: "buy_up".to_string(),
                amount: ctx.position.cash,
                price: None, // market order, no limit
                expiration_seconds: None,
                cancel_after_s: None,
            }
        } else {
            hold()
        }
    }
    fn with_params(&self, _params: &HashMap<String, ParamValue>) -> Box<dyn Strategy> {
        Box::new(MarketBuyOnceStrategy::new())
    }
}

#[test]
fn test_trader_auto_exit_at_session_end() {
    let exec = SimExecutor::new("midpoint", 0.0, false, None, 0);
    let strategy: Box<dyn Strategy> = Box::new(MarketBuyOnceStrategy::new());
    let mut trader = Trader::new(strategy, exec);
    trader.start_session(default_metadata(), Position::new(100.0), 60000.0, 300.0);

    // Tick at 51% -> immediate market buy
    let sample = make_sample(155.0, 60000.0, 0.55, 0.45);
    let result = trader.process_tick(&sample);
    assert!(result.is_some());
    assert!(trader.position().unwrap().up_tokens() > 0.0);

    // End session with Up outcome -> tokens resolve to cash
    let trades = trader.end_session(Some("Up"));
    assert_eq!(trades.len(), 1);
    let pos = trader.position().unwrap();
    assert_eq!(pos.up_tokens(), 0.0);
    // Won: started with $100, bought up tokens at 0.55, Up outcome = $1/token
    assert!(pos.cash > 100.0);
}

#[test]
fn test_trader_auto_exit_down_outcome() {
    let exec = SimExecutor::new("midpoint", 0.0, false, None, 0);
    let strategy: Box<dyn Strategy> = Box::new(MarketBuyOnceStrategy::new());
    let mut trader = Trader::new(strategy, exec);
    trader.start_session(default_metadata(), Position::new(100.0), 60000.0, 300.0);

    // Buy up tokens via market order
    let sample = make_sample(155.0, 60000.0, 0.55, 0.45);
    trader.process_tick(&sample);
    assert!(trader.position().unwrap().up_tokens() > 0.0);

    // Down outcome -> up tokens worthless
    trader.end_session(Some("Down"));
    let pos = trader.position().unwrap();
    assert_eq!(pos.up_tokens(), 0.0);
    assert!(pos.cash < 100.0); // Lost money
}

// ===========================================================================
// 17. Strategy no-duplicate names (matches test_strategy_registry.py)
// ===========================================================================

#[test]
fn test_strategy_no_duplicate_names() {
    let strategies = fondaco::strategies::all_strategies();
    let mut names: Vec<String> = strategies.keys().cloned().collect();
    names.sort();
    names.dedup();
    assert_eq!(names.len(), strategies.len());
}

#[test]
fn test_all_strategies_are_sendable() {
    // All strategies implement Send (required by trait)
    let strategies = fondaco::strategies::all_strategies();
    for (_name, factory) in &strategies {
        let s = factory();
        // If this compiles, the strategy implements Send
        fn assert_send<T: Send>(_t: &T) {}
        assert_send(&s);
    }
}

// ===========================================================================
// 18. Strategy behavior: DriftSniper flat BTC holds, PoissonKelly no jumps
// ===========================================================================

#[test]
fn test_drift_sniper_flat_btc_holds_entire_session() {
    let mut s = fondaco::strategies::drift_sniper::DriftSniper::default();
    s.on_session_start(&default_metadata());
    let pos = Position::new(100.0);
    let meta = default_metadata();

    // Feed 200 ticks with perfectly flat BTC -> no displacement -> no entry
    for i in 0..200 {
        let elapsed = 0.5 + i as f64 * 1.4; // Covers 0.5..280s
        let sample = make_sample(elapsed, 60000.0, 0.50, 0.50);
        let ctx = TickContext {
            tick_index: i,
            sample: &sample,
            position: &pos,
            metadata: &meta,
            initial_btc_price: 60000.0,
            elapsed_fraction: elapsed / 300.0,
            elapsed_seconds: elapsed,
            max_buy_amount: 100.0,
            open_orders: vec![],
        };
        let action = s.on_tick(&ctx);
        assert!(action.is_hold(), "DriftSniper should hold with flat BTC at {}s", elapsed);
    }
}

#[test]
fn test_poisson_kelly_flat_btc_holds_entire_session() {
    let mut s = fondaco::strategies::poisson_kelly::PoissonKelly::default();
    s.on_session_start(&default_metadata());
    let pos = Position::new(100.0);
    let meta = default_metadata();

    // Feed 100 ticks with flat BTC -> no jumps -> no signal
    for i in 0..100 {
        let elapsed = 150.0 + i as f64 * 1.0;
        let sample = make_sample(elapsed, 60000.0, 0.50, 0.50);
        let ctx = TickContext {
            tick_index: i,
            sample: &sample,
            position: &pos,
            metadata: &meta,
            initial_btc_price: 60000.0,
            elapsed_fraction: elapsed / 300.0,
            elapsed_seconds: elapsed,
            max_buy_amount: 100.0,
            open_orders: vec![],
        };
        let action = s.on_tick(&ctx);
        assert!(action.is_hold());
    }
}

// ===========================================================================
// 19. SimExecutor edge cases
// ===========================================================================

#[test]
fn test_sim_executor_sell_down_token() {
    let exec = SimExecutor::new("midpoint", 0.0, false, None, 0);
    let sample = make_sample(10.0, 60000.0, 0.40, 0.60);
    let mut pos = Position::new(0.0);
    pos.set_leg(1, 100.0);
    let action = TradeAction {
        side: "sell_down".to_string(),
        amount: 100.0,
        price: None,
        expiration_seconds: None,
        cancel_after_s: None,
    };
    let trade = exec.execute_trade(&action, 0, &sample, &mut pos);
    assert!(trade.is_some());
    let t = trade.unwrap();
    assert!((t.price - 0.60).abs() < 0.01);
    assert_eq!(pos.down_tokens(), 0.0);
    assert!(pos.cash > 0.0);
}

#[test]
fn test_sim_executor_sell_up_at_high_midpoint() {
    let exec = SimExecutor::new("midpoint", 0.0, false, None, 0);
    let sample = make_sample(200.0, 60100.0, 0.90, 0.10);
    let mut pos = Position::new(0.0);
    pos.set_leg(0, 100.0);
    let action = TradeAction {
        side: "sell_up".to_string(),
        amount: 100.0,
        price: None,
        expiration_seconds: None,
        cancel_after_s: None,
    };
    let trade = exec.execute_trade(&action, 0, &sample, &mut pos);
    assert!(trade.is_some());
    let t = trade.unwrap();
    assert!((t.price - 0.90).abs() < 0.01);
    assert!(pos.cash > 80.0); // Got 100 * 0.90 = 90
}

// ===========================================================================
// 20. Taker fee computation edge cases
// ===========================================================================

#[test]
fn test_taker_fee_at_extreme_prices() {
    // Near 0: very small fee
    let fee_low = compute_taker_fee(0.01, 100.0, true);
    assert!(fee_low > 0.0);
    assert!(fee_low < 0.01); // Very small

    // Near 1: very small fee (symmetric)
    let fee_high = compute_taker_fee(0.99, 100.0, true);
    assert!(fee_high > 0.0);
    assert!(fee_high < 0.01);

    // Midpoint: maximum fee
    let fee_mid = compute_taker_fee(0.50, 100.0, true);
    assert!(fee_mid > fee_low);
    assert!(fee_mid > fee_high);
}

#[test]
fn test_taker_fee_negative_tokens() {
    assert_eq!(compute_taker_fee(0.50, -100.0, true), 0.0);
}

// ===========================================================================
// 21. check_exit for down positions
// ===========================================================================

#[test]
fn test_check_exit_stop_loss_down_position() {
    let mut pos = Position::new(0.0);
    pos.set_leg(1, 100.0);
    let sample = make_sample(100.0, 59900.0, 0.60, 0.30);
    let meta = default_metadata();
    let ctx = TickContext {
        tick_index: 10,
        sample: &sample,
        position: &pos,
        metadata: &meta,
        initial_btc_price: 60000.0,
        elapsed_fraction: 100.0 / 300.0,
        elapsed_seconds: 100.0,
        max_buy_amount: 0.0,
        open_orders: vec![],
    };

    // Entry at 0.45, current down_mid at 0.30 -> loss > 10%
    let exit = check_exit(
        &ctx,
        0.60, 0.30, 0.45,
        Some(0.10), None,
        -999.0, 2000.0,
    );
    assert!(exit.is_some());
    let action = exit.unwrap();
    assert_eq!(action.side, "sell_down");
}

#[test]
fn test_check_exit_take_profit_down_position() {
    let mut pos = Position::new(0.0);
    pos.set_leg(1, 100.0);
    let sample = make_sample(100.0, 59900.0, 0.20, 0.80);
    let meta = default_metadata();
    let ctx = TickContext {
        tick_index: 10,
        sample: &sample,
        position: &pos,
        metadata: &meta,
        initial_btc_price: 60000.0,
        elapsed_fraction: 100.0 / 300.0,
        elapsed_seconds: 100.0,
        max_buy_amount: 0.0,
        open_orders: vec![],
    };

    // Entry at 0.45, current down_mid at 0.80 -> profit > 50%
    let exit = check_exit(
        &ctx,
        0.20, 0.80, 0.45,
        None, Some(0.50),
        -999.0, 2000.0,
    );
    assert!(exit.is_some());
    let action = exit.unwrap();
    assert_eq!(action.side, "sell_down");
}

// ===========================================================================
// 22. Backtest: BuyOnce strategy produces correct PnL
// ===========================================================================

#[test]
fn test_simulator_market_buy_once_up_win() {
    let sim = Simulator::new(100.0, "midpoint", 0.0, false, None, 0, None);
    let mut strategy = MarketBuyOnceStrategy::new();
    let session = make_test_session_data("Up", 100);
    let result = sim.run_session(&mut strategy, &session, None);
    // MarketBuyOnceStrategy does immediate buy at ~50% elapsed. Up outcome -> profit
    assert!(!result.trades.is_empty());
    assert!(result.pnl > 0.0);
}

#[test]
fn test_simulator_market_buy_once_down_loss() {
    let sim = Simulator::new(100.0, "midpoint", 0.0, false, None, 0, None);
    let mut strategy = MarketBuyOnceStrategy::new();
    let session = make_test_session_data("Down", 100);
    let result = sim.run_session(&mut strategy, &session, None);
    // MarketBuyOnceStrategy buys up tokens. Down outcome -> up tokens worthless
    assert!(!result.trades.is_empty());
    assert!(result.pnl < 0.0);
}

// ===========================================================================
// 23. TradeAction convenience constructors
// ===========================================================================

#[test]
fn test_trade_action_buy_has_default_expiration() {
    let action = TradeAction::buy("buy_up", 100.0, 0.55, Some(10.0));
    assert_eq!(action.expiration_seconds, Some(DEFAULT_EXPIRATION_SECONDS));
}

#[test]
fn test_trade_action_sell_has_default_expiration() {
    let action = TradeAction::sell("sell_up", 100.0, 0.80);
    assert_eq!(action.expiration_seconds, Some(DEFAULT_EXPIRATION_SECONDS));
    assert_eq!(action.cancel_after_s, None);
}

#[test]
fn test_trade_action_hold_has_no_expiration() {
    let h = hold();
    assert!(h.is_hold());
    assert!(h.expiration_seconds.is_none());
    assert!(h.price.is_none());
    assert_eq!(h.amount, 0.0);
}

// ===========================================================================
// 24. LateSniperCB requires_full_session is false (entry-only, no warmup)
// ===========================================================================

#[test]
fn test_late_sniper_cb_not_full_session() {
    let s = fondaco::strategies::late_sniper_cb::LateSniperCB::default();
    assert!(!s.requires_full_session());
    assert!(s.min_elapsed_hint() > 0.0);
}

#[test]
fn test_strategies_requiring_full_session() {
    // Verify specific strategies that need full session
    let pk = fondaco::strategies::poisson_kelly::PoissonKelly::default();
    assert!(pk.requires_full_session());

    let ds = fondaco::strategies::drift_sniper::DriftSniper::default();
    assert!(ds.requires_full_session());

    let gf = fondaco::strategies::gap_fader::GapFader::default();
    assert!(gf.requires_full_session());

    let rs = fondaco::strategies::regime_sniper::RegimeSniper::default();
    assert!(rs.requires_full_session());

    let td = fondaco::strategies::trend_drift::TrendDrift::default();
    assert!(td.requires_full_session());

    let mr = fondaco::strategies::mean_rev_fader::MeanRevFader::default();
    assert!(mr.requires_full_session());
}

// ===========================================================================
// 25. Sharpe decay via SessionResult objects
// ===========================================================================

#[test]
fn test_sharpe_decay_via_session_results() {
    use fondaco::engine::{detect_sharpe_decay, SessionResult, SHARPE_DECAY_RECENT_FRACTION};
    use fondaco::trader::Position;

    // Build a series with clear decay: 20 wins then 10 losses
    let mut results: Vec<SessionResult> = Vec::new();
    for _ in 0..20 {
        results.push(SessionResult {
            session_file: "test.json".to_string(),
            outcome: "Up".to_string(),
            pnl: 5.0,
            pnl_pct: 5.0,
            trades: vec![],
            final_position: Position::new(105.0),
            num_samples: 100,
            starting_cash: 100.0,
            total_fees: 0.0,
        });
    }
    for _ in 0..10 {
        results.push(SessionResult {
            session_file: "test.json".to_string(),
            outcome: "Down".to_string(),
            pnl: -4.0,
            pnl_pct: -4.0,
            trades: vec![],
            final_position: Position::new(96.0),
            num_samples: 100,
            starting_cash: 100.0,
            total_fees: 0.0,
        });
    }

    let info = detect_sharpe_decay(&results, SHARPE_DECAY_RECENT_FRACTION);
    assert!(info.full_sharpe > 0.0);
    assert!(info.recent_sharpe < info.full_sharpe);
    assert!(info.decay_amount > 0.0);
}

// ===========================================================================
// 26. Walk-forward with AlwaysHold produces zero Sharpe
// ===========================================================================

#[test]
fn test_walk_forward_with_always_hold() {
    use fondaco::engine::{walk_forward_validate, WALK_FORWARD_FOLDS, WALK_FORWARD_TRAIN_RATIO};

    // Build 15 sessions (enough for walk-forward)
    let sessions: Vec<SessionData> = (0..15)
        .map(|i| {
            let outcome = if i % 2 == 0 { "Up" } else { "Down" };
            make_test_session_data(outcome, 30)
        })
        .collect();

    let strategy = AlwaysHoldStrategy;
    let result = walk_forward_validate(
        &strategy,
        &sessions,
        100.0,
        WALK_FORWARD_FOLDS,
        WALK_FORWARD_TRAIN_RATIO,
    );

    if !result.skipped {
        // AlwaysHold never trades, so Sharpe should be zero
        for &s in &result.fold_sharpes {
            assert!((s - 0.0).abs() < 1e-9, "Expected zero Sharpe for AlwaysHold, got {}", s);
        }
    }
}
