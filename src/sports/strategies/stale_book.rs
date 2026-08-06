//! StaleBook — buy the side that just scored, before the book reprices.
//!
//! **EXAMPLE STRATEGY** — illustrative only, with no evidence of profitability.
//!
//! This is the pure layer-0 idea and needs no fair-value model at all: a goal
//! or touchdown just happened, and the book has not moved. It is therefore the
//! strategy most directly exposed to the Phase 1 latency measurement — if the
//! book consistently moves before the feed reports, this cannot work and the
//! collector's latency report will say so before a cent is risked.
//!
//! Two tunable parameters. The third lever, the reaction window, lives in
//! `StalenessConfig` because it is a property of the detector rather than of
//! the idea.

use std::collections::HashMap;

use crate::sports::models::{MarketGroup, Sport};
use crate::sports::strategy::{
    DecisionGate, SportsDecision, SportsStrategy, SportsTickContext, TradingWindow,
};
use crate::strategy::{Param, ParamValue};
use crate::trader::TradeAction;

pub struct StaleBook {
    /// Minimum score-implied move we expect before bothering.
    pub min_expected_move: f64,
    /// Never pay more than this for a leg.
    pub max_price: f64,
    /// Fraction of the allocated cash to deploy.
    pub bet_fraction: f64,
    // State
    entered_legs: Vec<usize>,
    last_score: Option<(i32, i32)>,
}

impl Default for StaleBook {
    fn default() -> Self {
        Self {
            min_expected_move: 0.04,
            max_price: 0.90,
            bet_fraction: 1.0,
            entered_legs: Vec::new(),
            last_score: None,
        }
    }
}

impl SportsStrategy for StaleBook {
    fn name(&self) -> &str {
        "StaleBook"
    }

    fn version(&self) -> &str {
        "1"
    }

    fn windows(&self) -> Vec<TradingWindow> {
        vec![TradingWindow::InPlay, TradingWindow::EndGame]
    }

    /// Layer 0: no model needed. The signal is the book disagreeing with a
    /// scoreline we can both see.
    fn requires_model(&self) -> bool {
        false
    }

    fn on_game_start(&mut self, _group: &MarketGroup, _sport: Sport) {
        self.entered_legs.clear();
        self.last_score = None;
    }

    fn on_tick(&mut self, ctx: &SportsTickContext) -> SportsDecision {
        let state = &ctx.sample.state;
        let score = (state.home_score, state.away_score);

        // Which side just scored?
        let scorer_leg = match self.last_score {
            Some((ph, _)) if score.0 > ph => Some(0usize),
            Some((_, pa)) if score.1 > pa => Some(1usize),
            _ => None,
        };
        self.last_score = Some(score);

        if !self.windows().contains(&ctx.window(5.0)) {
            return SportsDecision::hold(DecisionGate::OutsideWindow);
        }

        // Prefer a live staleness signal from the detector; fall back to the
        // score transition observed on this tick.
        let leg = match ctx.stale_signal.map(|s| s.favoured_leg).or(scorer_leg) {
            Some(l) => l,
            None => return SportsDecision::hold(DecisionGate::InsufficientEdge),
        };

        if self.entered_legs.contains(&leg) {
            return SportsDecision::hold(DecisionGate::AlreadyPositioned);
        }

        let mid = match ctx.leg_midpoint(leg) {
            Some(m) => m,
            None => return SportsDecision::hold(DecisionGate::NoPrice),
        };
        let ask = match ctx.leg_best_ask(leg) {
            Some(a) => a,
            None => return SportsDecision::hold(DecisionGate::NoPrice),
        };

        // How stale is the book? A signal from the detector carries the
        // observed (non-)move; without one, use the cross-leg incoherence as a
        // proxy for one side having repriced and the other not.
        let observed_move = ctx.stale_signal.map(|s| s.observed_move).unwrap_or(0.0);
        let implied_gap = if ctx.coherence.is_incoherent() {
            ctx.coherence.deviation.abs()
        } else {
            0.0
        };
        let staleness = (self.min_expected_move - observed_move).max(implied_gap);

        if staleness < self.min_expected_move {
            return SportsDecision::hold_with(DecisionGate::InsufficientEdge, leg, mid + staleness, mid);
        }

        if ask > self.max_price {
            return SportsDecision::hold_with(DecisionGate::PriceOutOfBand, leg, mid, ask);
        }

        if ctx.available_cash <= 0.0 {
            return SportsDecision::hold_with(DecisionGate::ExposureLimit, leg, mid, ask);
        }

        self.entered_legs.push(leg);
        SportsDecision::trade(
            TradeAction::buy(
                &leg_side(leg),
                ctx.available_cash * self.bet_fraction,
                ask,
                Some(10.0),
            ),
            leg,
            mid + staleness,
            mid,
        )
        .with_note(format!(
            "score {}-{}, book moved {:.3} since event",
            score.0, score.1, observed_move
        ))
    }

    fn on_trade_failed(&mut self, side: &str) {
        if let Some(leg) = crate::trader::Side::parse(side).and_then(|s| s.leg()) {
            self.entered_legs.retain(|l| *l != leg);
        }
    }

    fn param_defs(&self) -> HashMap<String, Param> {
        let mut m = HashMap::new();
        m.insert("min_expected_move".into(), Param::float(0.04, 0.02, 0.10));
        m.insert("max_price".into(), Param::float(0.90, 0.75, 0.95));
        m.insert("bet_fraction".into(), Param::fixed_float(1.0));
        m
    }

    fn params(&self) -> HashMap<String, ParamValue> {
        let mut m = HashMap::new();
        m.insert(
            "min_expected_move".into(),
            ParamValue::Float(self.min_expected_move),
        );
        m.insert("max_price".into(), ParamValue::Float(self.max_price));
        m
    }

    fn with_params(&self, params: &HashMap<String, ParamValue>) -> Box<dyn SportsStrategy> {
        let mut s = StaleBook::default();
        for (k, v) in params {
            match k.as_str() {
                "min_expected_move" => {
                    if let Some(f) = v.as_f64() {
                        s.min_expected_move = f;
                    }
                }
                "max_price" => {
                    if let Some(f) = v.as_f64() {
                        s.max_price = f;
                    }
                }
                "bet_fraction" => {
                    if let Some(f) = v.as_f64() {
                        s.bet_fraction = f;
                    }
                }
                _ => {}
            }
        }
        Box::new(s)
    }
}

fn leg_side(leg: usize) -> String {
    match leg {
        0 => "buy_home".to_string(),
        1 => "buy_away".to_string(),
        2 => "buy_draw".to_string(),
        n => format!("buy:{n}"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sports::fair_value::coherence;
    use crate::sports::fair_value::staleness::StaleSignal;
    use crate::sports::models::{
        GameSample, GameState, GameStatus, LegBook, SportsLeg, SportsMarketKind,
    };
    use crate::trader::Position;

    fn group() -> MarketGroup {
        MarketGroup {
            game_id: Some(1),
            league_key: "nfl".into(),
            event_id: "e".into(),
            event_slug: "s".into(),
            event_title: "t".into(),
            home_team: "H".into(),
            away_team: "A".into(),
            start_time_utc: "2026-02-09T23:30:00Z".into(),
            legs: vec![
                SportsLeg {
                    key: "home".into(),
                    label: "H".into(),
                    token_id: "th".into(),
                    condition_id: "c".into(),
                    kind: SportsMarketKind::Moneyline,
                    line: None,
                },
                SportsLeg {
                    key: "away".into(),
                    label: "A".into(),
                    token_id: "ta".into(),
                    condition_id: "c".into(),
                    kind: SportsMarketKind::Moneyline,
                    line: None,
                },
            ],
        }
    }

    fn sample(home: i32, away: i32, home_mid: f64) -> GameSample {
        GameSample {
            timestamp_utc: "2026-02-09T23:45:00Z".into(),
            elapsed_seconds: 900.0,
            state: GameState {
                home_score: home,
                away_score: away,
                period: "Q2".into(),
                clock: "07:00".into(),
                status: GameStatus::InProgress,
                elapsed_minutes: Some(23.0),
                home_red_cards: 0,
                away_red_cards: 0,
                under_review: false,
            },
            books: vec![
                LegBook {
                    midpoint: Some(home_mid),
                    best_bid: Some(home_mid - 0.01),
                    best_ask: Some(home_mid + 0.01),
                    bid_levels: vec![(home_mid - 0.01, 1000.0)],
                    ask_levels: vec![(home_mid + 0.01, 1000.0)],
                },
                LegBook {
                    midpoint: Some(1.0 - home_mid),
                    best_bid: Some(1.0 - home_mid - 0.01),
                    best_ask: Some(1.0 - home_mid + 0.01),
                    bid_levels: vec![(1.0 - home_mid - 0.01, 1000.0)],
                    ask_levels: vec![(1.0 - home_mid + 0.01, 1000.0)],
                },
            ],
            model_probs: vec![],
            reference_home_prob: None,
        }
    }

    struct Env {
        s: GameSample,
        g: MarketGroup,
        p: Position,
        c: coherence::CoherenceReport,
    }

    fn env(sample: GameSample) -> Env {
        let mids: Vec<Option<f64>> = sample.books.iter().map(|b| b.midpoint).collect();
        let asks: Vec<Option<f64>> = sample.books.iter().map(|b| b.best_ask).collect();
        Env {
            c: coherence::assess(&mids, &asks),
            s: sample,
            g: group(),
            p: Position::new(0.0),
        }
    }

    fn ctx<'a>(e: &'a Env, stale: Option<&'a StaleSignal>, cash: f64) -> SportsTickContext<'a> {
        SportsTickContext {
            sample: &e.s,
            group: &e.g,
            sport: Sport::AmericanFootball,
            position: &e.p,
            available_cash: cash,
            elapsed_seconds: 900.0,
            remaining_minutes: Some(37.0),
            fair_value: None,
            coherence: &e.c,
            stale_signal: stale,
            under_review: false,
            feeds_agree: true,
            market_kinds: &[],
        }
    }

    #[test]
    fn does_not_trade_before_anything_happens() {
        let mut s = StaleBook::default();
        s.on_game_start(&group(), Sport::AmericanFootball);
        let e = env(sample(0, 0, 0.50));
        let d = s.on_tick(&ctx(&e, None, 100.0));
        assert!(d.action.is_hold());
    }

    #[test]
    fn buys_the_scoring_side_when_the_book_has_not_moved() {
        let mut s = StaleBook::default();
        s.on_game_start(&group(), Sport::AmericanFootball);

        // Establish the baseline score.
        let e0 = env(sample(0, 0, 0.50));
        s.on_tick(&ctx(&e0, None, 100.0));

        // Home scores; the book is still at 0.50 and the detector confirms it
        // has not moved.
        let e1 = env(sample(7, 0, 0.50));
        let signal = StaleSignal {
            seconds_since_event: 6.0,
            observed_move: 0.0,
            favoured_leg: 0,
        };
        let d = s.on_tick(&ctx(&e1, Some(&signal), 100.0));

        assert_eq!(d.gate, DecisionGate::Traded);
        assert_eq!(d.action.leg(), Some(0));
        assert_eq!(d.action.side, "buy_home");
    }

    #[test]
    fn does_not_trade_once_the_book_has_already_repriced() {
        let mut s = StaleBook::default();
        s.on_game_start(&group(), Sport::AmericanFootball);
        let e0 = env(sample(0, 0, 0.50));
        s.on_tick(&ctx(&e0, None, 100.0));

        // Home scored and the market already moved a long way.
        let e1 = env(sample(7, 0, 0.68));
        let signal = StaleSignal {
            seconds_since_event: 6.0,
            observed_move: 0.18,
            favoured_leg: 0,
        };
        let d = s.on_tick(&ctx(&e1, Some(&signal), 100.0));
        assert_eq!(d.gate, DecisionGate::InsufficientEdge);
    }

    #[test]
    fn refuses_to_pay_above_the_price_cap() {
        let mut s = StaleBook::default();
        s.max_price = 0.80;
        s.on_game_start(&group(), Sport::AmericanFootball);
        let e0 = env(sample(0, 0, 0.90));
        s.on_tick(&ctx(&e0, None, 100.0));

        let e1 = env(sample(7, 0, 0.90));
        let signal = StaleSignal {
            seconds_since_event: 5.0,
            observed_move: 0.0,
            favoured_leg: 0,
        };
        let d = s.on_tick(&ctx(&e1, Some(&signal), 100.0));
        assert_eq!(d.gate, DecisionGate::PriceOutOfBand);
    }

    #[test]
    fn does_not_enter_the_same_leg_twice() {
        let mut s = StaleBook::default();
        s.on_game_start(&group(), Sport::AmericanFootball);
        let e0 = env(sample(0, 0, 0.50));
        s.on_tick(&ctx(&e0, None, 100.0));

        let e1 = env(sample(7, 0, 0.50));
        let signal = StaleSignal {
            seconds_since_event: 5.0,
            observed_move: 0.0,
            favoured_leg: 0,
        };
        assert_eq!(s.on_tick(&ctx(&e1, Some(&signal), 100.0)).gate, DecisionGate::Traded);
        assert_eq!(
            s.on_tick(&ctx(&e1, Some(&signal), 100.0)).gate,
            DecisionGate::AlreadyPositioned
        );
    }

    #[test]
    fn a_failed_trade_releases_the_leg_for_retry() {
        let mut s = StaleBook::default();
        s.on_game_start(&group(), Sport::AmericanFootball);
        let e0 = env(sample(0, 0, 0.50));
        s.on_tick(&ctx(&e0, None, 100.0));

        let e1 = env(sample(7, 0, 0.50));
        let signal = StaleSignal {
            seconds_since_event: 5.0,
            observed_move: 0.0,
            favoured_leg: 0,
        };
        s.on_tick(&ctx(&e1, Some(&signal), 100.0));
        s.on_trade_failed("buy_home");
        assert_eq!(
            s.on_tick(&ctx(&e1, Some(&signal), 100.0)).gate,
            DecisionGate::Traded,
            "a failed CLOB order must not burn the opportunity"
        );
    }

    #[test]
    fn respects_the_allocators_zero_budget() {
        let mut s = StaleBook::default();
        s.on_game_start(&group(), Sport::AmericanFootball);
        let e0 = env(sample(0, 0, 0.50));
        s.on_tick(&ctx(&e0, None, 100.0));

        let e1 = env(sample(7, 0, 0.50));
        let signal = StaleSignal {
            seconds_since_event: 5.0,
            observed_move: 0.0,
            favoured_leg: 0,
        };
        let d = s.on_tick(&ctx(&e1, Some(&signal), 0.0));
        assert_eq!(d.gate, DecisionGate::ExposureLimit);
    }

    #[test]
    fn needs_no_fair_value_model() {
        assert!(!StaleBook::default().requires_model());
    }

    #[test]
    fn stays_within_the_parameter_budget() {
        assert!(StaleBook::default().tunable_count() <= 3);
    }
}
