//! Base strategy trait and parameter system.

use crate::trader::{hold, Position, SessionMetadata, TickContext, TradeAction};
use std::collections::HashMap;

/// Declarative parameter descriptor for strategy tuning.
#[derive(Debug, Clone)]
pub struct Param {
    pub default: ParamValue,
    pub min: Option<f64>,
    pub max: Option<f64>,
    pub nullable: bool,
    pub tunable: bool,
}

#[derive(Debug, Clone, PartialEq)]
pub enum ParamValue {
    Float(f64),
    Int(i64),
    Bool(bool),
    None,
}

impl ParamValue {
    pub fn as_f64(&self) -> Option<f64> {
        match self {
            ParamValue::Float(v) => Some(*v),
            ParamValue::Int(v) => Some(*v as f64),
            _ => None,
        }
    }

    pub fn as_i64(&self) -> Option<i64> {
        match self {
            ParamValue::Int(v) => Some(*v),
            ParamValue::Float(v) => Some(*v as i64),
            _ => None,
        }
    }
}

impl Param {
    pub fn float(default: f64, min: f64, max: f64) -> Self {
        Self {
            default: ParamValue::Float(default),
            min: Some(min),
            max: Some(max),
            nullable: false,
            tunable: true,
        }
    }

    pub fn float_nullable(default: f64, min: f64, max: f64) -> Self {
        Self {
            default: ParamValue::Float(default),
            min: Some(min),
            max: Some(max),
            nullable: true,
            tunable: true,
        }
    }

    pub fn int(default: i64, min: i64, max: i64) -> Self {
        Self {
            default: ParamValue::Int(default),
            min: Some(min as f64),
            max: Some(max as f64),
            nullable: false,
            tunable: true,
        }
    }

    pub fn fixed_float(default: f64) -> Self {
        Self {
            default: ParamValue::Float(default),
            min: None,
            max: None,
            nullable: false,
            tunable: false,
        }
    }

    pub fn fixed_int(default: i64) -> Self {
        Self {
            default: ParamValue::Int(default),
            min: None,
            max: None,
            nullable: false,
            tunable: false,
        }
    }

    /// Generate n evenly-spaced grid values between min and max.
    pub fn grid(&self, n: usize) -> Vec<ParamValue> {
        let mut values = Vec::new();
        if self.nullable {
            values.push(ParamValue::None);
        }

        match (&self.min, &self.max) {
            (Some(min), Some(max)) if min != max => {
                let is_int = matches!(self.default, ParamValue::Int(_));
                let n = n.max(2);

                if is_int {
                    let min_i = *min as i64;
                    let max_i = *max as i64;
                    let all: Vec<i64> = (min_i..=max_i).collect();
                    if all.len() <= n {
                        for v in all {
                            values.push(ParamValue::Int(v));
                        }
                    } else {
                        let mut indices: Vec<usize> = (0..n)
                            .map(|i| ((i as f64 * (all.len() - 1) as f64 / (n - 1) as f64).round()) as usize)
                            .collect();
                        indices.dedup();
                        for idx in indices {
                            values.push(ParamValue::Int(all[idx]));
                        }
                    }
                } else {
                    let step = (max - min) / (n - 1) as f64;
                    let mut numeric: Vec<f64> = (0..n)
                        .map(|i| round6(min + step * i as f64))
                        .collect();

                    // Ensure default is included
                    if let ParamValue::Float(def) = self.default {
                        if !numeric.contains(&def) && def >= *min && def <= *max {
                            let nearest_idx = numeric
                                .iter()
                                .enumerate()
                                .min_by(|(_, a), (_, b)| {
                                    ((**a - def).abs())
                                        .partial_cmp(&((**b - def).abs()))
                                        .unwrap()
                                })
                                .map(|(i, _)| i)
                                .unwrap();
                            numeric[nearest_idx] = def;
                            numeric.sort_by(|a, b| a.partial_cmp(b).unwrap());
                        }
                    }

                    for v in numeric {
                        values.push(ParamValue::Float(v));
                    }
                }
            }
            _ => {
                values.push(self.default.clone());
            }
        }

        values
    }
}

fn round6(x: f64) -> f64 {
    (x * 1_000_000.0).round() / 1_000_000.0
}

/// Core strategy trait. All trading strategies implement this.
pub trait Strategy: Send {
    fn name(&self) -> &str;
    fn version(&self) -> &str;
    fn requires_full_session(&self) -> bool {
        false
    }
    fn min_elapsed_hint(&self) -> f64 {
        0.0
    }

    fn on_session_start(&mut self, metadata: &SessionMetadata);
    fn on_tick(&mut self, ctx: &TickContext) -> TradeAction;

    /// Drain accumulated tick log messages (no-op by default).
    fn drain_logs(&mut self) -> Vec<String> {
        Vec::new()
    }

    /// Get parameter definitions for grid search.
    fn param_defs(&self) -> HashMap<String, Param> {
        HashMap::new()
    }

    /// Get current parameter values.
    fn params(&self) -> HashMap<String, ParamValue> {
        HashMap::new()
    }

    /// Called when a trade execution fails.  Lets the strategy undo
    /// internal bookkeeping (entry_count, last_bought_side, etc.) so a
    /// failed CLOB call doesn't burn the session or poison the circuit breaker.
    fn on_trade_failed(&mut self, _side: &str) {}

    /// Clone this strategy with different parameters.
    fn with_params(&self, params: &HashMap<String, ParamValue>) -> Box<dyn Strategy>;

    /// Generate parameter grid for optimization.
    fn param_grid(&self, max_variants: usize) -> HashMap<String, Vec<ParamValue>> {
        let defs = self.param_defs();
        let tunable: HashMap<&String, &Param> = defs
            .iter()
            .filter(|(_, p)| {
                p.tunable
                    && ((p.min.is_some() && p.max.is_some() && p.min != p.max) || p.nullable)
            })
            .collect();

        if tunable.is_empty() {
            return HashMap::new();
        }

        // Target 3 points per param, scale down to 2 if too many
        for n in [3usize, 2] {
            let total: usize = tunable.values().map(|p| p.grid(n).len()).product();
            if total <= max_variants {
                let mut grid = HashMap::new();
                for (name, p) in &tunable {
                    let values = p.grid(n);
                    if values.len() > 1 {
                        grid.insert((*name).clone(), values);
                    }
                }
                return grid;
            }
        }

        // Fallback: just return 2-point grids
        let mut grid = HashMap::new();
        for (name, p) in &tunable {
            let values = p.grid(2);
            if values.len() > 1 {
                grid.insert((*name).clone(), values);
            }
        }
        grid
    }
}

/// Index of the first leg with a non-zero holding.
///
/// This is the generic primitive — sports strategies should use it rather than
/// `held_side`, whose `"up"`/`"down"` naming only makes sense for the crypto
/// market.
pub fn held_leg(position: &Position) -> Option<usize> {
    (0..crate::trader::MAX_LEGS).find(|&i| position.leg(i) > 0.0)
}

/// Helper methods for strategies (equivalent to Python Strategy base class methods).
///
/// Crypto-specific: maps leg 0 -> `"up"` and leg 1 -> `"down"`. Returns `None`
/// for holdings on any other leg, so a sports position can never be mistaken
/// for an up/down one.
pub fn held_side(position: &Position) -> Option<&'static str> {
    match held_leg(position) {
        Some(0) => Some("up"),
        Some(1) => Some("down"),
        _ => None,
    }
}

pub fn has_position(position: &Position) -> bool {
    held_leg(position).is_some()
}

/// Check stop-loss and take-profit for an existing position.
pub fn check_exit(
    ctx: &TickContext,
    up_mid: f64,
    down_mid: f64,
    entry_midpoint: f64,
    stop_loss_pct: Option<f64>,
    take_profit_pct: Option<f64>,
    last_trade_time_s: f64,
    cooldown_ms: f64,
) -> Option<TradeAction> {
    let held = held_side(ctx.position)?;

    if entry_midpoint <= 0.0 {
        return None;
    }

    let current_mid = if held == "up" { up_mid } else { down_mid };
    let sell_side = if held == "up" { "sell_up" } else { "sell_down" };
    let tokens = if held == "up" {
        ctx.position.up_tokens()
    } else {
        ctx.position.down_tokens()
    };

    let cooldown_s = cooldown_ms / 1000.0;

    // Stop-loss
    if let Some(sl) = stop_loss_pct {
        if current_mid <= entry_midpoint * (1.0 - sl) {
            if ctx.elapsed_seconds - last_trade_time_s < cooldown_s {
                return Some(hold());
            }
            return Some(TradeAction {
                side: sell_side.to_string(),
                amount: tokens,
                price: Some(current_mid),
                expiration_seconds: Some(crate::defaults::DEFAULT_EXPIRATION_SECONDS),
                cancel_after_s: None,
            });
        }
    }

    // Take-profit
    if let Some(tp) = take_profit_pct {
        if current_mid >= entry_midpoint * (1.0 + tp) {
            if ctx.elapsed_seconds - last_trade_time_s < cooldown_s {
                return Some(hold());
            }
            return Some(TradeAction {
                side: sell_side.to_string(),
                amount: tokens,
                price: Some(current_mid),
                expiration_seconds: Some(crate::defaults::DEFAULT_EXPIRATION_SECONDS),
                cancel_after_s: None,
            });
        }
    }

    None
}

/// Check if entry should be blocked by timing guards.
pub fn check_timing_guards(elapsed_fraction: f64, min_elapsed: f64, no_new_after: f64) -> bool {
    elapsed_fraction < min_elapsed || elapsed_fraction >= no_new_after
}

/// Reset entry state if a buy order expired without filling.
pub fn check_expired_entry(
    entry_midpoint: &mut f64,
    position: &Position,
) {
    if *entry_midpoint > 0.0 && held_side(position).is_none() {
        *entry_midpoint = 0.0;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_param_grid_float() {
        let p = Param::float(0.5, 0.0, 1.0);
        let grid = p.grid(3);
        assert_eq!(grid.len(), 3);
        assert_eq!(grid[0], ParamValue::Float(0.0));
        assert_eq!(grid[1], ParamValue::Float(0.5));
        assert_eq!(grid[2], ParamValue::Float(1.0));
    }

    #[test]
    fn test_param_grid_int() {
        let p = Param::int(3, 1, 5);
        let grid = p.grid(3);
        assert!(grid.len() >= 3);
    }

    #[test]
    fn test_param_grid_nullable() {
        let p = Param::float_nullable(0.5, 0.0, 1.0);
        let grid = p.grid(3);
        assert_eq!(grid[0], ParamValue::None);
        assert!(grid.len() >= 4); // None + 3 values
    }

    #[test]
    fn test_param_grid_fixed() {
        let p = Param::fixed_float(0.5);
        let grid = p.grid(3);
        assert_eq!(grid.len(), 1);
        assert_eq!(grid[0], ParamValue::Float(0.5));
    }

    #[test]
    fn test_held_side() {
        let p = Position::new(100.0);
        assert_eq!(held_side(&p), None);

        let mut p2 = Position::new(0.0);
        p2.set_leg(0, 50.0);
        assert_eq!(held_side(&p2), Some("up"));

        let mut p3 = Position::new(0.0);
        p3.set_leg(1, 50.0);
        assert_eq!(held_side(&p3), Some("down"));
    }

    #[test]
    fn test_check_timing_guards() {
        assert!(check_timing_guards(0.3, 0.5, 0.95));
        assert!(!check_timing_guards(0.6, 0.5, 0.95));
        assert!(check_timing_guards(0.96, 0.5, 0.95));
    }
}
