//! Poisson jump model for BTC up/down markets.

use crate::defaults::BPS_DIVISOR;
use crate::math_utils::norm_cdf;
use crate::strategy::*;
use crate::trader::*;
use std::collections::{HashMap, VecDeque};

pub struct PoissonKelly {
    // Tunable
    pub jump_bps: f64,
    pub min_elapsed: f64,
    pub edge_min: f64,
    pub max_price: f64,
    pub stop_loss_pct: f64,
    // Fixed
    pub min_jumps: i64,
    pub no_new_after: f64,
    pub cb_lookback: usize,
    pub cb_max_losses: usize,
    // Circuit breaker state (persists across sessions)
    cb_results: VecDeque<bool>,
    cb_dry_run: bool,
    last_bought_side: Option<String>,
    dry_run_side: Option<String>,
    // Session state
    prev_btc: f64,
    up_jumps: i64,
    down_jumps: i64,
    entry_count: i64,
    entry_midpoint: f64,
    stopped_out: bool,
    last_trade_time_s: f64,
    tick_logs: Vec<String>,
}

impl Default for PoissonKelly {
    fn default() -> Self {
        Self {
            jump_bps: 1.10,
            min_elapsed: 0.65,
            edge_min: 0.35,
            max_price: 0.90,
            stop_loss_pct: 0.10,
            min_jumps: 8,
            no_new_after: 0.95,
            cb_lookback: 8,
            cb_max_losses: 3,
            cb_results: VecDeque::with_capacity(8),
            cb_dry_run: false,
            last_bought_side: None,
            dry_run_side: None,
            prev_btc: 0.0,
            up_jumps: 0,
            down_jumps: 0,
            entry_count: 0,
            entry_midpoint: 0.0,
            stopped_out: false,
            last_trade_time_s: -999.0,
            tick_logs: Vec::new(),
        }
    }
}

impl PoissonKelly {
    fn reset_session_state(&mut self) {
        self.prev_btc = 0.0;
        self.up_jumps = 0;
        self.down_jumps = 0;
        self.entry_count = 0;
        self.entry_midpoint = 0.0;
        self.stopped_out = false;
        self.last_trade_time_s = -999.0;
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
                self.tick_logs.push("CB_RESUME shadow win".to_string());
            }
        } else if self.last_bought_side.is_some() {
            self.cb_results.push_back(won);
            while self.cb_results.len() > self.cb_lookback {
                self.cb_results.pop_front();
            }
        }

        self.last_bought_side = None;
        self.dry_run_side = None;
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
            self.tick_logs.push(format!(
                "CB_TRIPPED losses={}/{}", losses, self.cb_lookback
            ));
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
        let net_current = displacement_bps / self.jump_bps.max(0.01);

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
        Some(norm_cdf(z))
    }
}

impl Strategy for PoissonKelly {
    fn name(&self) -> &str {
        "PoissonKelly"
    }
    fn version(&self) -> &str {
        "12"
    }
    fn requires_full_session(&self) -> bool {
        true
    }

    fn on_session_start(&mut self, metadata: &SessionMetadata) {
        self.score_last_trade(metadata);
        self.update_circuit_breaker();
        self.reset_session_state();
        self.dry_run_side = None;
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

        // Count jumps
        if self.prev_btc > 0.0 {
            let ret_bps = (btc / self.prev_btc - 1.0) * BPS_DIVISOR;
            if ret_bps >= self.jump_bps {
                self.up_jumps += 1;
            } else if ret_bps <= -self.jump_bps {
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
                Some(self.stop_loss_pct),
                None,
                self.last_trade_time_s,
                2000.0,
            ) {
                if !exit.is_hold() {
                    self.tick_logs.push(format!("STOP_LOSS t={:.1}s entry={:.3}", ctx.elapsed_seconds, self.entry_midpoint));
                    self.stopped_out = true;
                    self.entry_midpoint = 0.0;
                    self.last_trade_time_s = ctx.elapsed_seconds;
                }
                return exit;
            }
            return hold();
        }

        // Check expired entry
        check_expired_entry(&mut self.entry_midpoint, ctx.position);

        if self.stopped_out || self.entry_count > 0 {
            return hold();
        }

        if check_timing_guards(ctx.elapsed_fraction, self.min_elapsed, self.no_new_after) {
            return hold();
        }

        let p_up = match self.compute_p_up(ctx) {
            Some(v) => v,
            None => return hold(),
        };
        let p_down = 1.0 - p_up;
        let edge_up = p_up - up_mid;
        let edge_down = p_down - down_mid;

        self.tick_logs.push(format!(
            "t={:.1}s p_up={:.3} edge_up={:.3} edge_down={:.3} jumps={}u/{}d up_mid={:.3} down_mid={:.3}",
            ctx.elapsed_seconds, p_up, edge_up, edge_down,
            self.up_jumps, self.down_jumps, up_mid, down_mid
        ));

        let (side, side_label, midpoint) = if edge_up >= edge_down
            && edge_up >= self.edge_min
            && up_mid <= self.max_price
        {
            ("buy_up", "up", up_mid)
        } else if edge_down >= self.edge_min && down_mid <= self.max_price {
            ("buy_down", "down", down_mid)
        } else {
            return hold();
        };

        // Circuit breaker: shadow pick only
        if self.cb_dry_run {
            if self.dry_run_side.is_none() {
                self.tick_logs.push(format!(
                    "CB_DRY_RUN shadow {} @{:.3} t={:.1}s",
                    side, midpoint, ctx.elapsed_seconds
                ));
                self.dry_run_side = Some(side_label.to_string());
                self.entry_count += 1;
            }
            return hold();
        }

        self.entry_count += 1;
        self.entry_midpoint = midpoint;
        self.last_trade_time_s = ctx.elapsed_seconds;
        self.last_bought_side = Some(side_label.to_string());

        self.tick_logs.push(format!(
            "ENTRY {} @{:.3} edge={:.3} cb={}/{}",
            side, midpoint,
            if side == "buy_up" { edge_up } else { edge_down },
            self.cb_results.iter().filter(|r| !**r).count(),
            self.cb_results.len()
        ));

        TradeAction::buy(side, ctx.position.cash, midpoint, Some(10.0))
    }

    fn on_trade_failed(&mut self, _side: &str) {
        self.entry_count = 0;
        self.entry_midpoint = 0.0;
        self.last_bought_side = None;
        self.last_trade_time_s = -999.0;
    }

    fn drain_logs(&mut self) -> Vec<String> {
        std::mem::take(&mut self.tick_logs)
    }

    fn param_defs(&self) -> HashMap<String, Param> {
        let mut m = HashMap::new();
        m.insert("jump_bps".into(), Param::float(1.10, 0.60, 1.50));
        m.insert("min_elapsed".into(), Param::float(0.65, 0.50, 0.80));
        m.insert("edge_min".into(), Param::float(0.35, 0.20, 0.50));
        m.insert("max_price".into(), Param::float(0.90, 0.80, 0.96));
        m.insert("stop_loss_pct".into(), Param::float(0.10, 0.05, 0.15));
        m.insert("min_jumps".into(), Param::fixed_int(8));
        m.insert("no_new_after".into(), Param::fixed_float(0.95));
        m.insert("cb_lookback".into(), Param::fixed_int(6));
        m.insert("cb_max_losses".into(), Param::fixed_int(3));
        m
    }

    fn params(&self) -> HashMap<String, ParamValue> {
        let mut m = HashMap::new();
        m.insert("jump_bps".into(), ParamValue::Float(self.jump_bps));
        m.insert("min_elapsed".into(), ParamValue::Float(self.min_elapsed));
        m.insert("edge_min".into(), ParamValue::Float(self.edge_min));
        m.insert("max_price".into(), ParamValue::Float(self.max_price));
        m.insert("stop_loss_pct".into(), ParamValue::Float(self.stop_loss_pct));
        m
    }

    fn with_params(&self, params: &HashMap<String, ParamValue>) -> Box<dyn Strategy> {
        let mut s = PoissonKelly::default();
        for (k, v) in params {
            if let Some(f) = v.as_f64() {
                match k.as_str() {
                    "jump_bps" => s.jump_bps = f,
                    "min_elapsed" => s.min_elapsed = f,
                    "edge_min" => s.edge_min = f,
                    "max_price" => s.max_price = f,
                    "stop_loss_pct" => s.stop_loss_pct = f,
                    "min_jumps" => s.min_jumps = f as i64,
                    "no_new_after" => s.no_new_after = f,
                    "cb_lookback" => s.cb_lookback = f as usize,
                    "cb_max_losses" => s.cb_max_losses = f as usize,
                    _ => {}
                }
            }
        }
        Box::new(s)
    }
}
