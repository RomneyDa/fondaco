//! GapFader — Buy dips in undecided markets, sell on take-profit, repeat.
//!
//! **EXAMPLE STRATEGY** — Provided for illustration only. There is no evidence
//! that this strategy is profitable. Use it as a starting point for your own
//! research, not as trading advice.

use crate::strategy::*;
use crate::trader::*;
use crate::volatility::compute_session_volatility;
use std::collections::HashMap;

pub struct GapFader {
    // Tunable
    pub gap_threshold: f64,
    pub max_buy_price: f64,
    pub take_profit_pct: f64,
    pub stop_loss_pct: f64,
    pub max_entries: i64,
    pub time_decay_power: f64,
    // Fixed
    pub observe_seconds: f64,
    pub cooldown_ms: f64,
    pub no_new_after: f64,
    pub vol_lookback: usize,
    pub min_vol_sessions: usize,
    pub vol_boost_scale: f64,
    pub vol_baseline_bps: f64,
    // State
    gap_samples: Vec<f64>,
    active: bool,
    vol_boost: f64,
    entry_count: i64,
    entry_midpoint: f64,
    entry_side: Option<String>,
    last_trade_time_s: f64,
    window_seconds: f64,
    tick_logs: Vec<String>,
}

impl Default for GapFader {
    fn default() -> Self {
        Self {
            gap_threshold: 0.06,
            max_buy_price: 0.50,
            take_profit_pct: 0.45,
            stop_loss_pct: 0.35,
            max_entries: 5,
            time_decay_power: 2.5,
            observe_seconds: 15.0,
            cooldown_ms: 2000.0,
            no_new_after: 0.85,
            vol_lookback: 5,
            min_vol_sessions: 3,
            vol_boost_scale: 0.0,
            vol_baseline_bps: 5.0,
            gap_samples: Vec::new(),
            active: false,
            vol_boost: 1.0,
            entry_count: 0,
            entry_midpoint: 0.0,
            entry_side: None,
            last_trade_time_s: -999.0,
            window_seconds: 300.0,
            tick_logs: Vec::new(),
        }
    }
}

impl GapFader {
    fn reset_state(&mut self) {
        self.gap_samples.clear();
        self.active = false;
        self.vol_boost = 1.0;
        self.entry_count = 0;
        self.entry_midpoint = 0.0;
        self.entry_side = None;
        self.last_trade_time_s = -999.0;
        self.window_seconds = 300.0;
    }

    fn effective_max_price(&self, elapsed_fraction: f64) -> f64 {
        let remaining = (1.0 - elapsed_fraction).max(0.0);
        let time_factor = remaining.powf(self.time_decay_power);
        self.max_buy_price * self.vol_boost * time_factor
    }
}

impl Strategy for GapFader {
    fn name(&self) -> &str {
        "GapFader"
    }
    fn version(&self) -> &str {
        "7"
    }
    fn requires_full_session(&self) -> bool {
        true
    }

    fn on_session_start(&mut self, metadata: &SessionMetadata) {
        self.reset_state();
        self.window_seconds = metadata.window_duration;

        // Compute volatility boost from recent sessions
        self.vol_boost = 1.0;
        let summaries = &metadata.prior_session_summaries;
        let vol = compute_session_volatility(summaries, self.vol_lookback, self.min_vol_sessions);
        if let Some((mean_abs, _std)) = vol {
            let vol_ratio = mean_abs / self.vol_baseline_bps.max(0.1);
            self.vol_boost = 1.0 + self.vol_boost_scale * (vol_ratio - 1.0).max(0.0);
            self.vol_boost = self.vol_boost.max(1.0).min(1.5);
        }
    }

    fn on_tick(&mut self, ctx: &TickContext) -> TradeAction {
        let up_mid = match ctx.sample.up_token_midpoint {
            Some(v) => v,
            None => return hold(),
        };
        let down_mid = match ctx.sample.down_token_midpoint {
            Some(v) => v,
            None => return hold(),
        };

        let elapsed_s = ctx.sample.elapsed_seconds;

        // Phase 1: Observation
        if !self.active {
            if elapsed_s <= self.observe_seconds {
                let gap = (up_mid - down_mid).abs();
                self.gap_samples.push(gap);
                return hold();
            }

            // Observation period over -- evaluate
            if self.gap_samples.is_empty() {
                // Prevent re-evaluation
                self.gap_samples.push(999.0);
                return hold();
            }

            let avg_gap: f64 =
                self.gap_samples.iter().sum::<f64>() / self.gap_samples.len() as f64;

            if avg_gap >= self.gap_threshold {
                // Mark as evaluated but not active
                self.gap_samples.clear();
                return hold();
            }

            self.active = true;
            self.tick_logs.push(format!("ACTIVE avg_gap={:.4} vol_boost={:.2}", avg_gap, self.vol_boost));
        }

        // Manage existing position
        if held_side(ctx.position).is_some() {
            if let Some(exit) = check_exit(
                ctx,
                up_mid,
                down_mid,
                self.entry_midpoint,
                Some(self.stop_loss_pct),
                Some(self.take_profit_pct),
                self.last_trade_time_s,
                self.cooldown_ms,
            ) {
                if !exit.is_hold() {
                    self.entry_side = None;
                }
                return exit;
            }
            return hold();
        }

        // No position: look for dip to buy
        check_expired_entry(&mut self.entry_midpoint, ctx.position);
        if self.entry_side.is_some() && self.entry_midpoint == 0.0 {
            self.entry_side = None;
        }

        if !self.active {
            return hold();
        }

        // Max entries
        if self.entry_count >= self.max_entries {
            return hold();
        }

        // Timing guard
        if ctx.elapsed_fraction >= self.no_new_after {
            return hold();
        }

        // Cooldown
        if ctx.elapsed_seconds - self.last_trade_time_s < self.cooldown_ms / 1000.0 {
            return hold();
        }

        // Effective max price decays with time, boosted by volatility
        let eff_max = self.effective_max_price(ctx.elapsed_fraction);

        // Buy whichever side is cheapest (below effective max)
        // Both must be below 0.50 (undecided market assumption)
        let mut candidates: Vec<(&str, &str, f64)> = Vec::new();
        if up_mid > 0.0 && up_mid <= eff_max.min(0.50) {
            candidates.push(("buy_up", "up", up_mid));
        }
        if down_mid > 0.0 && down_mid <= eff_max.min(0.50) {
            candidates.push(("buy_down", "down", down_mid));
        }

        if candidates.is_empty() {
            return hold();
        }

        // Pick the cheapest
        candidates.sort_by(|a, b| a.2.partial_cmp(&b.2).unwrap());
        let (side, side_label, price) = candidates[0];

        self.entry_count += 1;
        self.entry_midpoint = price;
        self.entry_side = Some(side_label.to_string());
        self.last_trade_time_s = ctx.elapsed_seconds;

        self.tick_logs.push(format!(
            "ENTRY #{} {} @{:.3} eff_max={:.3} t={:.1}s",
            self.entry_count, side, price, eff_max, ctx.elapsed_seconds
        ));

        let dollars = ctx.position.cash;

        TradeAction::buy(side, dollars, price, Some(10.0))
    }

    fn on_trade_failed(&mut self, _side: &str) {
        self.entry_count = 0;
        self.entry_midpoint = 0.0;
        self.last_trade_time_s = -999.0;
    }

    fn drain_logs(&mut self) -> Vec<String> {
        std::mem::take(&mut self.tick_logs)
    }

    fn param_defs(&self) -> HashMap<String, Param> {
        let mut m = HashMap::new();
        m.insert("gap_threshold".into(), Param::float(0.06, 0.04, 0.10));
        m.insert("max_buy_price".into(), Param::float(0.50, 0.44, 0.50));
        m.insert("take_profit_pct".into(), Param::float(0.45, 0.30, 0.55));
        m.insert("stop_loss_pct".into(), Param::float(0.35, 0.25, 0.45));
        m.insert("max_entries".into(), Param::int(5, 3, 7));
        m.insert("time_decay_power".into(), Param::float(2.5, 1.5, 3.0));
        m.insert("observe_seconds".into(), Param::fixed_float(15.0));
        m.insert("cooldown_ms".into(), Param::fixed_float(2000.0));
        m.insert("no_new_after".into(), Param::fixed_float(0.85));
        m.insert("vol_lookback".into(), Param::fixed_int(5));
        m.insert("min_vol_sessions".into(), Param::fixed_int(3));
        m.insert("vol_boost_scale".into(), Param::fixed_float(0.0));
        m.insert("vol_baseline_bps".into(), Param::fixed_float(5.0));
        m
    }

    fn params(&self) -> HashMap<String, ParamValue> {
        let mut m = HashMap::new();
        m.insert("gap_threshold".into(), ParamValue::Float(self.gap_threshold));
        m.insert("max_buy_price".into(), ParamValue::Float(self.max_buy_price));
        m.insert("take_profit_pct".into(), ParamValue::Float(self.take_profit_pct));
        m.insert("stop_loss_pct".into(), ParamValue::Float(self.stop_loss_pct));
        m.insert("max_entries".into(), ParamValue::Int(self.max_entries));
        m.insert("time_decay_power".into(), ParamValue::Float(self.time_decay_power));
        m
    }

    fn with_params(&self, params: &HashMap<String, ParamValue>) -> Box<dyn Strategy> {
        let mut s = GapFader::default();
        for (k, v) in params {
            match k.as_str() {
                "gap_threshold" => { if let Some(f) = v.as_f64() { s.gap_threshold = f; } }
                "max_buy_price" => { if let Some(f) = v.as_f64() { s.max_buy_price = f; } }
                "take_profit_pct" => { if let Some(f) = v.as_f64() { s.take_profit_pct = f; } }
                "stop_loss_pct" => { if let Some(f) = v.as_f64() { s.stop_loss_pct = f; } }
                "max_entries" => { if let Some(f) = v.as_f64() { s.max_entries = f as i64; } }
                "time_decay_power" => { if let Some(f) = v.as_f64() { s.time_decay_power = f; } }
                "observe_seconds" => { if let Some(f) = v.as_f64() { s.observe_seconds = f; } }
                "cooldown_ms" => { if let Some(f) = v.as_f64() { s.cooldown_ms = f; } }
                "no_new_after" => { if let Some(f) = v.as_f64() { s.no_new_after = f; } }
                "vol_lookback" => { if let Some(f) = v.as_f64() { s.vol_lookback = f as usize; } }
                "min_vol_sessions" => { if let Some(f) = v.as_f64() { s.min_vol_sessions = f as usize; } }
                "vol_boost_scale" => { if let Some(f) = v.as_f64() { s.vol_boost_scale = f; } }
                "vol_baseline_bps" => { if let Some(f) = v.as_f64() { s.vol_baseline_bps = f; } }
                _ => {}
            }
        }
        Box::new(s)
    }
}
