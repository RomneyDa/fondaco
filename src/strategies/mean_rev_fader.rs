//! MeanRevFader — Regime-gated mean reversion (flat trend only).

use crate::strategy::*;
use crate::trader::*;
use crate::volatility::*;
use std::collections::HashMap;

pub struct MeanRevFader {
    // Tunable
    pub max_buy_price: f64,
    pub take_profit_pct: f64,
    pub stop_loss_pct: f64,
    pub min_elapsed: f64,
    pub max_entries: i64,
    // Fixed
    pub no_new_after: f64,
    pub cooldown_ms: f64,
    // Regime gate
    regime_active: bool,
    // State
    entry_count: i64,
    entry_midpoint: f64,
    entry_side: Option<String>,
    last_trade_time_s: f64,
    tick_logs: Vec<String>,
}

impl Default for MeanRevFader {
    fn default() -> Self {
        Self {
            max_buy_price: 0.50,
            take_profit_pct: 0.15,
            stop_loss_pct: 0.08,
            min_elapsed: 0.25,
            max_entries: 2,
            no_new_after: 0.80,
            cooldown_ms: 3000.0,
            regime_active: false,
            entry_count: 0,
            entry_midpoint: 0.0,
            entry_side: None,
            last_trade_time_s: -999.0,
            tick_logs: Vec::new(),
        }
    }
}

impl MeanRevFader {
    fn reset_state(&mut self) {
        self.entry_count = 0;
        self.entry_midpoint = 0.0;
        self.entry_side = None;
        self.last_trade_time_s = -999.0;
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

        // Required vol: any (None means no vol gate)
        // Required trend: flat
        let trend_ok = match &regime.trend {
            Some(TrendRegime::Flat) => true,
            _ => false,
        };

        self.regime_active = trend_ok;
    }
}

impl Strategy for MeanRevFader {
    fn name(&self) -> &str {
        "MeanRevFader"
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

        let up_mid = match ctx.sample.up_token_midpoint {
            Some(v) => v,
            None => return hold(),
        };
        let down_mid = match ctx.sample.down_token_midpoint {
            Some(v) => v,
            None => return hold(),
        };

        // Manage position
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

        // No position: look for dip
        check_expired_entry(&mut self.entry_midpoint, ctx.position);
        if self.entry_side.is_some() && self.entry_midpoint == 0.0 {
            self.entry_side = None;
        }

        if self.entry_count >= self.max_entries {
            return hold();
        }

        if check_timing_guards(ctx.elapsed_fraction, self.min_elapsed, self.no_new_after) {
            return hold();
        }

        let cooldown_s = self.cooldown_ms / 1000.0;
        if ctx.elapsed_seconds - self.last_trade_time_s < cooldown_s {
            return hold();
        }

        // Buy whichever side is cheapest below threshold
        let mut candidates: Vec<(&str, &str, f64)> = Vec::new();
        if up_mid > 0.0 && up_mid <= self.max_buy_price.min(0.50) {
            candidates.push(("buy_up", "up", up_mid));
        }
        if down_mid > 0.0 && down_mid <= self.max_buy_price.min(0.50) {
            candidates.push(("buy_down", "down", down_mid));
        }

        if candidates.is_empty() {
            return hold();
        }

        candidates.sort_by(|a, b| a.2.partial_cmp(&b.2).unwrap());
        let (side, side_label, price) = candidates[0];

        self.entry_count += 1;
        self.entry_midpoint = price;
        self.entry_side = Some(side_label.to_string());
        self.last_trade_time_s = ctx.elapsed_seconds;

        self.tick_logs.push(format!(
            "ENTRY #{} {} @{:.3} t={:.1}s up={:.3} down={:.3}",
            self.entry_count, side, price, ctx.elapsed_seconds, up_mid, down_mid
        ));

        TradeAction::buy(side, ctx.position.cash, price, Some(10.0))
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
        m.insert("max_buy_price".into(), Param::float(0.50, 0.35, 0.50));
        m.insert("take_profit_pct".into(), Param::float(0.15, 0.15, 0.50));
        m.insert("stop_loss_pct".into(), Param::float(0.08, 0.08, 0.40));
        m.insert("min_elapsed".into(), Param::float(0.25, 0.05, 0.30));
        m.insert("max_entries".into(), Param::int(2, 1, 4));
        m.insert("no_new_after".into(), Param::fixed_float(0.80));
        m.insert("cooldown_ms".into(), Param::fixed_float(3000.0));
        m
    }

    fn params(&self) -> HashMap<String, ParamValue> {
        let mut m = HashMap::new();
        m.insert("max_buy_price".into(), ParamValue::Float(self.max_buy_price));
        m.insert("take_profit_pct".into(), ParamValue::Float(self.take_profit_pct));
        m.insert("stop_loss_pct".into(), ParamValue::Float(self.stop_loss_pct));
        m.insert("min_elapsed".into(), ParamValue::Float(self.min_elapsed));
        m.insert("max_entries".into(), ParamValue::Int(self.max_entries));
        m
    }

    fn with_params(&self, params: &HashMap<String, ParamValue>) -> Box<dyn Strategy> {
        let mut s = MeanRevFader::default();
        for (k, v) in params {
            match k.as_str() {
                "max_buy_price" => { if let Some(f) = v.as_f64() { s.max_buy_price = f; } }
                "take_profit_pct" => { if let Some(f) = v.as_f64() { s.take_profit_pct = f; } }
                "stop_loss_pct" => { if let Some(f) = v.as_f64() { s.stop_loss_pct = f; } }
                "min_elapsed" => { if let Some(f) = v.as_f64() { s.min_elapsed = f; } }
                "max_entries" => { if let Some(f) = v.as_f64() { s.max_entries = f as i64; } }
                "no_new_after" => { if let Some(f) = v.as_f64() { s.no_new_after = f; } }
                "cooldown_ms" => { if let Some(f) = v.as_f64() { s.cooldown_ms = f; } }
                _ => {}
            }
        }
        Box::new(s)
    }
}
