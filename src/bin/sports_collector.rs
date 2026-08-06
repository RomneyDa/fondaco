//! Sports data collector — one process per league, many concurrent games.
//!
//! The crypto collector runs one session at a time on a clock grid. Sports has
//! up to ~15 games running concurrently on no grid at all, so this process:
//!
//!   - holds **one** sports-feed WebSocket for live game state
//!   - holds **one** CLOB WebSocket subscribed to every live game's tokens
//!   - re-runs Gamma discovery periodically to pick up newly scheduled games
//!   - drives a per-game `CadenceController` so sampling follows the game
//!     rather than the clock
//!   - appends samples to per-game JSONL and measures feed latency
//!
//! Read-only by default: it collects and measures, it does not trade.
//!
//! Usage:
//!     cargo run --bin sports-collector -- --league nfl

use std::collections::HashMap;
use std::io::Write as IoWrite;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

use chrono::{DateTime, Utc};
use clap::Parser;
use log::{error, info, warn};
use reqwest::blocking::Client;

use fondaco::defaults::API_TIMEOUT_DEFAULT;
use fondaco::session_io::{remove_file_silent, write_pid_file};
use fondaco::sports::book::{fetch_books_batch, fetch_midpoints_batch, MultiBookClient};
use fondaco::sports::cadence::{CadenceConfig, CadenceController, CadencePhase};
use fondaco::sports::discovery::{discover_league, join_to_feed};
use fondaco::sports::espn;
use fondaco::sports::fair_value::{self, GameParams};
use fondaco::sports::feed::SportsFeedClient;
use fondaco::sports::latency::{summarize, LatencyTracker};
use fondaco::sports::selection::{describe, filter_from_flags, SkipReason};
use fondaco::sports::models::{
    outcome_key_from_state, GameSample, GameSessionMeta, GameState, GameStatus, League, LegBook,
    MarketGroup,
};
use fondaco::sports::storage::{now_utc, GameStore};

#[derive(Parser)]
#[command(name = "sports-collector", about = "Collect Polymarket sports market data")]
struct Cli {
    /// League key: nfl, epl, laliga, seriea, bundesliga, ucl, mls
    #[arg(long)]
    league: String,

    /// Output directory
    #[arg(long, default_value = "data")]
    output_dir: String,

    /// Seconds between Gamma discovery passes
    #[arg(long, default_value_t = 300)]
    discovery_interval: u64,

    /// Disable the ESPN secondary feed
    #[arg(long)]
    no_espn: bool,

    /// Seconds between ESPN polls
    #[arg(long, default_value_t = 20)]
    espn_interval: u64,

    /// Write logs to data/logs/sports-collector-<league>.log
    #[arg(long)]
    log_file: bool,

    /// Exit after this many games complete (0 = unlimited)
    #[arg(long, default_value_t = 0)]
    max_games: usize,

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
}

/// Everything the collector tracks for one in-flight game.
struct ActiveGame {
    group: MarketGroup,
    store: GameStore,
    cadence: CadenceController,
    started_at: Instant,
    started_utc: String,
    last_state: Option<GameState>,
    last_books: Vec<LegBook>,
    params: GameParams,
    params_fitted: bool,
    espn_event_id: Option<String>,
    reference_home_prob: Option<f64>,
    finalized: bool,
}

impl ActiveGame {
    fn midpoints(&self) -> Vec<Option<f64>> {
        self.last_books.iter().map(|b| b.midpoint).collect()
    }
}

fn main() {
    let cli = Cli::parse();

    let mut builder =
        env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("info"));
    builder.format(|buf, record| {
        let ts = chrono::Local::now().format("%Y-%m-%d %H:%M:%S%.3f");
        writeln!(buf, "{} [{}] {}", ts, record.level(), record.args())
    });
    if cli.log_file {
        let log_dir = format!("{}/logs", cli.output_dir);
        std::fs::create_dir_all(&log_dir).expect("create log dir");
        let log_path = format!("{log_dir}/sports-collector-{}.log", cli.league);
        let file = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&log_path)
            .unwrap_or_else(|e| panic!("open {log_path}: {e}"));
        builder.target(env_logger::Target::Pipe(Box::new(file)));
        eprintln!("Logging to {log_path}");
    }
    builder.init();

    run(cli);
}

fn run(cli: Cli) {
    let league = match League::known(&cli.league) {
        Some(l) => l,
        None => {
            error!(
                "Unknown league '{}'. Known: {:?}",
                cli.league,
                League::all_keys()
            );
            std::process::exit(1);
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

    // --list-games: show the slate the flags select, then exit. Lets you see
    // what a command would collect before committing to it.
    if cli.list_games {
        let http = Client::builder()
            .timeout(Duration::from_secs(API_TIMEOUT_DEFAULT))
            .build()
            .expect("build HTTP client");
        let feed = SportsFeedClient::new();
        feed.register_league(&league.feed_abbreviation, league.sport);
        feed.start();
        // Give the feed a moment to populate so the gameId join can succeed.
        thread::sleep(Duration::from_secs(5));
        let feed_games = feed.games_for_league(&league.feed_abbreviation);
        let groups = discover_league(&http, &league, &feed_games);
        feed.stop();

        let (kept, skipped) = filter.apply(&groups);
        println!("{} discovered, {} selected\n", groups.len(), kept.len());
        println!("SELECTED");
        for g in &kept {
            println!("  {}", describe(g));
        }
        if !skipped.is_empty() {
            println!("\nSKIPPED");
            for (g, r) in &skipped {
                println!("  {}  <- {}", describe(g), r.as_str());
            }
        }
        return;
    }

    // PID lock, mirroring the crypto collector.
    let pid_path = format!("/tmp/fondaco-sports-{}-collector.pid", league.key);
    if let Ok(contents) = std::fs::read_to_string(&pid_path) {
        if let Ok(old) = contents.trim().parse::<u32>() {
            let alive = std::process::Command::new("kill")
                .args(["-0", &old.to_string()])
                .stdout(std::process::Stdio::null())
                .stderr(std::process::Stdio::null())
                .status()
                .map(|s| s.success())
                .unwrap_or(false);
            if alive {
                error!(
                    "[{}] Another sports collector is running (PID {old}). Exiting.",
                    league.key
                );
                std::process::exit(1);
            }
        }
    }
    write_pid_file(&pid_path);

    let running = Arc::new(AtomicBool::new(true));
    {
        let r = Arc::clone(&running);
        ctrlc::set_handler(move || {
            info!("Shutdown signal received.");
            r.store(false, Ordering::SeqCst);
        })
        .expect("set Ctrl-C handler");
    }

    let http = Client::builder()
        .timeout(Duration::from_secs(API_TIMEOUT_DEFAULT))
        .build()
        .expect("build HTTP client");

    // One sports feed connection for the whole process.
    let feed = SportsFeedClient::new();
    feed.register_league(&league.feed_abbreviation, league.sport);
    feed.start();

    // One CLOB book connection for every live game's tokens.
    let books = MultiBookClient::new();
    books.start();

    let mut latency = LatencyTracker::new(&cli.output_dir, &league.key);

    let mut active: HashMap<i64, ActiveGame> = HashMap::new();
    let mut known_groups: Vec<MarketGroup> = Vec::new();
    let mut completed = 0usize;

    let mut last_discovery = Instant::now() - Duration::from_secs(cli.discovery_interval + 1);
    let mut last_espn = Instant::now() - Duration::from_secs(cli.espn_interval + 1);
    let mut last_status_log = Instant::now();
    let mut espn_by_teams: HashMap<(String, String), espn::EspnGame> = HashMap::new();

    info!(
        "[{}] Sports collector started ({:?}). Discovery every {}s.",
        league.key, league.sport, cli.discovery_interval
    );

    while running.load(Ordering::SeqCst) {
        let now = Instant::now();

        // ---- Discovery ------------------------------------------------
        if now.duration_since(last_discovery).as_secs() >= cli.discovery_interval {
            last_discovery = now;
            let feed_games = feed.games_for_league(&league.feed_abbreviation);
            let discovered = discover_league(&http, &league, &feed_games);
            if !discovered.is_empty() {
                let (kept, skipped) = filter.apply(&discovered);
                if !skipped.is_empty() {
                    record_skips(&cli.output_dir, &league.key, &skipped);
                }
                info!(
                    "[{}] {} discovered, {} selected by flags",
                    league.key,
                    discovered.len(),
                    kept.len()
                );
                known_groups = kept.into_iter().cloned().collect();
            } else {
                // Re-attempt the join on existing groups: the feed may only now
                // have published a game that discovery already found.
                join_to_feed(&mut known_groups, &feed_games);
                known_groups.retain(|g| filter.evaluate(g).is_ok());
            }

            let untradeable = known_groups.iter().filter(|g| !g.is_tradeable()).count();
            if untradeable > 0 {
                warn!(
                    "[{}] {untradeable}/{} groups are not joined to the live feed and \
                     will not be collected",
                    league.key,
                    known_groups.len()
                );
            }
        }

        // ---- ESPN secondary feed --------------------------------------
        if !cli.no_espn && now.duration_since(last_espn).as_secs() >= cli.espn_interval {
            last_espn = now;
            let games = espn::fetch_scoreboard(&http, &league);
            if !games.is_empty() {
                for g in &games {
                    latency.observe_espn(
                        now,
                        // ESPN has no Polymarket game id; match by teams below.
                        // Resolved via `espn_by_teams` when attaching to a game.
                        0,
                        g.state.home_score,
                        g.state.away_score,
                    );
                }
                espn_by_teams = espn::index_by_teams(games);
            }
        }

        // ---- Start collection for games that are due -------------------
        for group in &known_groups {
            let game_id = match group.game_id {
                Some(id) => id,
                None => continue,
            };
            if active.contains_key(&game_id) || !group.is_tradeable() {
                continue;
            }

            let secs_to_kickoff = seconds_to_kickoff(&group.start_time_utc);
            let feed_state = feed.game(game_id).map(|g| g.state);
            let is_live = feed_state
                .as_ref()
                .map(|s| s.status.is_live())
                .unwrap_or(false);
            let due = is_live
                || secs_to_kickoff
                    .map(|s| s <= CadenceConfig::default().pregame_lead.as_secs_f64())
                    .unwrap_or(false);

            // Do not start collecting a game that already finished.
            let ended = feed_state
                .as_ref()
                .map(|s| s.status == GameStatus::Final)
                .unwrap_or(false);
            if !due || ended {
                continue;
            }

            let meta = GameSessionMeta {
                group: group.clone(),
                league_key: league.key.clone(),
                sport: league.sport,
                collection_started_utc: now_utc(),
                collection_ended_utc: String::new(),
                final_state: None,
                outcome_leg_key: None,
            };

            let store = match GameStore::create(&cli.output_dir, &league.key, &meta) {
                Ok(s) => s,
                Err(e) => {
                    error!("[{}] Could not create store for {}: {e}", league.key, group.event_slug);
                    continue;
                }
            };

            info!(
                "[{}] START {} vs {} (gameId {}) -> {}",
                league.key,
                group.away_team,
                group.home_team,
                game_id,
                store.dir().display()
            );

            // League-average params until a fitted set is available. Marked
            // unfitted so downstream consumers know the model has no opinion
            // about who is better.
            let params = GameParams::league_average(league.sport);

            active.insert(
                game_id,
                ActiveGame {
                    group: group.clone(),
                    store,
                    cadence: CadenceController::new(league.sport, CadenceConfig::default()),
                    started_at: now,
                    started_utc: now_utc(),
                    last_state: None,
                    last_books: vec![LegBook::default(); group.legs.len()],
                    params,
                    params_fitted: false,
                    espn_event_id: None,
                    reference_home_prob: None,
                    finalized: false,
                },
            );
        }

        // ---- Keep the book subscription in sync ------------------------
        let mut subscribe: Vec<String> = Vec::new();
        for g in active.values() {
            if !g.cadence.is_finished() {
                subscribe.extend(g.group.token_ids());
            }
        }
        books.set_tokens(&subscribe);

        // ---- Per-game sampling ----------------------------------------
        // Games needing only midpoints are batched into one request; games in
        // a hot phase or holding a position get full depth.
        let mut want_mid: Vec<String> = Vec::new();
        let mut want_depth: Vec<String> = Vec::new();

        for game in active.values_mut() {
            if game.cadence.is_finished() {
                continue;
            }
            let state = feed.game(game.group.game_id.unwrap()).map(|g| g.state);
            let secs_to_kickoff = seconds_to_kickoff(&game.group.start_time_utc);
            game.cadence.observe(now, state.as_ref(), secs_to_kickoff);

            if !game.cadence.should_sample(now) {
                continue;
            }
            if game.cadence.wants_depth() {
                want_depth.extend(game.group.token_ids());
            } else {
                want_mid.extend(game.group.token_ids());
            }
        }

        // Prefer the WebSocket; fall back to batch REST for anything missing.
        let missing_depth: Vec<String> = want_depth
            .iter()
            .filter(|t| books.book(t).is_none())
            .cloned()
            .collect();
        if !missing_depth.is_empty() {
            books.apply_rest_books(fetch_books_batch(&http, &missing_depth));
        }

        let missing_mid: Vec<String> = want_mid
            .iter()
            .filter(|t| books.book(t).is_none())
            .cloned()
            .collect();
        if !missing_mid.is_empty() {
            let mids = fetch_midpoints_batch(&http, &missing_mid);
            let as_books: HashMap<String, LegBook> = mids
                .into_iter()
                .map(|(t, m)| {
                    (
                        t,
                        LegBook {
                            midpoint: Some(m),
                            ..Default::default()
                        },
                    )
                })
                .collect();
            books.apply_rest_books(as_books);
        }

        // ---- Record ----------------------------------------------------
        for (game_id, game) in active.iter_mut() {
            if game.cadence.is_finished() && game.finalized {
                continue;
            }
            if !game.cadence.should_sample(now) && !game.cadence.is_finished() {
                continue;
            }
            game.cadence.mark_sampled(now);

            let feed_state = feed.game(*game_id).map(|g| g.state);
            let state = match feed_state {
                Some(s) => s,
                None => continue,
            };

            // Attach ESPN reference data when the teams line up.
            if game.espn_event_id.is_none() {
                let key = (
                    game.group.home_team.to_ascii_lowercase(),
                    game.group.away_team.to_ascii_lowercase(),
                );
                if let Some(e) = espn_by_teams.get(&key) {
                    game.espn_event_id = Some(e.espn_event_id.clone());
                }
            }
            if let Some(eid) = game.espn_event_id.clone() {
                if !cli.no_espn && now.duration_since(last_espn).as_secs() < 2 {
                    game.reference_home_prob =
                        espn::fetch_home_win_probability(&http, &league, &eid);
                }
            }

            // Current books for this game's legs.
            let mut leg_books = Vec::with_capacity(game.group.legs.len());
            for leg in &game.group.legs {
                leg_books.push(books.book(&leg.token_id).unwrap_or_default());
            }

            let changed = game
                .last_state
                .as_ref()
                .map(|prev| prev.is_material_change(&state))
                .unwrap_or(true)
                || books_changed(&game.last_books, &leg_books);

            // Latency measurement, before overwriting the baseline.
            let midpoints: Vec<Option<f64>> = leg_books.iter().map(|b| b.midpoint).collect();
            latency.observe_state(now, *game_id, &state, &game.midpoints(), &now_utc());
            latency.observe_book(now, *game_id, &midpoints);

            // Model fair value.
            let fv = fair_value::evaluate(league.sport, &game.params, &state, game.params_fitted);
            let model_probs = fv
                .as_ref()
                .map(|f| f.probs.as_vec(league.sport.has_draw()))
                .unwrap_or_default();

            if game.cadence.should_persist(now, changed) {
                let sample = GameSample {
                    timestamp_utc: now_utc(),
                    elapsed_seconds: now.duration_since(game.started_at).as_secs_f64(),
                    state: state.clone(),
                    books: leg_books.clone(),
                    model_probs: model_probs.clone(),
                    reference_home_prob: game.reference_home_prob,
                };
                if let Err(e) = game.store.append_sample(&sample) {
                    warn!("[{}] sample write failed: {e}", league.key);
                }
                game.cadence.mark_persisted(now);

                let _ = game.store.write_state(&serde_json::json!({
                    "game_id": game_id,
                    "phase": game.cadence.phase().as_str(),
                    "status": format!("{:?}", state.status),
                    "score": format!("{}-{}", state.home_score, state.away_score),
                    "period": state.period,
                    "clock": state.clock,
                    "samples": game.store.sample_count(),
                    "model_probs": model_probs,
                    "reference_home_prob": game.reference_home_prob,
                    "started_utc": game.started_utc,
                    "updated_utc": now_utc(),
                }));
            }

            game.last_state = Some(state.clone());
            game.last_books = leg_books;

            // Finalize once the game ends.
            if state.status == GameStatus::Final && !game.finalized {
                let outcome = outcome_key_from_state(league.sport, &state);
                let meta = GameSessionMeta {
                    group: game.group.clone(),
                    league_key: league.key.clone(),
                    sport: league.sport,
                    collection_started_utc: game.started_utc.clone(),
                    collection_ended_utc: now_utc(),
                    final_state: Some(state.clone()),
                    outcome_leg_key: outcome.clone(),
                };
                if let Err(e) = game.store.finalize(&meta) {
                    warn!("[{}] finalize failed: {e}", league.key);
                }
                game.finalized = true;
                completed += 1;
                info!(
                    "[{}] END {} vs {} -> {}-{} outcome={:?} ({} samples)",
                    league.key,
                    game.group.away_team,
                    game.group.home_team,
                    state.home_score,
                    state.away_score,
                    outcome,
                    game.store.sample_count(),
                );
            }
        }

        latency.expire_stale(now);
        active.retain(|_, g| !(g.cadence.is_finished() && g.finalized));

        // ---- Periodic status ------------------------------------------
        if now.duration_since(last_status_log).as_secs() >= 60 {
            last_status_log = now;
            let hot = active
                .values()
                .filter(|g| g.cadence.phase() == CadencePhase::Hot)
                .count();
            info!(
                "[{}] active={} hot={} completed={} feed_msgs={} book_updates={} \
                 latency_records={} feed_silent_for={:?}s",
                league.key,
                active.len(),
                hot,
                completed,
                feed.message_count(),
                books.update_count(),
                latency.records_written(),
                feed.seconds_since_last_message().map(|s| s.round()),
            );

            if feed.message_count() == 0 {
                warn!(
                    "[{}] sports feed has produced no messages — verify the payload \
                     schema at {}",
                    league.key,
                    fondaco::sports::feed::SPORTS_WS_URL
                );
            }
        }

        if cli.max_games > 0 && completed >= cli.max_games {
            info!("[{}] Reached max games ({}). Stopping.", league.key, cli.max_games);
            break;
        }

        // The loop itself runs fast; per-game cadence decides what is actually
        // sampled, so this only bounds scheduling granularity.
        thread::sleep(Duration::from_millis(100));
    }

    feed.stop();
    books.stop();
    remove_file_silent(&pid_path);

    let records = fondaco::sports::latency::load_records(&cli.output_dir, &league.key);
    let summary = summarize(&records);
    info!(
        "[{}] Stopped. {completed} games completed. Latency: {} events, median feed->book {:?}s, \
         book-led fraction {:.1}%",
        league.key,
        summary.events,
        summary.median_feed_to_book_s,
        summary.fraction_book_first * 100.0,
    );
    if summary.events >= 20 && summary.fraction_book_first > 0.5 {
        warn!(
            "[{}] The book led the feed on {:.0}% of events — there may be no \
             event-reaction edge in this league.",
            league.key,
            summary.fraction_book_first * 100.0
        );
    }
}

/// Append skip records so "why wasn't this game collected" is answerable from
/// the logs rather than by re-deriving the filter by hand.
fn record_skips(output_dir: &str, league_key: &str, skipped: &[(&MarketGroup, SkipReason)]) {
    let path = std::path::Path::new(output_dir)
        .join("markets")
        .join(league_key)
        .join("skipped.jsonl");
    if let Some(parent) = path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
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
            "start_time_utc": g.start_time_utc,
            "reason": reason.as_str(),
        });
        let _ = writeln!(f, "{}", serde_json::to_string(&line).unwrap_or_default());
    }
}

/// Seconds until kickoff; negative once it has passed.
fn seconds_to_kickoff(start_time_utc: &str) -> Option<f64> {
    let start = DateTime::parse_from_rfc3339(start_time_utc).ok()?;
    Some((start.with_timezone(&Utc) - Utc::now()).num_milliseconds() as f64 / 1000.0)
}

/// Whether any leg's midpoint moved beyond the price tick.
fn books_changed(prev: &[LegBook], next: &[LegBook]) -> bool {
    if prev.len() != next.len() {
        return true;
    }
    prev.iter().zip(next.iter()).any(|(a, b)| match (a.midpoint, b.midpoint) {
        (Some(x), Some(y)) => (x - y).abs() > 1e-9,
        (None, None) => false,
        _ => true,
    })
}
