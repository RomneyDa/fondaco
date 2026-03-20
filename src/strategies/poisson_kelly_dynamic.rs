//! PoissonKellyDynamic — Poisson/Kelly with volatility and trend adaptation.

use crate::defaults::BPS_DIVISOR;
use crate::math_utils::norm_cdf;
use crate::strategy::*;
use crate::trader::*;
use crate::volatility::compute_signed_volatility;
use std::collections::HashMap;

/// Reference session-level BTC volatility (std dev of 5-min returns in bps).
const TYPICAL_SESSION_VOL_BPS: f64 = 15.0;

pub struct PoissonKellyDynamic {
    // Tunable
    pub jump_bps: f64,
    pub min_elapsed: f64,
    pub edge_min: f64,
    pub edge_step: f64,
    pub max_price: f64,
    pub max_entries: i64,
    pub stop_loss_pct: f64,
    pub vol_scale: f64,
    pub trend_strength: f64,
    // Fixed
    pub min_jumps: i64,
    pub no_new_after: f64,
    pub cooldown_ms: f64,
    pub lookback: usize,
    // Adapted (computed at session start)
    effective_jump_bps: f64,
    effective_stop_loss: f64,
    trend_bias: f64,
    // State
    prev_btc: f64,
    up_jumps: i64,
    down_jumps: i64,
    entry_count: i64,
    entry_midpoint: f64,
    last_trade_time_s: f64,
    tick_logs: Vec<String>,
}

impl Default for PoissonKellyDynamic {
    fn default() -> Self {
        Self {
            jump_bps: 0.18,
            min_elapsed: 0.65,
            edge_min: 0.10,
            edge_step: 0.03,
            max_price: 0.90,
            max_entries: 3,
            stop_loss_pct: 0.10,
            vol_scale: 0.5,
            trend_strength: 0.04,
            min_jumps: 8,
            no_new_after: 0.95,
            cooldown_ms: 4000.0,
            lookback: 30,
            effective_jump_bps: 0.18,
            effective_stop_loss: 0.10,
            trend_bias: 0.0,
            prev_btc: 0.0,
            up_jumps: 0,
            down_jumps: 0,
            entry_count: 0,
            entry_midpoint: 0.0,
            last_trade_time_s: -999.0,
            tick_logs: Vec::new(),
        }
    }
}

impl PoissonKellyDynamic {
    fn reset_state(&mut self) {
        self.prev_btc = 0.0;
        self.up_jumps = 0;
        self.down_jumps = 0;
        self.entry_count = 0;
        self.entry_midpoint = 0.0;
        self.last_trade_time_s = -999.0;
    }

    fn adapt_params(&mut self, metadata: &SessionMetadata) {
        let summaries = &metadata.prior_session_summaries;
        let n = summaries.len();
        let start = if n > self.lookback { n - self.lookback } else { 0 };
        let recent = &summaries[start..];

        // Volatility adaptation
        let vol = compute_signed_volatility(recent, self.lookback, 3);
        if let Some((_mean, recent_vol)) = vol {
            if recent_vol > 0.0 {
                let vol_ratio = (recent_vol / TYPICAL_SESSION_VOL_BPS).max(0.5).min(2.5);
                let scale = 1.0 + self.vol_scale * (vol_ratio - 1.0);
                self.effective_jump_bps = self.jump_bps * scale;
                self.effective_stop_loss = self.stop_loss_pct * scale;
            } else {
                self.effective_jump_bps = self.jump_bps;
                self.effective_stop_loss = self.stop_loss_pct;
            }
        } else {
            self.effective_jump_bps = self.jump_bps;
            self.effective_stop_loss = self.stop_loss_pct;
        }

        // Trend bias
        if self.trend_strength > 0.0 && !recent.is_empty() {
            let count = recent.len();
            let alpha = 2.0 / ((count.min(self.lookback)) as f64 + 1.0);
            let mut ema = 0.5_f64;
            for s in recent {
                let val = if s.outcome == "Up" { 1.0 } else { 0.0 };
                ema = alpha * val + (1.0 - alpha) * ema;
            }
            self.trend_bias = (ema - 0.5) * self.trend_strength;
        } else {
            self.trend_bias = 0.0;
        }
    }

    fn compute_p_up(&self, ctx: &TickContext) -> Option<f64> {
        let btc = ctx.sample.btc_price_usd?;
        let strike = ctx.initial_btc_price;
        if strike <= 0.0 {
            return None;
        }

        let elapsed = ctx.elapsed_fraction;
        if elapsed <= 0.01 {
            return None;
        }
        let remaining = 1.0 - elapsed;
        if remaining <= 0.01 {
            return None;
        }

        let total_jumps = self.up_jumps + self.down_jumps;
        if total_jumps < self.min_jumps {
            return None;
        }

        let lambda_up = self.up_jumps as f64 / elapsed;
        let lambda_down = self.down_jumps as f64 / elapsed;
        let mu_up = lambda_up * remaining;
        let mu_down = lambda_down * remaining;

        let displacement_bps = (btc / strike - 1.0) * BPS_DIVISOR;
        let net_current = displacement_bps / self.effective_jump_bps.max(0.01);

        let skellam_mean = mu_up - mu_down;
        let skellam_var = mu_up + mu_down;

        if skellam_var < 1e-10 {
            return Some(if net_current > 0.0 {
                1.0
            } else if net_current < 0.0 {
                0.0
            } else {
                0.5
            });
        }

        let z = (net_current + skellam_mean) / skellam_var.sqrt();
        let mut p = norm_cdf(z);

        // Apply trend bias
        if self.trend_bias != 0.0 {
            p = (p + self.trend_bias).max(0.01).min(0.99);
        }

        Some(p)
    }
}

impl Strategy for PoissonKellyDynamic {
    fn name(&self) -> &str {
        "PoissonKellyDynamic"
    }
    fn version(&self) -> &str {
        "7"
    }
    fn requires_full_session(&self) -> bool {
        true
    }

    fn on_session_start(&mut self, metadata: &SessionMetadata) {
        self.adapt_params(metadata);
        self.reset_state();
    }

    fn on_tick(&mut self, ctx: &TickContext) -> TradeAction {
        let btc = match ctx.sample.btc_price_usd {
            Some(v) => v,
            None => return hold(),
        };
        let up_mid = match ctx.sample.up_token_midpoint {
            Some(v) => v,
            None => return hold(),
        };
        let down_mid = match ctx.sample.down_token_midpoint {
            Some(v) => v,
            None => return hold(),
        };

        // Count jumps using adapted threshold
        if self.prev_btc > 0.0 {
            let ret_bps = (btc / self.prev_btc - 1.0) * BPS_DIVISOR;
            if ret_bps >= self.effective_jump_bps {
                self.up_jumps += 1;
            } else if ret_bps <= -self.effective_jump_bps {
                self.down_jumps += 1;
            }
        }
        self.prev_btc = btc;

        // Manage existing position: stop-loss
        if held_side(ctx.position).is_some() {
            if let Some(exit) = check_exit(
                ctx,
                up_mid,
                down_mid,
                self.entry_midpoint,
                Some(self.effective_stop_loss),
                None,
                self.last_trade_time_s,
                self.cooldown_ms,
            ) {
                return exit;
            }
            return hold();
        }

        // No position: look for entry
        check_expired_entry(&mut self.entry_midpoint, ctx.position);

        // Cooldown between trades
        if ctx.elapsed_seconds - self.last_trade_time_s < self.cooldown_ms / 1000.0 {
            return hold();
        }

        // Max entries cap
        if self.entry_count >= self.max_entries {
            return hold();
        }

        // Timing guards
        if check_timing_guards(ctx.elapsed_fraction, self.min_elapsed, self.no_new_after) {
            return hold();
        }

        // Compute model probability
        let p_up = match self.compute_p_up(ctx) {
            Some(v) => v,
            None => return hold(),
        };

        let p_down = 1.0 - p_up;

        // Edge escalation
        let effective_edge = self.edge_min + self.edge_step * self.entry_count as f64;

        let edge_up = p_up - up_mid;
        let edge_down = p_down - down_mid;

        self.tick_logs.push(format!(
            "t={:.1}s p_up={:.3} edge_up={:.3} edge_down={:.3} eff_edge={:.3} jumps={}u/{}d bias={:.3}",
            ctx.elapsed_seconds, p_up, edge_up, edge_down, effective_edge,
            self.up_jumps, self.down_jumps, self.trend_bias
        ));

        let (side, midpoint) = if edge_up >= edge_down
            && edge_up >= effective_edge
            && up_mid <= self.max_price
        {
            ("buy_up", up_mid)
        } else if edge_down >= effective_edge && down_mid <= self.max_price {
            ("buy_down", down_mid)
        } else {
            return hold();
        };

        self.entry_count += 1;
        self.entry_midpoint = midpoint;
        self.last_trade_time_s = ctx.elapsed_seconds;

        self.tick_logs.push(format!("ENTRY #{} {} @{:.3}", self.entry_count, side, midpoint));

        let dollars = ctx.position.cash;

        TradeAction::buy(side, dollars, midpoint, Some(10.0))
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
        m.insert("jump_bps".into(), Param::float(0.18, 0.10, 0.25));
        m.insert("min_elapsed".into(), Param::float(0.65, 0.50, 0.80));
        m.insert("edge_min".into(), Param::float(0.10, 0.05, 0.15));
        m.insert("edge_step".into(), Param::float(0.03, 0.0, 0.06));
        m.insert("max_price".into(), Param::float(0.90, 0.80, 0.96));
        m.insert("max_entries".into(), Param::int(3, 1, 10));
        m.insert("stop_loss_pct".into(), Param::float(0.10, 0.05, 0.15));
        m.insert("vol_scale".into(), Param::float(0.5, 0.0, 1.0));
        m.insert("trend_strength".into(), Param::float(0.04, 0.0, 0.08));
        m.insert("min_jumps".into(), Param::fixed_int(8));
        m.insert("no_new_after".into(), Param::fixed_float(0.95));
        m.insert("cooldown_ms".into(), Param::fixed_float(4000.0));
        m.insert("lookback".into(), Param::fixed_int(30));
        m
    }

    fn params(&self) -> HashMap<String, ParamValue> {
        let mut m = HashMap::new();
        m.insert("jump_bps".into(), ParamValue::Float(self.jump_bps));
        m.insert("min_elapsed".into(), ParamValue::Float(self.min_elapsed));
        m.insert("edge_min".into(), ParamValue::Float(self.edge_min));
        m.insert("edge_step".into(), ParamValue::Float(self.edge_step));
        m.insert("max_price".into(), ParamValue::Float(self.max_price));
        m.insert("max_entries".into(), ParamValue::Int(self.max_entries));
        m.insert("stop_loss_pct".into(), ParamValue::Float(self.stop_loss_pct));
        m.insert("vol_scale".into(), ParamValue::Float(self.vol_scale));
        m.insert("trend_strength".into(), ParamValue::Float(self.trend_strength));
        m
    }

    fn with_params(&self, params: &HashMap<String, ParamValue>) -> Box<dyn Strategy> {
        let mut s = PoissonKellyDynamic::default();
        for (k, v) in params {
            match k.as_str() {
                "jump_bps" => { if let Some(f) = v.as_f64() { s.jump_bps = f; s.effective_jump_bps = f; } }
                "min_elapsed" => { if let Some(f) = v.as_f64() { s.min_elapsed = f; } }
                "edge_min" => { if let Some(f) = v.as_f64() { s.edge_min = f; } }
                "edge_step" => { if let Some(f) = v.as_f64() { s.edge_step = f; } }
                "max_price" => { if let Some(f) = v.as_f64() { s.max_price = f; } }
                "max_entries" => { if let Some(f) = v.as_f64() { s.max_entries = f as i64; } }
                "stop_loss_pct" => { if let Some(f) = v.as_f64() { s.stop_loss_pct = f; s.effective_stop_loss = f; } }
                "vol_scale" => { if let Some(f) = v.as_f64() { s.vol_scale = f; } }
                "trend_strength" => { if let Some(f) = v.as_f64() { s.trend_strength = f; } }
                "min_jumps" => { if let Some(f) = v.as_f64() { s.min_jumps = f as i64; } }
                "no_new_after" => { if let Some(f) = v.as_f64() { s.no_new_after = f; } }
                "cooldown_ms" => { if let Some(f) = v.as_f64() { s.cooldown_ms = f; } }
                "lookback" => { if let Some(f) = v.as_f64() { s.lookback = f as usize; } }
                _ => {}
            }
        }
        Box::new(s)
    }
}
