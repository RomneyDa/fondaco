//! CLI entry point for backtesting strategies.
//!
//! Ports the logic from `simulator/backtest.py` to Rust. Uses clap for argument
//! parsing and rayon for parallel variant evaluation.

use clap::Parser;
use rayon::prelude::*;
use std::collections::HashMap;

use fondaco::defaults::{market_backtest_log, slug_key, DEFAULT_LATENCY_MS};
use fondaco::engine::{
    compute_kelly_sizing, format_params, load_session_summaries, load_sessions,
    resolve_data_dir, window_duration_from_data_dir, BacktestResult,
    KellySizing, Simulator, KELLY_FRACTION_DEFAULT,
};
use fondaco::models::SessionData;
use fondaco::session_cache::SessionStatsCache;
use fondaco::strategies::all_strategies;
use fondaco::strategy::{ParamValue, Strategy};

// ---------------------------------------------------------------------------
// CLI definition
// ---------------------------------------------------------------------------

#[derive(Parser)]
#[command(
    name = "backtest",
    about = "Backtest trading strategies on Polymarket session data."
)]
struct Cli {
    /// Path to session files directory (or slug key shorthand)
    data_dir: String,

    /// Strategy name or 'all'
    #[arg(long, default_value = "all")]
    strategy: String,

    /// Metric to optimize
    #[arg(long, default_value = "sharpe", value_parser = ["sharpe", "total_pnl", "win_rate", "kelly_growth"])]
    optimize: String,

    /// Fractional Kelly multiplier for sizing (0.5 = half-Kelly)
    #[arg(long, default_value_t = KELLY_FRACTION_DEFAULT)]
    kelly_fraction: f64,

    /// Override Laplace smoothing: inject N synthetic losses when none observed
    #[arg(long)]
    laplace: Option<usize>,

    /// Execution mode
    #[arg(long, default_value = "book", value_parser = ["midpoint_slippage", "midpoint", "book"])]
    execution: String,

    /// Half-spread for midpoint_slippage mode
    #[arg(long, default_value_t = 0.02)]
    slippage: f64,

    /// Midpoint margin for GTC/GTD fill simulation
    #[arg(long)]
    limit_fill_margin: Option<f64>,

    /// Starting cash per session
    #[arg(long, default_value_t = 100.0)]
    cash: f64,

    /// Show top N parameter combos
    #[arg(long, default_value_t = 10)]
    top: usize,

    /// Only use the N most recent sessions
    #[arg(long)]
    recent: Option<usize>,

    /// Per-session breakdown for best params
    #[arg(long)]
    verbose: bool,

    /// Override param grid values (repeatable). e.g. --param min_elapsed=0.4,0.5,0.6
    #[arg(long, value_name = "KEY=v1,v2,...")]
    param: Vec<String>,

    /// Run full param_grid() sweep
    #[arg(long)]
    sweep: bool,

    /// Two-stage screening: quick pass on 1/3 of sessions to find top N
    #[arg(long)]
    screen: Option<usize>,

    /// Simulated API latency in milliseconds
    #[arg(long, default_value_t = DEFAULT_LATENCY_MS)]
    latency: u64,

    /// Disable taker fee simulation
    #[arg(long)]
    no_fees: bool,

    /// Compound mode: deploy R% of bankroll per session, capped at $M (e.g. 0.08:100)
    #[arg(long, value_name = "R:M")]
    session_cash_ratio: Option<String>,
}

// ---------------------------------------------------------------------------
// Param overrides & variant building
// ---------------------------------------------------------------------------

/// Parse --param KEY=v1,v2,... into {key: [values]}.
fn parse_param_overrides(param_args: &[String]) -> HashMap<String, Vec<ParamValue>> {
    let mut overrides: HashMap<String, Vec<ParamValue>> = HashMap::new();
    for arg in param_args {
        let (key, vals_str) = match arg.split_once('=') {
            Some(pair) => pair,
            None => {
                eprintln!("Invalid --param format: '{}'. Expected KEY=v1,v2,...", arg);
                std::process::exit(1);
            }
        };
        let mut values = Vec::new();
        for v in vals_str.split(',') {
            let v = v.trim();
            match v.to_lowercase().as_str() {
                "none" => values.push(ParamValue::None),
                "true" => values.push(ParamValue::Bool(true)),
                "false" => values.push(ParamValue::Bool(false)),
                _ => {
                    if let Ok(i) = v.parse::<i64>() {
                        values.push(ParamValue::Int(i));
                    } else if let Ok(f) = v.parse::<f64>() {
                        values.push(ParamValue::Float(f));
                    } else {
                        // Fall back to float 0 for unparseable (shouldn't happen)
                        eprintln!("Warning: cannot parse param value '{}', skipping", v);
                    }
                }
            }
        }
        overrides.insert(key.to_string(), values);
    }
    overrides
}

/// Build all parameter combinations from a strategy's param_grid.
fn build_variants(
    base_strategy: &dyn Strategy,
    grid_overrides: Option<&HashMap<String, Vec<ParamValue>>>,
    sweep: bool,
) -> Vec<Box<dyn Strategy>> {
    let mut grid: HashMap<String, Vec<ParamValue>> = if sweep || grid_overrides.is_some() {
        base_strategy.param_grid(10000)
    } else {
        HashMap::new()
    };

    if let Some(overrides) = grid_overrides {
        for (k, v) in overrides {
            grid.insert(k.clone(), v.clone());
        }
    }

    if grid.is_empty() {
        return vec![base_strategy.with_params(&base_strategy.params())];
    }

    // Build cartesian product of all grid values
    let keys: Vec<String> = grid.keys().cloned().collect::<Vec<_>>();
    let mut keys_sorted = keys;
    keys_sorted.sort();
    let value_lists: Vec<&Vec<ParamValue>> = keys_sorted.iter().map(|k| &grid[k]).collect();

    let mut combos: Vec<HashMap<String, ParamValue>> = vec![HashMap::new()];
    for (i, key) in keys_sorted.iter().enumerate() {
        let mut new_combos = Vec::new();
        for combo in &combos {
            for val in value_lists[i] {
                let mut new_combo = combo.clone();
                new_combo.insert(key.clone(), val.clone());
                new_combos.push(new_combo);
            }
        }
        combos = new_combos;
    }

    combos
        .iter()
        .map(|params| base_strategy.with_params(params))
        .collect()
}

// ---------------------------------------------------------------------------
// Output formatting
// ---------------------------------------------------------------------------

fn sort_key(r: &BacktestResult, optimize: &str) -> f64 {
    match optimize {
        "sharpe" => r.sharpe,
        "total_pnl" => r.total_pnl,
        "win_rate" => r.win_rate,
        "kelly_growth" => r.geo_mean_return,
        _ => r.sharpe,
    }
}

fn print_leaderboard(results: &mut [BacktestResult], top_n: usize, optimize: &str, compound: bool) {
    results.sort_by(|a, b| {
        sort_key(b, optimize)
            .partial_cmp(&sort_key(a, optimize))
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    let shown = &results[..top_n.min(results.len())];

    if compound {
        println!(
            "\n{:<20} {:>8} {:>8} {:>8} {:>10} {:>8} {:>8} {:>8} {:>6}  {}",
            "Strategy", "GeoRet%", "Kelly_f", "MaxDD%", "Final$", "Fees", "Entry%", "Win%", "N", "Params"
        );
        println!("{}", "-".repeat(125));
        for r in shown {
            println!(
                "{:<20} {:>8.3} {:>8.3} {:>8.1} {:>10.2} {:>8.2} {:>7.1}% {:>7.1}% {:>6}  {}",
                r.strategy_name,
                r.geo_mean_return * 100.0,
                r.kelly_fraction,
                r.max_drawdown * 100.0,
                r.final_bankroll,
                r.total_fees,
                r.entry_rate * 100.0,
                r.win_rate * 100.0,
                r.num_sessions,
                format_params(&r.params),
            );
        }
    } else {
        println!(
            "\n{:<20} {:>8} {:>10} {:>10} {:>8} {:>8} {:>8} {:>8} {:>6}  {}",
            "Strategy", "Sharpe", "Total PnL", "Mean PnL", "Fees", "GeoRet%", "Entry%", "Win%", "N", "Params"
        );
        println!("{}", "-".repeat(125));
        for r in shown {
            println!(
                "{:<20} {:>8.3} {:>10.2} {:>10.2} {:>8.2} {:>8.3} {:>7.1}% {:>7.1}% {:>6}  {}",
                r.strategy_name,
                r.sharpe,
                r.total_pnl,
                r.mean_pnl,
                r.total_fees,
                r.geo_mean_return * 100.0,
                r.entry_rate * 100.0,
                r.win_rate * 100.0,
                r.num_sessions,
                format_params(&r.params),
            );
        }
    }
    println!();
}

fn print_verbose(result: &BacktestResult) {
    println!(
        "\n--- {} [{}] ---",
        result.strategy_name,
        format_params(&result.params)
    );
    for sr in &result.sessions {
        let trade_parts: Vec<String> = sr
            .trades
            .iter()
            .map(|t| {
                let fee_str = match t.fee {
                    Some(f) if f > 0.0 => format!(" fee={:.2}", f),
                    _ => String::new(),
                };
                format!("{}@{:.3}{}", t.side, t.price, fee_str)
            })
            .collect();
        let trade_str = if trade_parts.is_empty() {
            String::new()
        } else {
            format!(" | {}", trade_parts.join(", "))
        };
        let fee_str = if sr.total_fees > 0.0 {
            format!(" fees={:.2}", sr.total_fees)
        } else {
            String::new()
        };
        let file_name = sr
            .session_file
            .rsplit('/')
            .next()
            .unwrap_or("?");
        println!(
            "  {} outcome={:<4} pnl={:>+7.2}{} trades={:<2}{}",
            file_name, sr.outcome, sr.pnl, fee_str, sr.trades.len(), trade_str,
        );
    }
    println!(
        "  TOTAL: pnl={:+.2}  mean={:+.2}  entry={:.1}%  win={:.1}%  sharpe={:.3}",
        result.total_pnl,
        result.mean_pnl,
        result.entry_rate * 100.0,
        result.win_rate * 100.0,
        result.sharpe,
    );
}

// ---------------------------------------------------------------------------
// Logging
// ---------------------------------------------------------------------------

fn log_backtest_run(
    args: &Cli,
    num_sessions: usize,
    all_results: &[BacktestResult],
    optimize: &str,
    backtest_log_file: &str,
    kelly_sizings: &HashMap<String, KellySizing>,
) {
    // Group by strategy, find best per strategy
    let mut by_strategy: HashMap<String, Vec<&BacktestResult>> = HashMap::new();
    for r in all_results {
        by_strategy
            .entry(r.strategy_name.clone())
            .or_default()
            .push(r);
    }

    let mut best_per_strategy = Vec::new();
    for name in by_strategy.keys() {
        let variants = &by_strategy[name];
        let best = variants
            .iter()
            .max_by(|a, b| {
                sort_key(a, optimize)
                    .partial_cmp(&sort_key(b, optimize))
                    .unwrap_or(std::cmp::Ordering::Equal)
            })
            .unwrap();

        let mut entry = serde_json::json!({
            "strategy": best.strategy_name,
            "params": format_params(&best.params),
            "variants_tested": variants.len(),
            "sharpe": (best.sharpe * 10000.0).round() / 10000.0,
            "total_pnl": (best.total_pnl * 10000.0).round() / 10000.0,
            "mean_pnl": (best.mean_pnl * 10000.0).round() / 10000.0,
            "entry_rate": (best.entry_rate * 10000.0).round() / 10000.0,
            "win_rate": (best.win_rate * 10000.0).round() / 10000.0,
            "num_sessions": best.num_sessions,
            "total_fees": (best.total_fees * 10000.0).round() / 10000.0,
        });

        if let Some(kelly) = kelly_sizings.get(name.as_str()) {
            entry["kelly_f"] = serde_json::json!((kelly.kelly_f * 10000.0).round() / 10000.0);
            entry["cash_pct"] = serde_json::json!((kelly.cash_pct * 10000.0).round() / 10000.0);
            entry["kelly_multiplier"] = serde_json::json!(kelly.kelly_multiplier);
            entry["projected_final"] =
                serde_json::json!((kelly.projected_final * 100.0).round() / 100.0);
            entry["projected_max_dd"] =
                serde_json::json!((kelly.projected_max_dd * 10000.0).round() / 10000.0);
            entry["projected_geo_return"] =
                serde_json::json!((kelly.projected_geo_return * 1_000_000.0).round() / 1_000_000.0);
        }

        best_per_strategy.push(entry);
    }

    let log_entry = serde_json::json!({
        "timestamp": chrono::Utc::now().to_rfc3339(),
        "inputs": {
            "data_dir": args.data_dir,
            "strategy": args.strategy,
            "execution": args.execution,
            "slippage": args.slippage,
            "latency_ms": args.latency,
            "starting_cash": args.cash,
            "kelly_fraction": args.kelly_fraction,
            "optimize": optimize,
            "fees": !args.no_fees,
            "num_sessions": num_sessions,
            "total_variants": all_results.len(),
        },
        "best_per_strategy": best_per_strategy,
    });

    if let Some(parent) = std::path::Path::new(backtest_log_file).parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    match std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(backtest_log_file)
    {
        Ok(mut f) => {
            use std::io::Write;
            let line = serde_json::to_string(&log_entry).unwrap_or_default();
            let _ = writeln!(f, "{}", line);
        }
        Err(e) => {
            eprintln!("Warning: failed to write backtest log: {}", e);
        }
    }
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

fn main() {
    let mut args = Cli::parse();

    let starting_cash = args.cash;
    let fees = !args.no_fees;
    let kelly_mult = args.kelly_fraction;

    // Parse session-cash-ratio
    let session_cash_ratio: Option<(f64, f64)> = args.session_cash_ratio.as_ref().map(|s| {
        let parts: Vec<&str> = s.split(':').collect();
        if parts.len() != 2 {
            eprintln!(
                "Invalid --session-cash-ratio format: '{}'. Expected R:M (e.g. 0.08:100)",
                s
            );
            std::process::exit(1);
        }
        let ratio: f64 = parts[0].parse().unwrap_or_else(|_| {
            eprintln!("Invalid ratio in --session-cash-ratio: '{}'", parts[0]);
            std::process::exit(1);
        });
        let cap: f64 = parts[1].parse().unwrap_or_else(|_| {
            eprintln!("Invalid cap in --session-cash-ratio: '{}'", parts[1]);
            std::process::exit(1);
        });
        (ratio, cap)
    });

    // Resolve data dir
    args.data_dir = resolve_data_dir(&args.data_dir);
    let backtest_log_file = {
        // Extract slug key from data_dir path (e.g. "data/markets/btc-updown-5m/sessions" -> "btc-updown-5m")
        let path = std::path::Path::new(&args.data_dir);
        let sk = path
            .components()
            .filter_map(|c| c.as_os_str().to_str())
            .find(|s| s.starts_with("btc-") || s.starts_with("eth-"))
            .map(|s| slug_key(s))
            .unwrap_or_else(|| slug_key(&args.data_dir));
        let log_path = market_backtest_log(&sk);
        log_path.to_string_lossy().to_string()
    };

    // Load sessions
    let mut sessions = load_sessions(&args.data_dir, args.recent);
    if sessions.is_empty() {
        eprintln!("No resolved sessions found in: {}", args.data_dir);
        std::process::exit(1);
    }

    println!(
        "Loaded {} resolved sessions from {}",
        sessions.len(),
        args.data_dir
    );
    let latency_str = if args.latency > 0 {
        format!("  Latency: {}ms", args.latency)
    } else {
        String::new()
    };
    let compound_str = match &session_cash_ratio {
        Some((ratio, cap)) => {
            format!("  Compound: on (ratio={}, cap=${})", ratio, cap)
        }
        None => String::new(),
    };
    println!(
        "Execution: {} (slippage={})  Starting cash: ${}  Fees: {}{}{}",
        args.execution,
        args.slippage,
        starting_cash,
        if fees { "on" } else { "off" },
        latency_str,
        compound_str,
    );

    // Select strategies
    let registry = all_strategies();
    let strategy_names: Vec<String> = if args.strategy == "all" {
        registry.keys().cloned().collect()
    } else {
        if !registry.contains_key(&args.strategy) {
            eprintln!(
                "Unknown strategy: {}. Available: {}",
                args.strategy,
                registry.keys().cloned().collect::<Vec<_>>().join(", ")
            );
            std::process::exit(1);
        }
        vec![args.strategy.clone()]
    };

    // Phase 1: Strategy Selection
    println!("\n-- Phase 1: Strategy Selection (Sharpe on flat cash) --");

    // Build session summaries for lookback
    let session_summaries = load_session_summaries(&args.data_dir);
    let stats_cache = SessionStatsCache::from_summaries(&session_summaries);

    // Infer window duration
    let _window_duration = window_duration_from_data_dir(&args.data_dir).unwrap_or(300) as f64;

    // Pre-slice prior summaries
    // Use stats_cache to build per-session priors
    for session in sessions.iter_mut() {
        let ws = match session.metadata.window_start_utc.as_deref() {
            Some(s) if !s.is_empty() => s.to_string(),
            _ => continue,
        };
        let prior = stats_cache.prior(&ws, stats_cache.len());
        session._prior_summaries = Some(prior);
    }

    // Parse --param overrides
    let grid_overrides = if args.param.is_empty() {
        None
    } else {
        Some(parse_param_overrides(&args.param))
    };

    // Build all strategy variants upfront
    let mut all_variants: Vec<Box<dyn Strategy>> = Vec::new();
    for name in &strategy_names {
        let factory = &registry[name];
        let base = factory();
        let variants = build_variants(
            base.as_ref(),
            grid_overrides.as_ref(),
            args.sweep,
        );
        all_variants.extend(variants);
    }

    let total_variants = all_variants.len();

    // Warn on large grids
    if total_variants > 1000 {
        println!("\n  WARNING: {} variants is very large. Consider:", total_variants);
        println!("    --screen 50     (two-stage screening)");
        println!("    --param KEY=v1,v2  (narrow specific params)");
        println!("    --strategy X     (one strategy at a time)\n");
    }

    let screen_top = args.screen;
    let num_sessions = sessions.len();

    // Run variants in parallel using rayon
    let all_results: Vec<BacktestResult> = if let Some(screen_n) = screen_top {
        if num_sessions >= 6 && total_variants > screen_n * 2 {
            // Two-stage screening
            let screen_count = (num_sessions / 3).max(3);
            let screen_sessions = &sessions[sessions.len().saturating_sub(screen_count)..];
            println!(
                "Screening: {} variants x {} sessions (of {})...",
                total_variants,
                screen_sessions.len(),
                num_sessions,
            );

            // Screen pass: run all variants on subset
            let screen_results: Vec<(usize, BacktestResult)> = all_variants
                .par_iter_mut()
                .enumerate()
                .map(|(i, strategy)| {
                    let sim = Simulator::new(
                        starting_cash,
                        &args.execution,
                        args.slippage,
                        fees,
                        args.limit_fill_margin,
                        args.latency,
                        session_cash_ratio,
                    );
                    (i, sim.run_backtest(strategy.as_mut(), screen_sessions))
                })
                .collect();

            // Keep top N by Sharpe
            let mut indexed: Vec<(usize, f64)> = screen_results
                .iter()
                .map(|(i, r)| (*i, r.sharpe))
                .collect();
            indexed.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
            let top_indices: Vec<usize> = indexed
                .iter()
                .take(screen_n)
                .map(|(i, _)| *i)
                .collect();

            let eliminated = total_variants - top_indices.len();
            println!(
                "Screened: kept {} / {} variants (eliminated {})",
                top_indices.len(),
                total_variants,
                eliminated,
            );

            // Collect survivors
            let remaining = all_variants;
            // Sort indices descending so we can swap_remove safely
            let mut sorted_indices = top_indices;
            sorted_indices.sort_unstable();
            // Extract survivors by index (build fresh from with_params since
            // strategy state may have been mutated during screen pass)
            let mut survivor_refs: Vec<Box<dyn Strategy>> = Vec::new();
            for &idx in &sorted_indices {
                // Re-create from params to get clean state
                let params = remaining[idx].params();
                survivor_refs.push(remaining[idx].with_params(&params));
            }

            // Full pass on all sessions
            println!(
                "  Full pass: {} variants x {} sessions...",
                survivor_refs.len(),
                num_sessions,
            );
            survivor_refs
                .par_iter_mut()
                .map(|strategy| {
                    let sim = Simulator::new(
                        starting_cash,
                        &args.execution,
                        args.slippage,
                        fees,
                        args.limit_fill_margin,
                        args.latency,
                        session_cash_ratio,
                    );
                    sim.run_backtest(strategy.as_mut(), &sessions)
                })
                .collect()
        } else {
            // Not enough to screen, run all
            run_all_variants(
                &mut all_variants,
                &sessions,
                starting_cash,
                &args.execution,
                args.slippage,
                fees,
                args.limit_fill_margin,
                args.latency,
                session_cash_ratio,
            )
        }
    } else {
        run_all_variants(
            &mut all_variants,
            &sessions,
            starting_cash,
            &args.execution,
            args.slippage,
            fees,
            args.limit_fill_margin,
            args.latency,
            session_cash_ratio,
        )
    };

    println!(
        "Tested {} strategy variants across {} strategies",
        total_variants,
        strategy_names.len(),
    );

    // Phase 1 output
    let mut results_sorted = all_results.clone();
    print_leaderboard(
        &mut results_sorted,
        args.top,
        &args.optimize,
        session_cash_ratio.is_some(),
    );

    // Phase 2: Kelly Sizing
    println!(
        "-- Phase 2: Kelly Sizing ({:.0}% Kelly) --",
        kelly_mult * 100.0
    );

    let mut by_strategy: HashMap<String, Vec<&BacktestResult>> = HashMap::new();
    for r in &all_results {
        by_strategy
            .entry(r.strategy_name.clone())
            .or_default()
            .push(r);
    }

    println!(
        "\n{:<20} {:>8} {:>8} {:>8} {:>10} {:>8} {:>8} {:>8} {:>8}  {}",
        "Strategy",
        "Sharpe",
        "Kelly_f",
        "cash%",
        "Final$",
        "MaxDD%",
        "GeoRet%",
        "Entry%",
        "Win%",
        "Params"
    );
    println!("{}", "-".repeat(130));

    let mut kelly_sizings: HashMap<String, KellySizing> = HashMap::new();
    let mut sorted_names: Vec<&String> = by_strategy.keys().collect();
    sorted_names.sort();

    for name in &sorted_names {
        let variants = &by_strategy[*name];
        let best = variants
            .iter()
            .max_by(|a, b| {
                sort_key(a, &args.optimize)
                    .partial_cmp(&sort_key(b, &args.optimize))
                    .unwrap_or(std::cmp::Ordering::Equal)
            })
            .unwrap();

        let kelly = compute_kelly_sizing(
            &best.sessions,
            starting_cash,
            kelly_mult,
            args.laplace,
        );

        println!(
            "{:<20} {:>8.3} {:>8.3} {:>7.1}% {:>10.2} {:>7.1}% {:>7.3}% {:>7.1}% {:>7.1}%  {}",
            name,
            best.sharpe,
            kelly.kelly_f,
            kelly.cash_pct * 100.0,
            kelly.projected_final,
            kelly.projected_max_dd * 100.0,
            kelly.projected_geo_return * 100.0,
            best.entry_rate * 100.0,
            best.win_rate * 100.0,
            format_params(&best.params),
        );

        kelly_sizings.insert(name.to_string(), kelly);
    }

    println!();
    println!(
        "Kelly fraction multiplier: {:.0}% (use --kelly-fraction to adjust)",
        kelly_mult * 100.0
    );
    println!(
        "  cash% = Kelly_f * {:.0}% = fraction of bankroll per session",
        kelly_mult * 100.0
    );
    println!(
        "  Final$ = projected compound bankroll over {} sessions",
        num_sessions
    );
    println!();

    if args.verbose {
        let best_overall = all_results
            .iter()
            .max_by(|a, b| {
                sort_key(a, &args.optimize)
                    .partial_cmp(&sort_key(b, &args.optimize))
                    .unwrap_or(std::cmp::Ordering::Equal)
            });
        if let Some(best) = best_overall {
            print_verbose(best);
        }
    }

    // Persist to JSONL log
    log_backtest_run(
        &args,
        num_sessions,
        &all_results,
        &args.optimize,
        &backtest_log_file,
        &kelly_sizings,
    );
    println!("Results logged to {}", backtest_log_file);
}

/// Run all variants in parallel using rayon.
fn run_all_variants(
    variants: &mut [Box<dyn Strategy>],
    sessions: &[SessionData],
    starting_cash: f64,
    execution_mode: &str,
    slippage: f64,
    fees: bool,
    limit_fill_margin: Option<f64>,
    latency: u64,
    session_cash_ratio: Option<(f64, f64)>,
) -> Vec<BacktestResult> {
    let n = variants.len();
    if n == 0 {
        return Vec::new();
    }
    println!(
        "Running {} variants x {} sessions across {} threads...",
        n,
        sessions.len(),
        rayon::current_num_threads(),
    );

    variants
        .par_iter_mut()
        .map(|strategy| {
            let sim = Simulator::new(
                starting_cash,
                execution_mode,
                slippage,
                fees,
                limit_fill_margin,
                latency,
                session_cash_ratio,
            );
            sim.run_backtest(strategy.as_mut(), sessions)
        })
        .collect()
}
