//! Unified trading abstractions shared by the simulator and live bot.
//!
//! Contains core types (TradeAction, Position, TickContext), Trade record,
//! Executor trait, SimExecutor, and Trader.

use crate::defaults::*;
use crate::economics::MarketEconomics;
use crate::models::{Sample, SessionSummary};
use std::collections::HashMap;

// ---------------------------------------------------------------------------
// Legs
// ---------------------------------------------------------------------------

/// Maximum tradeable legs in one session.
///
/// Crypto up/down uses 2. A soccer game needs 3 for the moneyline alone
/// (home/draw/away) and more once spread and total markets are grouped under
/// the same game. Fixed-size so `Position` stays `Copy`-cheap in the rayon
/// backtest hot path — a `Vec` here costs an allocation per simulated session.
pub const MAX_LEGS: usize = 8;

/// Canonical leg index for a leg key.
///
/// The keys are globally unique across market kinds (`up`/`down` for crypto,
/// `home`/`away`/`draw` for sports moneylines) so a single table serves both
/// without the parser needing to know which market it is looking at. Legs
/// beyond these well-known names use the explicit `buy:3` numeric form.
pub fn leg_index_for_key(key: &str) -> Option<usize> {
    match key {
        "up" | "home" => Some(0),
        "down" | "away" => Some(1),
        "draw" => Some(2),
        _ => key.parse::<usize>().ok().filter(|i| *i < MAX_LEGS),
    }
}

/// Static description of one tradeable leg of a session.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct LegSpec {
    /// Short key used in side strings: `up`, `down`, `home`, `away`, `draw`.
    pub key: String,
    /// Human-readable label for logs and the dashboard.
    pub label: String,
    pub token_id: String,
    pub condition_id: String,
}

impl LegSpec {
    pub fn new(key: &str, label: &str, token_id: &str, condition_id: &str) -> Self {
        Self {
            key: key.to_string(),
            label: label.to_string(),
            token_id: token_id.to_string(),
            condition_id: condition_id.to_string(),
        }
    }

    /// The crypto up/down pair, in canonical leg order.
    pub fn crypto_pair(up_token_id: &str, down_token_id: &str) -> Vec<LegSpec> {
        vec![
            LegSpec::new("up", "Up", up_token_id, ""),
            LegSpec::new("down", "Down", down_token_id, ""),
        ]
    }
}

// ---------------------------------------------------------------------------
// Side
// ---------------------------------------------------------------------------

/// A parsed trade action side.
///
/// `TradeAction.side` stays a `String` on purpose: it is the strategy-facing
/// API, it is what lands in `bot_trades.jsonl`, and it is what the dashboard
/// renders. Keeping it means strategies and log formats are untouched by the
/// leg generalization. `Side` is the parsed form used by the execution path so
/// that routing is by leg index rather than by substring matching — the old
/// `side.contains("up")` test would match a sports leg keyed `up_the_middle`
/// and silently trade the wrong token.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Side {
    Hold,
    Buy(u8),
    Sell(u8),
    Cancel(u8),
}

impl Side {
    /// Parse a side string. Returns `None` for anything unrecognised, which
    /// callers must treat as "do not trade" rather than guessing a leg.
    pub fn parse(s: &str) -> Option<Side> {
        if s == HOLD_SIDE {
            return Some(Side::Hold);
        }
        let (verb, key) = s.split_once('_').or_else(|| s.split_once(':'))?;
        let leg = leg_index_for_key(key)? as u8;
        match verb {
            "buy" => Some(Side::Buy(leg)),
            "sell" => Some(Side::Sell(leg)),
            "cancel" => Some(Side::Cancel(leg)),
            _ => None,
        }
    }

    pub fn leg(&self) -> Option<usize> {
        match self {
            Side::Hold => None,
            Side::Buy(i) | Side::Sell(i) | Side::Cancel(i) => Some(*i as usize),
        }
    }

    pub fn is_buy(&self) -> bool {
        matches!(self, Side::Buy(_))
    }

    pub fn is_sell(&self) -> bool {
        matches!(self, Side::Sell(_))
    }

    pub fn is_cancel(&self) -> bool {
        matches!(self, Side::Cancel(_))
    }

    /// Render back to a side string using the session's leg keys.
    pub fn to_side_string(&self, legs: &[LegSpec]) -> String {
        let key = |i: usize| -> String {
            legs.get(i)
                .map(|l| l.key.clone())
                .unwrap_or_else(|| i.to_string())
        };
        match self {
            Side::Hold => HOLD_SIDE.to_string(),
            Side::Buy(i) => format!("buy_{}", key(*i as usize)),
            Side::Sell(i) => format!("sell_{}", key(*i as usize)),
            Side::Cancel(i) => format!("cancel_{}", key(*i as usize)),
        }
    }
}

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
        self.side == HOLD_SIDE
    }

    /// Parsed side. `Side::Hold` for anything unrecognised so an unparseable
    /// side can never be routed to a leg by accident.
    pub fn parsed_side(&self) -> Side {
        Side::parse(&self.side).unwrap_or(Side::Hold)
    }

    /// Leg this action targets, if any.
    pub fn leg(&self) -> Option<usize> {
        self.parsed_side().leg()
    }

    /// Build an action targeting a leg by index, rendered with the session's
    /// leg keys so logs stay human-readable.
    pub fn buy_leg(
        legs: &[LegSpec],
        leg: usize,
        amount: f64,
        price: f64,
        cancel_after_s: Option<f64>,
    ) -> Self {
        Self {
            side: Side::Buy(leg as u8).to_side_string(legs),
            amount,
            price: Some(price),
            expiration_seconds: Some(DEFAULT_EXPIRATION_SECONDS),
            cancel_after_s,
        }
    }

    pub fn sell_leg(legs: &[LegSpec], leg: usize, amount: f64, price: f64) -> Self {
        Self {
            side: Side::Sell(leg as u8).to_side_string(legs),
            amount,
            price: Some(price),
            expiration_seconds: Some(DEFAULT_EXPIRATION_SECONDS),
            cancel_after_s: None,
        }
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
    /// Leg index this order sits on.
    pub leg: usize,
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

/// Cash plus per-leg token holdings.
///
/// Legs are indexed, not named: index 0/1 are `up`/`down` for crypto and
/// `home`/`away` for a sports moneyline, index 2 is `draw`. `up_tokens()` /
/// `down_tokens()` are retained as accessors so crypto code and the dashboard
/// payloads read the same as before.
#[derive(Debug, Clone, PartialEq)]
pub struct Position {
    pub cash: f64,
    pub tokens: [f64; MAX_LEGS],
}

impl Position {
    pub fn new(cash: f64) -> Self {
        Self {
            cash,
            tokens: [0.0; MAX_LEGS],
        }
    }

    #[inline]
    pub fn leg(&self, i: usize) -> f64 {
        self.tokens.get(i).copied().unwrap_or(0.0)
    }

    #[inline]
    pub fn set_leg(&mut self, i: usize, v: f64) {
        if i < MAX_LEGS {
            self.tokens[i] = v;
        }
    }

    #[inline]
    pub fn add_leg(&mut self, i: usize, v: f64) {
        if i < MAX_LEGS {
            self.tokens[i] += v;
        }
    }

    #[inline]
    pub fn up_tokens(&self) -> f64 {
        self.tokens[0]
    }

    #[inline]
    pub fn down_tokens(&self) -> f64 {
        self.tokens[1]
    }

    /// Total tokens held across all legs.
    pub fn total_tokens(&self) -> f64 {
        self.tokens.iter().sum()
    }

    /// Cash view used when building an order.
    ///
    /// Crypto stores the bankroll on `position.cash`, so this is a clone.
    /// Sports stores the bankroll on the allocator and leaves `position.cash`
    /// at zero. The buy amount is that budget, and without it `build_order`
    /// refuses every sports buy as below the venue minimum. Sells still use
    /// the token balances on `position`.
    pub fn position_for_order(&self, action: &TradeAction) -> Self {
        let mut sizing = self.clone();
        if action.parsed_side().is_buy() {
            sizing.cash = sizing.cash.max(action.amount);
        }
        sizing
    }

    /// Compute the executable size for an action.
    ///
    /// Returns `(tokens, dollars)`. `(0.0, 0.0)` means "do not trade" — an
    /// unparseable side, a price outside `(0, 1)`, no cash, or a size below the
    /// venue minimum.
    pub fn compute_trade_size(
        &self,
        side: &str,
        price: Option<f64>,
        amount: f64,
        econ: &MarketEconomics,
    ) -> (f64, f64) {
        let price = match price {
            Some(p) if p > 0.0 && p < 1.0 => p,
            _ => return (0.0, 0.0),
        };

        match Side::parse(side) {
            Some(Side::Buy(_)) => {
                let dollars = amount.min(self.cash);
                if dollars <= 0.0 {
                    return (0.0, 0.0);
                }
                let tokens = round_to(dollars / price, econ.token_decimals);
                if tokens < econ.min_order_tokens {
                    return (0.0, 0.0);
                }
                let cost = tokens * price;
                (tokens, cost)
            }
            Some(Side::Sell(leg)) => {
                let available = self.leg(leg as usize);
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
        match Side::parse(side) {
            Some(Side::Buy(leg)) => {
                self.cash -= tokens * price;
                self.add_leg(leg as usize, tokens);
            }
            Some(Side::Sell(leg)) => {
                self.add_leg(leg as usize, -tokens);
                self.cash += tokens * price;
            }
            _ => {}
        }
    }

    /// Settle the session: the winning leg pays $1/token, everything else
    /// expires worthless.
    pub fn resolve_leg(&mut self, winning_leg: Option<usize>) {
        let leg = match winning_leg {
            Some(l) => l,
            None => return,
        };
        self.cash += self.leg(leg);
        self.tokens = [0.0; MAX_LEGS];
    }

    /// Crypto resolution by outcome name. Unknown outcomes leave the position
    /// untouched, matching the previous behaviour.
    pub fn resolve(&mut self, outcome: Option<&str>) {
        match outcome {
            Some("Up") => self.resolve_leg(Some(0)),
            Some("Down") => self.resolve_leg(Some(1)),
            _ => {}
        }
    }

    pub fn reserve_for_order(&mut self, side: &str, price: f64, tokens: f64) -> (f64, f64) {
        match Side::parse(side) {
            Some(Side::Buy(_)) => {
                let cost = tokens * price;
                self.cash -= cost;
                (cost, 0.0)
            }
            Some(Side::Sell(leg)) => {
                self.add_leg(leg as usize, -tokens);
                (0.0, tokens)
            }
            _ => (0.0, 0.0),
        }
    }

    pub fn unreserve_for_order(&mut self, side: &str, dollars_reserved: f64, tokens_reserved: f64) {
        match Side::parse(side) {
            Some(Side::Buy(_)) => {
                self.cash += dollars_reserved;
            }
            Some(Side::Sell(leg)) => {
                self.add_leg(leg as usize, tokens_reserved);
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
    /// Tradeable legs, in canonical index order. Empty means the crypto
    /// up/down pair, which `legs()` materialises from the token ids.
    pub legs: Vec<LegSpec>,
    /// Per-market fees, tick size and order-size limits.
    pub economics: MarketEconomics,
    /// Extra string fields from the raw metadata
    pub extra: HashMap<String, String>,
}

impl SessionMetadata {
    /// Legs for this session, defaulting to the crypto up/down pair.
    pub fn legs(&self) -> Vec<LegSpec> {
        if self.legs.is_empty() {
            LegSpec::crypto_pair(&self.up_token_id, &self.down_token_id)
        } else {
            self.legs.clone()
        }
    }

    /// Token id for a leg index.
    pub fn leg_token_id(&self, leg: usize) -> Option<String> {
        if self.legs.is_empty() {
            return match leg {
                0 => Some(self.up_token_id.clone()),
                1 => Some(self.down_token_id.clone()),
                _ => None,
            };
        }
        self.legs.get(leg).map(|l| l.token_id.clone())
    }
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
    pub economics: MarketEconomics,
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
        Self::with_economics(
            execution_mode,
            slippage,
            fees,
            limit_fill_margin,
            latency_ms,
            MarketEconomics::crypto(),
        )
    }

    pub fn with_economics(
        execution_mode: &str,
        slippage: f64,
        fees: bool,
        limit_fill_margin: Option<f64>,
        latency_ms: u64,
        economics: MarketEconomics,
    ) -> Self {
        Self {
            execution_mode: execution_mode.to_string(),
            slippage,
            fees,
            limit_fill_margin: limit_fill_margin.unwrap_or(slippage),
            latency_ms,
            economics,
            latency_seconds: latency_ms as f64 / 1000.0,
            sim_order_counter: 0,
        }
    }

    fn get_execution_price(&self, side: &str, sample: &Sample) -> Option<f64> {
        let parsed = Side::parse(side)?;
        let leg = parsed.leg()?;
        let is_buy = parsed.is_buy();

        match self.execution_mode.as_str() {
            "midpoint_slippage" => {
                let base = sample.leg_midpoint(leg)?;
                Some(if is_buy {
                    base + self.slippage
                } else {
                    base - self.slippage
                })
            }
            "midpoint" => sample.leg_midpoint(leg),
            "book" => {
                if is_buy {
                    sample.leg_best_ask(leg)
                } else {
                    sample.leg_midpoint(leg)
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
        let price = self
            .economics
            .round_price(price)
            .min(self.economics.max_token_price);

        let (tokens, dollars) =
            position.compute_trade_size(&action.side, Some(price), action.amount, &self.economics);
        if tokens <= 0.0 || tokens < self.economics.min_order_tokens {
            return None;
        }

        position.apply_trade(&action.side, price, tokens);
        let fee = self.economics.taker_fee(price, tokens, self.fees);
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

        let parsed = action.parsed_side();
        let leg = match parsed.leg() {
            Some(l) => l,
            None => return None,
        };

        let price = self.economics.round_price(action.price.unwrap());
        if price <= 0.0 || price >= 1.0 {
            return None;
        }

        let (mut tokens, _dollars) =
            position.compute_trade_size(&action.side, Some(price), action.amount, &self.economics);
        if tokens <= 0.0 || tokens < self.economics.min_order_tokens {
            return None;
        }

        // Floor buys to integer (same as ClobExecutor)
        if parsed.is_buy() {
            tokens = tokens.floor();
            if tokens < self.economics.min_order_tokens {
                return None;
            }
        }

        let (dollars_reserved, tokens_reserved) =
            position.reserve_for_order(&action.side, price, tokens);

        let expiration = action
            .expiration_seconds
            .map(|exp| sample.elapsed_seconds + exp);

        self.sim_order_counter += 1;

        Some(OpenOrder {
            order_id: format!("sim-{}", self.sim_order_counter),
            side: action.side.clone(),
            leg,
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

            let midpoint = match sample.leg_midpoint(order.leg) {
                Some(m) => m,
                None => {
                    remaining.push(order);
                    continue;
                }
            };

            let is_buy = Side::parse(&order.side).map(|s| s.is_buy()).unwrap_or(false);

            let filled = if is_buy {
                midpoint <= order.price - margin
            } else {
                let threshold = (order.price + margin).min(self.economics.max_token_price);
                midpoint >= threshold
            };

            if filled {
                // Apply trade to position. Funds were already reserved when the
                // order was placed, so only the acquired side moves here.
                if is_buy {
                    position.add_leg(order.leg, order.tokens);
                } else {
                    position.cash += order.tokens * order.price;
                }

                let fee = self.economics.taker_fee(order.price, order.tokens, self.fees);
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

        let parsed = action.parsed_side();

        // Cancel actions
        if let Side::Cancel(leg) = parsed {
            let leg = leg as usize;
            let position = self.position.as_mut().unwrap();
            let executor = &self.executor;
            self.open_orders.retain(|o| {
                if o.leg == leg {
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
        let action = if parsed.is_buy() {
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
            let (reserved_dollars, reserved_tokens) = match parsed {
                Side::Buy(_) => {
                    let rd = action.amount;
                    position.cash -= rd;
                    (rd, 0.0)
                }
                Side::Sell(leg) => {
                    let leg = leg as usize;
                    let rt = action.amount.min(position.leg(leg));
                    position.add_leg(leg, -rt);
                    (0.0, rt)
                }
                _ => (0.0, 0.0),
            };
            self.deferred_actions
                .push((target_elapsed, action, reserved_dollars, reserved_tokens));
            self.tick_index += 1;
            return None;
        }

        // Route: limit order vs FAK
        let trade = if action.price.is_some() {
            // Replace any existing order on the same leg
            self.replace_order_on_leg(&action);
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
            // Cancel deferred actions, returning each reservation to the leg it
            // came from. The previous implementation credited every deferred
            // sell back to `up_tokens` regardless of side, which silently
            // created up tokens (and destroyed down tokens) whenever a session
            // ended with a deferred `sell_down` in flight under --latency.
            for (_, action, rd, rt) in self.deferred_actions.drain(..) {
                position.cash += rd;
                if rt > 0.0 {
                    if let Some(leg) = action.leg() {
                        position.add_leg(leg, rt);
                    }
                }
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

    fn replace_order_on_leg(&mut self, action: &TradeAction) {
        let leg = match action.leg() {
            Some(l) => l,
            None => return,
        };
        let position = self.position.as_mut().unwrap();
        self.open_orders.retain(|order| {
            if order.leg == leg {
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
                match action.parsed_side() {
                    Side::Buy(_) => position.cash += rd,
                    Side::Sell(leg) => position.add_leg(leg as usize, rt),
                    _ => {}
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

    /// Crypto economics, matching the former global constants.
    fn econ_test() -> MarketEconomics {
        MarketEconomics::crypto()
    }

    #[test]
    fn test_position_new() {
        let p = Position::new(100.0);
        assert_eq!(p.cash, 100.0);
        assert_eq!(p.up_tokens(), 0.0);
        assert_eq!(p.down_tokens(), 0.0);
    }

    #[test]
    fn test_compute_trade_size_buy() {
        let p = Position::new(100.0);
        let (tokens, cost) = p.compute_trade_size("buy_up", Some(0.50), 100.0, &econ_test());
        assert!(tokens > 0.0);
        assert!((tokens - 200.0).abs() < 0.01);
        assert!((cost - 100.0).abs() < 0.01);
    }

    #[test]
    fn sports_buy_with_zero_position_cash_uses_the_action_budget() {
        let position = Position::new(0.0);
        let action = TradeAction::buy("buy_home", 25.0, 0.40, None);
        let (raw_tokens, _) =
            position.compute_trade_size("buy_home", Some(0.40), action.amount, &econ_test());
        assert_eq!(raw_tokens, 0.0);

        let sizing = position.position_for_order(&action);
        let (tokens, cost) =
            sizing.compute_trade_size("buy_home", Some(0.40), action.amount, &econ_test());
        assert!(tokens >= 5.0);
        assert!((cost - 25.0).abs() < 0.01);
        assert_eq!(position.cash, 0.0);
        assert_eq!(position.total_tokens(), 0.0);
    }

    #[test]
    fn sports_sell_still_uses_held_tokens_when_cash_is_zero() {
        let mut position = Position::new(0.0);
        position.set_leg(0, 12.0);
        let action = TradeAction::sell("sell_home", 12.0, 0.55);
        let sizing = position.position_for_order(&action);
        let (tokens, _) =
            sizing.compute_trade_size("sell_home", Some(0.55), action.amount, &econ_test());
        assert_eq!(tokens, 12.0);
        assert_eq!(sizing.cash, 0.0);
    }

    #[test]
    fn test_compute_trade_size_insufficient() {
        let p = Position::new(1.0);
        // 1.0 / 0.50 = 2.0 tokens, below min 5
        let (tokens, _) = p.compute_trade_size("buy_up", Some(0.50), 1.0, &econ_test());
        assert_eq!(tokens, 0.0);
    }

    #[test]
    fn test_compute_trade_size_sell() {
        let mut p = Position::new(0.0);
        p.set_leg(0, 100.0);
        let (tokens, proceeds) = p.compute_trade_size("sell_up", Some(0.80), 100.0, &econ_test());
        assert_eq!(tokens, 100.0);
        assert!((proceeds - 80.0).abs() < 0.01);
    }

    #[test]
    fn test_apply_trade() {
        let mut p = Position::new(100.0);
        p.apply_trade("buy_up", 0.50, 50.0);
        assert!((p.cash - 75.0).abs() < 0.01);
        assert!((p.up_tokens() - 50.0).abs() < 0.01);

        p.apply_trade("sell_up", 0.80, 50.0);
        assert!((p.up_tokens() - 0.0).abs() < 0.01);
        assert!((p.cash - 115.0).abs() < 0.01);
    }

    #[test]
    fn test_resolve_up() {
        let mut p = Position::new(0.0);
        p.set_leg(0, 100.0);
        p.set_leg(1, 50.0);
        p.resolve(Some("Up"));
        assert_eq!(p.cash, 100.0);
        assert_eq!(p.up_tokens(), 0.0);
        assert_eq!(p.down_tokens(), 0.0);
    }

    #[test]
    fn test_resolve_down() {
        let mut p = Position::new(0.0);
        p.set_leg(0, 100.0);
        p.set_leg(1, 50.0);
        p.resolve(Some("Down"));
        assert_eq!(p.cash, 50.0);
        assert_eq!(p.up_tokens(), 0.0);
        assert_eq!(p.down_tokens(), 0.0);
    }

    // -----------------------------------------------------------------
    // Side parsing / leg routing
    // -----------------------------------------------------------------

    #[test]
    fn side_parses_crypto_and_sports_keys() {
        assert_eq!(Side::parse("hold"), Some(Side::Hold));
        assert_eq!(Side::parse("buy_up"), Some(Side::Buy(0)));
        assert_eq!(Side::parse("buy_down"), Some(Side::Buy(1)));
        assert_eq!(Side::parse("sell_up"), Some(Side::Sell(0)));
        assert_eq!(Side::parse("sell_down"), Some(Side::Sell(1)));
        assert_eq!(Side::parse("cancel_up"), Some(Side::Cancel(0)));

        // Sports moneyline shares the same leg indices.
        assert_eq!(Side::parse("buy_home"), Some(Side::Buy(0)));
        assert_eq!(Side::parse("buy_away"), Some(Side::Buy(1)));
        assert_eq!(Side::parse("buy_draw"), Some(Side::Buy(2)));

        // Numeric form for legs without a well-known name (spread, total).
        assert_eq!(Side::parse("buy:3"), Some(Side::Buy(3)));
        assert_eq!(Side::parse("sell:7"), Some(Side::Sell(7)));
    }

    #[test]
    fn side_rejects_unknown_input_rather_than_guessing() {
        // The old code used `side.contains("up")`, so any of these would have
        // been routed to the up token. They must now refuse to route.
        for bad in [
            "buy_upside",
            "buy_uptown",
            "nonsense",
            "buy_",
            "",
            "buy:99",
            "trade_up",
        ] {
            assert_eq!(Side::parse(bad), None, "{bad} should not parse");
        }
    }

    #[test]
    fn unparseable_side_is_treated_as_hold_not_as_leg_zero() {
        let action = TradeAction::buy("buy_upside", 50.0, 0.5, None);
        assert_eq!(action.parsed_side(), Side::Hold);
        assert_eq!(action.leg(), None);

        // And it must move nothing.
        let mut p = Position::new(100.0);
        p.apply_trade("buy_upside", 0.5, 50.0);
        assert_eq!(p.cash, 100.0);
        assert_eq!(p.total_tokens(), 0.0);
    }

    #[test]
    fn side_round_trips_through_leg_labels() {
        let crypto = LegSpec::crypto_pair("t-up", "t-down");
        assert_eq!(Side::Buy(0).to_side_string(&crypto), "buy_up");
        assert_eq!(Side::Sell(1).to_side_string(&crypto), "sell_down");

        let sports = vec![
            LegSpec::new("home", "Chiefs", "t-h", "c1"),
            LegSpec::new("away", "Eagles", "t-a", "c1"),
            LegSpec::new("draw", "Draw", "t-d", "c1"),
        ];
        assert_eq!(Side::Buy(0).to_side_string(&sports), "buy_home");
        assert_eq!(Side::Buy(2).to_side_string(&sports), "buy_draw");
        assert_eq!(
            Side::parse(&Side::Buy(2).to_side_string(&sports)),
            Some(Side::Buy(2))
        );
    }

    #[test]
    fn position_handles_a_third_leg() {
        let mut p = Position::new(100.0);
        p.apply_trade("buy_draw", 0.25, 100.0);
        assert!((p.cash - 75.0).abs() < 1e-9);
        assert_eq!(p.leg(2), 100.0);
        assert_eq!(p.up_tokens(), 0.0);
        assert_eq!(p.down_tokens(), 0.0);

        p.resolve_leg(Some(2));
        assert!((p.cash - 175.0).abs() < 1e-9);
        assert_eq!(p.total_tokens(), 0.0);
    }

    #[test]
    fn resolve_leg_pays_only_the_winning_leg() {
        let mut p = Position::new(0.0);
        p.set_leg(0, 10.0);
        p.set_leg(1, 20.0);
        p.set_leg(2, 30.0);

        p.resolve_leg(Some(1));
        assert_eq!(p.cash, 20.0);
        assert_eq!(p.total_tokens(), 0.0);
    }

    #[test]
    fn resolve_leg_none_leaves_position_untouched() {
        let mut p = Position::new(5.0);
        p.set_leg(0, 10.0);
        p.resolve_leg(None);
        assert_eq!(p.cash, 5.0);
        assert_eq!(p.leg(0), 10.0);
    }

    #[test]
    fn sell_reserves_and_unreserves_the_correct_leg() {
        let mut p = Position::new(0.0);
        p.set_leg(1, 40.0);

        let (dollars, tokens) = p.reserve_for_order("sell_down", 0.6, 25.0);
        assert_eq!(dollars, 0.0);
        assert_eq!(tokens, 25.0);
        assert_eq!(p.leg(1), 15.0);
        assert_eq!(p.leg(0), 0.0, "reservation must not touch the up leg");

        p.unreserve_for_order("sell_down", 0.0, 25.0);
        assert_eq!(p.leg(1), 40.0);
        assert_eq!(p.leg(0), 0.0);
    }

    /// Regression: `end_session` used to credit every deferred sell back to
    /// `up_tokens` regardless of side, inventing up tokens and destroying down
    /// tokens whenever a session ended with a deferred `sell_down` in flight.
    #[test]
    fn deferred_sell_down_is_returned_to_the_down_leg_at_session_end() {
        struct SellDownOnce {
            sold: bool,
        }
        impl crate::strategy::Strategy for SellDownOnce {
            fn name(&self) -> &str {
                "SellDownOnce"
            }
            fn version(&self) -> &str {
                "1"
            }
            fn on_session_start(&mut self, _m: &SessionMetadata) {
                self.sold = false;
            }
            fn on_tick(&mut self, ctx: &TickContext) -> TradeAction {
                if !self.sold && ctx.position.down_tokens() > 0.0 {
                    self.sold = true;
                    return TradeAction::sell("sell_down", 50.0, 0.60);
                }
                hold()
            }
            fn with_params(
                &self,
                _p: &HashMap<String, crate::strategy::ParamValue>,
            ) -> Box<dyn crate::strategy::Strategy> {
                Box::new(SellDownOnce { sold: false })
            }
        }

        // Latency long enough that the deferred sell is still in flight when
        // the session ends.
        let executor = SimExecutor::new("midpoint", 0.0, false, None, 60_000);
        let mut trader = Trader::new(Box::new(SellDownOnce { sold: false }), executor);

        let mut position = Position::new(0.0);
        position.set_leg(1, 50.0);
        trader.start_session(SessionMetadata::default(), position, 0.0, 300.0);

        trader.process_tick(&make_sample(1.0, Some(0.40), Some(0.60)));

        // Mid-flight: the tokens are reserved out of the down leg.
        assert_eq!(trader.position().unwrap().leg(1), 0.0);
        assert_eq!(trader.position().unwrap().leg(0), 0.0);

        trader.end_session(None);

        let p = trader.position().unwrap();
        assert_eq!(p.leg(1), 50.0, "down tokens must be returned to the down leg");
        assert_eq!(p.leg(0), 0.0, "no up tokens may be invented");
    }

    #[test]
    fn compute_trade_size_respects_per_market_min_order_size() {
        let p = Position::new(100.0);
        let mut econ = MarketEconomics::crypto();

        // 100 / 0.5 = 200 tokens, comfortably above the crypto minimum of 5.
        let (tokens, _) = p.compute_trade_size("buy_up", Some(0.50), 100.0, &econ);
        assert!((tokens - 200.0).abs() < 0.01);

        econ.min_order_tokens = 500.0;
        let (tokens, _) = p.compute_trade_size("buy_up", Some(0.50), 100.0, &econ);
        assert_eq!(tokens, 0.0, "must refuse a size below the venue minimum");
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
        assert_eq!(p.compute_trade_size("buy_up", Some(0.0), 100.0, &econ_test()), (0.0, 0.0));
        assert_eq!(p.compute_trade_size("buy_up", Some(1.0), 100.0, &econ_test()), (0.0, 0.0));
        assert_eq!(p.compute_trade_size("buy_up", None, 100.0, &econ_test()), (0.0, 0.0));
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
