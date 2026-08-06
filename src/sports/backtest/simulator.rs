//! Event-time simulator for overlapping games.
//!
//! `engine::run_backtest` walks sessions sequentially and compounds the
//! bankroll between them. That is correct for crypto, where exactly one 5-minute
//! window is live at a time. Sports games overlap — thirteen NFL games kick off
//! at once on a Sunday afternoon — so a sequential walk would let the same
//! dollar be deployed in every one of them simultaneously.
//!
//! This simulator merges all games onto a single wall-clock timeline, holds one
//! shared bankroll, and enforces exposure caps across concurrently open
//! positions. It also records the peak concurrent exposure, which is the risk
//! number that simply does not exist in the crypto path.

use std::collections::HashMap;

use serde::Serialize;

use crate::sports::backtest::fills::{
    fill_market_buy, fill_market_sell, FillConfig,
};
use crate::sports::backtest::metrics::{
    brier_score, brier_skill_score, expected_calibration_error, log_loss, sample_size_warning,
    summarize_clv, Prediction, SportsEvaluation, TradeRecord,
};
use crate::sports::fair_value::coherence;
use crate::sports::fair_value::staleness::{StalenessConfig, StalenessDetector};
use crate::sports::fair_value::{self, FairValue, FairValueSource, GameParams};
use crate::sports::models::{GameSample, GameSessionMeta, GameState};
use crate::sports::strategy::{
    common_gates, DecisionGate, DecisionRecord, SportsStrategy, SportsTickContext,
};
use crate::trader::{Position, Side};

/// Portfolio-level risk limits.
///
/// None of this exists in the crypto path, where only one position can be open
/// at a time. With overlapping games the same bankroll would otherwise be
/// committed many times over, and thirteen Sunday-afternoon games resolving
/// together is a fat tail, not thirteen independent bets.
#[derive(Debug, Clone)]
pub struct AllocatorConfig {
    /// Fraction of bankroll deployable to any single game.
    pub max_per_game_fraction: f64,
    /// Absolute cap on a single game's stake.
    pub max_per_game_cash: f64,
    /// Fraction of bankroll deployable across all open positions at once.
    pub max_total_exposure_fraction: f64,
    /// Hard cap on the number of simultaneously open games.
    pub max_concurrent_games: usize,
}

impl Default for AllocatorConfig {
    fn default() -> Self {
        Self {
            max_per_game_fraction: 0.05,
            max_per_game_cash: 50.0,
            max_total_exposure_fraction: 0.30,
            max_concurrent_games: 6,
        }
    }
}

#[derive(Debug, Clone)]
pub struct SimConfig {
    pub starting_cash: f64,
    pub allocator: AllocatorConfig,
    pub fills: FillConfig,
    /// Charge fees on fills.
    pub fees: bool,
    /// Endgame boundary in minutes, for window classification.
    pub endgame_minutes: f64,
    /// Record every decision, not only trades.
    pub record_decisions: bool,
    /// Market kinds strategies may trade. Empty means moneyline only.
    pub market_kinds: Vec<crate::sports::models::SportsMarketKind>,
}

impl Default for SimConfig {
    fn default() -> Self {
        Self {
            starting_cash: 1000.0,
            allocator: AllocatorConfig::default(),
            fills: FillConfig::default(),
            fees: true,
            endgame_minutes: 5.0,
            record_decisions: false,
            market_kinds: Vec::new(),
        }
    }
}

/// One game's simulation state.
struct GameSim {
    key: String,
    meta: GameSessionMeta,
    samples: Vec<GameSample>,
    cursor: usize,
    position: Position,
    /// Cash the allocator committed to this game.
    committed: f64,
    strategy: Box<dyn SportsStrategy>,
    staleness: StalenessDetector,
    params: GameParams,
    /// Entry bookkeeping per leg, for CLV.
    entries: HashMap<usize, (f64, f64, f64, f64)>,
    open: bool,
    /// Previous state/midpoints, for driving the staleness detector.
    last_state: Option<crate::sports::models::GameState>,
    last_midpoints: Vec<Option<f64>>,
}

/// A timeline event: game `index` has a sample at `timestamp`.
struct TimelineEvent {
    timestamp: f64,
    game: usize,
    sample: usize,
}

/// Result of a full simulation run.
#[derive(Debug, Clone, Serialize)]
pub struct SimResult {
    pub evaluation: SportsEvaluation,
    pub trades: Vec<TradeRecord>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub decisions: Vec<DecisionRecord>,
    pub final_bankroll: f64,
}

/// Run a strategy across a set of games on a merged timeline.
pub fn run(
    factory: &dyn Fn() -> Box<dyn SportsStrategy>,
    games: &[(GameSessionMeta, Vec<GameSample>)],
    params_by_game: &HashMap<String, GameParams>,
    cfg: &SimConfig,
) -> SimResult {
    let mut sims: Vec<GameSim> = Vec::new();

    for (meta, samples) in games {
        if samples.is_empty() {
            continue;
        }
        let key = meta.group.dir_name();
        let params = params_by_game
            .get(&key)
            .cloned()
            .unwrap_or_else(|| GameParams::league_average(meta.sport));
        let mut strategy = factory();
        strategy.on_game_start(&meta.group, meta.sport);

        sims.push(GameSim {
            key,
            meta: meta.clone(),
            samples: samples.clone(),
            cursor: 0,
            position: Position::new(0.0),
            committed: 0.0,
            strategy,
            staleness: StalenessDetector::new(StalenessConfig::default()),
            params,
            entries: HashMap::new(),
            open: false,
            last_state: None,
            last_midpoints: Vec::new(),
        });
    }

    // Merge every sample of every game onto one wall-clock timeline. Sorting by
    // absolute timestamp is what makes concurrent exposure meaningful — a
    // per-game walk would let the same dollar fund every game at once.
    let mut timeline: Vec<TimelineEvent> = Vec::new();
    for (gi, sim) in sims.iter().enumerate() {
        let base = parse_epoch(&sim.meta.collection_started_utc).unwrap_or(0.0);
        for (si, s) in sim.samples.iter().enumerate() {
            let ts = parse_epoch(&s.timestamp_utc).unwrap_or(base + s.elapsed_seconds);
            timeline.push(TimelineEvent {
                timestamp: ts,
                game: gi,
                sample: si,
            });
        }
    }
    timeline.sort_by(|a, b| {
        a.timestamp
            .partial_cmp(&b.timestamp)
            .unwrap_or(std::cmp::Ordering::Equal)
    });

    // Simulated time is projected onto the real monotonic clock relative to the
    // first event, so offsets stay small. Adding a raw unix epoch to an Instant
    // would be a ~56 year offset.
    let sim_epoch = std::time::Instant::now();
    let timeline_start = timeline.first().map(|e| e.timestamp).unwrap_or(0.0);

    let mut bankroll = cfg.starting_cash;
    let mut trades: Vec<TradeRecord> = Vec::new();
    let mut decisions: Vec<DecisionRecord> = Vec::new();
    let mut max_exposure = 0.0_f64;
    let mut max_open_games = 0usize;
    let mut model_preds: Vec<Prediction> = Vec::new();
    let mut market_preds: Vec<Prediction> = Vec::new();

    for ev in &timeline {
        // Split the borrow: we need the game mutably and the others' exposure.
        let exposure_elsewhere: f64 = sims
            .iter()
            .enumerate()
            .filter(|(i, _)| *i != ev.game)
            .map(|(_, s)| s.committed)
            .sum();
        let open_elsewhere = sims
            .iter()
            .enumerate()
            .filter(|(i, s)| *i != ev.game && s.open)
            .count();

        let sim = &mut sims[ev.game];
        sim.cursor = ev.sample;
        let sample = sim.samples[ev.sample].clone();
        let sample = &sample;
        let sport = sim.meta.sport;

        // Drive the staleness detector from the recorded stream so layer-0
        // strategies see the same signal in backtest as they would live.
        let now_instant = simulated_instant(sim_epoch, ev.timestamp - timeline_start);
        let midpoints_now: Vec<Option<f64>> = sample.books.iter().map(|b| b.midpoint).collect();
        if let Some(prev) = sim.last_state.clone() {
            if let Some(scorer) = scoring_leg(&prev, &sample.state) {
                sim.staleness
                    .on_event(now_instant, &sim.last_midpoints, scorer);
            }
        }
        let stale_signal = sim.staleness.evaluate(now_instant, &midpoints_now);
        sim.last_state = Some(sample.state.clone());
        sim.last_midpoints = midpoints_now.clone();

        // Fair value from this game's params.
        let fv: Option<FairValue> =
            fair_value::evaluate(sport, &sim.params, &sample.state, true);

        // Layer-0 signals.
        let midpoints = midpoints_now;
        let asks: Vec<Option<f64>> = sample.books.iter().map(|b| b.best_ask).collect();
        let ml_count = sim.meta.group.moneyline_legs().len();
        let coherence_report = coherence::assess(
            &midpoints[..ml_count.min(midpoints.len())],
            &asks[..ml_count.min(asks.len())],
        );

        // Allocator: how much may this game deploy right now?
        let per_game_cap = (bankroll * cfg.allocator.max_per_game_fraction)
            .min(cfg.allocator.max_per_game_cash);
        let total_room =
            (bankroll * cfg.allocator.max_total_exposure_fraction - exposure_elsewhere).max(0.0);
        let concurrency_ok = sim.open || open_elsewhere < cfg.allocator.max_concurrent_games;
        let available = if concurrency_ok {
            per_game_cap.min(total_room).min(bankroll)
        } else {
            0.0
        };

        let ctx = SportsTickContext {
            sample,
            group: &sim.meta.group,
            sport,
            position: &sim.position,
            available_cash: available,
            elapsed_seconds: sample.elapsed_seconds,
            remaining_minutes: sample.state.remaining_minutes(sport),
            fair_value: fv.as_ref(),
            coherence: &coherence_report,
            stale_signal: stale_signal.as_ref(),
            under_review: sample.state.under_review,
            // In backtest we have only the recorded primary feed, so feeds are
            // treated as agreeing. Live trading applies the real check.
            feeds_agree: true,
            market_kinds: &cfg.market_kinds,
        };

        // Common gates first, then the strategy.
        let decision = match common_gates(&ctx, sim.strategy.requires_model()) {
            Some(gate) => crate::sports::strategy::SportsDecision::hold(gate),
            None if !concurrency_ok => {
                crate::sports::strategy::SportsDecision::hold(DecisionGate::ExposureLimit)
            }
            None => sim.strategy.on_tick(&ctx),
        };

        if cfg.record_decisions
            || matches!(decision.gate, DecisionGate::Traded)
        {
            decisions.push(decision.to_record(
                sim.strategy.name(),
                sample.elapsed_seconds,
                &sample.timestamp_utc,
            ));
        }

        // Calibration samples: one model prediction and one market prediction
        // per tick, both scored against the eventual outcome.
        if let (Some(fv), Some(outcome_leg)) = (fv.as_ref(), sim.meta.outcome_leg()) {
            if let Some(p) = fv.probs.for_leg(0) {
                model_preds.push(Prediction {
                    p,
                    occurred: outcome_leg == 0,
                });
            }
            if let Some(Some(mid)) = midpoints.first() {
                market_preds.push(Prediction {
                    p: *mid,
                    occurred: outcome_leg == 0,
                });
            }
        }

        // Execute.
        if !decision.action.is_hold() {
            let parsed = decision.action.parsed_side();
            if let Some(leg) = parsed.leg() {
                let book = sample.books.get(leg).cloned().unwrap_or_default();
                match parsed {
                    Side::Buy(_) => {
                        let budget = decision.action.amount.min(available);
                        let price_hint = decision
                            .action
                            .price
                            .or(book.best_ask)
                            .unwrap_or(0.0);
                        if price_hint > 0.0 && price_hint < 1.0 && budget > 0.0 {
                            let want = budget / price_hint;
                            let fill = fill_market_buy(&book, want, &cfg.fills);
                            if !fill.is_empty() {
                                let cost = fill.tokens * fill.avg_price;
                                let fee = sim
                                    .meta
                                    .group
                                    .legs
                                    .first()
                                    .map(|_| {
                                        crate::economics::MarketEconomics::sports()
                                            .taker_fee(fill.avg_price, fill.tokens, cfg.fees)
                                    })
                                    .unwrap_or(0.0);
                                bankroll -= cost + fee;
                                sim.committed += cost + fee;
                                sim.position.add_leg(leg, fill.tokens);
                                sim.open = true;
                                sim.entries.insert(
                                    leg,
                                    (
                                        fill.avg_price,
                                        midpoints.get(leg).copied().flatten().unwrap_or(fill.avg_price),
                                        decision.model_prob.unwrap_or(0.0),
                                        sample.elapsed_seconds,
                                    ),
                                );
                            }
                        }
                    }
                    Side::Sell(_) => {
                        let held = sim.position.leg(leg);
                        let want = decision.action.amount.min(held);
                        if want > 0.0 {
                            let fill = fill_market_sell(&book, want, &cfg.fills);
                            if !fill.is_empty() {
                                let proceeds = fill.tokens * fill.avg_price;
                                bankroll += proceeds;
                                sim.committed = (sim.committed - proceeds).max(0.0);
                                sim.position.add_leg(leg, -fill.tokens);
                            }
                        }
                    }
                    _ => {}
                }
            }
        }

        let total_exposure: f64 = sims.iter().map(|s| s.committed).sum();
        max_exposure = max_exposure.max(total_exposure);
        let open_now = sims.iter().filter(|s| s.open).count();
        max_open_games = max_open_games.max(open_now);

        // Resolve at the last sample of the game.
        let sim = &mut sims[ev.game];
        if ev.sample + 1 == sim.samples.len() {
            let outcome_leg = sim.meta.outcome_leg();
            let closing: Vec<Option<f64>> = sim
                .samples
                .last()
                .map(|s| s.books.iter().map(|b| b.midpoint).collect())
                .unwrap_or_default();

            for (leg, (entry_price, entry_mid, model_prob, elapsed)) in sim.entries.clone() {
                let tokens = sim.position.leg(leg);
                if tokens <= 0.0 {
                    continue;
                }
                let won = outcome_leg == Some(leg);
                let payout = if won { tokens } else { 0.0 };
                let pnl = payout - tokens * entry_price;
                trades.push(TradeRecord {
                    game_key: sim.key.clone(),
                    leg,
                    leg_key: sim
                        .meta
                        .group
                        .legs
                        .get(leg)
                        .map(|l| l.key.clone())
                        .unwrap_or_default(),
                    entry_price,
                    entry_midpoint: entry_mid,
                    closing_midpoint: closing
                        .get(leg)
                        .copied()
                        .flatten()
                        .unwrap_or(entry_price),
                    model_prob,
                    won,
                    tokens,
                    pnl,
                    elapsed_seconds: elapsed,
                });
            }

            if let Some(leg) = outcome_leg {
                bankroll += sim.position.leg(leg);
            }
            sim.position = Position::new(0.0);
            sim.committed = 0.0;
            sim.open = false;
        }
    }

    let games_traded = trades
        .iter()
        .map(|t| t.game_key.clone())
        .collect::<std::collections::HashSet<_>>()
        .len();
    let total_pnl: f64 = trades.iter().map(|t| t.pnl).sum();
    let n_games = sims.len();

    let tunable = factory().tunable_count();

    let evaluation = SportsEvaluation {
        games: n_games,
        games_traded,
        total_pnl,
        mean_pnl_per_game: if n_games > 0 {
            total_pnl / n_games as f64
        } else {
            0.0
        },
        max_concurrent_exposure: max_exposure,
        max_concurrent_games_open: max_open_games,
        clv: summarize_clv(&trades),
        model_brier: brier_score(&model_preds),
        model_log_loss: log_loss(&model_preds),
        model_ece: expected_calibration_error(&model_preds, 10),
        market_brier: brier_score(&market_preds),
        brier_skill_vs_market: brier_skill_score(&model_preds, &market_preds),
        sample_size_warning: sample_size_warning(n_games, tunable),
    };

    SimResult {
        evaluation,
        trades,
        decisions,
        final_bankroll: bankroll,
    }
}

/// Which leg just scored, if any.
fn scoring_leg(prev: &GameState, next: &GameState) -> Option<usize> {
    if next.home_score > prev.home_score {
        Some(0)
    } else if next.away_score > prev.away_score {
        Some(1)
    } else {
        None
    }
}

/// Map a simulated wall-clock timestamp onto an `Instant`.
///
/// `StalenessDetector` works in `Instant`s because it is a live component; the
/// backtest needs the same code path, so simulated time is projected onto the
/// real monotonic clock relative to a fixed origin.
fn simulated_instant(origin: std::time::Instant, offset_seconds: f64) -> std::time::Instant {
    origin + std::time::Duration::from_secs_f64(offset_seconds.max(0.0))
}

/// Seconds since the epoch for an RFC3339 timestamp.
fn parse_epoch(ts: &str) -> Option<f64> {
    chrono::DateTime::parse_from_rfc3339(ts)
        .ok()
        .map(|d| d.timestamp_millis() as f64 / 1000.0)
}

/// Split games into train/test folds by **season**.
///
/// Random splits leak: two halves of the same season share team form, injuries
/// and schedule structure, so a random split flatters any model. Season
/// boundaries are the only honest split for sports.
pub fn season_split(
    games: &[(GameSessionMeta, Vec<GameSample>)],
    season_of: &dyn Fn(&GameSessionMeta) -> String,
) -> HashMap<String, Vec<usize>> {
    let mut out: HashMap<String, Vec<usize>> = HashMap::new();
    for (i, (meta, _)) in games.iter().enumerate() {
        out.entry(season_of(meta)).or_default().push(i);
    }
    out
}

/// Default season key: the calendar year of kickoff, or `unknown`.
pub fn default_season_key(meta: &GameSessionMeta) -> String {
    meta.group
        .start_time_utc
        .get(0..4)
        .unwrap_or("unknown")
        .to_string()
}

/// Whether a fair value came from fitted rather than fallback parameters.
pub fn is_fitted(fv: &FairValue) -> bool {
    fv.source == FairValueSource::ModelFitted
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sports::models::{
        GameStatus, LegBook, MarketGroup, Sport, SportsLeg, SportsMarketKind,
    };
    use crate::sports::strategy::{SportsDecision, TradingWindow};
    use crate::trader::TradeAction;

    /// Buys the home leg on the first tick it is allowed to, at the ask.
    struct AlwaysBuyHome {
        done: bool,
    }

    impl SportsStrategy for AlwaysBuyHome {
        fn name(&self) -> &str {
            "AlwaysBuyHome"
        }
        fn version(&self) -> &str {
            "1"
        }
        fn requires_model(&self) -> bool {
            false
        }
        fn windows(&self) -> Vec<TradingWindow> {
            vec![TradingWindow::InPlay, TradingWindow::EndGame]
        }
        fn on_game_start(&mut self, _g: &MarketGroup, _s: Sport) {
            self.done = false;
        }
        fn on_tick(&mut self, ctx: &SportsTickContext) -> SportsDecision {
            if self.done || ctx.available_cash <= 0.0 {
                return SportsDecision::hold(DecisionGate::AlreadyPositioned);
            }
            let ask = match ctx.leg_best_ask(0) {
                Some(a) => a,
                None => return SportsDecision::hold(DecisionGate::NoPrice),
            };
            self.done = true;
            SportsDecision::trade(
                TradeAction::buy("buy_home", ctx.available_cash, ask, None),
                0,
                0.7,
                ctx.leg_midpoint(0).unwrap_or(ask),
            )
        }
        fn with_params(
            &self,
            _p: &HashMap<String, crate::strategy::ParamValue>,
        ) -> Box<dyn SportsStrategy> {
            Box::new(AlwaysBuyHome { done: false })
        }
    }

    fn group(id: i64, date: &str) -> MarketGroup {
        MarketGroup {
            game_id: Some(id),
            league_key: "nfl".into(),
            event_id: format!("e{id}"),
            event_slug: format!("s{id}"),
            event_title: "t".into(),
            home_team: format!("Home{id}"),
            away_team: format!("Away{id}"),
            start_time_utc: format!("{date}T20:00:00Z"),
            legs: vec![
                SportsLeg {
                    key: "home".into(),
                    label: "H".into(),
                    token_id: format!("th{id}"),
                    condition_id: "c".into(),
                    kind: SportsMarketKind::Moneyline,
                    line: None,
                },
                SportsLeg {
                    key: "away".into(),
                    label: "A".into(),
                    token_id: format!("ta{id}"),
                    condition_id: "c".into(),
                    kind: SportsMarketKind::Moneyline,
                    line: None,
                },
            ],
        }
    }

    fn deep_book(mid: f64) -> LegBook {
        LegBook {
            midpoint: Some(mid),
            best_bid: Some(mid - 0.01),
            best_ask: Some(mid + 0.01),
            bid_levels: vec![(mid - 0.01, 100_000.0)],
            ask_levels: vec![(mid + 0.01, 100_000.0)],
        }
    }

    /// A game whose samples all share one wall-clock start, so overlapping
    /// games can be constructed precisely.
    fn make_game(
        id: i64,
        date: &str,
        start_utc: &str,
        home_wins: bool,
        n: usize,
    ) -> (GameSessionMeta, Vec<GameSample>) {
        let meta = GameSessionMeta {
            group: group(id, date),
            league_key: "nfl".into(),
            sport: Sport::AmericanFootball,
            collection_started_utc: start_utc.into(),
            collection_ended_utc: String::new(),
            final_state: None,
            outcome_leg_key: Some(if home_wins { "home".into() } else { "away".into() }),
        };

        let base = chrono::DateTime::parse_from_rfc3339(start_utc).unwrap();
        let samples = (0..n)
            .map(|i| {
                let ts = base + chrono::Duration::seconds(i as i64 * 60);
                GameSample {
                    timestamp_utc: ts.to_rfc3339(),
                    elapsed_seconds: i as f64 * 60.0,
                    state: GameState {
                        home_score: 0,
                        away_score: 0,
                        period: "Q1".into(),
                        clock: "15:00".into(),
                        status: GameStatus::InProgress,
                        elapsed_minutes: Some(i as f64),
                        home_red_cards: 0,
                        away_red_cards: 0,
                        under_review: false,
                    },
                    books: vec![deep_book(0.60), deep_book(0.40)],
                    model_probs: vec![],
                    reference_home_prob: None,
                }
            })
            .collect();

        (meta, samples)
    }

    fn factory() -> Box<dyn SportsStrategy> {
        Box::new(AlwaysBuyHome { done: false })
    }

    #[test]
    fn a_winning_bet_increases_the_bankroll() {
        let games = vec![make_game(1, "2026-01-04", "2026-01-04T20:00:00Z", true, 5)];
        let cfg = SimConfig::default();
        let r = run(&factory, &games, &HashMap::new(), &cfg);

        assert_eq!(r.trades.len(), 1);
        assert!(r.trades[0].won);
        assert!(
            r.final_bankroll > cfg.starting_cash,
            "bankroll {} should exceed {}",
            r.final_bankroll,
            cfg.starting_cash
        );
    }

    #[test]
    fn a_losing_bet_decreases_the_bankroll() {
        let games = vec![make_game(1, "2026-01-04", "2026-01-04T20:00:00Z", false, 5)];
        let cfg = SimConfig::default();
        let r = run(&factory, &games, &HashMap::new(), &cfg);
        assert!(!r.trades[0].won);
        assert!(r.final_bankroll < cfg.starting_cash);
    }

    #[test]
    fn concurrent_games_share_one_bankroll_rather_than_each_getting_it_all() {
        // Three games starting at the same instant.
        let games: Vec<_> = (1..=3)
            .map(|i| make_game(i, "2026-01-04", "2026-01-04T20:00:00Z", true, 5))
            .collect();

        let mut cfg = SimConfig::default();
        cfg.starting_cash = 1000.0;
        cfg.allocator.max_per_game_fraction = 0.10; // 100 per game
        cfg.allocator.max_total_exposure_fraction = 0.15; // 150 total
        cfg.allocator.max_per_game_cash = 1000.0;

        let r = run(&factory, &games, &HashMap::new(), &cfg);

        assert!(
            r.evaluation.max_concurrent_exposure <= 155.0,
            "peak exposure {} breached the 15% portfolio cap",
            r.evaluation.max_concurrent_exposure
        );
        assert!(
            r.evaluation.max_concurrent_exposure > 100.0,
            "more than one game should have been funded: {}",
            r.evaluation.max_concurrent_exposure
        );
    }

    #[test]
    fn the_concurrent_game_count_is_capped() {
        let games: Vec<_> = (1..=8)
            .map(|i| make_game(i, "2026-01-04", "2026-01-04T20:00:00Z", true, 4))
            .collect();

        let mut cfg = SimConfig::default();
        cfg.allocator.max_concurrent_games = 2;
        cfg.allocator.max_total_exposure_fraction = 1.0;
        cfg.allocator.max_per_game_fraction = 0.05;

        let r = run(&factory, &games, &HashMap::new(), &cfg);
        // games_traded counts the whole run: a resolved game frees its slot, so
        // more than two games may trade over time. What the cap governs is how
        // many are open *at the same instant*.
        assert!(
            r.evaluation.max_concurrent_games_open <= 2,
            "{} games were open simultaneously with a cap of 2",
            r.evaluation.max_concurrent_games_open
        );
    }

    #[test]
    fn sequential_games_can_each_use_the_full_per_game_allowance() {
        // Same three games, but a week apart — no overlap, so the cap on
        // simultaneous exposure never binds.
        let games = vec![
            make_game(1, "2026-01-04", "2026-01-04T20:00:00Z", true, 4),
            make_game(2, "2026-01-11", "2026-01-11T20:00:00Z", true, 4),
            make_game(3, "2026-01-18", "2026-01-18T20:00:00Z", true, 4),
        ];
        let mut cfg = SimConfig::default();
        cfg.allocator.max_concurrent_games = 1;
        let r = run(&factory, &games, &HashMap::new(), &cfg);
        assert_eq!(
            r.evaluation.games_traded, 3,
            "non-overlapping games must not be blocked by the concurrency cap"
        );
    }

    #[test]
    fn clv_is_recorded_against_the_closing_midpoint() {
        let (meta, mut samples) = make_game(1, "2026-01-04", "2026-01-04T20:00:00Z", true, 4);
        // Market drifts up after entry.
        for (i, s) in samples.iter_mut().enumerate() {
            let mid = 0.60 + i as f64 * 0.05;
            s.books = vec![deep_book(mid), deep_book(1.0 - mid)];
        }
        let r = run(&factory, &[(meta, samples)], &HashMap::new(), &SimConfig::default());
        let t = &r.trades[0];
        assert!(t.clv() > 0.0, "CLV was {}", t.clv());
        assert!(r.evaluation.clv.positive_rate > 0.0);
    }

    #[test]
    fn calibration_is_measured_against_the_market_as_benchmark() {
        let games: Vec<_> = (1..=4)
            .map(|i| {
                make_game(
                    i,
                    "2026-01-04",
                    &format!("2026-01-{:02}T20:00:00Z", 4 + i),
                    i % 2 == 0,
                    3,
                )
            })
            .collect();
        let r = run(&factory, &games, &HashMap::new(), &SimConfig::default());
        assert!(r.evaluation.model_brier.is_some());
        assert!(r.evaluation.market_brier.is_some());
        assert!(r.evaluation.brier_skill_vs_market.is_some());
    }

    #[test]
    fn a_small_game_count_raises_the_sample_size_warning() {
        // AlwaysBuyHome has no tunable params, so no warning.
        let games = vec![make_game(1, "2026-01-04", "2026-01-04T20:00:00Z", true, 3)];
        let r = run(&factory, &games, &HashMap::new(), &SimConfig::default());
        assert_eq!(r.evaluation.sample_size_warning, None);

        // But the helper must fire for a realistic sweep.
        assert!(sample_size_warning(272, 5).is_some());
    }

    #[test]
    fn season_split_groups_by_year_not_randomly() {
        let games = vec![
            make_game(1, "2025-11-04", "2025-11-04T20:00:00Z", true, 2),
            make_game(2, "2025-12-04", "2025-12-04T20:00:00Z", true, 2),
            make_game(3, "2026-01-04", "2026-01-04T20:00:00Z", true, 2),
        ];
        let split = season_split(&games, &default_season_key);
        assert_eq!(split.get("2025").map(|v| v.len()), Some(2));
        assert_eq!(split.get("2026").map(|v| v.len()), Some(1));
    }

    #[test]
    fn games_with_no_samples_are_skipped_without_panicking() {
        let (meta, _) = make_game(1, "2026-01-04", "2026-01-04T20:00:00Z", true, 3);
        let r = run(&factory, &[(meta, vec![])], &HashMap::new(), &SimConfig::default());
        assert_eq!(r.evaluation.games, 0);
        assert!(r.trades.is_empty());
    }

    #[test]
    fn a_thin_book_prevents_the_fill_rather_than_inventing_one() {
        let (meta, mut samples) = make_game(1, "2026-01-04", "2026-01-04T20:00:00Z", true, 3);
        for s in samples.iter_mut() {
            // Touch prices but no recorded depth.
            s.books = vec![
                LegBook {
                    midpoint: Some(0.60),
                    best_bid: Some(0.59),
                    best_ask: Some(0.61),
                    bid_levels: vec![],
                    ask_levels: vec![],
                },
                deep_book(0.40),
            ];
        }
        let r = run(&factory, &[(meta, samples)], &HashMap::new(), &SimConfig::default());
        assert!(
            r.trades.is_empty(),
            "no depth means no honest fill; got {:?}",
            r.trades
        );
    }
}
