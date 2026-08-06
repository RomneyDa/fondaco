//! Sports backtester.
//!
//! Differs from the crypto backtester in three ways that matter:
//!
//!   1. Games **overlap**, so all games run on one merged timeline with a
//!      shared bankroll and portfolio exposure caps.
//!   2. Fills walk **recorded book depth** rather than assuming a midpoint
//!      cross.
//!   3. The headline metrics are **calibration** and **closing line value**,
//!      not PnL Sharpe — with 272 NFL games a year, PnL has almost no
//!      statistical power and a parameter sweep fits noise.
//!
//! Usage:
//!     cargo run --bin sports-backtest -- --league nfl
//!     cargo run --bin sports-backtest -- --league epl --strategy ModelEdge --walk-forward

use std::collections::HashMap;
use std::io::Write as IoWrite;

use clap::Parser;

use fondaco::sports::backtest::simulator::{
    default_season_key, run, season_split, AllocatorConfig, SimConfig,
};
use fondaco::sports::backtest::fills::FillConfig;
use fondaco::sports::backtest::metrics::MIN_GAMES_FOR_SWEEP;
use fondaco::sports::fair_value::GameParams;
use fondaco::sports::models::{GameSample, GameSessionMeta};
use fondaco::sports::storage::load_completed_games;
use fondaco::sports::strategies::all_sports_strategies;

#[derive(Parser)]
#[command(name = "sports-backtest", about = "Backtest sports strategies")]
struct Cli {
    /// League key
    #[arg(long)]
    league: String,

    /// Strategy name, or "all"
    #[arg(long, default_value = "all")]
    strategy: String,

    /// Data directory
    #[arg(long, default_value = "data")]
    data_dir: String,

    /// Only use the N most recent games
    #[arg(long)]
    recent: Option<usize>,

    /// Starting bankroll
    #[arg(long, default_value_t = 1000.0)]
    cash: f64,

    /// Max fraction of bankroll per game
    #[arg(long, default_value_t = 0.05)]
    max_per_game: f64,

    /// Max fraction of bankroll deployed across all open games
    #[arg(long, default_value_t = 0.30)]
    max_exposure: f64,

    /// Max simultaneously open games
    #[arg(long, default_value_t = 6)]
    max_concurrent: usize,

    /// Fraction of each displayed book level we assume we can take
    #[arg(long, default_value_t = 0.5)]
    participation: f64,

    /// Disable fees
    #[arg(long)]
    no_fees: bool,

    /// Run season-split walk-forward validation
    #[arg(long)]
    walk_forward: bool,

    /// Write the full decision trace (not only trades) to this JSON file
    #[arg(long)]
    decisions_out: Option<String>,

    /// Write results as JSON to this path
    #[arg(long)]
    json_out: Option<String>,
}

fn main() {
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("warn")).init();
    let cli = Cli::parse();

    let games = load_completed_games(&cli.data_dir, &cli.league, cli.recent);
    if games.is_empty() {
        eprintln!(
            "No completed games found for league '{}' under {}/markets/{}/games/.\n\
             Run the collector first:  ./target/release/sports-collector --league {}",
            cli.league, cli.data_dir, cli.league, cli.league
        );
        std::process::exit(1);
    }

    println!(
        "Loaded {} completed games for {} ({} samples total)",
        games.len(),
        cli.league,
        games.iter().map(|(_, s)| s.len()).sum::<usize>(),
    );

    if games.len() < MIN_GAMES_FOR_SWEEP {
        println!(
            "\n  NOTE: {} games is a small sample for sports. Treat any parameter\n\
             \x20       comparison as indicative only, and prefer calibration and CLV\n\
             \x20       over PnL when judging a strategy.\n",
            games.len()
        );
    }

    let cfg = SimConfig {
        starting_cash: cli.cash,
        allocator: AllocatorConfig {
            max_per_game_fraction: cli.max_per_game,
            max_per_game_cash: cli.cash,
            max_total_exposure_fraction: cli.max_exposure,
            max_concurrent_games: cli.max_concurrent,
        },
        fills: FillConfig {
            participation: cli.participation,
            fill_at_touch: false,
        },
        fees: !cli.no_fees,
        endgame_minutes: 5.0,
        record_decisions: cli.decisions_out.is_some(),
        market_kinds: Vec::new(),
    };

    let registry = all_sports_strategies();
    let names: Vec<String> = if cli.strategy == "all" {
        let mut n: Vec<String> = registry.keys().cloned().collect();
        n.sort();
        n
    } else {
        if !registry.contains_key(&cli.strategy) {
            eprintln!(
                "Unknown strategy '{}'. Available: {:?}",
                cli.strategy,
                registry.keys().collect::<Vec<_>>()
            );
            std::process::exit(1);
        }
        vec![cli.strategy.clone()]
    };

    // Fitted per-game parameters would come from the model server. Without
    // them every game falls back to league averages, which `FairValueSource`
    // marks untradeable for model-dependent strategies.
    let params: HashMap<String, GameParams> = HashMap::new();

    let mut all_json = serde_json::Map::new();

    for name in &names {
        let factory = registry[name];
        let result = run(&factory, &games, &params, &cfg);

        println!("\n=== {name} ===");
        print_evaluation(&result);

        if let Some(warn) = &result.evaluation.sample_size_warning {
            println!("\n  WARNING: {warn}");
        }

        if cli.walk_forward {
            run_walk_forward(&factory, &games, &params, &cfg, name);
        }

        all_json.insert(
            name.clone(),
            serde_json::to_value(&result.evaluation).unwrap_or(serde_json::Value::Null),
        );

        if let Some(path) = &cli.decisions_out {
            let out = format!("{path}.{name}.json");
            if let Ok(json) = serde_json::to_string_pretty(&result.decisions) {
                if let Ok(mut f) = std::fs::File::create(&out) {
                    let _ = f.write_all(json.as_bytes());
                    println!("  decision trace -> {out} ({} records)", result.decisions.len());
                }
            }
        }
    }

    if let Some(path) = &cli.json_out {
        if let Ok(json) = serde_json::to_string_pretty(&all_json) {
            if let Ok(mut f) = std::fs::File::create(path) {
                let _ = f.write_all(json.as_bytes());
                println!("\nResults written to {path}");
            }
        }
    }
}

fn print_evaluation(result: &fondaco::sports::backtest::SimResult) {
    let e = &result.evaluation;
    println!(
        "  games={}  traded={}  peak_exposure=${:.2}  peak_open_games={}",
        e.games, e.games_traded, e.max_concurrent_exposure, e.max_concurrent_games_open
    );

    // Calibration first: it is the metric with real statistical power here.
    println!("  -- calibration (primary) --");
    match (e.model_brier, e.market_brier) {
        (Some(m), Some(k)) => {
            println!("     model Brier   {m:.5}   market Brier {k:.5}");
            if let Some(skill) = e.brier_skill_vs_market {
                let verdict = if skill > 0.0 {
                    "model beats the market"
                } else {
                    "market beats the model"
                };
                println!("     Brier skill vs market {skill:+.4}  ({verdict})");
            }
        }
        _ => println!("     (no probabilistic predictions recorded)"),
    }
    if let Some(ll) = e.model_log_loss {
        println!("     log loss      {ll:.5}");
    }
    if let Some(ece) = e.model_ece {
        println!("     calibration error {ece:.5}");
    }

    println!("  -- closing line value --");
    let c = &e.clv;
    if c.trades == 0 {
        println!("     (no trades)");
    } else {
        println!(
            "     trades={}  mean CLV {:+.4}  median {:+.4}  positive {:.1}%",
            c.trades,
            c.mean_clv,
            c.median_clv,
            c.positive_rate * 100.0
        );
        println!(
            "     claimed edge {:+.4}   (compare against realised CLV above)",
            c.mean_claimed_edge
        );
        if let Some(t) = c.clv_t_stat {
            println!("     CLV t-stat {t:+.2}");
        }
    }

    // PnL last, deliberately: it is the noisiest number in the set.
    println!("  -- pnl (noisiest; read last) --");
    println!(
        "     total ${:+.2}  per game ${:+.4}  final bankroll ${:.2}",
        e.total_pnl, e.mean_pnl_per_game, result.final_bankroll
    );
}

/// Season-split walk-forward.
///
/// Random splits leak: two halves of the same season share team form, injuries
/// and schedule structure. Only season boundaries give an honest out-of-sample
/// estimate for sports.
fn run_walk_forward(
    factory: &fn() -> Box<dyn fondaco::sports::strategy::SportsStrategy>,
    games: &[(GameSessionMeta, Vec<GameSample>)],
    params: &HashMap<String, GameParams>,
    cfg: &SimConfig,
    name: &str,
) {
    let split = season_split(games, &default_season_key);
    let mut seasons: Vec<String> = split.keys().cloned().collect();
    seasons.sort();

    if seasons.len() < 2 {
        println!(
            "  walk-forward: only {} season(s) present — need at least 2 for an \
             honest out-of-sample split",
            seasons.len()
        );
        return;
    }

    println!("  -- walk-forward by season --");
    for (i, season) in seasons.iter().enumerate().skip(1) {
        // Train on everything before this season, test on it. We do not fit
        // parameters here, so this reports out-of-sample behaviour of a fixed
        // configuration — which is the point: it shows stability across
        // seasons rather than in-sample fit.
        let test_idx = &split[season];
        let test: Vec<(GameSessionMeta, Vec<GameSample>)> =
            test_idx.iter().map(|i| games[*i].clone()).collect();

        let r = run(factory, &test, params, cfg);
        let skill = r
            .evaluation
            .brier_skill_vs_market
            .map(|s| format!("{s:+.4}"))
            .unwrap_or_else(|| "n/a".into());
        println!(
            "     fold {i}: test season {season}  games={}  CLV {:+.4}  Brier skill {skill}  \
             PnL ${:+.2}",
            r.evaluation.games, r.evaluation.clv.mean_clv, r.evaluation.total_pnl,
        );
    }
    let _ = name;
}
