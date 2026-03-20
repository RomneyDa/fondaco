//! LateSniperCB — Late entry with circuit breaker, regime-gated (medium/high vol).

use crate::strategy::*;
use crate::trader::*;
use crate::volatility::*;
use std::collections::{HashMap, VecDeque};

pub struct LateSniperCB {
    // Tunable
    pub min_elapsed: f64,
    pub confidence_threshold: f64,
    pub max_price: f64,
    pub confirm_ms: f64,
    // Fixed
    pub no_new_after: f64,
    pub cb_lookback: usize,
    pub cb_max_losses: usize,
    // Regime gate
    regime_active: bool,
    // Circuit breaker state (persists across sessions within a single process
    // lifetime; resets if the bot-worker restarts)
    cb_results: VecDeque<bool>,
    cb_dry_run: bool,
    last_bought_side: Option<String>,
    dry_run_side: Option<String>,
    // Session state
    entry_count: i64,
    entry_midpoint: f64,
    confirm_side: Option<String>,
    confirm_start_s: f64,
    tick_logs: Vec<String>,
}

impl Default for LateSniperCB {
    fn default() -> Self {
        Self {
            min_elapsed: 0.75,
            confidence_threshold: 0.75,
            max_price: 0.75,
            confirm_ms: 800.0,
            no_new_after: 0.90,
            cb_lookback: 6,
            cb_max_losses: 2,
            regime_active: false,
            cb_results: VecDeque::with_capacity(6),
            cb_dry_run: false,
            last_bought_side: None,
            dry_run_side: None,
            entry_count: 0,
            entry_midpoint: 0.0,
            confirm_side: None,
            confirm_start_s: -999.0,
            tick_logs: Vec::new(),
        }
    }
}

impl LateSniperCB {
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

        // Required trend: any (None means no trend gate)
        self.regime_active = vol_ok;
    }

    fn score_last_trade(&mut self, metadata: &SessionMetadata) {
        let side = self
            .last_bought_side
            .clone()
            .or_else(|| self.dry_run_side.clone());

        let side = match side {
            Some(s) => s,
            None => return,
        };

        let prior = &metadata.prior_session_summaries;
        if prior.is_empty() {
            self.last_bought_side = None;
            return;
        }

        let last_outcome = &prior[prior.len() - 1].outcome;
        if last_outcome.is_empty() {
            self.last_bought_side = None;
            return;
        }

        let won = (side == "up" && last_outcome == "Up")
            || (side == "down" && last_outcome == "Down");

        if self.cb_dry_run && self.dry_run_side.is_some() {
            if won {
                self.cb_dry_run = false;
                self.cb_results.clear();
            }
        } else if self.last_bought_side.is_some() {
            self.cb_results.push_back(won);
            while self.cb_results.len() > self.cb_lookback {
                self.cb_results.pop_front();
            }
        }

        self.last_bought_side = None;
    }

    fn update_circuit_breaker(&mut self) {
        if self.cb_dry_run {
            return;
        }
        if self.cb_results.len() < self.cb_lookback {
            return;
        }
        let losses = self.cb_results.iter().filter(|&&r| !r).count();
        if losses >= self.cb_max_losses {
            self.cb_dry_run = true;
        }
    }
}

impl Strategy for LateSniperCB {
    fn name(&self) -> &str {
        "LateSniperCB"
    }
    fn version(&self) -> &str {
        "2"
    }
    fn requires_full_session(&self) -> bool {
        false
    }
    fn min_elapsed_hint(&self) -> f64 {
        0.65
    }

    fn on_session_start(&mut self, metadata: &SessionMetadata) {
        self.check_regime(metadata);
        self.score_last_trade(metadata);
        self.update_circuit_breaker();

        self.entry_count = 0;
        self.entry_midpoint = 0.0;
        self.confirm_side = None;
        self.confirm_start_s = -999.0;
        self.dry_run_side = None;
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

        // Already holding
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

        // Determine confident side
        let (side, signal_mid) = if up_mid >= self.confidence_threshold {
            ("up", up_mid)
        } else if down_mid >= self.confidence_threshold {
            ("down", down_mid)
        } else {
            self.confirm_side = None;
            self.confirm_start_s = -999.0;
            return hold();
        };

        if signal_mid > self.max_price {
            return hold();
        }

        // Confirmation timer
        let side_str = side.to_string();
        if self.confirm_side.as_deref() != Some(side) {
            self.confirm_side = Some(side_str);
            self.confirm_start_s = ctx.elapsed_seconds;
            return hold();
        }

        let confirm_s = self.confirm_ms / 1000.0;
        if ctx.elapsed_seconds - self.confirm_start_s < confirm_s {
            return hold();
        }

        // All gates passed
        let buy_side = if side == "up" { "buy_up" } else { "buy_down" };

        // Circuit breaker: shadow pick only
        if self.cb_dry_run {
            if self.dry_run_side.is_none() {
                self.tick_logs.push(format!("CB_DRY_RUN shadow {} @{:.3} t={:.1}s", side, signal_mid, ctx.elapsed_seconds));
                self.dry_run_side = Some(side.to_string());
                self.entry_count += 1;
            }
            return hold();
        }

        self.entry_count += 1;
        self.entry_midpoint = signal_mid;
        self.last_bought_side = Some(side.to_string());

        self.tick_logs.push(format!(
            "ENTRY {} @{:.3} t={:.1}s conf={:.0}ms cb_losses={}",
            buy_side, signal_mid, ctx.elapsed_seconds,
            (ctx.elapsed_seconds - self.confirm_start_s) * 1000.0,
            self.cb_results.iter().filter(|r| !**r).count()
        ));

        TradeAction::buy(buy_side, ctx.position.cash, signal_mid, Some(5.0))
    }

    fn on_trade_failed(&mut self, _side: &str) {
        self.entry_count = 0;
        self.entry_midpoint = 0.0;
        self.last_bought_side = None;
    }

    fn drain_logs(&mut self) -> Vec<String> {
        std::mem::take(&mut self.tick_logs)
    }

    fn param_defs(&self) -> HashMap<String, Param> {
        let mut m = HashMap::new();
        m.insert("min_elapsed".into(), Param::float(0.75, 0.50, 0.85));
        m.insert("confidence_threshold".into(), Param::float(0.75, 0.70, 0.85));
        m.insert("max_price".into(), Param::float(0.75, 0.65, 0.80));
        m.insert("confirm_ms".into(), Param::float(800.0, 400.0, 1200.0));
        m.insert("no_new_after".into(), Param::fixed_float(0.90));
        m.insert("cb_lookback".into(), Param::fixed_int(6));
        m.insert("cb_max_losses".into(), Param::fixed_int(2));
        m
    }

    fn params(&self) -> HashMap<String, ParamValue> {
        let mut m = HashMap::new();
        m.insert("min_elapsed".into(), ParamValue::Float(self.min_elapsed));
        m.insert("confidence_threshold".into(), ParamValue::Float(self.confidence_threshold));
        m.insert("max_price".into(), ParamValue::Float(self.max_price));
        m.insert("confirm_ms".into(), ParamValue::Float(self.confirm_ms));
        m
    }

    fn with_params(&self, params: &HashMap<String, ParamValue>) -> Box<dyn Strategy> {
        let mut s = LateSniperCB::default();
        for (k, v) in params {
            match k.as_str() {
                "min_elapsed" => { if let Some(f) = v.as_f64() { s.min_elapsed = f; } }
                "confidence_threshold" => { if let Some(f) = v.as_f64() { s.confidence_threshold = f; } }
                "max_price" => { if let Some(f) = v.as_f64() { s.max_price = f; } }
                "confirm_ms" => { if let Some(f) = v.as_f64() { s.confirm_ms = f; } }
                "no_new_after" => { if let Some(f) = v.as_f64() { s.no_new_after = f; } }
                "cb_lookback" => { if let Some(f) = v.as_f64() { s.cb_lookback = f as usize; } }
                "cb_max_losses" => { if let Some(f) = v.as_f64() { s.cb_max_losses = f as usize; } }
                _ => {}
            }
        }
        Box::new(s)
    }
}
