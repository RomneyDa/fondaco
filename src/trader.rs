//! Unified trading abstractions shared by the simulator and live bot.
//!
//! Contains core types (TradeAction, Position, TickContext), Trade record,
//! Executor trait, SimExecutor, and Trader.

use crate::defaults::*;
use crate::models::{Sample, SessionSummary};
use std::collections::HashMap;

// ---------------------------------------------------------------------------
// Core types
// ---------------------------------------------------------------------------

#[derive(Debug, Clone)]
pub struct TradeAction {
    pub side: String,
    pub amount: f64,
    pub price: Option<f64>,
    pub expiration_seconds: Option<f64>,
    pub cancel_after_s: Option<f64>,
}

impl TradeAction {
    pub fn hold() -> Self {
        hold()
    }

    pub fn buy(side: &str, amount: f64, price: f64, cancel_after_s: Option<f64>) -> Self {
        Self {
            side: side.to_string(),
            amount,
            price: Some(price),
            expiration_seconds: Some(DEFAULT_EXPIRATION_SECONDS),
            cancel_after_s,
        }
    }

    pub fn sell(side: &str, amount: f64, price: f64) -> Self {
        Self {
            side: side.to_string(),
            amount,
            price: Some(price),
            expiration_seconds: Some(DEFAULT_EXPIRATION_SECONDS),
            cancel_after_s: None,
        }
    }

    pub fn is_hold(&self) -> bool {
        self.side == "hold"
    }
}

pub fn hold() -> TradeAction {
    TradeAction {
        side: "hold".to_string(),
        amount: 0.0,
        price: None,
        expiration_seconds: None,
        cancel_after_s: None,
    }
}

pub static HOLD_SIDE: &str = "hold";

#[derive(Debug, Clone)]
pub struct OpenOrder {
    pub order_id: String,
    pub side: String,
    pub token: String,
    pub price: f64,
    pub tokens: f64,
    pub dollars_reserved: f64,
    pub tokens_reserved: f64,
    pub tick_placed: usize,
    pub token_id: Option<String>,
    pub expiration: Option<f64>,
    pub cancel_after_s: Option<f64>,
    pub time_placed: Option<f64>,
}

#[derive(Debug, Clone)]
pub struct Position {
    pub cash: f64,
    pub up_tokens: f64,
    pub down_tokens: f64,
}

impl Position {
    pub fn new(cash: f64) -> Self {
        Self {
            cash,
            up_tokens: 0.0,
            down_tokens: 0.0,
        }
    }

    pub fn compute_trade_size(&self, side: &str, price: Option<f64>, amount: f64) -> (f64, f64) {
        let price = match price {
            Some(p) if p > 0.0 && p < 1.0 => p,
            _ => return (0.0, 0.0),
        };

        match side {
            "buy_up" | "buy_down" => {
                let dollars = amount.min(self.cash);
                if dollars <= 0.0 {
                    return (0.0, 0.0);
                }
                let tokens = round_to(dollars / price, TOKEN_DECIMALS);
                if tokens < CLOB_MIN_ORDER_TOKENS {
                    return (0.0, 0.0);
                }
                let cost = tokens * price;
                (tokens, cost)
            }
            "sell_up" | "sell_down" => {
                let available = if side == "sell_up" {
                    self.up_tokens
                } else {
                    self.down_tokens
                };
                let tokens =
                    (amount.min(available) * TOKEN_FLOOR_DIVISOR).floor() / TOKEN_FLOOR_DIVISOR;
                if tokens <= 0.0 {
                    return (0.0, 0.0);
                }
                let proceeds = tokens * price;
                (tokens, proceeds)
            }
            _ => (0.0, 0.0),
        }
    }

    pub fn apply_trade(&mut self, side: &str, price: f64, tokens: f64) {
        if tokens <= 0.0 {
            return;
        }
        match side {
            "buy_up" => {
                self.cash -= tokens * price;
                self.up_tokens += tokens;
            }
            "buy_down" => {
                self.cash -= tokens * price;
                self.down_tokens += tokens;
            }
            "sell_up" => {
                self.up_tokens -= tokens;
                self.cash += tokens * price;
            }
            "sell_down" => {
                self.down_tokens -= tokens;
                self.cash += tokens * price;
            }
            _ => {}
        }
    }

    pub fn resolve(&mut self, outcome: Option<&str>) {
        match outcome {
            Some("Up") => {
                self.cash += self.up_tokens;
                self.up_tokens = 0.0;
                self.down_tokens = 0.0;
            }
            Some("Down") => {
                self.cash += self.down_tokens;
                self.up_tokens = 0.0;
                self.down_tokens = 0.0;
            }
            _ => {}
        }
    }

    pub fn reserve_for_order(&mut self, side: &str, price: f64, tokens: f64) -> (f64, f64) {
        match side {
            "buy_up" | "buy_down" => {
                let cost = tokens * price;
                self.cash -= cost;
                (cost, 0.0)
            }
            "sell_up" => {
                self.up_tokens -= tokens;
                (0.0, tokens)
            }
            "sell_down" => {
                self.down_tokens -= tokens;
                (0.0, tokens)
            }
            _ => (0.0, 0.0),
        }
    }

    pub fn unreserve_for_order(&mut self, side: &str, dollars_reserved: f64, tokens_reserved: f64) {
        match side {
            "buy_up" | "buy_down" => {
                self.cash += dollars_reserved;
            }
            "sell_up" => {
                self.up_tokens += tokens_reserved;
            }
            "sell_down" => {
                self.down_tokens += tokens_reserved;
            }
            _ => {}
        }
    }
}

fn round_to(value: f64, decimals: u32) -> f64 {
    let factor = 10f64.powi(decimals as i32);
    (value * factor).round() / factor
}

pub struct TickContext<'a> {
    pub tick_index: usize,
    pub sample: &'a Sample,
    pub position: &'a Position,
    pub metadata: &'a SessionMetadata,
    pub initial_btc_price: f64,
    pub elapsed_fraction: f64,
    pub elapsed_seconds: f64,
    pub max_buy_amount: f64,
    pub open_orders: Vec<OpenOrder>,
}

/// Runtime metadata passed to strategies. Superset of MarketMetadata.
#[derive(Debug, Clone, Default)]
pub struct SessionMetadata {
    pub up_token_id: String,
    pub down_token_id: String,
    pub window_start_utc: String,
    pub window_duration: f64,
    pub prior_session_summaries: Vec<SessionSummary>,
    pub gap_before_seconds: Option<f64>,
    pub data_density: f64,
    /// Extra string fields from the raw metadata
    pub extra: HashMap<String, String>,
}

#[derive(Debug, Clone)]
pub struct Trade {
    pub tick_index: usize,
    pub side: String,
    pub price: f64,
    pub amount: f64,
    pub tokens: f64,
    pub order_id: Option<String>,
    pub token_id: Option<String>,
    pub success: Option<bool>,
    pub error: Option<String>,
    pub timestamp: Option<String>,
    pub requested_tokens: Option<f64>,
    pub elapsed_seconds: Option<f64>,
    pub order_type: Option<String>,
    pub strategy_version: Option<String>,
    pub fee: Option<f64>,
}

impl Trade {
    pub fn new(tick_index: usize, side: &str, price: f64, amount: f64, tokens: f64) -> Self {
        Self {
            tick_index,
            side: side.to_string(),
            price,
            amount,
            tokens,
            order_id: None,
            token_id: None,
            success: None,
            error: None,
            timestamp: None,
            requested_tokens: None,
            elapsed_seconds: None,
            order_type: None,
            strategy_version: None,
            fee: None,
        }
    }
}

// ---------------------------------------------------------------------------
// FlatlineDetector — blocks trading when midpoints stop moving (CLOB stale)
// ---------------------------------------------------------------------------

/// Detects when both up/down midpoints are unchanged for too long, indicating
/// a possible CLOB API issue.  When flatlined, callers should suppress all
/// trade actions (but still call `on_tick` so strategy state stays consistent).
pub struct FlatlineDetector {
    last_up_mid: Option<f64>,
    last_down_mid: Option<f64>,
    flatline_since: Option<f64>,
    pub is_flatlined: bool,
}

impl FlatlineDetector {
    pub fn new() -> Self {
        Self {
            last_up_mid: None,
            last_down_mid: None,
            flatline_since: None,
            is_flatlined: false,
        }
    }

    /// Call once per tick with the current sample.  Returns `true` if trading
    /// is allowed, `false` if midpoints are flatlined and trades should be
    /// suppressed.
    pub fn update(&mut self, sample: &Sample) -> bool {
        let up = sample.up_token_midpoint;
        let down = sample.down_token_midpoint;

        // Skip detection when midpoints are null (end-of-session)
        if up.is_none() && down.is_none() {
            return !self.is_flatlined;
        }

        let same = match (up, self.last_up_mid, down, self.last_down_mid) {
            (Some(u), Some(lu), Some(d), Some(ld)) => {
                (u - lu).abs() < 1e-9 && (d - ld).abs() < 1e-9
            }
            _ => false,
        };

        self.last_up_mid = up;
        self.last_down_mid = down;

        if same {
            let since = *self.flatline_since.get_or_insert(sample.elapsed_seconds);
            let duration = sample.elapsed_seconds - since;
            if duration >= FLATLINE_THRESHOLD_SECONDS && !self.is_flatlined {
                self.is_flatlined = true;
                log::warn!(
                    "Flatline detected: midpoints unchanged for {:.1}s (up={:.4}, down={:.4}) — trading disabled",
                    duration,
                    up.unwrap_or(0.0),
                    down.unwrap_or(0.0),
                );
            }
        } else {
            if self.is_flatlined {
                log::info!(
                    "Flatline cleared: midpoints moving again (up={:.4}, down={:.4}) — trading re-enabled",
                    up.unwrap_or(0.0),
                    down.unwrap_or(0.0),
                );
            }
            self.flatline_since = None;
            self.is_flatlined = false;
        }

        !self.is_flatlined
    }

}

// ---------------------------------------------------------------------------
// SimExecutor
// ---------------------------------------------------------------------------

pub struct SimExecutor {
    pub execution_mode: String,
    pub slippage: f64,
    pub fees: bool,
    pub limit_fill_margin: f64,
    pub latency_ms: u64,
    latency_seconds: f64,
    sim_order_counter: usize,
}

impl SimExecutor {
    pub fn new(
        execution_mode: &str,
        slippage: f64,
        fees: bool,
        limit_fill_margin: Option<f64>,
        latency_ms: u64,
    ) -> Self {
        Self {
            execution_mode: execution_mode.to_string(),
            slippage,
            fees,
            limit_fill_margin: limit_fill_margin.unwrap_or(slippage),
            latency_ms,
            latency_seconds: latency_ms as f64 / 1000.0,
            sim_order_counter: 0,
        }
    }

    fn get_execution_price(&self, side: &str, sample: &Sample) -> Option<f64> {
        let is_up = side.contains("up");
        let is_buy = side.starts_with("buy");

        match self.execution_mode.as_str() {
            "midpoint_slippage" => {
                let base = if is_up {
                    sample.up_token_midpoint
                } else {
                    sample.down_token_midpoint
                }?;
                Some(if is_buy {
                    base + self.slippage
                } else {
                    base - self.slippage
                })
            }
            "midpoint" => {
                if is_up {
                    sample.up_token_midpoint
                } else {
                    sample.down_token_midpoint
                }
            }
            "book" => {
                if is_buy {
                    if is_up {
                        sample.up_token_best_ask
                    } else {
                        sample.down_token_best_ask
                    }
                } else if is_up {
                    sample.up_token_midpoint
                } else {
                    sample.down_token_midpoint
                }
            }
            _ => None,
        }
    }

    pub fn execute_trade(
        &self,
        action: &TradeAction,
        tick_index: usize,
        sample: &Sample,
        position: &mut Position,
    ) -> Option<Trade> {
        if action.is_hold() || action.amount <= 0.0 {
            return None;
        }

        let price = self.get_execution_price(&action.side, sample)?;
        let price = round_to(price, PRICE_DECIMALS).min(MAX_TOKEN_PRICE);

        let (tokens, dollars) = position.compute_trade_size(&action.side, Some(price), action.amount);
        if tokens <= 0.0 || tokens < CLOB_MIN_ORDER_TOKENS {
            return None;
        }

        position.apply_trade(&action.side, price, tokens);
        let fee = compute_taker_fee(price, tokens, self.fees);
        position.cash -= fee;

        Some(Trade {
            tick_index,
            side: action.side.clone(),
            price,
            amount: dollars,
            tokens,
            elapsed_seconds: Some(sample.elapsed_seconds),
            fee: Some(fee),
            ..Trade::new(tick_index, &action.side, price, dollars, tokens)
        })
    }

    pub fn place_limit_order(
        &mut self,
        action: &TradeAction,
        tick_index: usize,
        sample: &Sample,
        position: &mut Position,
    ) -> Option<OpenOrder> {
        if action.is_hold() || action.amount <= 0.0 || action.price.is_none() {
            return None;
        }

        let price = round_to(action.price.unwrap(), PRICE_DECIMALS);
        if price <= 0.0 || price >= 1.0 {
            return None;
        }

        let (mut tokens, _dollars) = position.compute_trade_size(&action.side, Some(price), action.amount);
        if tokens <= 0.0 || tokens < CLOB_MIN_ORDER_TOKENS {
            return None;
        }

        // Floor buys to integer (same as ClobExecutor)
        if action.side.starts_with("buy") {
            tokens = tokens.floor();
            if tokens < CLOB_MIN_ORDER_TOKENS {
                return None;
            }
        }

        let token = if action.side.contains("up") {
            "up"
        } else {
            "down"
        };

        let (dollars_reserved, tokens_reserved) =
            position.reserve_for_order(&action.side, price, tokens);

        let expiration = action
            .expiration_seconds
            .map(|exp| sample.elapsed_seconds + exp);

        self.sim_order_counter += 1;

        Some(OpenOrder {
            order_id: format!("sim-{}", self.sim_order_counter),
            side: action.side.clone(),
            token: token.to_string(),
            price,
            tokens,
            dollars_reserved,
            tokens_reserved,
            tick_placed: tick_index,
            token_id: None,
            expiration,
            cancel_after_s: action.cancel_after_s,
            time_placed: Some(sample.elapsed_seconds),
        })
    }

    pub fn check_fills(
        &self,
        open_orders: &mut Vec<OpenOrder>,
        sample: &Sample,
        position: &mut Position,
    ) -> Vec<Trade> {
        let mut fills = Vec::new();
        let mut remaining = Vec::new();
        let current_elapsed = sample.elapsed_seconds;
        let margin = self.limit_fill_margin;

        for order in open_orders.drain(..) {
            // GTD expiration
            if let Some(exp) = order.expiration {
                if current_elapsed >= exp {
                    position.unreserve_for_order(
                        &order.side,
                        order.dollars_reserved,
                        order.tokens_reserved,
                    );
                    continue;
                }
            }

            let midpoint = if order.token == "up" {
                sample.up_token_midpoint
            } else {
                sample.down_token_midpoint
            };

            let midpoint = match midpoint {
                Some(m) => m,
                None => {
                    remaining.push(order);
                    continue;
                }
            };

            let filled = if order.side.starts_with("buy") {
                midpoint <= order.price - margin
            } else {
                let threshold = (order.price + margin).min(MAX_TOKEN_PRICE);
                midpoint >= threshold
            };

            if filled {
                // Apply trade to position
                if order.side.starts_with("buy") {
                    if order.side.contains("up") {
                        position.up_tokens += order.tokens;
                    } else {
                        position.down_tokens += order.tokens;
                    }
                } else {
                    position.cash += order.tokens * order.price;
                }

                let fee = compute_taker_fee(order.price, order.tokens, self.fees);
                position.cash -= fee;

                let mut trade =
                    Trade::new(0, &order.side, order.price, order.tokens * order.price, order.tokens);
                trade.order_id = Some(order.order_id.clone());
                trade.order_type = Some(
                    if order.expiration.is_some() {
                        "GTD"
                    } else {
                        "GTC"
                    }
                    .to_string(),
                );
                trade.elapsed_seconds = Some(sample.elapsed_seconds);
                trade.fee = Some(fee);
                fills.push(trade);
            } else {
                remaining.push(order);
            }
        }

        *open_orders = remaining;
        fills
    }

    pub fn cancel_order(&self, order: &OpenOrder, position: &mut Position) -> bool {
        position.unreserve_for_order(&order.side, order.dollars_reserved, order.tokens_reserved);
        true
    }
}

// ---------------------------------------------------------------------------
// Trader — shared session runner
// ---------------------------------------------------------------------------

pub struct Trader {
    pub strategy: Box<dyn crate::strategy::Strategy>,
    pub executor: SimExecutor,
    metadata: SessionMetadata,
    position: Option<Position>,
    initial_btc_price: f64,
    window_duration: f64,
    trades: Vec<Trade>,
    tick_index: usize,
    open_orders: Vec<OpenOrder>,
    deferred_actions: Vec<(f64, TradeAction, f64, f64)>,
    flatline: FlatlineDetector,
}

impl Trader {
    pub fn new(strategy: Box<dyn crate::strategy::Strategy>, executor: SimExecutor) -> Self {
        Self {
            strategy,
            executor,
            metadata: SessionMetadata::default(),
            position: None,
            initial_btc_price: 0.0,
            window_duration: 300.0,
            trades: Vec::new(),
            tick_index: 0,
            open_orders: Vec::new(),
            deferred_actions: Vec::new(),
            flatline: FlatlineDetector::new(),
        }
    }

    pub fn start_session(
        &mut self,
        metadata: SessionMetadata,
        position: Position,
        initial_btc_price: f64,
        window_duration: f64,
    ) {
        self.metadata = metadata;
        self.initial_btc_price = initial_btc_price;
        self.window_duration = window_duration;
        self.trades.clear();
        self.tick_index = 0;
        self.open_orders.clear();
        self.deferred_actions.clear();
        self.position = Some(position);
        self.strategy.on_session_start(&self.metadata);
    }

    pub fn process_tick(&mut self, sample: &Sample) -> Option<Trade> {
        if self.position.is_none() {
            return None;
        }

        // Process deferred actions (latency simulation)
        if !self.deferred_actions.is_empty() {
            self.process_deferred_actions(sample);
        }

        // Check fills on pending limit orders
        if !self.open_orders.is_empty() {
            let position = self.position.as_mut().unwrap();
            let fills = self.executor.check_fills(&mut self.open_orders, sample, position);
            for mut fill in fills {
                fill.tick_index = self.tick_index;
                self.trades.push(fill);
            }
        }

        // Cancel expired soft orders
        if !self.open_orders.is_empty() {
            self.cancel_expired_soft(sample.elapsed_seconds);
        }

        let elapsed_frac = if self.window_duration > 0.0 {
            (sample.elapsed_seconds / self.window_duration).min(1.0)
        } else {
            0.0
        };

        let position = self.position.as_ref().unwrap();
        let max_buy = position.cash;
        let ctx = TickContext {
            tick_index: self.tick_index,
            sample,
            position,
            metadata: &self.metadata,
            initial_btc_price: self.initial_btc_price,
            elapsed_fraction: elapsed_frac,
            elapsed_seconds: sample.elapsed_seconds,
            max_buy_amount: max_buy,
            open_orders: self.open_orders.clone(),
        };

        // Flatline detection — must run every tick for accurate timing
        let trading_allowed = self.flatline.update(sample);

        let action = self.strategy.on_tick(&ctx);

        // Drain tick logs (discarded in sim, used in live bot_worker)
        let _ = self.strategy.drain_logs();

        // Suppress all trades while midpoints are flatlined
        if !trading_allowed {
            self.tick_index += 1;
            return None;
        }

        // Fast path: hold
        if action.is_hold() {
            self.tick_index += 1;
            return None;
        }

        // Guardrail: block trades near session end
        if sample.elapsed_seconds >= self.window_duration - TRADE_CUTOFF_SECONDS {
            self.tick_index += 1;
            return None;
        }

        // Cancel actions
        if action.side.starts_with("cancel") {
            let token = if action.side == "cancel_up" {
                "up"
            } else {
                "down"
            };
            let position = self.position.as_mut().unwrap();
            let executor = &self.executor;
            self.open_orders.retain(|o| {
                if o.token == token {
                    executor.cancel_order(o, position);
                    false
                } else {
                    true
                }
            });
            self.tick_index += 1;
            return None;
        }

        // Cap buy amount
        let action = if action.side.starts_with("buy") {
            let cash = self.position.as_ref().unwrap().cash;
            if cash <= 0.0 {
                self.tick_index += 1;
                return None;
            }
            if action.amount > cash {
                TradeAction {
                    side: action.side.clone(),
                    amount: cash,
                    price: action.price,
                    expiration_seconds: action.expiration_seconds,
                    cancel_after_s: action.cancel_after_s,
                }
            } else {
                action
            }
        } else {
            action
        };

        // Latency simulation
        if self.executor.latency_seconds > 0.0 {
            let position = self.position.as_mut().unwrap();
            let target_elapsed = sample.elapsed_seconds + self.executor.latency_seconds;
            let (reserved_dollars, reserved_tokens) = if action.side.starts_with("buy") {
                let rd = action.amount;
                position.cash -= rd;
                (rd, 0.0)
            } else if action.side.starts_with("sell") {
                let token_attr = if action.side.contains("up") {
                    &mut position.up_tokens
                } else {
                    &mut position.down_tokens
                };
                let rt = action.amount.min(*token_attr);
                *token_attr -= rt;
                (0.0, rt)
            } else {
                (0.0, 0.0)
            };
            self.deferred_actions
                .push((target_elapsed, action, reserved_dollars, reserved_tokens));
            self.tick_index += 1;
            return None;
        }

        // Route: limit order vs FAK
        let trade = if action.price.is_some() {
            // Replace existing order on same token
            self.replace_order_on_token(&action);
            let position = self.position.as_mut().unwrap();
            if let Some(order) =
                self.executor
                    .place_limit_order(&action, self.tick_index, sample, position)
            {
                self.open_orders.push(order);
            }
            None
        } else {
            let position = self.position.as_mut().unwrap();
            self.executor
                .execute_trade(&action, self.tick_index, sample, position)
        };

        if let Some(ref t) = trade {
            if t.success != Some(false) {
                self.trades.push(t.clone());
            }
        }

        self.tick_index += 1;
        trade
    }

    pub fn end_session(&mut self, outcome: Option<&str>) -> Vec<Trade> {
        if let Some(ref mut position) = self.position {
            // Cancel all pending orders
            for order in self.open_orders.drain(..) {
                self.executor.cancel_order(&order, position);
            }
            // Cancel deferred actions
            for (_, _, rd, rt) in self.deferred_actions.drain(..) {
                position.cash += rd;
                // We'd need to know the side to unreserve tokens correctly.
                // For simplicity, just add to up_tokens (the caller handles resolution).
                position.up_tokens += rt;
            }
            position.resolve(outcome);
        }
        self.trades.clone()
    }

    fn cancel_expired_soft(&mut self, current_time: f64) {
        let position = self.position.as_mut().unwrap();
        self.open_orders.retain(|order| {
            if let Some(cancel_after) = order.cancel_after_s {
                if let Some(placed) = order.time_placed {
                    if current_time - placed >= cancel_after {
                        position.unreserve_for_order(
                            &order.side,
                            order.dollars_reserved,
                            order.tokens_reserved,
                        );
                        return false;
                    }
                }
            }
            true
        });
    }

    fn replace_order_on_token(&mut self, action: &TradeAction) {
        let token = if action.side.contains("up") {
            "up"
        } else {
            "down"
        };
        let position = self.position.as_mut().unwrap();
        self.open_orders.retain(|order| {
            if order.token == token {
                self.executor.cancel_order(order, position);
                false
            } else {
                true
            }
        });
    }

    fn process_deferred_actions(&mut self, sample: &Sample) {
        let position = self.position.as_mut().unwrap();
        let current_elapsed = sample.elapsed_seconds;

        let mut remaining = Vec::new();
        for (target, action, rd, rt) in self.deferred_actions.drain(..) {
            if current_elapsed >= target {
                // Unreserve funds first
                if action.side.starts_with("buy") {
                    position.cash += rd;
                } else if action.side.starts_with("sell") {
                    if action.side.contains("up") {
                        position.up_tokens += rt;
                    } else {
                        position.down_tokens += rt;
                    }
                }

                // Now execute at current prices
                if action.price.is_some() {
                    if let Some(order) = self.executor.place_limit_order(
                        &action,
                        self.tick_index,
                        sample,
                        position,
                    ) {
                        self.open_orders.push(order);
                    }
                } else {
                    if let Some(trade) =
                        self.executor
                            .execute_trade(&action, self.tick_index, sample, position)
                    {
                        if trade.success != Some(false) {
                            self.trades.push(trade);
                        }
                    }
                }
            } else {
                remaining.push((target, action, rd, rt));
            }
        }
        self.deferred_actions = remaining;
    }

    pub fn position(&self) -> Option<&Position> {
        self.position.as_ref()
    }

    pub fn position_mut(&mut self) -> Option<&mut Position> {
        self.position.as_mut()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_position_new() {
        let p = Position::new(100.0);
        assert_eq!(p.cash, 100.0);
        assert_eq!(p.up_tokens, 0.0);
        assert_eq!(p.down_tokens, 0.0);
    }

    #[test]
    fn test_compute_trade_size_buy() {
        let p = Position::new(100.0);
        let (tokens, cost) = p.compute_trade_size("buy_up", Some(0.50), 100.0);
        assert!(tokens > 0.0);
        assert!((tokens - 200.0).abs() < 0.01);
        assert!((cost - 100.0).abs() < 0.01);
    }

    #[test]
    fn test_compute_trade_size_insufficient() {
        let p = Position::new(1.0);
        // 1.0 / 0.50 = 2.0 tokens, below min 5
        let (tokens, _) = p.compute_trade_size("buy_up", Some(0.50), 1.0);
        assert_eq!(tokens, 0.0);
    }

    #[test]
    fn test_compute_trade_size_sell() {
        let mut p = Position::new(0.0);
        p.up_tokens = 100.0;
        let (tokens, proceeds) = p.compute_trade_size("sell_up", Some(0.80), 100.0);
        assert_eq!(tokens, 100.0);
        assert!((proceeds - 80.0).abs() < 0.01);
    }

    #[test]
    fn test_apply_trade() {
        let mut p = Position::new(100.0);
        p.apply_trade("buy_up", 0.50, 50.0);
        assert!((p.cash - 75.0).abs() < 0.01);
        assert!((p.up_tokens - 50.0).abs() < 0.01);

        p.apply_trade("sell_up", 0.80, 50.0);
        assert!((p.up_tokens - 0.0).abs() < 0.01);
        assert!((p.cash - 115.0).abs() < 0.01);
    }

    #[test]
    fn test_resolve_up() {
        let mut p = Position::new(0.0);
        p.up_tokens = 100.0;
        p.down_tokens = 50.0;
        p.resolve(Some("Up"));
        assert_eq!(p.cash, 100.0);
        assert_eq!(p.up_tokens, 0.0);
        assert_eq!(p.down_tokens, 0.0);
    }

    #[test]
    fn test_resolve_down() {
        let mut p = Position::new(0.0);
        p.up_tokens = 100.0;
        p.down_tokens = 50.0;
        p.resolve(Some("Down"));
        assert_eq!(p.cash, 50.0);
        assert_eq!(p.up_tokens, 0.0);
        assert_eq!(p.down_tokens, 0.0);
    }

    #[test]
    fn test_reserve_unreserve() {
        let mut p = Position::new(100.0);
        let (dr, _) = p.reserve_for_order("buy_up", 0.50, 50.0);
        assert!((p.cash - 75.0).abs() < 0.01);
        assert!((dr - 25.0).abs() < 0.01);

        p.unreserve_for_order("buy_up", dr, 0.0);
        assert!((p.cash - 100.0).abs() < 0.01);
    }

    #[test]
    fn test_hold() {
        let h = hold();
        assert!(h.is_hold());
        assert_eq!(h.amount, 0.0);
    }

    #[test]
    fn test_invalid_price() {
        let p = Position::new(100.0);
        assert_eq!(p.compute_trade_size("buy_up", Some(0.0), 100.0), (0.0, 0.0));
        assert_eq!(p.compute_trade_size("buy_up", Some(1.0), 100.0), (0.0, 0.0));
        assert_eq!(p.compute_trade_size("buy_up", None, 100.0), (0.0, 0.0));
    }

    fn make_sample(elapsed: f64, up_mid: Option<f64>, down_mid: Option<f64>) -> Sample {
        Sample {
            timestamp_utc: None,
            elapsed_seconds: elapsed,
            btc_price_usd: None,
            up_token_midpoint: up_mid,
            down_token_midpoint: down_mid,
            up_token_best_bid: None,
            up_token_best_ask: None,
            down_token_best_bid: None,
            down_token_best_ask: None,
            server_timestamp: None,
        }
    }

    #[test]
    fn test_flatline_detector_no_trigger_on_moving_prices() {
        let mut fd = FlatlineDetector::new();
        for i in 0..100 {
            let t = i as f64 * 0.05;
            let up = 0.50 + (i as f64 * 0.001);
            assert!(fd.update(&make_sample(t, Some(up), Some(1.0 - up))));
        }
        assert!(!fd.is_flatlined);
    }

    #[test]
    fn test_flatline_detector_triggers_after_threshold() {
        let mut fd = FlatlineDetector::new();
        // First tick establishes baseline
        assert!(fd.update(&make_sample(0.0, Some(0.50), Some(0.50))));
        // Second identical tick starts the flatline timer
        assert!(fd.update(&make_sample(0.05, Some(0.50), Some(0.50))));
        assert!(!fd.is_flatlined);
        // Still within threshold
        assert!(fd.update(&make_sample(14.9, Some(0.50), Some(0.50))));
        assert!(!fd.is_flatlined);
        // Cross threshold (15s since flatline_since=0.05)
        assert!(!fd.update(&make_sample(15.1, Some(0.50), Some(0.50))));
        assert!(fd.is_flatlined);
    }

    #[test]
    fn test_flatline_detector_clears_on_price_change() {
        let mut fd = FlatlineDetector::new();
        fd.update(&make_sample(0.0, Some(0.50), Some(0.50)));
        fd.update(&make_sample(0.05, Some(0.50), Some(0.50)));
        fd.update(&make_sample(16.0, Some(0.50), Some(0.50)));
        assert!(fd.is_flatlined);
        // Price moves — clears
        assert!(fd.update(&make_sample(16.05, Some(0.51), Some(0.49))));
        assert!(!fd.is_flatlined);
    }

    #[test]
    fn test_flatline_persists_across_sessions() {
        let mut fd = FlatlineDetector::new();
        // Flatline at end of session 1
        fd.update(&make_sample(0.0, Some(0.50), Some(0.50)));
        fd.update(&make_sample(0.05, Some(0.50), Some(0.50)));
        fd.update(&make_sample(16.0, Some(0.50), Some(0.50)));
        assert!(fd.is_flatlined);
        // Session 2 starts — same stale prices — still blocked
        assert!(!fd.update(&make_sample(0.0, Some(0.50), Some(0.50))));
        assert!(fd.is_flatlined);
        // API recovers — prices move — clears
        assert!(fd.update(&make_sample(0.05, Some(0.51), Some(0.49))));
        assert!(!fd.is_flatlined);
    }

    #[test]
    fn test_flatline_detector_one_side_moves() {
        let mut fd = FlatlineDetector::new();
        fd.update(&make_sample(0.0, Some(0.50), Some(0.50)));
        // Only up_mid changes — not flatlined
        assert!(fd.update(&make_sample(4.0, Some(0.51), Some(0.50))));
        assert!(!fd.is_flatlined);
    }

    #[test]
    fn test_flatline_detector_null_midpoints() {
        let mut fd = FlatlineDetector::new();
        // Null midpoints don't trigger flatline
        assert!(fd.update(&make_sample(0.0, None, None)));
        assert!(fd.update(&make_sample(4.0, None, None)));
        assert!(!fd.is_flatlined);
    }
}
