//! Sports bot worker — one strategy, one league, many concurrent games.
//!
//! The crypto worker handles one session at a time. This one holds a
//! `HashMap<game_id, GameSession>` and routes ticks by game, because a Sunday
//! NFL slate has thirteen games running at once.
//!
//! Three things it owns that the crypto worker does not need:
//!
//!   - a **portfolio allocator**, so no strategy can size past the book's
//!     limits and thirteen correlated games cannot each take a full stake;
//!   - an **event-confirmation gate**, so a disallowed goal or a reversed
//!     touchdown is not bought at the top;
//!   - a **decision log**, recording every considered trade and the gate that
//!     blocked it — affordable here because sports makes few decisions per game.
//!
//! Defaults to dry-run. Live trading requires `--live`, and every order still
//! goes through the same clob-bridge `ClobExecutor` path as crypto.
//!
//! Usage:
//!     cargo run --bin sports-bot-worker -- --league nfl --strategy StaleBook

use std::collections::HashMap;
use std::io::Write as IoWrite;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

use chrono::Utc;
use clap::Parser;
use log::{error, info, warn};
use reqwest::blocking::Client;
use serde_json::Value;

use fondaco::defaults::API_TIMEOUT_DEFAULT;
use fondaco::economics::MarketEconomics;
use fondaco::notifications::{NotificationEvent, NotificationManager};
use fondaco::session_io::{remove_file_silent, write_pid_file};
use fondaco::sports::allocator::{Allocator, AllocatorLimits, AllocationVerdict};
use fondaco::sports::book::MultiBookClient;
use fondaco::sports::confirmation::{ConfirmationConfig, ConfirmationGate, ConfirmationStatus};
use fondaco::sports::discovery::discover_league;
use fondaco::sports::espn;
use fondaco::sports::fair_value::coherence;
use fondaco::sports::fair_value::staleness::{StalenessConfig, StalenessDetector};
use fondaco::sports::fair_value::{self, FairValue};
use fondaco::sports::feed::SportsFeedClient;
use fondaco::sports::model_client::{ModelClient, DEFAULT_MODEL_SERVER};
use fondaco::sports::selection::{
    describe, filter_from_flags, rank, Candidate, SelectionPolicy, SkipReason,
};
use fondaco::sports::models::{
    outcome_key_from_state, GameSample, GameStatus, League, LegBook, MarketGroup, Sport,
};
use fondaco::sports::storage::now_utc;
use fondaco::sports::strategies::all_sports_strategies;
use fondaco::sports::strategy::{
    common_gates, DecisionGate, SportsDecision, SportsStrategy, SportsTickContext,
};
use fondaco::trader::{Position, TradeAction};

#[derive(Parser)]
#[command(name = "sports-bot-worker", about = "Run a sports strategy against live markets")]
struct Cli {
    #[arg(long)]
    league: String,

    #[arg(long)]
    strategy: String,

    /// Starting bankroll
    #[arg(long, default_value_t = 500.0)]
    cash: f64,

    /// Place real orders. Without this the worker logs what it would do.
    #[arg(long)]
    live: bool,

    #[arg(long, default_value = "http://127.0.0.1:8421")]
    clob_bridge: String,

    #[arg(long, default_value = DEFAULT_MODEL_SERVER)]
    model_server: String,

    #[arg(long, default_value = "data")]
    data_dir: String,

    /// Max fraction of bankroll per game
    #[arg(long, default_value_t = 0.05)]
    max_per_game: f64,

    /// Max fraction of bankroll across all open games
    #[arg(long, default_value_t = 0.30)]
    max_exposure: f64,

    /// Max simultaneously open games
    #[arg(long, default_value_t = 6)]
    max_concurrent: usize,

    /// Seconds between discovery passes
    #[arg(long, default_value_t = 300)]
    discovery_interval: u64,

    /// Disable ESPN cross-confirmation. Not recommended: it is one of the three
    /// defences against acting on a retracted score.
    #[arg(long)]
    no_espn: bool,

    // ---- Game selection (flags only; restart to change) ----------------
    /// Only games involving this team. Repeatable. Substring match.
    #[arg(long = "team", value_name = "NAME")]
    teams: Vec<String>,

    /// Never these teams. Repeatable. Substring match.
    #[arg(long = "exclude-team", value_name = "NAME")]
    exclude_teams: Vec<String>,

    /// A specific game: feed gameId, or a slug/title fragment. Repeatable.
    /// Overrides --team / --exclude-team.
    #[arg(long = "game", value_name = "ID_OR_SLUG")]
    games: Vec<String>,

    /// Only games kicking off at or after this. RFC3339, or HH:MM for today UTC.
    #[arg(long, value_name = "TIME")]
    min_start: Option<String>,

    /// Only games kicking off at or before this.
    #[arg(long, value_name = "TIME")]
    max_start: Option<String>,

    /// Require this market type: moneyline, spread, total. Repeatable.
    /// Default is moneyline only.
    #[arg(long = "market-kind", value_name = "KIND")]
    market_kinds: Vec<String>,

    /// Print the games that match the flags, then exit without collecting.
    #[arg(long)]
    list_games: bool,

    /// Which games win a slot when more are eligible than --max-concurrent:
    /// random, edge, scheduled, liquidity.
    #[arg(long, default_value = "random")]
    prefer: String,

    /// Seed for --prefer random. Fixed by default so restarts pick the same
    /// games; change it to resample the slate.
    #[arg(long, default_value_t = 0)]
    prefer_seed: u64,
}

/// Per-game live state.
struct GameSession {
    group: MarketGroup,
    strategy: Box<dyn SportsStrategy>,
    position: Position,
    staleness: StalenessDetector,
    params_fitted: bool,
    params: fondaco::sports::fair_value::GameParams,
    last_books: Vec<LegBook>,
    last_state: Option<fondaco::sports::models::GameState>,
    entry_cost: f64,
    /// Last observed |model - market| and book depth, used by --prefer.
    last_edge: Option<f64>,
    last_depth: Option<f64>,
    trade_count: usize,
    resolved: bool,
    started_at: Instant,
}

impl GameSession {
    fn key(&self) -> String {
        self.group.dir_name()
    }
}

fn main() {
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("info"))
        .format(|buf, record| {
            let ts = chrono::Local::now().format("%Y-%m-%d %H:%M:%S%.3f");
            writeln!(buf, "{} [{}] {}", ts, record.level(), record.args())
        })
        .init();
    run(Cli::parse());
}

fn run(cli: Cli) {
    let league = match League::known(&cli.league) {
        Some(l) => l,
        None => {
            error!("Unknown league '{}'. Known: {:?}", cli.league, League::all_keys());
            std::process::exit(1);
        }
    };

    let registry = all_sports_strategies();
    let factory = match registry.get(&cli.strategy) {
        Some(f) => *f,
        None => {
            error!(
                "Unknown strategy '{}'. Available: {:?}",
                cli.strategy,
                registry.keys().collect::<Vec<_>>()
            );
            std::process::exit(1);
        }
    };

    let policy = match SelectionPolicy::parse(&cli.prefer) {
        Some(p) => p,
        None => {
            error!(
                "Unknown --prefer '{}'. Expected: random, edge, scheduled, liquidity",
                cli.prefer
            );
            std::process::exit(2);
        }
    };

    let filter = match filter_from_flags(
        &cli.teams,
        &cli.exclude_teams,
        &cli.games,
        cli.min_start.as_deref(),
        cli.max_start.as_deref(),
        &cli.market_kinds,
        Utc::now(),
    ) {
        Ok(f) => f,
        Err(e) => {
            error!("{e}");
            std::process::exit(2);
        }
    };

    if !cli.live {
        info!("DRY RUN — no orders will be placed. Pass --live to trade for real.");
    } else {
        warn!("LIVE MODE — real orders will be placed with real money.");
    }

    // --list-games: show what these flags would trade, then exit. Nothing is
    // started, so it is always safe to run.
    if cli.list_games {
        let http = Client::builder()
            .timeout(Duration::from_secs(API_TIMEOUT_DEFAULT))
            .build()
            .expect("build HTTP client");
        let feed = SportsFeedClient::new();
        feed.register_league(&league.feed_abbreviation, league.sport);
        feed.start();
        thread::sleep(Duration::from_secs(5));
        let feed_games = feed.games_for_league(&league.feed_abbreviation);
        let groups = discover_league(&http, &league, &feed_games);
        feed.stop();

        let (kept, skipped) = filter.apply(&groups);
        println!(
            "{} discovered, {} selected  (--prefer {}, seed {}, --max-concurrent {})\n",
            groups.len(),
            kept.len(),
            policy.as_str(),
            cli.prefer_seed,
            cli.max_concurrent,
        );

        // Show the order the cap would actually apply in.
        let mut cands: Vec<Candidate> = kept
            .iter()
            .filter_map(|g| {
                Some(Candidate {
                    game_id: g.game_id?,
                    edge: None,
                    liquidity: None,
                    start_epoch: chrono::DateTime::parse_from_rfc3339(&g.start_time_utc)
                        .ok()
                        .map(|d| d.timestamp()),
                    already_open: false,
                })
            })
            .collect();
        rank(&mut cands, policy, cli.prefer_seed);

        println!("SELECTED (in --prefer order; first {} would get slots)", cli.max_concurrent);
        for (i, c) in cands.iter().enumerate() {
            if let Some(g) = kept.iter().find(|g| g.game_id == Some(c.game_id)) {
                let marker = if i < cli.max_concurrent { "*" } else { " " };
                println!("  {marker} {}", describe(g));
            }
        }
        if !skipped.is_empty() {
            println!("\nSKIPPED");
            for (g, r) in &skipped {
                println!("    {}  <- {}", describe(g), r.as_str());
            }
        }
        return;
    }

    let notifier = NotificationManager::load();
    notifier.notify(
        NotificationEvent::ProcessStart,
        "Sports Bot Started",
        format!(
            "{} on {} — ${:.2}{}",
            cli.strategy,
            league.key,
            cli.cash,
            if cli.live { "" } else { " (dry run)" }
        ),
    );

    let running = Arc::new(AtomicBool::new(true));
    {
        let r = Arc::clone(&running);
        ctrlc::set_handler(move || {
            info!("Shutdown signal received.");
            r.store(false, Ordering::SeqCst);
        })
        .expect("set Ctrl-C handler");
    }

    let pid_path = format!("/tmp/fondaco-sports-{}-bot-{}.pid", league.key, cli.strategy);
    write_pid_file(&pid_path);

    let http = Client::builder()
        .timeout(Duration::from_secs(API_TIMEOUT_DEFAULT))
        .build()
        .expect("build HTTP client");

    let feed = SportsFeedClient::new();
    feed.register_league(&league.feed_abbreviation, league.sport);
    feed.start();

    let books = MultiBookClient::new();
    books.start();

    let model = ModelClient::new(&cli.model_server);
    if !model.healthy() {
        warn!(
            "Model server at {} is unreachable — every game will fall back to \
             league-average rates, which model-dependent strategies refuse to trade on.",
            cli.model_server
        );
    }

    let mut allocator = Allocator::new(
        cli.cash,
        AllocatorLimits {
            max_per_game_fraction: cli.max_per_game,
            max_per_game_cash: cli.cash,
            max_total_exposure_fraction: cli.max_exposure,
            max_concurrent_games: cli.max_concurrent,
            ..AllocatorLimits::default()
        },
    );

    let mut confirmation = ConfirmationGate::new(match league.sport {
        Sport::Soccer => ConfirmationConfig::soccer(),
        Sport::AmericanFootball => ConfirmationConfig::american_football(),
    });

    let economics = MarketEconomics::sports();
    let market_dir = format!("{}/markets/{}", cli.data_dir, league.key);
    let _ = std::fs::create_dir_all(&market_dir);

    let mut sessions: HashMap<i64, GameSession> = HashMap::new();
    let mut groups: Vec<MarketGroup> = Vec::new();
    let mut last_discovery = Instant::now() - Duration::from_secs(cli.discovery_interval + 1);
    let mut last_espn = Instant::now() - Duration::from_secs(60);
    let mut last_status = Instant::now();

    info!(
        "[{}:{}] Sports bot started. Bankroll ${:.2}, per-game {:.0}%, exposure {:.0}%, \
         max concurrent {}",
        league.key,
        cli.strategy,
        cli.cash,
        cli.max_per_game * 100.0,
        cli.max_exposure * 100.0,
        cli.max_concurrent,
    );

    while running.load(Ordering::SeqCst) {
        let now = Instant::now();

        // ---- Discovery -------------------------------------------------
        if now.duration_since(last_discovery).as_secs() >= cli.discovery_interval {
            last_discovery = now;
            let feed_games = feed.games_for_league(&league.feed_abbreviation);
            let found = discover_league(&http, &league, &feed_games);
            if !found.is_empty() {
                let (kept, skipped) = filter.apply(&found);
                if !skipped.is_empty() {
                    record_skips(&market_dir, &league.key, &skipped);
                }
                info!(
                    "[{}:{}] {} discovered, {} selected by flags",
                    league.key,
                    cli.strategy,
                    found.len(),
                    kept.len()
                );
                groups = kept.into_iter().cloned().collect();
            }
        }

        // ---- ESPN cross-confirmation -----------------------------------
        if !cli.no_espn && now.duration_since(last_espn).as_secs() >= 15 {
            last_espn = now;
            for e in espn::fetch_scoreboard(&http, &league) {
                // Match ESPN's game to ours by team names, then feed the
                // scoreline into the confirmation gate.
                for (gid, s) in sessions.iter() {
                    if fondaco::sports::discovery::team_matches(&e.home_team, &s.group.home_team)
                        && fondaco::sports::discovery::team_matches(
                            &e.away_team,
                            &s.group.away_team,
                        )
                    {
                        confirmation.observe_secondary(
                            *gid,
                            e.state.home_score,
                            e.state.away_score,
                        );
                    }
                }
            }
        }

        // ---- Start sessions for live games -----------------------------
        for group in &groups {
            let gid = match group.game_id {
                Some(g) => g,
                None => continue,
            };
            if sessions.contains_key(&gid) || !group.is_tradeable() {
                continue;
            }
            let live = feed
                .fresh_game(gid)
                .map(|g| g.state.status.is_live())
                .unwrap_or(false);
            if !live {
                continue;
            }

            let fetched = model.params_for(
                &league.key,
                league.sport,
                &group.home_team,
                &group.away_team,
            );
            let mut strategy = factory();
            strategy.on_game_start(group, league.sport);

            info!(
                "[{}:{}] Tracking {} vs {} (gameId {}) — params {}",
                league.key,
                cli.strategy,
                group.away_team,
                group.home_team,
                gid,
                if fetched.fitted { "fitted" } else { "FALLBACK" },
            );

            sessions.insert(
                gid,
                GameSession {
                    group: group.clone(),
                    strategy,
                    position: Position::new(0.0),
                    staleness: StalenessDetector::new(StalenessConfig::default()),
                    params_fitted: fetched.fitted,
                    params: fetched.params,
                    last_books: vec![LegBook::default(); group.legs.len()],
                    last_state: None,
                    entry_cost: 0.0,
                    last_edge: None,
                    last_depth: None,
                    trade_count: 0,
                    resolved: false,
                    started_at: now,
                },
            );
        }

        // ---- Keep book subscriptions current ---------------------------
        let tokens: Vec<String> = sessions
            .values()
            .filter(|s| !s.resolved)
            .flat_map(|s| s.group.token_ids())
            .collect();
        books.set_tokens(&tokens);

        // ---- Per-game tick ---------------------------------------------
        // Rank before iterating. `sessions.keys()` is a HashMap and Rust
        // randomises its iteration order per process, so the previous
        // first-come loop filled --max-concurrent with a different arbitrary
        // subset on every restart — silently discarding the best games. Ranking
        // makes the choice explicit, deterministic and reproducible.
        let mut candidates: Vec<Candidate> = sessions
            .iter()
            .map(|(gid, s)| Candidate {
                game_id: *gid,
                edge: s.last_edge,
                liquidity: s.last_depth,
                start_epoch: chrono::DateTime::parse_from_rfc3339(&s.group.start_time_utc)
                    .ok()
                    .map(|d| d.timestamp()),
                already_open: s.position.total_tokens() > 0.0,
            })
            .collect();
        rank(&mut candidates, policy, cli.prefer_seed);
        let game_ids: Vec<i64> = candidates.into_iter().map(|c| c.game_id).collect();

        for gid in game_ids {
            let feed_game = match feed.fresh_game(gid) {
                Some(g) => g,
                None => continue,
            };
            let state = feed_game.state.clone();

            confirmation.observe(now, gid, &state);
            let conf_status = confirmation.status(now, gid, &state);

            let session = match sessions.get_mut(&gid) {
                Some(s) => s,
                None => continue,
            };
            if session.resolved {
                continue;
            }

            // Resolve finished games.
            if state.status == GameStatus::Final {
                let outcome = outcome_key_from_state(league.sport, &state);
                let winning_leg = outcome
                    .as_ref()
                    .and_then(|k| session.group.leg_index(k));
                let payout = winning_leg.map(|l| session.position.leg(l)).unwrap_or(0.0);
                let pnl = payout - session.entry_cost;

                allocator.close_game(&session.key(), payout);
                session.resolved = true;

                if session.trade_count > 0 {
                    let event = if pnl >= 0.0 {
                        NotificationEvent::SessionEndWin
                    } else {
                        NotificationEvent::SessionEndLoss
                    };
                    notifier.notify(
                        event,
                        &format!(
                            "{} — {} {}",
                            cli.strategy,
                            outcome.as_deref().unwrap_or("?"),
                            if pnl >= 0.0 { "OK" } else { "LOSS" }
                        ),
                        format!(
                            "{} vs {} {}-{} | PnL ${:+.2} | bankroll ${:.2}",
                            session.group.away_team,
                            session.group.home_team,
                            state.home_score,
                            state.away_score,
                            pnl,
                            allocator.bankroll(),
                        ),
                    );
                }

                info!(
                    "[{}:{}] RESOLVED {} vs {} {}-{} outcome={:?} PnL ${:+.2} bankroll ${:.2}",
                    league.key,
                    cli.strategy,
                    session.group.away_team,
                    session.group.home_team,
                    state.home_score,
                    state.away_score,
                    outcome,
                    pnl,
                    allocator.bankroll(),
                );
                log_jsonl(
                    &market_dir,
                    "sports_bot_log.jsonl",
                    &serde_json::json!({
                        "timestamp": now_utc(),
                        "strategy": cli.strategy,
                        "league": league.key,
                        "game": session.key(),
                        "outcome": outcome,
                        "score": format!("{}-{}", state.home_score, state.away_score),
                        "trades": session.trade_count,
                        "pnl": round2(pnl),
                        "bankroll": round2(allocator.bankroll()),
                        "dry_run": !cli.live,
                    }),
                );
                continue;
            }

            // Current books.
            let mut leg_books = Vec::with_capacity(session.group.legs.len());
            for leg in &session.group.legs {
                leg_books.push(books.book(&leg.token_id).unwrap_or_default());
            }
            let midpoints: Vec<Option<f64>> = leg_books.iter().map(|b| b.midpoint).collect();
            let asks: Vec<Option<f64>> = leg_books.iter().map(|b| b.best_ask).collect();

            // Staleness detection needs the pre-event book, so feed it before
            // overwriting last_books.
            let prev_mids: Vec<Option<f64>> =
                session.last_books.iter().map(|b| b.midpoint).collect();
            if let Some(prev) = session.last_state.as_ref() {
                if let Some(scorer) = scoring_leg(prev, &state) {
                    session.staleness.on_event(now, &prev_mids, scorer);
                }
            }
            session.last_state = Some(state.clone());
            let stale_signal = session.staleness.evaluate(now, &midpoints);

            let ml = session.group.moneyline_legs().len().min(midpoints.len());
            let coherence_report = coherence::assess(&midpoints[..ml], &asks[..ml]);

            let fv: Option<FairValue> = fair_value::evaluate(
                league.sport,
                &session.params,
                &state,
                session.params_fitted,
            );

            let allocation = allocator.allocate(&session.key(), &league.key);

            let sample = GameSample {
                timestamp_utc: now_utc(),
                elapsed_seconds: now.duration_since(session.started_at).as_secs_f64(),
                state: state.clone(),
                books: leg_books.clone(),
                model_probs: fv
                    .as_ref()
                    .map(|f| f.probs.as_vec(league.sport.has_draw()))
                    .unwrap_or_default(),
                reference_home_prob: None,
            };

            let ctx = SportsTickContext {
                sample: &sample,
                group: &session.group,
                sport: league.sport,
                position: &session.position,
                available_cash: allocation.cash,
                elapsed_seconds: sample.elapsed_seconds,
                remaining_minutes: state.remaining_minutes(league.sport),
                fair_value: fv.as_ref(),
                coherence: &coherence_report,
                stale_signal: stale_signal.as_ref(),
                under_review: state.under_review,
                feeds_agree: conf_status != ConfirmationStatus::FeedsDisagree,
                market_kinds: &filter.market_kinds,
            };

            // Confirmation gate outranks everything: a retracted goal is the
            // single most expensive mistake available here.
            let decision = if !conf_status.is_tradeable() {
                SportsDecision::hold(match conf_status {
                    ConfirmationStatus::UnderReview => DecisionGate::UnderReview,
                    ConfirmationStatus::FeedsDisagree => DecisionGate::FeedDisagreement,
                    _ => DecisionGate::StaleData,
                })
                .with_note(format!("confirmation: {}", conf_status.as_str()))
            } else if allocation.verdict == AllocationVerdict::ConcurrencyLimit
                || allocation.verdict == AllocationVerdict::DrawdownBrake
            {
                SportsDecision::hold(DecisionGate::ExposureLimit)
                    .with_note(format!("allocator: {:?}", allocation.verdict))
            } else if let Some(gate) = common_gates(&ctx, session.strategy.requires_model()) {
                SportsDecision::hold(gate)
            } else {
                session.strategy.on_tick(&ctx)
            };

            // Every considered decision is logged — affordable because sports
            // makes few of them per game, and invaluable for debugging.
            log_jsonl(
                &market_dir,
                "sports_decisions.jsonl",
                &serde_json::json!({
                    "timestamp": now_utc(),
                    "strategy": cli.strategy,
                    "game": session.key(),
                    "gate": decision.gate.as_str(),
                    "leg": decision.leg,
                    "model_prob": decision.model_prob,
                    "market_mid": decision.market_mid,
                    "edge": decision.edge,
                    "confirmation": conf_status.as_str(),
                    "allocation": format!("{:?}", allocation.verdict),
                    "available_cash": round2(allocation.cash),
                    "note": decision.note,
                }),
            );

            // Execute.
            if !decision.action.is_hold() {
                let parsed = decision.action.parsed_side();
                if let Some(leg) = parsed.leg() {
                    let executed = execute(
                        &http,
                        &cli,
                        &economics,
                        &session.group,
                        leg,
                        &decision.action,
                        &leg_books,
                    );

                    match executed {
                        Some((tokens, price)) if parsed.is_buy() => {
                            let cost = tokens * price;
                            session.position.add_leg(leg, tokens);
                            session.entry_cost += cost;
                            session.trade_count += 1;
                            allocator.commit(&session.key(), &league.key, cost);
                            session.staleness.consume();

                            info!(
                                "[{}:{}] {} BUY {} {:.2} @ {:.3} (${:.2}) — {}",
                                league.key,
                                cli.strategy,
                                if cli.live { "LIVE" } else { "DRY" },
                                decision.action.side,
                                tokens,
                                price,
                                cost,
                                session.group.event_title,
                            );
                            notifier.notify(
                                NotificationEvent::TradeBuy,
                                &format!("{} — {}", cli.strategy, decision.action.side),
                                format!(
                                    "{tokens:.2} @ {price:.3} on {} vs {}",
                                    session.group.away_team, session.group.home_team
                                ),
                            );
                            log_trade(&market_dir, &cli, session, leg, &decision, tokens, price);
                        }
                        Some((tokens, price)) if parsed.is_sell() => {
                            let proceeds = tokens * price;
                            session.position.add_leg(leg, -tokens);
                            session.trade_count += 1;
                            allocator.release(&session.key(), proceeds);
                            info!(
                                "[{}:{}] {} SELL {} {:.2} @ {:.3}",
                                league.key,
                                cli.strategy,
                                if cli.live { "LIVE" } else { "DRY" },
                                decision.action.side,
                                tokens,
                                price
                            );
                            notifier.notify(
                                NotificationEvent::TradeSell,
                                &format!("{} — {}", cli.strategy, decision.action.side),
                                format!("{tokens:.2} @ {price:.3}"),
                            );
                            log_trade(&market_dir, &cli, session, leg, &decision, tokens, price);
                        }
                        _ => {
                            session.strategy.on_trade_failed(&decision.action.side);
                        }
                    }
                }
            }

            session.last_edge = decision.edge.map(f64::abs);
            session.last_depth = Some(
                leg_books
                    .iter()
                    .map(|b| {
                        b.bid_levels.iter().map(|(_, s)| s).sum::<f64>()
                            + b.ask_levels.iter().map(|(_, s)| s).sum::<f64>()
                    })
                    .sum(),
            );
            session.last_books = leg_books;
        }

        confirmation.expire(now);
        sessions.retain(|_, s| !s.resolved);

        if now.duration_since(last_status).as_secs() >= 60 {
            last_status = now;
            let snap = allocator.snapshot();
            info!(
                "[{}:{}] games={} bankroll=${:.2} exposure=${:.2} open={} drawdown={:.1}% \
                 pending_confirmations={}",
                league.key,
                cli.strategy,
                sessions.len(),
                snap.bankroll,
                snap.total_exposure,
                snap.open_games,
                snap.drawdown * 100.0,
                confirmation.pending_count(),
            );
        }

        thread::sleep(Duration::from_millis(200));
    }

    feed.stop();
    books.stop();
    remove_file_silent(&pid_path);
    notifier.notify(
        NotificationEvent::ProcessStop,
        "Sports Bot Stopped",
        format!(
            "{} on {} — final bankroll ${:.2}",
            cli.strategy,
            league.key,
            allocator.bankroll()
        ),
    );
    info!(
        "[{}:{}] Stopped. Final bankroll ${:.2}",
        league.key,
        cli.strategy,
        allocator.bankroll()
    );
}

/// Place the order, or simulate it in dry-run.
///
/// Returns `(tokens, price)` on success. All live orders go through the same
/// clob-bridge endpoint as the crypto worker — there are no ad-hoc order paths.
fn execute(
    http: &Client,
    cli: &Cli,
    econ: &MarketEconomics,
    group: &MarketGroup,
    leg: usize,
    action: &TradeAction,
    books: &[LegBook],
) -> Option<(f64, f64)> {
    let book = books.get(leg)?;
    let parsed = action.parsed_side();

    let price = action.price.or(if parsed.is_buy() {
        book.best_ask
    } else {
        book.best_bid
    })?;
    let price = econ.round_price(price);
    if price <= 0.0 || price >= 1.0 {
        return None;
    }

    let tokens = if parsed.is_buy() {
        (action.amount / price).floor()
    } else {
        action.amount
    };
    if tokens < econ.min_order_tokens {
        return None;
    }

    if !cli.live {
        return Some((tokens, price));
    }

    let token_id = group.legs.get(leg).map(|l| l.token_id.clone())?;
    let body = serde_json::json!({
        "token_id": token_id,
        "side": if parsed.is_buy() { "BUY" } else { "SELL" },
        "price": format!("{:.*}", econ.price_decimals() as usize, price),
        "size": format!("{:.*}", econ.token_decimals as usize, tokens),
        "order_type": "GTD",
        "expiration_seconds": action.expiration_seconds.unwrap_or(120.0) as u64,
    });

    let resp = http
        .post(format!("{}/create_and_post_order", cli.clob_bridge.trim_end_matches('/')))
        .json(&body)
        .send()
        .ok()?;
    if !resp.status().is_success() {
        warn!("[SPORTS-BOT] bridge returned {}", resp.status());
        return None;
    }
    let json: Value = resp.json().ok()?;
    let ok = json.get("success").and_then(|v| v.as_bool()).unwrap_or(true);
    if !ok {
        warn!(
            "[SPORTS-BOT] order rejected: {}",
            json.get("error").and_then(|v| v.as_str()).unwrap_or("unknown")
        );
        return None;
    }

    Some((tokens, price))
}

/// Append skip records so an unselected game is explainable from the logs.
fn record_skips(
    market_dir: &str,
    league_key: &str,
    skipped: &[(&fondaco::sports::models::MarketGroup, SkipReason)],
) {
    let path = std::path::Path::new(market_dir).join("skipped.jsonl");
    let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(path) else {
        return;
    };
    for (g, reason) in skipped {
        let line = serde_json::json!({
            "timestamp": now_utc(),
            "league": league_key,
            "game_id": g.game_id,
            "event_slug": g.event_slug,
            "away_team": g.away_team,
            "home_team": g.home_team,
            "reason": reason.as_str(),
        });
        let _ = writeln!(f, "{}", serde_json::to_string(&line).unwrap_or_default());
    }
}

/// Which leg just scored. `None` when nothing changed, which keeps the
/// staleness detector armed only on real events rather than on clock ticks.
fn scoring_leg(
    prev: &fondaco::sports::models::GameState,
    next: &fondaco::sports::models::GameState,
) -> Option<usize> {
    if next.home_score > prev.home_score {
        Some(0)
    } else if next.away_score > prev.away_score {
        Some(1)
    } else {
        None
    }
}

fn log_trade(
    market_dir: &str,
    cli: &Cli,
    session: &GameSession,
    leg: usize,
    decision: &SportsDecision,
    tokens: f64,
    price: f64,
) {
    log_jsonl(
        market_dir,
        "sports_bot_trades.jsonl",
        &serde_json::json!({
            "timestamp": Utc::now().to_rfc3339(),
            "strategy": cli.strategy,
            "league": cli.league,
            "game": session.key(),
            "side": decision.action.side,
            "leg": leg,
            "tokens": round2(tokens),
            "price": price,
            "amount": round2(tokens * price),
            "model_prob": decision.model_prob,
            "market_mid": decision.market_mid,
            "edge": decision.edge,
            "dry_run": !cli.live,
        }),
    );
}

fn log_jsonl(dir: &str, name: &str, value: &Value) {
    let path = std::path::Path::new(dir).join(name);
    if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(path) {
        let _ = writeln!(f, "{}", serde_json::to_string(value).unwrap_or_default());
    }
}

fn round2(v: f64) -> f64 {
    (v * 100.0).round() / 100.0
}
