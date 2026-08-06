//! Fair value estimation.
//!
//! Three layers, in increasing cost. **Layer 0 alone may be the whole
//! business** — it needs no model at all.
//!
//! - **Layer 0** (`staleness`, `coherence`): the book disagreeing with itself
//!   or with a score that changed eight seconds ago. Zero modelling.
//! - **Layer 1** (`espn`): ESPN's free live NFL win probability.
//! - **Layer 2** (`soccer`, `football`): our own in-play model.
//!
//! The layer-2 split is deliberate and is the key design decision:
//! **Python fits, Rust evaluates.** The model server is asked once per game
//! for a *parameterisation* (goal rates, not a probability); Rust then
//! evaluates the in-play probability locally from (rates, score, minutes
//! remaining) in microseconds on every tick. An HTTP round trip per tick would
//! be both too slow and pointlessly chatty.

pub mod coherence;
pub mod football;
pub mod soccer;
pub mod staleness;

use serde::{Deserialize, Serialize};

use super::models::{GameState, Sport};

/// Probabilities for the moneyline legs, indexed home / away / draw.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct OutcomeProbs {
    pub home: f64,
    pub away: f64,
    pub draw: f64,
}

impl OutcomeProbs {
    pub fn two_way(home: f64) -> Self {
        Self {
            home,
            away: 1.0 - home,
            draw: 0.0,
        }
    }

    pub fn sum(&self) -> f64 {
        self.home + self.away + self.draw
    }

    /// Renormalise so the legs sum to 1.
    pub fn normalized(self) -> Self {
        let s = self.sum();
        if s <= 0.0 {
            return Self {
                home: 1.0 / 3.0,
                away: 1.0 / 3.0,
                draw: 1.0 / 3.0,
            };
        }
        Self {
            home: self.home / s,
            away: self.away / s,
            draw: self.draw / s,
        }
    }

    /// Probability for a canonical leg index (0=home, 1=away, 2=draw).
    pub fn for_leg(&self, leg: usize) -> Option<f64> {
        match leg {
            0 => Some(self.home),
            1 => Some(self.away),
            2 => Some(self.draw),
            _ => None,
        }
    }

    pub fn as_vec(&self, has_draw: bool) -> Vec<f64> {
        if has_draw {
            vec![self.home, self.away, self.draw]
        } else {
            vec![self.home, self.away]
        }
    }
}

/// Pre-match parameterisation for one game, fetched once from the model server.
///
/// This is what makes the "Python fits, Rust evaluates" split work: the server
/// returns *rates*, and every in-play evaluation is then a local closed-form
/// computation.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct GameParams {
    /// Expected goals/points for the home side over a full match.
    pub home_rate: f64,
    pub away_rate: f64,
    /// Dixon-Coles low-score correlation parameter. Soccer only; 0 reduces the
    /// model to independent Poisson.
    #[serde(default)]
    pub rho: f64,
    /// Scoring-rate multiplier applied per red card against a side.
    #[serde(default = "default_red_card_factor")]
    pub red_card_factor: f64,
}

fn default_red_card_factor() -> f64 {
    // A red card is worth roughly a 25% cut to the offending side's scoring
    // rate and a corresponding boost to the opponent's. Refit from data before
    // relying on it.
    0.75
}

impl GameParams {
    /// Neutral fallback when the model server is unreachable.
    ///
    /// These are league-average rates, not team-specific: using them means the
    /// model has no opinion about *who* is better, only about the scoreline and
    /// the clock. That is a real degradation and callers should treat a
    /// fallback-derived probability with much less confidence.
    pub fn league_average(sport: Sport) -> Self {
        match sport {
            Sport::Soccer => Self {
                home_rate: 1.50,
                away_rate: 1.20,
                rho: -0.05,
                red_card_factor: default_red_card_factor(),
            },
            Sport::AmericanFootball => Self {
                home_rate: 24.0,
                away_rate: 21.5,
                rho: 0.0,
                red_card_factor: 1.0,
            },
        }
    }
}

/// A fair-value estimate with provenance, so downstream code can weight it.
#[derive(Debug, Clone, PartialEq)]
pub struct FairValue {
    pub probs: OutcomeProbs,
    pub source: FairValueSource,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FairValueSource {
    /// ESPN's published live win probability.
    Reference,
    /// Our own model with team-specific fitted rates.
    ModelFitted,
    /// Our own model with league-average rates (no team information).
    ModelFallback,
}

impl FairValueSource {
    pub fn as_str(&self) -> &'static str {
        match self {
            FairValueSource::Reference => "reference",
            FairValueSource::ModelFitted => "model_fitted",
            FairValueSource::ModelFallback => "model_fallback",
        }
    }

    /// Whether this estimate is trustworthy enough to size a position from.
    pub fn is_tradeable(&self) -> bool {
        !matches!(self, FairValueSource::ModelFallback)
    }
}

/// Evaluate in-play fair value for a game.
pub fn evaluate(
    sport: Sport,
    params: &GameParams,
    state: &GameState,
    fitted: bool,
) -> Option<FairValue> {
    let remaining = state.remaining_minutes(sport)?;
    let probs = match sport {
        Sport::Soccer => soccer::in_play_probs(
            params,
            state.home_score,
            state.away_score,
            remaining,
            state.home_red_cards,
            state.away_red_cards,
        ),
        Sport::AmericanFootball => football::in_play_probs(
            params,
            state.home_score,
            state.away_score,
            remaining,
        ),
    };
    Some(FairValue {
        probs,
        source: if fitted {
            FairValueSource::ModelFitted
        } else {
            FairValueSource::ModelFallback
        },
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn two_way_probs_sum_to_one() {
        let p = OutcomeProbs::two_way(0.62);
        assert!((p.sum() - 1.0).abs() < 1e-9);
        assert_eq!(p.draw, 0.0);
    }

    #[test]
    fn normalization_fixes_an_unnormalised_triple() {
        let p = OutcomeProbs {
            home: 2.0,
            away: 1.0,
            draw: 1.0,
        }
        .normalized();
        assert!((p.sum() - 1.0).abs() < 1e-9);
        assert!((p.home - 0.5).abs() < 1e-9);
    }

    #[test]
    fn normalizing_a_degenerate_triple_yields_a_uniform_prior() {
        let p = OutcomeProbs {
            home: 0.0,
            away: 0.0,
            draw: 0.0,
        }
        .normalized();
        assert!((p.sum() - 1.0).abs() < 1e-9);
        assert!((p.home - 1.0 / 3.0).abs() < 1e-9);
    }

    #[test]
    fn leg_indices_follow_the_canonical_order() {
        let p = OutcomeProbs {
            home: 0.5,
            away: 0.3,
            draw: 0.2,
        };
        assert_eq!(p.for_leg(0), Some(0.5));
        assert_eq!(p.for_leg(1), Some(0.3));
        assert_eq!(p.for_leg(2), Some(0.2));
        assert_eq!(p.for_leg(3), None);
    }

    #[test]
    fn fallback_params_are_marked_untradeable() {
        assert!(!FairValueSource::ModelFallback.is_tradeable());
        assert!(FairValueSource::ModelFitted.is_tradeable());
        assert!(FairValueSource::Reference.is_tradeable());
    }

    #[test]
    fn evaluate_requires_a_known_clock() {
        let params = GameParams::league_average(Sport::Soccer);
        let mut state = GameState::scheduled();
        // elapsed_minutes is None -> no remaining time -> no estimate.
        assert!(evaluate(Sport::Soccer, &params, &state, true).is_none());

        state.elapsed_minutes = Some(45.0);
        assert!(evaluate(Sport::Soccer, &params, &state, true).is_some());
    }
}
