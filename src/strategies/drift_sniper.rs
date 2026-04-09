//! DriftSniper — BTC momentum with OLS velocity confirmation.
//!
//! **EXAMPLE STRATEGY** — Provided for illustration only. There is no evidence
//! that this strategy is profitable. Use it as a starting point for your own
//! research, not as trading advice.

use crate::defaults::BPS_DIVISOR;
use crate::math_utils::ols_slope_bps_timed;
use crate::strategy::*;
use crate::trader::*;
use std::collections::HashMap;

pub struct DriftSniper {
    // Tunable
    pub return_threshold_bps: f64,
    pub velocity_window_ms: f64,
    pub min_velocity_bps_s: f64,
    pub min_elapsed: f64,
    pub max_price: f64,
    pub stop_loss_pct: Option<f64>,
    // Fixed
    pub no_new_after: f64,
    pub bet_fraction: f64,
    // State
    btc_prices: Vec<f64>,
    btc_times: Vec<f64>,
    entry_midpoint: f64,
    entry_count: i64,
    last_sell_time_s: f64,
    tick_logs: Vec<String>,
}

impl Default for DriftSniper {
    fn default() -> Self {
        Self {
            return_threshold_bps: 4.0,
            velocity_window_ms: 2000.0,
            min_velocity_bps_s: 0.36,
            min_elapsed: 0.50,
            max_price: 0.92,
            stop_loss_pct: Some(0.06),
            no_new_after: 0.95,
            bet_fraction: 1.0,
            btc_prices: Vec::new(),
            btc_times: Vec::new(),
            entry_midpoint: 0.0,
            entry_count: 0,
            last_sell_time_s: -999.0,
            tick_logs: Vec::new(),
        }
    }
}

impl DriftSniper {
    fn reset_state(&mut self) {
        self.btc_prices.clear();
        self.btc_times.clear();
        self.entry_midpoint = 0.0;
        self.entry_count = 0;
        self.last_sell_time_s = -999.0;
    }
}

impl Strategy for DriftSniper {
    fn name(&self) -> &str {
        "DriftSniper"
    }
    fn version(&self) -> &str {
        "9"
    }
    fn requires_full_session(&self) -> bool {
        true
    }

    fn on_session_start(&mut self, _metadata: &SessionMetadata) {
        self.reset_state();
    }

    fn on_tick(&mut self, ctx: &TickContext) -> TradeAction {
        let btc = ctx.sample.btc_price_usd;
        let up_mid = ctx.sample.up_token_midpoint;
        let down_mid = ctx.sample.down_token_midpoint;

        if let Some(b) = btc {
            if b > 0.0 {
                self.btc_prices.push(b);
                self.btc_times.push(ctx.elapsed_seconds);
            }
        }

        let up_mid = match up_mid {
            Some(v) => v,
            None => return hold(),
        };
        let down_mid = match down_mid {
            Some(v) => v,
            None => return hold(),
        };

        // Manage existing position -- stop-loss
        if held_side(ctx.position).is_some() {
            if let Some(exit) = check_exit(
                ctx,
                up_mid,
                down_mid,
                self.entry_midpoint,
                self.stop_loss_pct,
                None,
                self.last_sell_time_s,
                2000.0,
            ) {
                return exit;
            }
            return hold();
        }

        // Buy order expired without filling
        check_expired_entry(&mut self.entry_midpoint, ctx.position);

        // Only one entry per session
        if self.entry_count > 0 {
            return hold();
        }

        // Timing guards
        if check_timing_guards(ctx.elapsed_fraction, self.min_elapsed, self.no_new_after) {
            return hold();
        }

        // Need initial BTC price and enough history
        let window_s = self.velocity_window_ms / 1000.0;
        if ctx.initial_btc_price <= 0.0 || ctx.elapsed_seconds < window_s {
            return hold();
        }

        // BTC return from session start
        let current_btc = match self.btc_prices.last() {
            Some(v) => *v,
            None => return hold(),
        };
        let btc_return_bps =
            (current_btc - ctx.initial_btc_price) / ctx.initial_btc_price * BPS_DIVISOR;

        if btc_return_bps.abs() < self.return_threshold_bps {
            return hold();
        }

        // Velocity must confirm direction -- OLS over time window
        let cutoff = ctx.elapsed_seconds - window_s;
        let idx = self
            .btc_times
            .iter()
            .position(|&t| t >= cutoff)
            .unwrap_or(self.btc_times.len());

        let velocity =
            match ols_slope_bps_timed(&self.btc_times[idx..], &self.btc_prices[idx..]) {
                Some(v) => v,
                None => return hold(),
            };

        if velocity.abs() < self.min_velocity_bps_s {
            return hold();
        }

        // Return and velocity must agree on direction
        if (btc_return_bps > 0.0) != (velocity > 0.0) {
            return hold();
        }

        self.tick_logs.push(format!(
            "t={:.1}s btc_ret={:.1}bps vel={:.2}bps/s up_mid={:.3} down_mid={:.3}",
            ctx.elapsed_seconds, btc_return_bps, velocity, up_mid, down_mid
        ));

        // BTC going up -> buy UP, BTC going down -> buy DOWN
        if btc_return_bps > 0.0 {
            if up_mid <= self.max_price {
                self.entry_count += 1;
                self.entry_midpoint = up_mid;
                self.tick_logs.push(format!("ENTRY buy_up @{:.3}", up_mid));
                return TradeAction::buy(
                    "buy_up",
                    ctx.position.cash * self.bet_fraction,
                    up_mid,
                    Some(10.0),
                );
            }
        } else if down_mid <= self.max_price {
            self.entry_count += 1;
            self.entry_midpoint = down_mid;
            self.tick_logs.push(format!("ENTRY buy_down @{:.3}", down_mid));
            return TradeAction::buy(
                "buy_down",
                ctx.position.cash * self.bet_fraction,
                down_mid,
                Some(10.0),
            );
        }

        hold()
    }

    fn on_trade_failed(&mut self, _side: &str) {
        self.entry_count = 0;
        self.entry_midpoint = 0.0;
    }

    fn drain_logs(&mut self) -> Vec<String> {
        std::mem::take(&mut self.tick_logs)
    }

    fn param_defs(&self) -> HashMap<String, Param> {
        let mut m = HashMap::new();
        m.insert("return_threshold_bps".into(), Param::float(4.0, 2.0, 15.0));
        m.insert("velocity_window_ms".into(), Param::float(2000.0, 1000.0, 10000.0));
        m.insert("min_velocity_bps_s".into(), Param::float(0.36, 0.18, 0.91));
        m.insert("min_elapsed".into(), Param::float(0.50, 0.20, 0.70));
        m.insert("max_price".into(), Param::float(0.92, 0.85, 0.96));
        m.insert("stop_loss_pct".into(), Param::float_nullable(0.06, 0.04, 0.15));
        m.insert("no_new_after".into(), Param::fixed_float(0.95));
        m.insert("bet_fraction".into(), Param::fixed_float(1.0));
        m
    }

    fn params(&self) -> HashMap<String, ParamValue> {
        let mut m = HashMap::new();
        m.insert("return_threshold_bps".into(), ParamValue::Float(self.return_threshold_bps));
        m.insert("velocity_window_ms".into(), ParamValue::Float(self.velocity_window_ms));
        m.insert("min_velocity_bps_s".into(), ParamValue::Float(self.min_velocity_bps_s));
        m.insert("min_elapsed".into(), ParamValue::Float(self.min_elapsed));
        m.insert("max_price".into(), ParamValue::Float(self.max_price));
        match self.stop_loss_pct {
            Some(v) => { m.insert("stop_loss_pct".into(), ParamValue::Float(v)); }
            None => { m.insert("stop_loss_pct".into(), ParamValue::None); }
        };
        m
    }

    fn with_params(&self, params: &HashMap<String, ParamValue>) -> Box<dyn Strategy> {
        let mut s = DriftSniper::default();
        for (k, v) in params {
            match k.as_str() {
                "return_threshold_bps" => { if let Some(f) = v.as_f64() { s.return_threshold_bps = f; } }
                "velocity_window_ms" => { if let Some(f) = v.as_f64() { s.velocity_window_ms = f; } }
                "min_velocity_bps_s" => { if let Some(f) = v.as_f64() { s.min_velocity_bps_s = f; } }
                "min_elapsed" => { if let Some(f) = v.as_f64() { s.min_elapsed = f; } }
                "max_price" => { if let Some(f) = v.as_f64() { s.max_price = f; } }
                "stop_loss_pct" => {
                    match v {
                        ParamValue::None => s.stop_loss_pct = None,
                        _ => { if let Some(f) = v.as_f64() { s.stop_loss_pct = Some(f); } }
                    }
                }
                "no_new_after" => { if let Some(f) = v.as_f64() { s.no_new_after = f; } }
                "bet_fraction" => { if let Some(f) = v.as_f64() { s.bet_fraction = f; } }
                _ => {}
            }
        }
        Box::new(s)
    }
}
