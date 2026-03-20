//! RegimeSniper — Drift + velocity + consistency filter, sliding max_price.

use crate::defaults::BPS_DIVISOR;
use crate::math_utils::ols_slope_bps_timed;
use crate::strategy::*;
use crate::trader::*;
use std::collections::HashMap;

pub struct RegimeSniper {
    // Tunable
    pub min_elapsed: f64,
    pub min_displacement_bps: f64,
    pub min_consistency: f64,
    pub max_price: f64,
    pub price_slope: f64,
    // Fixed
    pub consistency_window_ms: f64,
    pub velocity_window_ms: f64,
    pub min_velocity_bps_s: f64,
    pub no_new_after: f64,
    pub bet_fraction: f64,
    // State
    btc_prices: Vec<f64>,
    btc_times: Vec<f64>,
    midpoint_signs: Vec<i32>,
    midpoint_times: Vec<f64>,
    entry_midpoint: f64,
    entry_count: i64,
    last_sell_time_s: f64,
    tick_logs: Vec<String>,
}

impl Default for RegimeSniper {
    fn default() -> Self {
        Self {
            min_elapsed: 0.25,
            min_displacement_bps: 8.0,
            min_consistency: 0.55,
            max_price: 0.49,
            price_slope: 0.50,
            consistency_window_ms: 5500.0,
            velocity_window_ms: 1000.0,
            min_velocity_bps_s: 0.55,
            no_new_after: 0.95,
            bet_fraction: 1.0,
            btc_prices: Vec::new(),
            btc_times: Vec::new(),
            midpoint_signs: Vec::new(),
            midpoint_times: Vec::new(),
            entry_midpoint: 0.0,
            entry_count: 0,
            last_sell_time_s: -999.0,
            tick_logs: Vec::new(),
        }
    }
}

impl RegimeSniper {
    fn reset_state(&mut self) {
        self.btc_prices.clear();
        self.btc_times.clear();
        self.midpoint_signs.clear();
        self.midpoint_times.clear();
        self.entry_midpoint = 0.0;
        self.entry_count = 0;
        self.last_sell_time_s = -999.0;
    }

    fn compute_consistency(&self, direction: &str, elapsed_seconds: f64) -> f64 {
        let cutoff = elapsed_seconds - self.consistency_window_ms / 1000.0;
        let recent: Vec<i32> = self
            .midpoint_times
            .iter()
            .zip(self.midpoint_signs.iter())
            .filter(|(&t, _)| t >= cutoff)
            .map(|(_, &s)| s)
            .collect();

        if recent.is_empty() {
            return 0.0;
        }

        let count = if direction == "up" {
            recent.iter().filter(|&&v| v > 0).count()
        } else {
            recent.iter().filter(|&&v| v < 0).count()
        };

        count as f64 / recent.len() as f64
    }

    fn effective_max_price(&self, elapsed_fraction: f64) -> f64 {
        let time_past_min = (elapsed_fraction - self.min_elapsed).max(0.0);
        (self.max_price + self.price_slope * time_past_min).min(0.92)
    }
}

impl Strategy for RegimeSniper {
    fn name(&self) -> &str {
        "RegimeSniper"
    }
    fn version(&self) -> &str {
        "8"
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

        if let Some(um) = up_mid {
            self.midpoint_signs.push(if um > 0.50 { 1 } else { -1 });
            self.midpoint_times.push(ctx.elapsed_seconds);
        }

        let _btc = match btc {
            Some(v) => v,
            None => return hold(),
        };
        let up_mid = match up_mid {
            Some(v) => v,
            None => return hold(),
        };
        let down_mid = match down_mid {
            Some(v) => v,
            None => return hold(),
        };

        // Position held: hold to resolution (no stop-loss)
        if held_side(ctx.position).is_some() {
            return hold();
        }

        // Buy order expired without filling
        check_expired_entry(&mut self.entry_midpoint, ctx.position);

        // Single entry per session
        if self.entry_count > 0 {
            return hold();
        }

        // Timing guards
        if check_timing_guards(ctx.elapsed_fraction, self.min_elapsed, self.no_new_after) {
            return hold();
        }

        let velocity_window_s = self.velocity_window_ms / 1000.0;
        if ctx.initial_btc_price <= 0.0 || ctx.elapsed_seconds < velocity_window_s {
            return hold();
        }

        // Signal 1: BTC displacement
        let current_btc = match self.btc_prices.last() {
            Some(v) => *v,
            None => return hold(),
        };
        let displacement_bps =
            (current_btc - ctx.initial_btc_price) / ctx.initial_btc_price * BPS_DIVISOR;

        if displacement_bps.abs() < self.min_displacement_bps {
            return hold();
        }

        let direction = if displacement_bps > 0.0 { "up" } else { "down" };

        // Signal 2: BTC velocity confirms
        let cutoff = ctx.elapsed_seconds - velocity_window_s;
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

        if (displacement_bps > 0.0) != (velocity > 0.0) {
            return hold();
        }

        // Signal 3: Midpoint consistency
        let consistency = self.compute_consistency(direction, ctx.elapsed_seconds);
        if consistency < self.min_consistency {
            return hold();
        }

        // Price check (sliding)
        let signal_mid = if direction == "up" { up_mid } else { down_mid };
        let eff_max = self.effective_max_price(ctx.elapsed_fraction);
        if signal_mid > eff_max {
            return hold();
        }

        self.tick_logs.push(format!(
            "t={:.1}s disp={:.1}bps vel={:.2}bps/s consist={:.2} dir={} mid={:.3} eff_max={:.3}",
            ctx.elapsed_seconds, displacement_bps, velocity, consistency, direction, signal_mid, eff_max
        ));

        // All signals confirmed -- ENTER
        self.entry_count += 1;
        self.entry_midpoint = signal_mid;
        self.tick_logs.push(format!("ENTRY {} @{:.3}", direction, signal_mid));

        let side = if direction == "up" {
            "buy_up"
        } else {
            "buy_down"
        };
        let dollars = ctx.position.cash * self.bet_fraction;

        TradeAction::buy(side, dollars, signal_mid, Some(10.0))
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
        m.insert("min_elapsed".into(), Param::float(0.25, 0.15, 0.40));
        m.insert("min_displacement_bps".into(), Param::float(8.0, 5.0, 12.0));
        m.insert("min_consistency".into(), Param::float(0.55, 0.40, 0.70));
        m.insert("max_price".into(), Param::float(0.49, 0.45, 0.55));
        m.insert("price_slope".into(), Param::float(0.50, 0.35, 0.65));
        m.insert("consistency_window_ms".into(), Param::fixed_float(5500.0));
        m.insert("velocity_window_ms".into(), Param::fixed_float(1000.0));
        m.insert("min_velocity_bps_s".into(), Param::fixed_float(0.55));
        m.insert("no_new_after".into(), Param::fixed_float(0.95));
        m.insert("bet_fraction".into(), Param::fixed_float(1.0));
        m
    }

    fn params(&self) -> HashMap<String, ParamValue> {
        let mut m = HashMap::new();
        m.insert("min_elapsed".into(), ParamValue::Float(self.min_elapsed));
        m.insert("min_displacement_bps".into(), ParamValue::Float(self.min_displacement_bps));
        m.insert("min_consistency".into(), ParamValue::Float(self.min_consistency));
        m.insert("max_price".into(), ParamValue::Float(self.max_price));
        m.insert("price_slope".into(), ParamValue::Float(self.price_slope));
        m
    }

    fn with_params(&self, params: &HashMap<String, ParamValue>) -> Box<dyn Strategy> {
        let mut s = RegimeSniper::default();
        for (k, v) in params {
            if let Some(f) = v.as_f64() {
                match k.as_str() {
                    "min_elapsed" => s.min_elapsed = f,
                    "min_displacement_bps" => s.min_displacement_bps = f,
                    "min_consistency" => s.min_consistency = f,
                    "max_price" => s.max_price = f,
                    "price_slope" => s.price_slope = f,
                    "consistency_window_ms" => s.consistency_window_ms = f,
                    "velocity_window_ms" => s.velocity_window_ms = f,
                    "min_velocity_bps_s" => s.min_velocity_bps_s = f,
                    "no_new_after" => s.no_new_after = f,
                    "bet_fraction" => s.bet_fraction = f,
                    _ => {}
                }
            }
        }
        Box::new(s)
    }
}
