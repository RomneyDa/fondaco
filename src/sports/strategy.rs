//! Sports strategy trait and shared helpers.
//!
//! Mirrors `strategy::Strategy` but with a sports tick context: a game state, a
//! fair-value estimate, and the layer-0 signals. Trade actions reuse
//! `TradeAction` — since sides now parse to leg indices, `"buy_home"` routes
//! exactly as `"buy_up"` does.
//!
//! Two deliberate differences from the crypto trait:
//!
//! 1. **Trading windows are explicit.** Crypto has a single `min_elapsed` /
//!    `no_new_after` fraction. Sports edge is concentrated in distinct phases
//!    (pre-game news, immediately post-event, endgame), so a strategy declares
//!    which windows it wants.
//! 2. **Every considered decision is logged.** Sports makes few decisions per
//!    game, so unlike crypto at 18 Hz we can afford to record the gate that
//!    blocked each one. That trace is the most useful debugging artefact the
//!    sports path produces.

use std::collections::HashMap;

use serde::{Deserialize, Serialize};

use crate::sports::fair_value::{FairValue, OutcomeProbs};
use crate::sports::fair_value::coherence::CoherenceReport;
use crate::sports::fair_value::staleness::StaleSignal;
use crate::sports::models::{GameSample, MarketGroup, Sport};
use crate::strategy::{Param, ParamValue};
use crate::trader::{Position, TradeAction};

/// Phase of the game a strategy is willing to act in.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TradingWindow {
    PreGame,
    InPlay,
    /// Final minutes, where probabilities move fastest.
    EndGame,
}

/// Why a strategy did not trade. Recorded for every considered decision.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DecisionGate {
    Traded,
    /// Outside the strategy's declared trading windows.
    OutsideWindow,
    /// The fair value came from league-average params, so it has no opinion
    /// about who is better.
    UntrustedModel,
    /// Game state or book is stale.
    StaleData,
    /// Edge below the strategy's threshold.
    InsufficientEdge,
    /// Price outside the acceptable band.
    PriceOutOfBand,
    /// A position is already open.
    AlreadyPositioned,
    /// Play or goal under official review.
    UnderReview,
    /// Blocked by the portfolio allocator.
    ExposureLimit,
    /// Feeds disagree about the score.
    FeedDisagreement,
    /// Book has no usable price.
    NoPrice,
    Other,
}

impl DecisionGate {
    pub fn as_str(&self) -> &'static str {
        match self {
            DecisionGate::Traded => "traded",
            DecisionGate::OutsideWindow => "outside_window",
            DecisionGate::UntrustedModel => "untrusted_model",
            DecisionGate::StaleData => "stale_data",
            DecisionGate::InsufficientEdge => "insufficient_edge",
            DecisionGate::PriceOutOfBand => "price_out_of_band",
            DecisionGate::AlreadyPositioned => "already_positioned",
            DecisionGate::UnderReview => "under_review",
            DecisionGate::ExposureLimit => "exposure_limit",
            DecisionGate::FeedDisagreement => "feed_disagreement",
            DecisionGate::NoPrice => "no_price",
            DecisionGate::Other => "other",
        }
    }
}

/// One decision, whether or not it resulted in a trade.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct DecisionRecord {
    pub timestamp_utc: String,
    pub strategy: String,
    pub elapsed_seconds: f64,
    pub gate: DecisionGate,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub leg: Option<usize>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub model_prob: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub market_mid: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub edge: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub note: Option<String>,
}

/// What a strategy sees each tick.
pub struct SportsTickContext<'a> {
    pub sample: &'a GameSample,
    pub group: &'a MarketGroup,
    pub sport: Sport,
    pub position: &'a Position,
    /// Cash the allocator has made available for this game.
    pub available_cash: f64,
    pub elapsed_seconds: f64,
    /// Minutes of regulation remaining, when the clock is interpretable.
    pub remaining_minutes: Option<f64>,
    /// Model fair value, when available.
    pub fair_value: Option<&'a FairValue>,
    /// Cross-leg coherence of the current book.
    pub coherence: &'a CoherenceReport,
    /// Set when the book has not yet reacted to a scoring event.
    pub stale_signal: Option<&'a StaleSignal>,
    /// True while a play or goal is under official review — trading must be
    /// suspended, since the state may be retracted.
    pub under_review: bool,
    /// True when the primary and secondary feeds report the same score.
    pub feeds_agree: bool,
}

impl SportsTickContext<'_> {
    pub fn leg_midpoint(&self, leg: usize) -> Option<f64> {
        self.sample.leg_midpoint(leg)
    }

    pub fn leg_best_ask(&self, leg: usize) -> Option<f64> {
        self.sample.books.get(leg).and_then(|b| b.best_ask)
    }

    pub fn model_probs(&self) -> Option<OutcomeProbs> {
        self.fair_value.map(|f| f.probs)
    }

    /// Current trading window.
    pub fn window(&self, endgame_minutes: f64) -> TradingWindow {
        match self.remaining_minutes {
            None => TradingWindow::PreGame,
            Some(r) if r <= endgame_minutes => TradingWindow::EndGame,
            Some(_) => TradingWindow::InPlay,
        }
    }

    /// Whether any leg is held.
    pub fn has_position(&self) -> bool {
        crate::strategy::held_leg(self.position).is_some()
    }
}

/// Result of a strategy tick: an action plus why.
pub struct SportsDecision {
    pub action: TradeAction,
    pub gate: DecisionGate,
    pub leg: Option<usize>,
    pub model_prob: Option<f64>,
    pub market_mid: Option<f64>,
    pub edge: Option<f64>,
    pub note: Option<String>,
}

impl SportsDecision {
    pub fn hold(gate: DecisionGate) -> Self {
        Self {
            action: crate::trader::hold(),
            gate,
            leg: None,
            model_prob: None,
            market_mid: None,
            edge: None,
            note: None,
        }
    }

    pub fn hold_with(gate: DecisionGate, leg: usize, model: f64, mid: f64) -> Self {
        Self {
            action: crate::trader::hold(),
            gate,
            leg: Some(leg),
            model_prob: Some(model),
            market_mid: Some(mid),
            edge: Some(model - mid),
            note: None,
        }
    }

    pub fn trade(action: TradeAction, leg: usize, model: f64, mid: f64) -> Self {
        Self {
            action,
            gate: DecisionGate::Traded,
            leg: Some(leg),
            model_prob: Some(model),
            market_mid: Some(mid),
            edge: Some(model - mid),
            note: None,
        }
    }

    pub fn with_note(mut self, note: impl Into<String>) -> Self {
        self.note = Some(note.into());
        self
    }

    pub fn to_record(&self, strategy: &str, elapsed_seconds: f64, timestamp_utc: &str) -> DecisionRecord {
        DecisionRecord {
            timestamp_utc: timestamp_utc.to_string(),
            strategy: strategy.to_string(),
            elapsed_seconds,
            gate: self.gate.clone(),
            leg: self.leg,
            model_prob: self.model_prob,
            market_mid: self.market_mid,
            edge: self.edge,
            note: self.note.clone(),
        }
    }
}

/// Core sports strategy trait.
pub trait SportsStrategy: Send {
    fn name(&self) -> &str;
    fn version(&self) -> &str;

    /// Windows this strategy is willing to trade in.
    fn windows(&self) -> Vec<TradingWindow> {
        vec![TradingWindow::InPlay]
    }

    /// Whether this strategy needs a trustworthy (team-fitted) model. A
    /// strategy that trades purely on layer-0 signals does not.
    fn requires_model(&self) -> bool {
        true
    }

    fn on_game_start(&mut self, group: &MarketGroup, sport: Sport);
    fn on_tick(&mut self, ctx: &SportsTickContext) -> SportsDecision;

    fn on_trade_failed(&mut self, _side: &str) {}

    fn param_defs(&self) -> HashMap<String, Param> {
        HashMap::new()
    }
    fn params(&self) -> HashMap<String, ParamValue> {
        HashMap::new()
    }
    fn with_params(&self, params: &HashMap<String, ParamValue>) -> Box<dyn SportsStrategy>;

    /// Number of tunable parameters. Used to warn when a sweep is hopeless
    /// against the available game count.
    fn tunable_count(&self) -> usize {
        self.param_defs().values().filter(|p| p.tunable).count()
    }
}

/// Shared entry gate applied before any strategy-specific logic.
///
/// Centralised because every one of these is a way to lose money that has
/// nothing to do with the strategy's idea.
pub fn common_gates(ctx: &SportsTickContext, requires_model: bool) -> Option<DecisionGate> {
    if ctx.under_review {
        // A disallowed goal or a reversed touchdown means the state we would be
        // trading on may be retracted within seconds.
        return Some(DecisionGate::UnderReview);
    }
    if !ctx.feeds_agree {
        return Some(DecisionGate::FeedDisagreement);
    }
    if requires_model {
        match ctx.fair_value {
            None => return Some(DecisionGate::UntrustedModel),
            Some(fv) if !fv.source.is_tradeable() => {
                return Some(DecisionGate::UntrustedModel)
            }
            _ => {}
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sports::fair_value::{coherence, FairValueSource};
    use crate::sports::models::{GameState, LegBook, SportsLeg, SportsMarketKind};

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

    fn sample() -> GameSample {
        GameSample {
            timestamp_utc: "2026-02-09T23:45:00Z".into(),
            elapsed_seconds: 900.0,
            state: GameState::scheduled(),
            books: vec![
                LegBook {
                    midpoint: Some(0.60),
                    best_ask: Some(0.61),
                    ..Default::default()
                },
                LegBook {
                    midpoint: Some(0.40),
                    best_ask: Some(0.41),
                    ..Default::default()
                },
            ],
            model_probs: vec![],
            reference_home_prob: None,
        }
    }

    struct Ctx {
        sample: GameSample,
        group: MarketGroup,
        position: Position,
        coherence: CoherenceReport,
        fv: FairValue,
    }

    fn build(source: FairValueSource) -> Ctx {
        Ctx {
            sample: sample(),
            group: group(),
            position: Position::new(100.0),
            coherence: coherence::assess(&[Some(0.60), Some(0.40)], &[Some(0.61), Some(0.41)]),
            fv: FairValue {
                probs: OutcomeProbs::two_way(0.68),
                source,
            },
        }
    }

    fn ctx<'a>(c: &'a Ctx, under_review: bool, feeds_agree: bool, with_model: bool) -> SportsTickContext<'a> {
        SportsTickContext {
            sample: &c.sample,
            group: &c.group,
            sport: Sport::AmericanFootball,
            position: &c.position,
            available_cash: 100.0,
            elapsed_seconds: 900.0,
            remaining_minutes: Some(30.0),
            fair_value: if with_model { Some(&c.fv) } else { None },
            coherence: &c.coherence,
            stale_signal: None,
            under_review,
            feeds_agree,
        }
    }

    #[test]
    fn a_play_under_review_blocks_everything() {
        let c = build(FairValueSource::ModelFitted);
        let gate = common_gates(&ctx(&c, true, true, true), true);
        assert_eq!(gate, Some(DecisionGate::UnderReview));
    }

    #[test]
    fn disagreeing_feeds_block_trading() {
        let c = build(FairValueSource::ModelFitted);
        let gate = common_gates(&ctx(&c, false, false, true), true);
        assert_eq!(gate, Some(DecisionGate::FeedDisagreement));
    }

    #[test]
    fn a_fallback_model_is_not_tradeable_when_a_model_is_required() {
        let c = build(FairValueSource::ModelFallback);
        let gate = common_gates(&ctx(&c, false, true, true), true);
        assert_eq!(
            gate,
            Some(DecisionGate::UntrustedModel),
            "league-average params have no opinion about who is better"
        );
    }

    #[test]
    fn a_layer_zero_strategy_may_trade_without_a_model() {
        let c = build(FairValueSource::ModelFallback);
        // requires_model = false -> the model gate does not apply.
        assert_eq!(common_gates(&ctx(&c, false, true, false), false), None);
    }

    #[test]
    fn a_fitted_model_passes_the_common_gates() {
        let c = build(FairValueSource::ModelFitted);
        assert_eq!(common_gates(&ctx(&c, false, true, true), true), None);
    }

    #[test]
    fn windows_are_derived_from_remaining_minutes() {
        let c = build(FairValueSource::ModelFitted);
        let mut t = ctx(&c, false, true, true);

        t.remaining_minutes = None;
        assert_eq!(t.window(5.0), TradingWindow::PreGame);

        t.remaining_minutes = Some(30.0);
        assert_eq!(t.window(5.0), TradingWindow::InPlay);

        t.remaining_minutes = Some(3.0);
        assert_eq!(t.window(5.0), TradingWindow::EndGame);
    }

    #[test]
    fn decision_records_capture_the_blocking_gate_and_the_numbers() {
        let d = SportsDecision::hold_with(DecisionGate::InsufficientEdge, 0, 0.62, 0.60);
        let rec = d.to_record("Test", 900.0, "2026-02-09T23:45:00Z");
        assert_eq!(rec.gate, DecisionGate::InsufficientEdge);
        assert_eq!(rec.leg, Some(0));
        assert!((rec.edge.unwrap() - 0.02).abs() < 1e-9);
    }

    #[test]
    fn a_traded_decision_carries_the_action() {
        let action = TradeAction::buy("buy_home", 50.0, 0.61, Some(10.0));
        let d = SportsDecision::trade(action, 0, 0.68, 0.60);
        assert_eq!(d.gate, DecisionGate::Traded);
        assert_eq!(d.action.leg(), Some(0));
        assert!((d.edge.unwrap() - 0.08).abs() < 1e-9);
    }

    #[test]
    fn sports_sides_route_to_legs_exactly_as_crypto_sides_do() {
        assert_eq!(
            TradeAction::buy("buy_home", 1.0, 0.5, None).leg(),
            TradeAction::buy("buy_up", 1.0, 0.5, None).leg()
        );
        assert_eq!(TradeAction::buy("buy_draw", 1.0, 0.5, None).leg(), Some(2));
    }
}
