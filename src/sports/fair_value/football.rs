//! In-play American football win probability.
//!
//! **ESPN is the primary source for NFL** — it publishes a free,
//! unauthenticated, nflfastR-class live win probability, and nothing built here
//! quickly would beat it. This module exists as a fallback for when ESPN is
//! unavailable and as an independent check on it, not as the main signal.
//!
//! The model is deliberately simple and honest about that: final margin is
//! treated as normally distributed around the current margin plus the expected
//! remaining scoring differential, with variance shrinking as the clock runs
//! down. That captures the two things that dominate in-game win probability —
//! margin and time — but knows nothing about down, distance, field position or
//! possession, which is exactly why ESPN's model is better.

use super::{GameParams, OutcomeProbs};

/// Standard deviation of an NFL final margin, in points, over a full game.
/// Roughly the historical value; the model scales it by remaining time.
const FULL_GAME_MARGIN_SD: f64 = 13.5;

/// A tie is possible but rare. Treating the margin as continuous would assign
/// it zero probability, so a narrow band around zero is counted as a tie.
const TIE_BAND: f64 = 0.5;

/// Standard normal CDF via the error function.
fn norm_cdf(z: f64) -> f64 {
    0.5 * (1.0 + erf(z / std::f64::consts::SQRT_2))
}

/// Abramowitz & Stegun 7.1.26 — accurate to ~1.5e-7, far beyond what any
/// trading decision here resolves.
fn erf(x: f64) -> f64 {
    let sign = if x < 0.0 { -1.0 } else { 1.0 };
    let x = x.abs();

    let a1 = 0.254829592;
    let a2 = -0.284496736;
    let a3 = 1.421413741;
    let a4 = -1.453152027;
    let a5 = 1.061405429;
    let p = 0.3275911;

    let t = 1.0 / (1.0 + p * x);
    let y = 1.0 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * (-x * x).exp();
    sign * y
}

/// In-play win/tie/loss probabilities from margin and time remaining.
pub fn in_play_probs(
    params: &GameParams,
    home_score: i32,
    away_score: i32,
    remaining_minutes: f64,
) -> OutcomeProbs {
    let remaining = remaining_minutes.max(0.0);
    let margin = (home_score - away_score) as f64;

    if remaining <= 0.0 {
        return match home_score.cmp(&away_score) {
            std::cmp::Ordering::Greater => OutcomeProbs {
                home: 1.0,
                away: 0.0,
                draw: 0.0,
            },
            std::cmp::Ordering::Less => OutcomeProbs {
                home: 0.0,
                away: 1.0,
                draw: 0.0,
            },
            // Regulation ties go to overtime, which is close to a coin flip.
            std::cmp::Ordering::Equal => OutcomeProbs {
                home: 0.5,
                away: 0.5,
                draw: 0.0,
            },
        };
    }

    let fraction = (remaining / 60.0).clamp(0.0, 1.5);

    // Expected additional scoring differential over the remaining time.
    let expected_delta = (params.home_rate - params.away_rate) * fraction;
    let mean = margin + expected_delta;

    // Uncertainty scales with the square root of remaining time, as for any
    // random walk.
    let sd = (FULL_GAME_MARGIN_SD * fraction.sqrt()).max(0.5);

    // P(final margin > 0), with a narrow band counted as a tie.
    let p_away = norm_cdf((-TIE_BAND - mean) / sd);
    let p_home = 1.0 - norm_cdf((TIE_BAND - mean) / sd);
    let p_tie = (1.0 - p_home - p_away).max(0.0);

    // A regulation tie is resolved in overtime; split it evenly rather than
    // reporting a draw leg that the NFL moneyline does not offer.
    OutcomeProbs {
        home: p_home + p_tie / 2.0,
        away: p_away + p_tie / 2.0,
        draw: 0.0,
    }
    .normalized()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sports::models::Sport;

    fn params() -> GameParams {
        GameParams::league_average(Sport::AmericanFootball)
    }

    #[test]
    fn probabilities_sum_to_one() {
        for &(h, a, rem) in &[(0, 0, 60.0), (7, 0, 45.0), (0, 21, 12.0), (24, 24, 1.0)] {
            let p = in_play_probs(&params(), h, a, rem);
            assert!((p.sum() - 1.0).abs() < 1e-6, "{h}-{a} @ {rem}");
            assert_eq!(p.draw, 0.0, "the NFL moneyline has no draw leg");
        }
    }

    #[test]
    fn kickoff_favours_the_home_side_modestly() {
        let p = in_play_probs(&params(), 0, 0, 60.0);
        assert!(
            p.home > 0.5 && p.home < 0.62,
            "home win prob at kickoff was {}, outside a plausible band",
            p.home
        );
    }

    #[test]
    fn a_lead_strengthens_as_time_runs_out() {
        let early = in_play_probs(&params(), 7, 0, 50.0);
        let mid = in_play_probs(&params(), 7, 0, 25.0);
        let late = in_play_probs(&params(), 7, 0, 2.0);
        assert!(
            early.home < mid.home && mid.home < late.home,
            "{} -> {} -> {}",
            early.home,
            mid.home,
            late.home
        );
        assert!(late.home > 0.9);
    }

    #[test]
    fn a_three_score_deficit_late_is_nearly_hopeless() {
        let p = in_play_probs(&params(), 0, 21, 2.0);
        assert!(p.home < 0.01, "home win prob was {}", p.home);
    }

    #[test]
    fn a_tied_game_at_the_whistle_is_a_coin_flip() {
        let p = in_play_probs(&params(), 21, 21, 0.0);
        assert!((p.home - 0.5).abs() < 1e-9);
        assert!((p.away - 0.5).abs() < 1e-9);
    }

    #[test]
    fn a_finished_game_collapses_to_the_scoreline() {
        let p = in_play_probs(&params(), 24, 21, 0.0);
        assert_eq!(p.home, 1.0);
        let p = in_play_probs(&params(), 21, 24, 0.0);
        assert_eq!(p.away, 1.0);
    }

    #[test]
    fn a_tied_game_late_is_close_to_even() {
        let p = in_play_probs(&params(), 17, 17, 1.0);
        assert!(
            (p.home - 0.5).abs() < 0.1,
            "tied with a minute left: home {}",
            p.home
        );
    }

    #[test]
    fn erf_matches_known_values() {
        // Abramowitz & Stegun 7.1.26 is accurate to ~1.5e-7, so that is the
        // tolerance — it does not return exactly 0 at the origin.
        assert!(erf(0.0).abs() < 1.5e-7);
        assert!((erf(1.0) - 0.842_700_79).abs() < 1e-6);
        assert!((erf(-1.0) + 0.842_700_79).abs() < 1e-6);
    }

    #[test]
    fn norm_cdf_is_a_valid_cdf() {
        assert!((norm_cdf(0.0) - 0.5).abs() < 1e-7);
        assert!((norm_cdf(1.96) - 0.975).abs() < 1e-3);
        assert!(norm_cdf(-5.0) < 1e-6);
        assert!(norm_cdf(5.0) > 1.0 - 1e-6);
        // Monotonic.
        let mut prev = 0.0;
        for i in -40..=40 {
            let v = norm_cdf(i as f64 / 10.0);
            assert!(v >= prev, "cdf decreased at {i}");
            prev = v;
        }
    }

    #[test]
    fn stronger_teams_get_higher_probabilities() {
        let mut strong_home = params();
        strong_home.home_rate = 31.0;
        strong_home.away_rate = 17.0;

        let base = in_play_probs(&params(), 0, 0, 60.0);
        let favoured = in_play_probs(&strong_home, 0, 0, 60.0);
        assert!(favoured.home > base.home + 0.05, "{} vs {}", favoured.home, base.home);
    }
}
