//! In-play soccer win/draw/loss from a bivariate Poisson model.
//!
//! Given team scoring rates for a full match, the current scoreline, and the
//! minutes remaining, the probability of each final outcome is closed-form:
//! scale the rates to the remaining time, enumerate the joint distribution of
//! *additional* goals, and sum the mass over each final-margin region.
//!
//! Rates come from a Dixon–Coles fit performed offline in Python (see
//! `model-server/`), which is where the team-strength estimation belongs. This
//! module is only the in-play evaluation, which must be fast enough to run on
//! every tick.
//!
//! The Dixon–Coles low-score correction (`rho`) adjusts the four cells
//! 0-0, 0-1, 1-0 and 1-1, where independent Poisson is known to misprice.
//! Applying it to *remaining* goals is an approximation — the correction was
//! estimated for full-match scores — so it is only applied when a meaningful
//! amount of the match is left.

use super::{GameParams, OutcomeProbs};

/// Maximum additional goals per side considered. At realistic rates the tail
/// beyond this is far below the precision anyone trades on.
const MAX_GOALS: usize = 12;

/// Below this many minutes remaining the Dixon–Coles correction is dropped:
/// it was fitted on full matches, and applying it to a two-minute residual is
/// outside the range where it means anything.
const RHO_MIN_REMAINING_MINUTES: f64 = 15.0;

/// Dixon–Coles tau correction for low-scoring cells.
fn tau(x: usize, y: usize, lambda: f64, mu: f64, rho: f64) -> f64 {
    match (x, y) {
        (0, 0) => 1.0 - lambda * mu * rho,
        (0, 1) => 1.0 + lambda * rho,
        (1, 0) => 1.0 + mu * rho,
        (1, 1) => 1.0 - rho,
        _ => 1.0,
    }
}

/// Poisson pmf, computed iteratively to avoid factorial overflow.
fn poisson_pmf(lambda: f64, k: usize) -> f64 {
    if lambda <= 0.0 {
        return if k == 0 { 1.0 } else { 0.0 };
    }
    let mut p = (-lambda).exp();
    for i in 1..=k {
        p *= lambda / i as f64;
    }
    p
}

/// Effective scoring rates after red-card adjustment.
///
/// A team down a man scores less and concedes more. Both directions are
/// applied so a red card moves the market the way it actually does.
fn adjusted_rates(params: &GameParams, home_reds: i32, away_reds: i32) -> (f64, f64) {
    let f = params.red_card_factor.clamp(0.1, 1.0);
    // Cap the exponent: beyond a couple of dismissals the linear-in-log model
    // stops meaning anything, and an 11-red-card input should not produce a
    // zero rate.
    let home_penalty = f.powi(home_reds.clamp(0, 3));
    let away_penalty = f.powi(away_reds.clamp(0, 3));

    // Own rate falls with own cards; the opponent's rate rises by the same
    // factor, so a red card moves the market in both directions as it should.
    let home = params.home_rate * home_penalty / away_penalty;
    let away = params.away_rate * away_penalty / home_penalty;
    (home.max(0.0), away.max(0.0))
}

/// In-play win/draw/loss probabilities.
///
/// `remaining_minutes` of 0 means the match is over and the current scoreline
/// is final.
pub fn in_play_probs(
    params: &GameParams,
    home_score: i32,
    away_score: i32,
    remaining_minutes: f64,
    home_reds: i32,
    away_reds: i32,
) -> OutcomeProbs {
    let remaining = remaining_minutes.max(0.0);

    // Full time: the scoreline is the outcome.
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
            std::cmp::Ordering::Equal => OutcomeProbs {
                home: 0.0,
                away: 0.0,
                draw: 1.0,
            },
        };
    }

    let (home_rate, away_rate) = adjusted_rates(params, home_reds, away_reds);
    let fraction = (remaining / 90.0).clamp(0.0, 1.5);
    let lambda = (home_rate * fraction).max(0.0);
    let mu = (away_rate * fraction).max(0.0);

    let use_rho = remaining >= RHO_MIN_REMAINING_MINUTES && params.rho != 0.0;
    let rho = if use_rho { params.rho } else { 0.0 };

    let current_diff = home_score - away_score;

    let mut p_home = 0.0;
    let mut p_away = 0.0;
    let mut p_draw = 0.0;
    let mut total = 0.0;

    for x in 0..=MAX_GOALS {
        let px = poisson_pmf(lambda, x);
        if px <= 0.0 && x > 0 {
            // Rates are small enough that the remaining tail is negligible.
            break;
        }
        for y in 0..=MAX_GOALS {
            let py = poisson_pmf(mu, y);
            let mut p = px * py;
            if rho != 0.0 {
                p *= tau(x, y, lambda, mu, rho);
            }
            // The correction can push a cell slightly negative for extreme rho.
            if p <= 0.0 {
                continue;
            }
            total += p;

            let final_diff = current_diff + x as i32 - y as i32;
            match final_diff.cmp(&0) {
                std::cmp::Ordering::Greater => p_home += p,
                std::cmp::Ordering::Less => p_away += p,
                std::cmp::Ordering::Equal => p_draw += p,
            }
        }
    }

    if total <= 0.0 {
        return OutcomeProbs {
            home: 1.0 / 3.0,
            away: 1.0 / 3.0,
            draw: 1.0 / 3.0,
        };
    }

    OutcomeProbs {
        home: p_home / total,
        away: p_away / total,
        draw: p_draw / total,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sports::models::Sport;

    fn params() -> GameParams {
        GameParams::league_average(Sport::Soccer)
    }

    #[test]
    fn probabilities_always_sum_to_one() {
        for &(h, a, rem) in &[
            (0, 0, 90.0),
            (1, 0, 45.0),
            (0, 2, 10.0),
            (3, 3, 1.0),
            (2, 1, 89.0),
        ] {
            let p = in_play_probs(&params(), h, a, rem, 0, 0);
            assert!(
                (p.sum() - 1.0).abs() < 1e-6,
                "sum was {} for {h}-{a} @ {rem}min",
                p.sum()
            );
        }
    }

    #[test]
    fn full_time_collapses_to_the_scoreline() {
        let p = in_play_probs(&params(), 2, 1, 0.0, 0, 0);
        assert_eq!(p.home, 1.0);
        assert_eq!(p.away, 0.0);
        assert_eq!(p.draw, 0.0);

        let p = in_play_probs(&params(), 1, 1, 0.0, 0, 0);
        assert_eq!(p.draw, 1.0);

        let p = in_play_probs(&params(), 0, 3, 0.0, 0, 0);
        assert_eq!(p.away, 1.0);
    }

    #[test]
    fn a_lead_is_worth_more_as_the_clock_runs_down() {
        let early = in_play_probs(&params(), 1, 0, 80.0, 0, 0);
        let mid = in_play_probs(&params(), 1, 0, 45.0, 0, 0);
        let late = in_play_probs(&params(), 1, 0, 5.0, 0, 0);

        assert!(
            early.home < mid.home && mid.home < late.home,
            "1-0 lead should strengthen: {} -> {} -> {}",
            early.home,
            mid.home,
            late.home
        );
        assert!(late.home > 0.9, "a 1-0 lead with 5 min left: {}", late.home);
    }

    #[test]
    fn the_draw_is_a_material_outcome_at_kickoff() {
        let p = in_play_probs(&params(), 0, 0, 90.0, 0, 0);
        assert!(
            p.draw > 0.15 && p.draw < 0.40,
            "draw probability at kickoff was {}, outside a plausible band",
            p.draw
        );
    }

    #[test]
    fn a_level_game_late_is_mostly_a_draw() {
        let p = in_play_probs(&params(), 1, 1, 2.0, 0, 0);
        assert!(p.draw > 0.85, "draw was only {}", p.draw);
    }

    #[test]
    fn home_advantage_shows_at_kickoff() {
        let p = in_play_probs(&params(), 0, 0, 90.0, 0, 0);
        assert!(
            p.home > p.away,
            "home {} should exceed away {} given home_rate > away_rate",
            p.home,
            p.away
        );
    }

    #[test]
    fn a_red_card_hurts_the_offending_side() {
        let clean = in_play_probs(&params(), 0, 0, 60.0, 0, 0);
        let home_carded = in_play_probs(&params(), 0, 0, 60.0, 1, 0);
        let away_carded = in_play_probs(&params(), 0, 0, 60.0, 0, 1);

        assert!(
            home_carded.home < clean.home,
            "home red card should reduce home win prob: {} vs {}",
            home_carded.home,
            clean.home
        );
        assert!(
            away_carded.away < clean.away,
            "away red card should reduce away win prob: {} vs {}",
            away_carded.away,
            clean.away
        );
        assert!((home_carded.sum() - 1.0).abs() < 1e-6);
    }

    #[test]
    fn two_red_cards_hurt_more_than_one() {
        let one = in_play_probs(&params(), 0, 0, 60.0, 1, 0);
        let two = in_play_probs(&params(), 0, 0, 60.0, 2, 0);
        assert!(two.home < one.home);
    }

    #[test]
    fn a_two_goal_deficit_late_is_nearly_hopeless() {
        let p = in_play_probs(&params(), 0, 2, 3.0, 0, 0);
        assert!(p.home < 0.01, "home win prob was {}", p.home);
        assert!(p.away > 0.97, "away win prob was {}", p.away);
    }

    #[test]
    fn the_dixon_coles_correction_is_dropped_when_little_time_remains() {
        // With a large rho, results should still be sane late in the game
        // because the correction is suppressed rather than extrapolated.
        let mut p = params();
        p.rho = -0.3;
        let late = in_play_probs(&p, 0, 0, 2.0, 0, 0);
        assert!((late.sum() - 1.0).abs() < 1e-6);
        assert!(late.draw > 0.8, "level with 2 min left: draw {}", late.draw);
    }

    #[test]
    fn rho_shifts_probability_without_breaking_normalisation() {
        let mut a = params();
        a.rho = 0.0;
        let mut b = params();
        b.rho = -0.15;

        let pa = in_play_probs(&a, 0, 0, 90.0, 0, 0);
        let pb = in_play_probs(&b, 0, 0, 90.0, 0, 0);

        assert!((pa.sum() - 1.0).abs() < 1e-6);
        assert!((pb.sum() - 1.0).abs() < 1e-6);
        assert!(
            (pa.draw - pb.draw).abs() > 1e-4,
            "rho should move the draw probability"
        );
    }

    #[test]
    fn zero_scoring_rates_freeze_the_scoreline() {
        let p = GameParams {
            home_rate: 0.0,
            away_rate: 0.0,
            rho: 0.0,
            red_card_factor: 1.0,
        };
        let probs = in_play_probs(&p, 1, 0, 45.0, 0, 0);
        assert!(probs.home > 0.999, "no more goals possible: {}", probs.home);
    }

    #[test]
    fn poisson_pmf_is_a_distribution() {
        for lambda in [0.1, 1.0, 3.0, 7.5] {
            let total: f64 = (0..60).map(|k| poisson_pmf(lambda, k)).sum();
            assert!(
                (total - 1.0).abs() < 1e-9,
                "pmf for lambda={lambda} summed to {total}"
            );
        }
    }

    #[test]
    fn poisson_pmf_matches_known_values() {
        // P(X=0 | lambda=1) = e^-1
        assert!((poisson_pmf(1.0, 0) - (-1.0f64).exp()).abs() < 1e-12);
        // P(X=2 | lambda=2) = e^-2 * 2^2/2! = 2 e^-2
        assert!((poisson_pmf(2.0, 2) - 2.0 * (-2.0f64).exp()).abs() < 1e-12);
    }
}
