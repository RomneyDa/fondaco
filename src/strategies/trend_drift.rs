//! TrendDrift — Regime-gated trend follower (trending + medium/high vol).

use crate::defaults::BPS_DIVISOR;
use crate::math_utils::ols_slope_bps_timed;
use crate::strategy::*;
use crate::trader::*;
use crate::volatility::*;
use std::collections::HashMap;

pub struct TrendDrift {
    // Tunable
    pub return_threshold_bps: f64,
    pub min_elapsed: f64,
    pub max_price: f64,
    pub velocity_window_ms: f64,
    pub min_velocity_bps_s: f64,
    // Fixed
    pub no_new_after: f64,
    // Regime gate
    regime_active: bool,
    // State
    btc_prices: Vec<f64>,
    btc_times: Vec<f64>,
    entry_midpoint: f64,
    entry_count: i64,
    tick_logs: Vec<String>,
}

impl Default for TrendDrift {
    fn default() -> Self {
        Self {
            return_threshold_bps: 12.0,
            min_elapsed: 0.55,
            max_price: 0.75,
            velocity_window_ms: 2000.0,
            min_velocity_bps_s: 0.15,
            no_new_after: 0.95,
            regime_active: false,
            btc_prices: Vec::new(),
            btc_times: Vec::new(),
            entry_midpoint: 0.0,
            entry_count: 0,
            tick_logs: Vec::new(),
        }
    }
}

impl TrendDrift {
    fn reset_state(&mut self) {
        self.btc_prices.clear();
        self.btc_times.clear();
        self.entry_midpoint = 0.0;
        self.entry_count = 0;
    }

    fn check_regime(&mut self, metadata: &SessionMetadata) {
        let regime = classify_regime(
            &metadata.prior_session_summaries,
            20,
            10,
            metadata.gap_before_seconds,
            metadata.data_density,
            Some(0.5),
            Some(600.0),
        );

        // Skip stale data
        if regime.data_quality == "stale" {
            self.regime_active = false;
            return;
        }

        // Required vol: medium or high
        let vol_ok = match &regime.vol {
            Some(VolRegime::Medium) | Some(VolRegime::High) => true,
            _ => false,
        };

        // Required trend: trending
        let trend_ok = match &regime.trend {
            Some(TrendRegime::Trending) => true,
            _ => false,
        };

        self.regime_active = vol_ok && trend_ok;
    }
}

impl Strategy for TrendDrift {
    fn name(&self) -> &str {
        "TrendDrift"
    }
    fn version(&self) -> &str {
        "2"
    }
    fn requires_full_session(&self) -> bool {
        true
    }

    fn on_session_start(&mut self, metadata: &SessionMetadata) {
        self.check_regime(metadata);
        self.reset_state();
    }

    fn on_tick(&mut self, ctx: &TickContext) -> TradeAction {
        if !self.regime_active {
            return hold();
        }

        let btc = ctx.sample.btc_price_usd;
        let up_mid = ctx.sample.up_token_midpoint;
        let down_mid = ctx.sample.down_token_midpoint;

        if let Some(b) = btc {
            if b > 0.0 {
                self.btc_prices.push(b);
                self.btc_times.push(ctx.elapsed_seconds);
            }
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

        // Already holding -- hold to resolution (no stop-loss)
        if held_side(ctx.position).is_some() {
            return hold();
        }

        check_expired_entry(&mut self.entry_midpoint, ctx.position);
        if self.entry_count > 0 {
            return hold();
        }

        if check_timing_guards(ctx.elapsed_fraction, self.min_elapsed, self.no_new_after) {
            return hold();
        }

        let window_s = self.velocity_window_ms / 1000.0;
        if ctx.initial_btc_price <= 0.0 || ctx.elapsed_seconds < window_s {
            return hold();
        }

        // BTC drift from session start
        let current_btc = match self.btc_prices.last() {
            Some(v) => *v,
            None => return hold(),
        };
        let drift_bps =
            (current_btc - ctx.initial_btc_price) / ctx.initial_btc_price * BPS_DIVISOR;

        if drift_bps.abs() < self.return_threshold_bps {
            return hold();
        }

        // Velocity confirmation
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

        if (drift_bps > 0.0) != (velocity > 0.0) {
            return hold();
        }

        // Price check
        let direction = if drift_bps > 0.0 { "up" } else { "down" };
        let signal_mid = if direction == "up" { up_mid } else { down_mid };
        if signal_mid > self.max_price {
            return hold();
        }

        self.tick_logs.push(format!(
            "t={:.1}s drift={:.1}bps vel={:.2}bps/s dir={} mid={:.3}",
            ctx.elapsed_seconds, drift_bps, velocity, direction, signal_mid
        ));

        // Enter
        self.entry_count += 1;
        self.entry_midpoint = signal_mid;
        self.tick_logs.push(format!("ENTRY {} @{:.3}", direction, signal_mid));
        let side = if direction == "up" {
            "buy_up"
        } else {
            "buy_down"
        };

        TradeAction::buy(side, ctx.position.cash, signal_mid, Some(10.0))
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
        m.insert("return_threshold_bps".into(), Param::float(12.0, 3.0, 15.0));
        m.insert("min_elapsed".into(), Param::float(0.55, 0.20, 0.55));
        m.insert("max_price".into(), Param::float(0.75, 0.45, 0.75));
        m.insert("velocity_window_ms".into(), Param::float(2000.0, 1000.0, 5000.0));
        m.insert("min_velocity_bps_s".into(), Param::float(0.15, 0.15, 0.60));
        m.insert("no_new_after".into(), Param::fixed_float(0.95));
        m
    }

    fn params(&self) -> HashMap<String, ParamValue> {
        let mut m = HashMap::new();
        m.insert("return_threshold_bps".into(), ParamValue::Float(self.return_threshold_bps));
        m.insert("min_elapsed".into(), ParamValue::Float(self.min_elapsed));
        m.insert("max_price".into(), ParamValue::Float(self.max_price));
        m.insert("velocity_window_ms".into(), ParamValue::Float(self.velocity_window_ms));
        m.insert("min_velocity_bps_s".into(), ParamValue::Float(self.min_velocity_bps_s));
        m
    }

    fn with_params(&self, params: &HashMap<String, ParamValue>) -> Box<dyn Strategy> {
        let mut s = TrendDrift::default();
        for (k, v) in params {
            if let Some(f) = v.as_f64() {
                match k.as_str() {
                    "return_threshold_bps" => s.return_threshold_bps = f,
                    "min_elapsed" => s.min_elapsed = f,
                    "max_price" => s.max_price = f,
                    "velocity_window_ms" => s.velocity_window_ms = f,
                    "min_velocity_bps_s" => s.min_velocity_bps_s = f,
                    "no_new_after" => s.no_new_after = f,
                    _ => {}
                }
            }
        }
        Box::new(s)
    }
}
