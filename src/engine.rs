//! Core simulation engine for backtesting strategies.
//!
//! Ports the logic from `simulator/engine.py` to Rust. Provides session loading,
//! simulation execution, and Kelly metrics computation.

use crate::defaults::{market_sessions_dir, slug_key};
use crate::markets::known_markets;
use crate::models::{SessionData, SessionSummary};
use crate::strategy::{ParamValue, Strategy};
use crate::trader::{Position, SessionMetadata, SimExecutor, TickContext, TradeAction, Trader};
use std::collections::HashMap;
use std::path::{Path, PathBuf};

// ---------------------------------------------------------------------------
// Result types
// ---------------------------------------------------------------------------

/// Result for one simulated session.
#[derive(Debug, Clone)]
pub struct SessionResult {
    pub session_file: String,
    pub outcome: String,
    pub pnl: f64,
    pub pnl_pct: f64,
    pub trades: Vec<crate::trader::Trade>,
    pub final_position: Position,
    pub num_samples: usize,
    pub starting_cash: f64,
    pub total_fees: f64,
}

/// Aggregate results across all sessions for one strategy variant.
#[derive(Debug, Clone)]
pub struct BacktestResult {
    pub strategy_name: String,
    pub params: HashMap<String, ParamValue>,
    pub sessions: Vec<SessionResult>,
    pub total_pnl: f64,
    pub mean_pnl: f64,
    pub entry_rate: f64,
    pub win_rate: f64,
    pub sharpe: f64,
    pub num_sessions: usize,
    pub geo_mean_return: f64,
    pub kelly_fraction: f64,
    pub dd_ratio: f64,
    pub log_growth: f64,
    pub final_bankroll: f64,
    pub max_drawdown: f64,
    pub total_fees: f64,
}

// ---------------------------------------------------------------------------
// Session loading
// ---------------------------------------------------------------------------

/// List all session JSON files in a data directory, sorted by name.
/// Excludes sidecar files (.summary.json, .trading.json, etc).
fn list_session_files(data_dir: &str) -> Vec<PathBuf> {
    let is_session_file = |p: &PathBuf| -> bool {
        let name = p.file_name().and_then(|n| n.to_str()).unwrap_or("");
        name.ends_with(".json")
            && !name.contains(".summary.")
            && !name.contains(".trading.")
    };

    let pattern = format!("{}/**/*.json", data_dir);
    let mut files: Vec<PathBuf> = glob::glob(&pattern)
        .unwrap_or_else(|_| glob::glob("").unwrap())
        .filter_map(|r| r.ok())
        .filter(is_session_file)
        .collect();
    if files.is_empty() {
        let pattern = format!("{}/*.json", data_dir);
        files = glob::glob(&pattern)
            .unwrap_or_else(|_| glob::glob("").unwrap())
            .filter_map(|r| r.ok())
            .filter(is_session_file)
            .collect();
    }
    files.sort();
    files
}

/// Load resolved session JSON files from a directory tree.
///
/// If `recent` is set, only the N most recent valid sessions are loaded.
pub fn load_sessions(data_dir: &str, recent: Option<usize>) -> Vec<SessionData> {
    let files = list_session_files(data_dir);
    if files.is_empty() {
        return Vec::new();
    }

    if let Some(recent_n) = recent {
        if recent_n < files.len() {
            let mut sessions = Vec::new();
            for f in files.iter().rev() {
                if let Ok(contents) = std::fs::read_to_string(f) {
                    if let Ok(mut data) = serde_json::from_str::<SessionData>(&contents) {
                        if data.outcome.is_none() || data.samples.is_empty() {
                            continue;
                        }
                        data.source_file = f.to_string_lossy().to_string();
                        sessions.push(data);
                        if sessions.len() >= recent_n {
                            break;
                        }
                    }
                }
            }
            sessions.reverse();
            return sessions;
        }
    }

    let mut sessions = Vec::new();
    for f in &files {
        if let Ok(contents) = std::fs::read_to_string(f) {
            if let Ok(mut data) = serde_json::from_str::<SessionData>(&contents) {
                if data.outcome.is_none() || data.samples.is_empty() {
                    continue;
                }
                data.source_file = f.to_string_lossy().to_string();
                sessions.push(data);
            }
        }
    }
    sessions
}

/// Load lightweight session summaries for all sessions.
///
/// Reads `.summary.json` sidecar files when available (tiny, no sample data).
/// Falls back to parsing the full session JSON for sessions without a sidecar.
pub fn load_session_summaries(data_dir: &str) -> Vec<SessionSummary> {
    let files = list_session_files(data_dir);
    if files.is_empty() {
        return Vec::new();
    }

    let mut summaries = Vec::new();
    for f in &files {
        // Try sidecar first: session.json → session.summary.json
        let summary_path = f.with_extension("summary.json");
        if summary_path.exists() {
            if let Ok(contents) = std::fs::read_to_string(&summary_path) {
                if let Ok(summary) = serde_json::from_str::<SessionSummary>(&contents) {
                    if !summary.window_start.is_empty()
                        && !summary.outcome.is_empty()
                        && summary.initial_btc > 0.0
                        && summary.final_btc > 0.0
                    {
                        summaries.push(summary);
                        continue;
                    }
                }
            }
        }

        // Fallback: parse full session file
        if let Ok(contents) = std::fs::read_to_string(f) {
            if let Ok(data) = serde_json::from_str::<SessionData>(&contents) {
                if data.outcome.is_none() || data.samples.is_empty() {
                    continue;
                }
                let ws = match data.metadata.window_start_utc.as_deref() {
                    Some(s) if !s.is_empty() => s.to_string(),
                    _ => continue,
                };
                let initial_btc = match data.initial_btc_price_usd {
                    Some(v) if v > 0.0 => v,
                    _ => continue,
                };
                let final_btc = match data.final_btc_price_usd {
                    Some(v) if v > 0.0 => v,
                    _ => continue,
                };
                let outcome = match &data.outcome {
                    Some(o) if !o.is_empty() => o.clone(),
                    _ => continue,
                };
                let summary = SessionSummary {
                    window_start: ws,
                    initial_btc,
                    final_btc,
                    outcome,
                };

                // Write missing sidecar so future loads are fast
                if let Ok(json) = serde_json::to_string(&summary) {
                    let tmp = summary_path.with_extension("json.tmp");
                    if let Ok(mut out) = std::fs::File::create(&tmp) {
                        use std::io::Write;
                        let _ = out.write_all(json.as_bytes());
                        let _ = std::fs::rename(&tmp, &summary_path);
                    }
                }

                summaries.push(summary);
            }
        }
    }
    summaries.sort_by(|a, b| a.window_start.cmp(&b.window_start));
    summaries
}

/// Pre-compute per-session prior summaries and attach to session metadata.
///
/// Without this, every variant x session combo would need to filter the full
/// summary list. Pre-slicing does it once per session before the parallel run.
pub fn pre_slice_summaries(
    sessions: &mut [SessionData],
    summaries: &[SessionSummary],
    _window_duration: f64,
) {
    for session in sessions.iter_mut() {
        let ws = match session.metadata.window_start_utc.as_deref() {
            Some(s) if !s.is_empty() => s.to_string(),
            _ => continue,
        };
        let prior: Vec<SessionSummary> = summaries
            .iter()
            .filter(|s| s.window_start.as_str() < ws.as_str())
            .cloned()
            .collect();
        session._prior_summaries = Some(prior);
    }
}

// ---------------------------------------------------------------------------
// Kelly metrics
// ---------------------------------------------------------------------------

/// Compute E[log(1 + f*r)] for a given fraction f and return series.
fn log_growth_at_f(f: f64, returns: &[f64]) -> f64 {
    let mut g = 0.0;
    for &ret in returns {
        let v = 1.0 + f * ret;
        if v <= 0.0 {
            return f64::NEG_INFINITY;
        }
        g += v.ln();
    }
    g
}

/// Find Kelly-optimal f via ternary search on the concave log-growth function.
fn ternary_kelly_search(returns: &[f64]) -> f64 {
    if returns.is_empty() {
        return 0.0;
    }
    let mut lo = 0.001_f64;
    let mut hi = 1.0_f64;
    let tol = 0.005;

    for _ in 0..50 {
        if hi - lo < tol {
            break;
        }
        let m1 = lo + (hi - lo) / 3.0;
        let m2 = hi - (hi - lo) / 3.0;
        let g1 = log_growth_at_f(m1, returns);
        let g2 = log_growth_at_f(m2, returns);
        if g1 < g2 {
            lo = m1;
        } else {
            hi = m2;
        }
    }
    let best_f = (lo + hi) / 2.0;
    if log_growth_at_f(best_f, returns) <= 0.0 {
        return 0.0;
    }
    best_f
}

struct KellyMetrics {
    geo_mean_return: f64,
    kelly_fraction: f64,
    dd_ratio: f64,
    log_growth: f64,
    final_bankroll: f64,
    max_drawdown: f64,
}

/// Compute Kelly criterion and geometric growth metrics from session results.
fn compute_kelly_metrics(
    results: &[SessionResult],
    starting_cash: f64,
    compound: bool,
    bankroll_path: Option<&[f64]>,
) -> KellyMetrics {
    let n = results.len();
    if n == 0 {
        return KellyMetrics {
            geo_mean_return: 0.0,
            kelly_fraction: 0.0,
            dd_ratio: 1.0,
            log_growth: 0.0,
            final_bankroll: starting_cash,
            max_drawdown: 0.0,
        };
    }

    // Per-session return fractions
    let returns: Vec<f64> = results
        .iter()
        .map(|r| {
            let base = if compound {
                r.starting_cash
            } else {
                starting_cash
            };
            if base > 0.0 {
                r.pnl / base
            } else {
                0.0
            }
        })
        .collect();

    // Log-wealth growth and geometric mean return
    let (log_growth, geo_mean_return) =
        if let Some(bp) = bankroll_path.filter(|bp| bp.len() > 1) {
            let final_b = *bp.last().unwrap();
            let start_b = bp[0];
            if final_b > 0.0 && start_b > 0.0 {
                let lg = (final_b / start_b).ln();
                (lg, (lg / n as f64).exp() - 1.0)
            } else {
                (f64::NEG_INFINITY, -1.0)
            }
        } else {
            let mut lg = 0.0;
            let mut ok = true;
            for &ret in &returns {
                let v = 1.0 + ret;
                if v > 0.0 {
                    lg += v.ln();
                } else {
                    ok = false;
                    break;
                }
            }
            if ok && n > 0 {
                (lg, (lg / n as f64).exp() - 1.0)
            } else {
                (f64::NEG_INFINITY, -1.0)
            }
        };

    // Max drawdown and final bankroll
    let (max_drawdown, final_bankroll) =
        if let Some(bp) = bankroll_path.filter(|bp| bp.len() > 1) {
            let mut peak = bp[0];
            let mut max_dd = 0.0_f64;
            for &b in &bp[1..] {
                if b > peak {
                    peak = b;
                }
                let dd = if peak > 0.0 {
                    (peak - b) / peak
                } else {
                    0.0
                };
                if dd > max_dd {
                    max_dd = dd;
                }
            }
            (max_dd, *bp.last().unwrap())
        } else {
            let mut wealth = 1.0_f64;
            let mut peak = 1.0_f64;
            let mut max_dd = 0.0_f64;
            for &ret in &returns {
                wealth *= 1.0 + ret;
                if wealth > peak {
                    peak = wealth;
                }
                let dd = if peak > 0.0 {
                    (peak - wealth) / peak
                } else {
                    0.0
                };
                if dd > max_dd {
                    max_dd = dd;
                }
            }
            let mut fb = starting_cash;
            for &ret in &returns {
                fb *= 1.0 + ret;
            }
            (max_dd, fb)
        };

    // Kelly fraction via ternary search
    let kelly_f = ternary_kelly_search(&returns);

    // dd_ratio: max fraction keeping drawdown <= 25%
    let target_max_dd: f64 = 0.25;
    let win_count = returns.iter().filter(|&&r| r > 0.0).count();
    let traded_count = returns.iter().filter(|&&r| r != 0.0).count();
    let loss_rate = if traded_count > 0 {
        1.0 - (win_count as f64 / traded_count as f64)
    } else {
        0.5
    };
    let sessions_per_month: f64 = 8640.0;
    let consec_losses = if loss_rate > 0.0 && loss_rate < 1.0 {
        5.max(
            (sessions_per_month.ln() / (1.0 / loss_rate).ln()).ceil() as usize,
        )
    } else {
        10
    };

    let loss_fracs: Vec<f64> = results
        .iter()
        .filter(|r| r.pnl < 0.0)
        .map(|r| {
            let base = if compound {
                r.starting_cash
            } else {
                starting_cash
            };
            if base > 0.0 {
                r.pnl.abs() / base
            } else {
                0.0
            }
        })
        .collect();

    let dd_ratio = if !loss_fracs.is_empty() {
        let avg_loss_frac: f64 = loss_fracs.iter().sum::<f64>() / loss_fracs.len() as f64;
        if avg_loss_frac > 0.0 {
            let exponent: f64 = 1.0 / consec_losses as f64;
            let max_per_session_loss: f64 =
                1.0 - (1.0 - target_max_dd).powf(exponent);
            ((max_per_session_loss / avg_loss_frac).min(1.0) * 100.0).round() / 100.0
        } else {
            1.0
        }
    } else {
        1.0
    };

    let log_growth_out = if log_growth.is_finite() {
        log_growth
    } else {
        -999.0
    };

    KellyMetrics {
        geo_mean_return,
        kelly_fraction: kelly_f.max(0.0).min(1.0),
        dd_ratio,
        log_growth: log_growth_out,
        final_bankroll: final_bankroll.max(0.0),
        max_drawdown,
    }
}

/// Default fractional Kelly multiplier (half-Kelly).
pub const KELLY_FRACTION_DEFAULT: f64 = 0.5;

/// Kelly sizing output.
#[derive(Debug, Clone)]
pub struct KellySizing {
    pub kelly_f: f64,
    pub cash_pct: f64,
    pub kelly_multiplier: f64,
    pub projected_geo_return: f64,
    pub projected_final: f64,
    pub projected_max_dd: f64,
}

/// Compute optimal position sizing from raw (unscaled) session returns.
///
/// Phase 2 of the two-phase backtest: given a strategy's per-session return
/// series from flat-cash backtesting, compute the Kelly-optimal fraction
/// of bankroll to deploy per session.
pub fn compute_kelly_sizing(
    results: &[SessionResult],
    starting_cash: f64,
    kelly_multiplier: f64,
    laplace_losses: Option<usize>,
) -> KellySizing {
    let returns: Vec<f64> = results
        .iter()
        .map(|r| {
            if r.starting_cash > 0.0 {
                r.pnl / r.starting_cash
            } else {
                0.0
            }
        })
        .collect();

    let n = returns.len();
    if n == 0 {
        return KellySizing {
            kelly_f: 0.0,
            cash_pct: 0.0,
            kelly_multiplier,
            projected_geo_return: 0.0,
            projected_final: starting_cash,
            projected_max_dd: 0.0,
        };
    }

    // Laplace smoothing via Rule of Three
    let traded_returns: Vec<f64> = returns.iter().copied().filter(|&r| r != 0.0).collect();
    let loss_count = traded_returns.iter().filter(|&&r| r < 0.0).count();

    let smoothed_returns = if loss_count == 0 && !traded_returns.is_empty() {
        let n_inject = match laplace_losses {
            Some(l) => l,
            None => {
                let n_traded = traded_returns.len();
                (3.0 * n_traded as f64 / (n_traded as f64 + 3.0))
                    .round()
                    .max(1.0) as usize
            }
        };
        let mut sr = returns.clone();
        for _ in 0..n_inject {
            sr.push(-1.0);
        }
        sr
    } else {
        returns.clone()
    };

    let kelly_f = ternary_kelly_search(&smoothed_returns);
    let cash_pct = ((kelly_f * kelly_multiplier) * 10000.0).round() / 10000.0;

    // Project compound growth at recommended cash_pct
    let mut bankroll = starting_cash;
    let mut peak = starting_cash;
    let mut max_dd = 0.0_f64;
    for &ret in &returns {
        let session_cash = bankroll * cash_pct;
        let pnl = session_cash * ret;
        bankroll += pnl;
        if bankroll > peak {
            peak = bankroll;
        }
        let dd = if peak > 0.0 {
            (peak - bankroll) / peak
        } else {
            0.0
        };
        if dd > max_dd {
            max_dd = dd;
        }
    }

    let geo_return = if bankroll > 0.0 && starting_cash > 0.0 && n > 0 {
        ((bankroll / starting_cash).ln() / n as f64).exp() - 1.0
    } else {
        0.0
    };

    KellySizing {
        kelly_f,
        cash_pct,
        kelly_multiplier,
        projected_geo_return: geo_return,
        projected_final: bankroll,
        projected_max_dd: max_dd,
    }
}

// ---------------------------------------------------------------------------
// StrategyRef — allows Simulator to use a borrowed strategy with Trader
// ---------------------------------------------------------------------------

/// A wrapper that holds a raw pointer to a `dyn Strategy`, allowing
/// the Simulator to pass a borrowed strategy into Trader (which owns
/// a `Box<dyn Strategy>`).
///
/// # Safety
/// The caller must ensure the pointed-to strategy outlives this wrapper.
/// This is guaranteed within `Simulator::run_session` where the wrapper
/// lives on the stack and the strategy reference comes from the caller.
struct StrategyRef(*mut dyn Strategy);

// SAFETY: The raw pointer is only dereferenced within single-threaded
// `run_session` scope. The Strategy trait requires Send.
unsafe impl Send for StrategyRef {}

impl Strategy for StrategyRef {
    fn name(&self) -> &str {
        unsafe { &*self.0 }.name()
    }

    fn version(&self) -> &str {
        unsafe { &*self.0 }.version()
    }

    fn requires_full_session(&self) -> bool {
        unsafe { &*self.0 }.requires_full_session()
    }

    fn min_elapsed_hint(&self) -> f64 {
        unsafe { &*self.0 }.min_elapsed_hint()
    }

    fn on_session_start(&mut self, metadata: &SessionMetadata) {
        unsafe { &mut *self.0 }.on_session_start(metadata);
    }

    fn on_tick(&mut self, ctx: &TickContext) -> TradeAction {
        unsafe { &mut *self.0 }.on_tick(ctx)
    }

    fn drain_logs(&mut self) -> Vec<String> {
        unsafe { &mut *self.0 }.drain_logs()
    }

    fn param_defs(&self) -> HashMap<String, crate::strategy::Param> {
        unsafe { &*self.0 }.param_defs()
    }

    fn params(&self) -> HashMap<String, ParamValue> {
        unsafe { &*self.0 }.params()
    }

    fn with_params(&self, params: &HashMap<String, ParamValue>) -> Box<dyn Strategy> {
        unsafe { &*self.0 }.with_params(params)
    }
}

// ---------------------------------------------------------------------------
// Simulator
// ---------------------------------------------------------------------------

/// Core simulation engine. Replays sessions through strategies and computes
/// aggregate metrics.
pub struct Simulator {
    pub starting_cash: f64,
    pub execution_mode: String,
    pub slippage: f64,
    pub fees: bool,
    pub limit_fill_margin: Option<f64>,
    pub latency_ms: u64,
    pub compound: bool,
    pub session_cash_ratio: Option<(f64, f64)>,
}

impl Simulator {
    pub fn new(
        starting_cash: f64,
        execution_mode: &str,
        slippage: f64,
        fees: bool,
        limit_fill_margin: Option<f64>,
        latency_ms: u64,
        session_cash_ratio: Option<(f64, f64)>,
    ) -> Self {
        let compound = session_cash_ratio.is_some();
        Self {
            starting_cash,
            execution_mode: execution_mode.to_string(),
            slippage,
            fees,
            limit_fill_margin,
            latency_ms,
            compound,
            session_cash_ratio,
        }
    }

    /// Replay one session through a strategy.
    ///
    /// Uses a `StrategyRef` wrapper to pass the borrowed strategy into the
    /// `Trader` without transferring ownership. The strategy's mutable state
    /// (session start, tick processing) is forwarded through the raw pointer.
    pub fn run_session(
        &self,
        strategy: &mut dyn Strategy,
        session: &SessionData,
        session_cash: Option<f64>,
    ) -> SessionResult {
        let cash = session_cash.unwrap_or(self.starting_cash);
        let samples = &session.samples;
        let outcome = session.outcome.as_deref().unwrap_or("");
        let initial_btc = session.initial_btc_price_usd.unwrap_or(0.0);
        let source_file = &session.source_file;

        // Build SessionMetadata from the session data
        let prior_summaries = session._prior_summaries.clone().unwrap_or_default();

        // Compute window duration from samples
        let window_duration = if samples.len() >= 2 {
            samples.last().unwrap().elapsed_seconds
        } else {
            300.0
        };

        let metadata = SessionMetadata {
            up_token_id: session
                .metadata
                .up_token_id
                .clone()
                .unwrap_or_default(),
            down_token_id: session
                .metadata
                .down_token_id
                .clone()
                .unwrap_or_default(),
            window_start_utc: session
                .metadata
                .window_start_utc
                .clone()
                .unwrap_or_default(),
            window_duration,
            prior_session_summaries: prior_summaries,
            gap_before_seconds: None,
            data_density: 1.0,
            extra: HashMap::new(),
        };

        let position = Position::new(cash);
        let executor = SimExecutor::new(
            &self.execution_mode,
            self.slippage,
            self.fees,
            self.limit_fill_margin,
            self.latency_ms,
        );

        // SAFETY: StrategyRef wraps a raw pointer to strategy. The pointer
        // remains valid for the entire scope: trader is created, used, and
        // dropped before this function returns and the borrow ends.
        // We transmute to erase the lifetime from the trait object pointer,
        // which is safe because the StrategyRef does not outlive this scope.
        let raw_ptr: *mut dyn Strategy = strategy;
        let static_ptr: *mut (dyn Strategy + 'static) =
            unsafe { std::mem::transmute(raw_ptr) };
        let strategy_ref = StrategyRef(static_ptr);
        let mut trader = Trader::new(Box::new(strategy_ref), executor);
        trader.start_session(metadata, position, initial_btc, window_duration);

        for sample in samples {
            trader.process_tick(sample);
        }

        let trades = trader.end_session(if outcome.is_empty() {
            None
        } else {
            Some(outcome)
        });

        let final_position = trader
            .position()
            .cloned()
            .unwrap_or_else(|| Position::new(0.0));
        let pnl = final_position.cash - cash;
        let pnl_pct = if cash > 0.0 {
            (pnl / cash) * 100.0
        } else {
            0.0
        };
        let total_fees: f64 = trades.iter().map(|t| t.fee.unwrap_or(0.0)).sum();

        SessionResult {
            session_file: source_file.clone(),
            outcome: outcome.to_string(),
            pnl,
            pnl_pct,
            trades,
            final_position,
            num_samples: samples.len(),
            starting_cash: cash,
            total_fees,
        }
    }

    /// Run a strategy across all sessions and aggregate stats.
    pub fn run_backtest(
        &self,
        strategy: &mut dyn Strategy,
        sessions: &[SessionData],
    ) -> BacktestResult {
        let strategy_name = strategy.name().to_string();
        let strategy_params = strategy.params();
        let mut results = Vec::new();
        let mut bankroll = self.starting_cash;
        let mut bankroll_path = vec![bankroll];

        for session in sessions {
            if self.compound {
                if bankroll < 0.01 {
                    break;
                }
                let session_cash = if let Some((ratio, cap)) = self.session_cash_ratio {
                    (bankroll * ratio).min(cap)
                } else {
                    bankroll
                };
                let result = self.run_session(strategy, session, Some(session_cash));
                bankroll = (bankroll - session_cash) + result.final_position.cash;
                bankroll_path.push(bankroll);
                results.push(result);
            } else {
                let result = self.run_session(strategy, session, None);
                results.push(result);
            }
        }

        let total_pnl: f64 = results.iter().map(|r| r.pnl).sum();
        let total_fees: f64 = results.iter().map(|r| r.total_fees).sum();
        let n = results.len();
        let mean_pnl = if n > 0 {
            total_pnl / n as f64
        } else {
            0.0
        };
        let traded = results.iter().filter(|r| !r.trades.is_empty()).count();
        let wins = results
            .iter()
            .filter(|r| !r.trades.is_empty() && r.pnl > 0.0)
            .count();
        let entry_rate = if n > 0 {
            traded as f64 / n as f64
        } else {
            0.0
        };
        let win_rate = if traded > 0 {
            wins as f64 / traded as f64
        } else {
            0.0
        };

        // Sharpe: mean / std of per-session PnL
        let sharpe = if n > 1 {
            let pnls: Vec<f64> = results.iter().map(|r| r.pnl).collect();
            let variance: f64 =
                pnls.iter().map(|&p| (p - mean_pnl).powi(2)).sum::<f64>() / (n - 1) as f64;
            let std = variance.sqrt();
            if std > 0.0 {
                mean_pnl / std
            } else {
                0.0
            }
        } else {
            0.0
        };

        let kelly = compute_kelly_metrics(
            &results,
            self.starting_cash,
            self.compound,
            if self.compound {
                Some(bankroll_path.as_slice())
            } else {
                None
            },
        );

        BacktestResult {
            strategy_name,
            params: strategy_params,
            sessions: results,
            total_pnl,
            mean_pnl,
            entry_rate,
            win_rate,
            sharpe,
            num_sessions: n,
            geo_mean_return: kelly.geo_mean_return,
            kelly_fraction: kelly.kelly_fraction,
            dd_ratio: kelly.dd_ratio,
            log_growth: kelly.log_growth,
            final_bankroll: kelly.final_bankroll,
            max_drawdown: kelly.max_drawdown,
            total_fees,
        }
    }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/// Resolve a data directory path: if it looks like a slug key, expand to
/// the market sessions directory.
pub fn resolve_data_dir(raw_path: &str) -> String {
    let path = Path::new(raw_path);
    if path.is_dir() {
        return raw_path.to_string();
    }
    let sk = slug_key(raw_path);
    let market_path = market_sessions_dir(&sk);
    if market_path.is_dir() {
        return market_path.to_string_lossy().to_string();
    }
    raw_path.to_string()
}

/// Infer window duration from data_dir path by matching against known markets.
pub fn window_duration_from_data_dir(data_dir: &str) -> Option<u64> {
    let norm_dir = std::path::Path::new(data_dir)
        .canonicalize()
        .unwrap_or_else(|_| PathBuf::from(data_dir));
    let norm_str = norm_dir.to_string_lossy();

    let markets = known_markets();
    for (prefix, cfg) in &markets {
        let sk = slug_key(prefix);
        if norm_str.contains(&sk) {
            return Some(cfg.window_duration_seconds);
        }
    }
    None
}

// ---------------------------------------------------------------------------
// Sharpe decay detection
// ---------------------------------------------------------------------------

/// Threshold for absolute Sharpe decay to flag concern.
pub const SHARPE_DECAY_THRESHOLD: f64 = 0.3;

/// Fraction of recent sessions used for decay comparison.
pub const SHARPE_DECAY_RECENT_FRACTION: f64 = 0.33;

/// Default number of walk-forward folds.
pub const WALK_FORWARD_FOLDS: usize = 3;

/// Default training ratio for walk-forward validation.
pub const WALK_FORWARD_TRAIN_RATIO: f64 = 0.67;

/// Minimum Sharpe improvement to consider a param refinement worthwhile.
pub const MIN_SHARPE_IMPROVEMENT: f64 = 0.05;

/// Result of Sharpe decay analysis.
#[derive(Debug, Clone)]
pub struct SharpeDecayInfo {
    pub full_sharpe: f64,
    pub recent_sharpe: f64,
    pub decaying: bool,
    pub decay_amount: f64,
}

/// Detect whether a strategy's Sharpe ratio is decaying over recent sessions.
///
/// Compares full-period Sharpe to the Sharpe of the most recent `recent_fraction`
/// of sessions. Flags decay when the drop exceeds `SHARPE_DECAY_THRESHOLD` and
/// the recent Sharpe is less than half the full Sharpe.
pub fn detect_sharpe_decay(results: &[SessionResult], recent_fraction: f64) -> SharpeDecayInfo {
    let n = results.len();
    if n < 6 {
        return SharpeDecayInfo {
            full_sharpe: 0.0,
            recent_sharpe: 0.0,
            decaying: false,
            decay_amount: 0.0,
        };
    }

    let pnls: Vec<f64> = results.iter().map(|r| r.pnl).collect();
    let mean_all: f64 = pnls.iter().sum::<f64>() / n as f64;
    let var_all: f64 =
        pnls.iter().map(|&p| (p - mean_all).powi(2)).sum::<f64>() / (n - 1) as f64;
    let std_all = var_all.sqrt();
    let full_sharpe = if std_all > 0.0 {
        mean_all / std_all
    } else {
        0.0
    };

    let recent_n = 3.max((n as f64 * recent_fraction) as usize);
    let recent_pnls = &pnls[n - recent_n..];
    let mean_recent: f64 = recent_pnls.iter().sum::<f64>() / recent_n as f64;
    let recent_sharpe = if recent_n > 1 {
        let var_recent: f64 = recent_pnls
            .iter()
            .map(|&p| (p - mean_recent).powi(2))
            .sum::<f64>()
            / (recent_n - 1) as f64;
        let std_recent = var_recent.sqrt();
        if std_recent > 0.0 {
            mean_recent / std_recent
        } else {
            0.0
        }
    } else {
        0.0
    };

    let decay = full_sharpe - recent_sharpe;
    let decaying = decay > SHARPE_DECAY_THRESHOLD && recent_sharpe < full_sharpe * 0.5;

    SharpeDecayInfo {
        full_sharpe: (full_sharpe * 1000.0).round() / 1000.0,
        recent_sharpe: (recent_sharpe * 1000.0).round() / 1000.0,
        decaying,
        decay_amount: (decay * 1000.0).round() / 1000.0,
    }
}

// ---------------------------------------------------------------------------
// Walk-forward validation
// ---------------------------------------------------------------------------

/// Result of walk-forward cross-validation.
#[derive(Debug, Clone)]
pub struct WalkForwardResult {
    pub fold_sharpes: Vec<f64>,
    pub mean_val_sharpe: f64,
    pub min_val_sharpe: f64,
    pub stable: bool,
    pub skipped: bool,
}

/// Walk-forward validation: split sessions into expanding train/validation
/// windows and measure out-of-sample Sharpe stability.
///
/// Creates `folds` sliding windows where each fold trains on `train_ratio`
/// of the data and validates on the next block. Returns per-fold Sharpe
/// ratios and an overall stability flag (all folds positive).
pub fn walk_forward_validate(
    strategy: &dyn Strategy,
    sessions: &[SessionData],
    starting_cash: f64,
    folds: usize,
    train_ratio: f64,
) -> WalkForwardResult {
    let n = sessions.len();
    if n < 10 {
        return WalkForwardResult {
            fold_sharpes: vec![],
            mean_val_sharpe: 0.0,
            min_val_sharpe: 0.0,
            stable: true,
            skipped: true,
        };
    }

    let train_size = 5.max((n as f64 * train_ratio) as usize);
    let step = 1.max((n - train_size) / folds.max(1));

    let mut fold_sharpes = Vec::new();
    let sim = Simulator::new(starting_cash, "book", 0.02, false, None, 0, None);

    for fold in 0..folds {
        let train_start = fold * step;
        let train_end = (train_start + train_size).min(n);
        let val_start = train_end;
        let val_end = (val_start + step).min(n);

        if val_start >= n || val_end <= val_start {
            break;
        }

        let val_sessions = &sessions[val_start..val_end];
        if val_sessions.len() < 3 {
            continue;
        }

        let params = strategy.params();
        let mut strat = strategy.with_params(&params);
        let result = sim.run_backtest(strat.as_mut(), val_sessions);
        fold_sharpes.push(result.sharpe);
    }

    if fold_sharpes.is_empty() {
        return WalkForwardResult {
            fold_sharpes: vec![],
            mean_val_sharpe: 0.0,
            min_val_sharpe: 0.0,
            stable: true,
            skipped: true,
        };
    }

    let mean_val = fold_sharpes.iter().sum::<f64>() / fold_sharpes.len() as f64;
    let min_val = fold_sharpes
        .iter()
        .cloned()
        .fold(f64::INFINITY, f64::min);

    WalkForwardResult {
        fold_sharpes: fold_sharpes.clone(),
        mean_val_sharpe: mean_val,
        min_val_sharpe: min_val,
        stable: fold_sharpes.iter().all(|&s| s > 0.0),
        skipped: false,
    }
}

// ---------------------------------------------------------------------------
// Edge detection and grid refinement
// ---------------------------------------------------------------------------

/// Detect which parameters are at the edge of their grid range.
///
/// Returns a map of param_name -> "low" or "high" for any best param
/// whose value equals the minimum or maximum value in the grid.
pub fn detect_edge_params(
    best_params: &HashMap<String, ParamValue>,
    grid: &HashMap<String, Vec<ParamValue>>,
) -> HashMap<String, String> {
    let mut edges = HashMap::new();

    for (name, best_val) in best_params {
        if let Some(grid_vals) = grid.get(name) {
            if grid_vals.len() < 2 {
                continue;
            }
            let best_f = match best_val.as_f64() {
                Some(v) => v,
                None => continue,
            };
            let first_f = match grid_vals.first().and_then(|v| v.as_f64()) {
                Some(v) => v,
                None => continue,
            };
            let last_f = match grid_vals.last().and_then(|v| v.as_f64()) {
                Some(v) => v,
                None => continue,
            };

            if (best_f - first_f).abs() < 1e-9 {
                edges.insert(name.clone(), "low".to_string());
            } else if (best_f - last_f).abs() < 1e-9 {
                edges.insert(name.clone(), "high".to_string());
            }
        }
    }

    edges
}

/// Build a refined grid that extends beyond edge parameters.
///
/// For params flagged as "low" edge, extends the grid below the current minimum.
/// For "high" edge, extends above the current maximum. Interior params keep
/// their original grid values.
pub fn build_refined_grid(
    _best_params: &HashMap<String, ParamValue>,
    grid: &HashMap<String, Vec<ParamValue>>,
    edges: &HashMap<String, String>,
) -> HashMap<String, Vec<ParamValue>> {
    let mut refined = grid.clone();

    for (name, edge_side) in edges {
        let grid_vals = match grid.get(name) {
            Some(v) if v.len() >= 2 => v,
            _ => continue,
        };

        let first_f = match grid_vals.first().and_then(|v| v.as_f64()) {
            Some(v) => v,
            None => continue,
        };
        let last_f = match grid_vals.last().and_then(|v| v.as_f64()) {
            Some(v) => v,
            None => continue,
        };
        let step = (last_f - first_f) / (grid_vals.len() - 1) as f64;
        if step <= 0.0 {
            continue;
        }

        let is_int = matches!(grid_vals.first(), Some(ParamValue::Int(_)));

        let mut new_vals: Vec<ParamValue> = grid_vals.clone();

        if edge_side == "low" {
            // Extend below: add 2 steps below the minimum
            for i in 1..=2 {
                let v = first_f - step * i as f64;
                if v < 0.0 {
                    break;
                }
                if is_int {
                    new_vals.insert(0, ParamValue::Int(v.round() as i64));
                } else {
                    new_vals.insert(0, ParamValue::Float(v));
                }
            }
        } else if edge_side == "high" {
            // Extend above: add 2 steps above the maximum
            for i in 1..=2 {
                let v = last_f + step * i as f64;
                if is_int {
                    new_vals.push(ParamValue::Int(v.round() as i64));
                } else {
                    new_vals.push(ParamValue::Float(v));
                }
            }
        }

        refined.insert(name.clone(), new_vals);
    }

    refined
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/// Format parameter map for display.
pub fn format_params(params: &HashMap<String, ParamValue>) -> String {
    if params.is_empty() {
        return "-".to_string();
    }
    let mut parts: Vec<String> = params
        .iter()
        .map(|(k, v)| match v {
            ParamValue::Float(f) => format!("{}={:.3}", k, f),
            ParamValue::Int(i) => format!("{}={}", k, i),
            ParamValue::Bool(b) => format!("{}={}", k, b),
            ParamValue::None => format!("{}=None", k),
        })
        .collect();
    parts.sort();
    parts.join(", ")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_ternary_kelly_search_empty() {
        assert_eq!(ternary_kelly_search(&[]), 0.0);
    }

    #[test]
    fn test_ternary_kelly_search_all_positive() {
        let returns = vec![0.1, 0.2, 0.05, 0.15, 0.1];
        let f = ternary_kelly_search(&returns);
        assert!(f > 0.0);
        assert!(f <= 1.0);
    }

    #[test]
    fn test_ternary_kelly_search_all_negative() {
        let returns = vec![-0.1, -0.2, -0.05];
        let f = ternary_kelly_search(&returns);
        assert_eq!(f, 0.0);
    }

    #[test]
    fn test_log_growth_at_f() {
        let returns = vec![0.1, -0.05, 0.2];
        let g = log_growth_at_f(1.0, &returns);
        assert!(g.is_finite());
        assert!(g > 0.0);

        let g0 = log_growth_at_f(0.0, &returns);
        assert!((g0 - 0.0).abs() < 1e-10);
    }

    #[test]
    fn test_log_growth_at_f_ruin() {
        let returns = vec![-1.5];
        let g = log_growth_at_f(1.0, &returns);
        assert!(g.is_infinite() && g < 0.0);
    }

    #[test]
    fn test_format_params_empty() {
        let p: HashMap<String, ParamValue> = HashMap::new();
        assert_eq!(format_params(&p), "-");
    }

    #[test]
    fn test_format_params() {
        let mut p = HashMap::new();
        p.insert("alpha".to_string(), ParamValue::Float(0.5));
        p.insert("beta".to_string(), ParamValue::Int(3));
        let s = format_params(&p);
        assert!(s.contains("alpha="));
        assert!(s.contains("beta=3"));
    }

    #[test]
    fn test_compute_kelly_sizing_empty() {
        let sizing = compute_kelly_sizing(&[], 100.0, 0.5, None);
        assert_eq!(sizing.kelly_f, 0.0);
        assert_eq!(sizing.cash_pct, 0.0);
        assert_eq!(sizing.projected_final, 100.0);
    }

    #[test]
    fn test_resolve_data_dir_passthrough() {
        let result = resolve_data_dir("/nonexistent/path");
        assert_eq!(result, "/nonexistent/path");
    }

    #[test]
    fn test_compute_kelly_metrics_empty() {
        let km = compute_kelly_metrics(&[], 100.0, false, None);
        assert_eq!(km.geo_mean_return, 0.0);
        assert_eq!(km.kelly_fraction, 0.0);
        assert_eq!(km.final_bankroll, 100.0);
    }

    // -- Sharpe decay tests --

    fn make_session_result(pnl: f64) -> SessionResult {
        SessionResult {
            session_file: "test.json".to_string(),
            outcome: "Up".to_string(),
            pnl,
            pnl_pct: pnl,
            trades: vec![],
            final_position: Position::new(100.0 + pnl),
            num_samples: 100,
            starting_cash: 100.0,
            total_fees: 0.0,
        }
    }

    #[test]
    fn test_sharpe_decay_stable() {
        // Stable positive PnL series -> no decay
        let results: Vec<SessionResult> = (0..20)
            .map(|_| make_session_result(2.0))
            .collect();
        let info = detect_sharpe_decay(&results, SHARPE_DECAY_RECENT_FRACTION);
        assert!(!info.decaying);
    }

    #[test]
    fn test_sharpe_decay_detected() {
        // Good start with variance, then losses -> decay detected
        let mut results: Vec<SessionResult> = Vec::new();
        for i in 0..20 {
            // Positive returns with some variance
            let pnl = 3.0 + (i as f64 % 3.0);
            results.push(make_session_result(pnl));
        }
        for i in 0..10 {
            // Negative returns in recent period
            let pnl = -4.0 - (i as f64 % 2.0);
            results.push(make_session_result(pnl));
        }
        let info = detect_sharpe_decay(&results, SHARPE_DECAY_RECENT_FRACTION);
        assert!(info.full_sharpe > info.recent_sharpe,
            "full {} should exceed recent {}", info.full_sharpe, info.recent_sharpe);
        assert!(info.decaying,
            "decay_amount={}, threshold={}, recent={}, full*0.5={}",
            info.decay_amount, SHARPE_DECAY_THRESHOLD, info.recent_sharpe, info.full_sharpe * 0.5);
    }

    #[test]
    fn test_sharpe_decay_insufficient() {
        // Too few sessions -> no decay reported
        let results: Vec<SessionResult> = (0..3)
            .map(|_| make_session_result(1.0))
            .collect();
        let info = detect_sharpe_decay(&results, SHARPE_DECAY_RECENT_FRACTION);
        assert!(!info.decaying);
        assert_eq!(info.full_sharpe, 0.0);
    }

    // -- Walk-forward tests --

    #[test]
    fn test_walk_forward_skips_small_dataset() {
        // < 10 sessions -> skipped
        let result = walk_forward_validate(
            &crate::strategies::poisson_kelly::PoissonKelly::default(),
            &[],
            100.0,
            WALK_FORWARD_FOLDS,
            WALK_FORWARD_TRAIN_RATIO,
        );
        assert!(result.skipped);
        assert!(result.fold_sharpes.is_empty());
    }

    // -- Edge param tests --

    #[test]
    fn test_detect_edge_params_at_boundary() {
        let mut best = HashMap::new();
        best.insert("alpha".to_string(), ParamValue::Float(0.1));
        best.insert("beta".to_string(), ParamValue::Float(0.9));

        let mut grid = HashMap::new();
        grid.insert(
            "alpha".to_string(),
            vec![
                ParamValue::Float(0.1),
                ParamValue::Float(0.3),
                ParamValue::Float(0.5),
            ],
        );
        grid.insert(
            "beta".to_string(),
            vec![
                ParamValue::Float(0.3),
                ParamValue::Float(0.6),
                ParamValue::Float(0.9),
            ],
        );

        let edges = detect_edge_params(&best, &grid);
        assert_eq!(edges.get("alpha").map(|s| s.as_str()), Some("low"));
        assert_eq!(edges.get("beta").map(|s| s.as_str()), Some("high"));
    }

    #[test]
    fn test_detect_edge_params_interior() {
        let mut best = HashMap::new();
        best.insert("alpha".to_string(), ParamValue::Float(0.3));

        let mut grid = HashMap::new();
        grid.insert(
            "alpha".to_string(),
            vec![
                ParamValue::Float(0.1),
                ParamValue::Float(0.3),
                ParamValue::Float(0.5),
            ],
        );

        let edges = detect_edge_params(&best, &grid);
        assert!(edges.is_empty());
    }

    #[test]
    fn test_build_refined_grid_extends() {
        let mut best = HashMap::new();
        best.insert("alpha".to_string(), ParamValue::Float(0.5));

        let mut grid = HashMap::new();
        grid.insert(
            "alpha".to_string(),
            vec![
                ParamValue::Float(0.1),
                ParamValue::Float(0.3),
                ParamValue::Float(0.5),
            ],
        );

        let mut edges = HashMap::new();
        edges.insert("alpha".to_string(), "high".to_string());

        let refined = build_refined_grid(&best, &grid, &edges);
        let alpha_vals = refined.get("alpha").unwrap();
        // Original had 3 values, should now have 5 (extended by 2 above)
        assert_eq!(alpha_vals.len(), 5);
        // Last value should be 0.5 + 0.2*2 = 0.9
        let last = alpha_vals.last().unwrap().as_f64().unwrap();
        assert!((last - 0.9).abs() < 1e-9);
    }

    #[test]
    fn test_compute_kelly_metrics_with_results() {
        let results = vec![
            SessionResult {
                session_file: "s1.json".to_string(),
                outcome: "Up".to_string(),
                pnl: 5.0,
                pnl_pct: 5.0,
                trades: vec![],
                final_position: Position::new(105.0),
                num_samples: 100,
                starting_cash: 100.0,
                total_fees: 0.1,
            },
            SessionResult {
                session_file: "s2.json".to_string(),
                outcome: "Down".to_string(),
                pnl: -3.0,
                pnl_pct: -3.0,
                trades: vec![],
                final_position: Position::new(97.0),
                num_samples: 100,
                starting_cash: 100.0,
                total_fees: 0.1,
            },
        ];
        let km = compute_kelly_metrics(&results, 100.0, false, None);
        assert!(km.geo_mean_return > -1.0);
        assert!(km.max_drawdown >= 0.0);
    }
}
