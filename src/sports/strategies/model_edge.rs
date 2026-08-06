//! ModelEdge — trade when the fair-value model disagrees with the market.
//!
//! **EXAMPLE STRATEGY** — illustrative only, with no evidence of profitability.
//!
//! The layer-2 idea: the in-play model says a leg is worth more than the book
//! is charging. Unlike `StaleBook` this genuinely requires a trustworthy model,
//! so it refuses to act on league-average fallback parameters — those have no
//! opinion about which team is better, and an "edge" measured against them is
//! an artefact rather than a signal.

use std::collections::HashMap;

use crate::sports::models::{MarketGroup, Sport};
use crate::sports::strategy::{
    DecisionGate, SportsDecision, SportsStrategy, SportsTickContext, TradingWindow,
};
use crate::strategy::{Param, ParamValue};
use crate::trader::TradeAction;

pub struct ModelEdge {
    /// Minimum (model probability - ask) required to enter.
    pub min_edge: f64,
    /// Never pay more than this for a leg.
    pub max_price: f64,
    /// Exit if the model's view deteriorates by this much versus entry.
    pub stop_edge: f64,
    // State
    entry: HashMap<usize, f64>,
}

impl Default for ModelEdge {
    fn default() -> Self {
        Self {
            min_edge: 0.05,
            max_price: 0.90,
            stop_edge: 0.08,
            entry: HashMap::new(),
        }
    }
}

impl SportsStrategy for ModelEdge {
    fn name(&self) -> &str {
        "ModelEdge"
    }

    fn version(&self) -> &str {
        "1"
    }

    fn windows(&self) -> Vec<TradingWindow> {
        vec![TradingWindow::InPlay]
    }

    fn requires_model(&self) -> bool {
        true
    }

    fn on_game_start(&mut self, _group: &MarketGroup, _sport: Sport) {
        self.entry.clear();
    }

    fn on_tick(&mut self, ctx: &SportsTickContext) -> SportsDecision {
        let probs = match ctx.model_probs() {
            Some(p) => p,
            None => return SportsDecision::hold(DecisionGate::UntrustedModel),
        };

        // Manage an open position first: if the model has turned against us,
        // get out rather than riding it to resolution.
        for (&leg, &entry_prob) in self.entry.clone().iter() {
            if ctx.position.leg(leg) <= 0.0 {
                continue;
            }
            let current = match probs.for_leg(leg) {
                Some(p) => p,
                None => continue,
            };
            if entry_prob - current >= self.stop_edge {
                let mid = ctx.leg_midpoint(leg).unwrap_or(0.0);
                if mid > 0.0 {
                    self.entry.remove(&leg);
                    return SportsDecision::trade(
                        TradeAction::sell(&leg_side(leg, false), ctx.position.leg(leg), mid),
                        leg,
                        current,
                        mid,
                    )
                    .with_note("model deteriorated past stop_edge");
                }
            }
        }

        if !self.windows().contains(&ctx.window(5.0)) {
            return SportsDecision::hold(DecisionGate::OutsideWindow);
        }

        if ctx.has_position() {
            return SportsDecision::hold(DecisionGate::AlreadyPositioned);
        }

        if ctx.available_cash <= 0.0 {
            return SportsDecision::hold(DecisionGate::ExposureLimit);
        }

        // Pick the leg with the largest edge against its *ask* — the price we
        // would actually pay. Measuring edge against the midpoint would count
        // half the spread as profit.
        let mut best: Option<(usize, f64, f64, f64)> = None;
        for (leg, _) in ctx.group.moneyline_legs() {
            let model_p = match probs.for_leg(leg) {
                Some(p) => p,
                None => continue,
            };
            let ask = match ctx.leg_best_ask(leg) {
                Some(a) => a,
                None => continue,
            };
            let mid = ctx.leg_midpoint(leg).unwrap_or(ask);
            let edge = model_p - ask;
            if best.map(|(_, _, _, e)| edge > e).unwrap_or(true) {
                best = Some((leg, model_p, mid, edge));
            }
        }

        let (leg, model_p, mid, edge) = match best {
            Some(b) => b,
            None => return SportsDecision::hold(DecisionGate::NoPrice),
        };

        if edge < self.min_edge {
            return SportsDecision::hold_with(DecisionGate::InsufficientEdge, leg, model_p, mid);
        }

        let ask = ctx.leg_best_ask(leg).unwrap_or(mid);
        if ask > self.max_price {
            return SportsDecision::hold_with(DecisionGate::PriceOutOfBand, leg, model_p, ask);
        }

        self.entry.insert(leg, model_p);
        SportsDecision::trade(
            TradeAction::buy(&leg_side(leg, true), ctx.available_cash, ask, Some(15.0)),
            leg,
            model_p,
            mid,
        )
        .with_note(format!("edge {edge:.3} vs ask {ask:.3}"))
    }

    fn on_trade_failed(&mut self, side: &str) {
        if let Some(leg) = crate::trader::Side::parse(side).and_then(|s| s.leg()) {
            self.entry.remove(&leg);
        }
    }

    fn param_defs(&self) -> HashMap<String, Param> {
        let mut m = HashMap::new();
        m.insert("min_edge".into(), Param::float(0.05, 0.02, 0.15));
        m.insert("max_price".into(), Param::float(0.90, 0.70, 0.95));
        m.insert("stop_edge".into(), Param::float(0.08, 0.04, 0.20));
        m
    }

    fn params(&self) -> HashMap<String, ParamValue> {
        let mut m = HashMap::new();
        m.insert("min_edge".into(), ParamValue::Float(self.min_edge));
        m.insert("max_price".into(), ParamValue::Float(self.max_price));
        m.insert("stop_edge".into(), ParamValue::Float(self.stop_edge));
        m
    }

    fn with_params(&self, params: &HashMap<String, ParamValue>) -> Box<dyn SportsStrategy> {
        let mut s = ModelEdge::default();
        for (k, v) in params {
            match k.as_str() {
                "min_edge" => {
                    if let Some(f) = v.as_f64() {
                        s.min_edge = f;
                    }
                }
                "max_price" => {
                    if let Some(f) = v.as_f64() {
                        s.max_price = f;
                    }
                }
                "stop_edge" => {
                    if let Some(f) = v.as_f64() {
                        s.stop_edge = f;
                    }
                }
                _ => {}
            }
        }
        Box::new(s)
    }
}

fn leg_side(leg: usize, buy: bool) -> String {
    let verb = if buy { "buy" } else { "sell" };
    match leg {
        0 => format!("{verb}_home"),
        1 => format!("{verb}_away"),
        2 => format!("{verb}_draw"),
        n => format!("{verb}:{n}"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sports::fair_value::{coherence, FairValue, FairValueSource, OutcomeProbs};
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

    fn sample(home_mid: f64) -> GameSample {
        GameSample {
            timestamp_utc: "2026-02-09T23:45:00Z".into(),
            elapsed_seconds: 900.0,
            state: GameState {
                home_score: 0,
                away_score: 0,
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
                    bid_levels: vec![(home_mid - 0.01, 500.0)],
                    ask_levels: vec![(home_mid + 0.01, 500.0)],
                },
                LegBook {
                    midpoint: Some(1.0 - home_mid),
                    best_bid: Some(0.99 - home_mid),
                    best_ask: Some(1.01 - home_mid),
                    bid_levels: vec![(0.99 - home_mid, 500.0)],
                    ask_levels: vec![(1.01 - home_mid, 500.0)],
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
        fv: FairValue,
    }

    fn env(home_mid: f64, model_home: f64, source: FairValueSource) -> Env {
        let s = sample(home_mid);
        let mids: Vec<Option<f64>> = s.books.iter().map(|b| b.midpoint).collect();
        let asks: Vec<Option<f64>> = s.books.iter().map(|b| b.best_ask).collect();
        Env {
            c: coherence::assess(&mids, &asks),
            s,
            g: group(),
            p: Position::new(0.0),
            fv: FairValue {
                probs: OutcomeProbs::two_way(model_home),
                source,
            },
        }
    }

    fn ctx<'a>(e: &'a Env, cash: f64) -> SportsTickContext<'a> {
        SportsTickContext {
            sample: &e.s,
            group: &e.g,
            sport: Sport::AmericanFootball,
            position: &e.p,
            available_cash: cash,
            elapsed_seconds: 900.0,
            remaining_minutes: Some(37.0),
            fair_value: Some(&e.fv),
            coherence: &e.c,
            stale_signal: None,
            under_review: false,
            feeds_agree: true,
        }
    }

    #[test]
    fn buys_when_the_model_beats_the_ask_by_the_threshold() {
        let mut s = ModelEdge::default();
        s.on_game_start(&group(), Sport::AmericanFootball);
        // Market ask 0.61, model 0.72 -> edge 0.11 > 0.05.
        let e = env(0.60, 0.72, FairValueSource::ModelFitted);
        let d = s.on_tick(&ctx(&e, 100.0));
        assert_eq!(d.gate, DecisionGate::Traded);
        assert_eq!(d.action.side, "buy_home");
    }

    #[test]
    fn edge_is_measured_against_the_ask_not_the_midpoint() {
        let mut s = ModelEdge::default();
        s.min_edge = 0.05;
        s.on_game_start(&group(), Sport::AmericanFootball);

        // Model 0.655, mid 0.60, ask 0.61.
        // Against the mid the edge is 0.055 (would trade);
        // against the ask it is 0.045 (must not).
        let e = env(0.60, 0.655, FairValueSource::ModelFitted);
        let d = s.on_tick(&ctx(&e, 100.0));
        assert_eq!(
            d.gate,
            DecisionGate::InsufficientEdge,
            "counting half the spread as profit is exactly the mistake to avoid"
        );
    }

    #[test]
    fn does_not_trade_on_fallback_parameters() {
        let mut s = ModelEdge::default();
        s.on_game_start(&group(), Sport::AmericanFootball);
        // Huge apparent edge, but the model has no team information.
        let e = env(0.40, 0.85, FairValueSource::ModelFallback);
        // The common gates catch this in the simulator; the strategy also has
        // to declare it needs a model.
        assert!(s.requires_model());
        let gate = crate::sports::strategy::common_gates(&ctx(&e, 100.0), s.requires_model());
        assert_eq!(gate, Some(DecisionGate::UntrustedModel));
    }

    #[test]
    fn refuses_prices_above_the_cap_even_with_edge() {
        let mut s = ModelEdge::default();
        s.max_price = 0.80;
        s.on_game_start(&group(), Sport::AmericanFootball);
        let e = env(0.90, 0.99, FairValueSource::ModelFitted);
        let d = s.on_tick(&ctx(&e, 100.0));
        assert_eq!(d.gate, DecisionGate::PriceOutOfBand);
    }

    #[test]
    fn picks_the_leg_with_the_larger_edge() {
        let mut s = ModelEdge::default();
        s.on_game_start(&group(), Sport::AmericanFootball);
        // Home mid 0.70 / ask 0.71, away mid 0.30 / ask 0.31.
        // Model says home 0.60 (edge -0.11), away 0.40 (edge +0.09).
        let mut e = env(0.70, 0.60, FairValueSource::ModelFitted);
        e.fv.probs = OutcomeProbs {
            home: 0.60,
            away: 0.40,
            draw: 0.0,
        };
        let d = s.on_tick(&ctx(&e, 100.0));
        assert_eq!(d.gate, DecisionGate::Traded);
        assert_eq!(d.action.side, "buy_away");
    }

    #[test]
    fn holds_when_no_leg_has_enough_edge() {
        let mut s = ModelEdge::default();
        s.on_game_start(&group(), Sport::AmericanFootball);
        let e = env(0.60, 0.60, FairValueSource::ModelFitted);
        let d = s.on_tick(&ctx(&e, 100.0));
        assert_eq!(d.gate, DecisionGate::InsufficientEdge);
    }

    #[test]
    fn exits_when_the_model_turns_against_the_position() {
        let mut s = ModelEdge::default();
        s.on_game_start(&group(), Sport::AmericanFootball);

        let e = env(0.60, 0.72, FairValueSource::ModelFitted);
        assert_eq!(s.on_tick(&ctx(&e, 100.0)).gate, DecisionGate::Traded);

        // Now hold the position, and the model collapses to 0.60.
        let mut e2 = env(0.60, 0.60, FairValueSource::ModelFitted);
        e2.p.set_leg(0, 100.0);
        let d = s.on_tick(&ctx(&e2, 0.0));
        assert_eq!(d.gate, DecisionGate::Traded);
        assert_eq!(d.action.side, "sell_home");
    }

    #[test]
    fn does_not_exit_on_a_small_model_wobble() {
        let mut s = ModelEdge::default();
        s.on_game_start(&group(), Sport::AmericanFootball);
        let e = env(0.60, 0.72, FairValueSource::ModelFitted);
        s.on_tick(&ctx(&e, 100.0));

        // Model slips only 0.03, below stop_edge = 0.08.
        let mut e2 = env(0.60, 0.69, FairValueSource::ModelFitted);
        e2.p.set_leg(0, 100.0);
        let d = s.on_tick(&ctx(&e2, 0.0));
        assert!(!d.action.side.starts_with("sell"), "got {}", d.action.side);
    }

    #[test]
    fn stays_within_the_parameter_budget() {
        assert_eq!(ModelEdge::default().tunable_count(), 3);
    }
}
