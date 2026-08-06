//! Evaluation metrics for sports.
//!
//! **PnL Sharpe is the wrong primary objective here.** The NFL plays 272
//! regular-season games a year; the EPL 380. Against a few hundred observations
//! the best variant of a six-parameter grid is noise with near-certainty, and
//! per-session PnL variance swamps any real signal.
//!
//! Two metrics have far more statistical power per observation:
//!
//! - **Calibration** (Brier score, log loss). Every game contributes a
//!   probabilistic prediction against a known outcome — and every *tick*
//!   contributes one too. This answers "is the model any good" using orders of
//!   magnitude more data than PnL does.
//! - **CLV** (closing line value): entry price versus the price at game end.
//!   Positive CLV is the standard evidence that a bet was good independent of
//!   whether it happened to win.
//!
//! PnL is still reported, but it is the noisiest number in the set and should
//! be the last thing consulted, not the first.

use serde::{Deserialize, Serialize};

/// One probabilistic prediction paired with its realised outcome.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Prediction {
    /// Predicted probability of the event.
    pub p: f64,
    /// Whether the event actually occurred.
    pub occurred: bool,
}

/// Brier score: mean squared error of probabilistic predictions.
/// Lower is better; 0.25 is the score of always predicting 0.5.
pub fn brier_score(preds: &[Prediction]) -> Option<f64> {
    if preds.is_empty() {
        return None;
    }
    let sum: f64 = preds
        .iter()
        .map(|pr| {
            let o = if pr.occurred { 1.0 } else { 0.0 };
            (pr.p - o).powi(2)
        })
        .sum();
    Some(sum / preds.len() as f64)
}

/// Mean negative log likelihood. Lower is better.
///
/// Probabilities are clamped away from 0 and 1 — an overconfident model that is
/// wrong once would otherwise score infinitely badly and dominate the mean.
pub fn log_loss(preds: &[Prediction]) -> Option<f64> {
    if preds.is_empty() {
        return None;
    }
    const EPS: f64 = 1e-6;
    let sum: f64 = preds
        .iter()
        .map(|pr| {
            let p = pr.p.clamp(EPS, 1.0 - EPS);
            if pr.occurred {
                -p.ln()
            } else {
                -(1.0 - p).ln()
            }
        })
        .sum();
    Some(sum / preds.len() as f64)
}

/// Brier skill score against a reference set of predictions.
///
/// Positive means the model beats the reference; 0 means no better. The natural
/// reference is the market itself, which is the only comparison that matters:
/// a model can be well calibrated and still have no edge.
pub fn brier_skill_score(model: &[Prediction], reference: &[Prediction]) -> Option<f64> {
    let bs_model = brier_score(model)?;
    let bs_ref = brier_score(reference)?;
    if bs_ref <= 0.0 {
        return None;
    }
    Some(1.0 - bs_model / bs_ref)
}

/// One bucket of a calibration (reliability) curve.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct CalibrationBucket {
    pub lower: f64,
    pub upper: f64,
    pub count: usize,
    /// Mean predicted probability in the bucket.
    pub mean_predicted: f64,
    /// Fraction that actually occurred.
    pub observed_rate: f64,
}

/// Build a reliability curve. A well-calibrated model has
/// `mean_predicted ≈ observed_rate` in every populated bucket.
pub fn calibration_curve(preds: &[Prediction], buckets: usize) -> Vec<CalibrationBucket> {
    let buckets = buckets.max(1);
    let mut sums = vec![0.0_f64; buckets];
    let mut hits = vec![0_usize; buckets];
    let mut counts = vec![0_usize; buckets];

    for pr in preds {
        let p = pr.p.clamp(0.0, 1.0);
        // p == 1.0 must land in the last bucket, not one past the end.
        let idx = ((p * buckets as f64) as usize).min(buckets - 1);
        sums[idx] += p;
        counts[idx] += 1;
        if pr.occurred {
            hits[idx] += 1;
        }
    }

    (0..buckets)
        .map(|i| {
            let n = counts[i];
            CalibrationBucket {
                lower: i as f64 / buckets as f64,
                upper: (i + 1) as f64 / buckets as f64,
                count: n,
                mean_predicted: if n > 0 { sums[i] / n as f64 } else { 0.0 },
                observed_rate: if n > 0 { hits[i] as f64 / n as f64 } else { 0.0 },
            }
        })
        .collect()
}

/// Expected calibration error: bucket-count-weighted mean absolute gap between
/// predicted and observed rates.
pub fn expected_calibration_error(preds: &[Prediction], buckets: usize) -> Option<f64> {
    if preds.is_empty() {
        return None;
    }
    let curve = calibration_curve(preds, buckets);
    let total: usize = curve.iter().map(|b| b.count).sum();
    if total == 0 {
        return None;
    }
    let err: f64 = curve
        .iter()
        .filter(|b| b.count > 0)
        .map(|b| {
            (b.count as f64 / total as f64) * (b.mean_predicted - b.observed_rate).abs()
        })
        .sum();
    Some(err)
}

// ---------------------------------------------------------------------------
// Closing line value
// ---------------------------------------------------------------------------

/// One trade, recorded for CLV and calibration analysis.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct TradeRecord {
    pub game_key: String,
    pub leg: usize,
    pub leg_key: String,
    /// Price actually paid.
    pub entry_price: f64,
    /// Market midpoint for the same leg at entry.
    pub entry_midpoint: f64,
    /// Market midpoint for the same leg at the last observation of the game.
    pub closing_midpoint: f64,
    /// Model probability at entry.
    pub model_prob: f64,
    /// Whether this leg won.
    pub won: bool,
    pub tokens: f64,
    /// Realised profit on this trade.
    pub pnl: f64,
    /// Seconds of game elapsed at entry.
    pub elapsed_seconds: f64,
}

impl TradeRecord {
    /// Closing line value: how much the market moved in our favour after entry.
    ///
    /// Positive CLV means we bought before the market repriced upward — the
    /// standard evidence that a bet was good regardless of whether it won.
    pub fn clv(&self) -> f64 {
        self.closing_midpoint - self.entry_price
    }

    /// Edge the model believed it had at entry.
    pub fn claimed_edge(&self) -> f64 {
        self.model_prob - self.entry_price
    }
}

#[derive(Debug, Clone, Default, Serialize)]
pub struct ClvSummary {
    pub trades: usize,
    pub mean_clv: f64,
    pub median_clv: f64,
    /// Fraction of trades with positive CLV.
    pub positive_rate: f64,
    /// Mean edge the model claimed, for comparison against realised CLV.
    pub mean_claimed_edge: f64,
    /// t-statistic of mean CLV against zero. Interpret with care at small n.
    pub clv_t_stat: Option<f64>,
}

pub fn summarize_clv(trades: &[TradeRecord]) -> ClvSummary {
    if trades.is_empty() {
        return ClvSummary::default();
    }
    let mut clvs: Vec<f64> = trades.iter().map(|t| t.clv()).collect();
    let n = clvs.len() as f64;
    let mean = clvs.iter().sum::<f64>() / n;
    let positive = clvs.iter().filter(|c| **c > 0.0).count() as f64;
    let mean_edge = trades.iter().map(|t| t.claimed_edge()).sum::<f64>() / n;

    let t_stat = if clvs.len() > 1 {
        let var = clvs.iter().map(|c| (c - mean).powi(2)).sum::<f64>() / (n - 1.0);
        let se = (var / n).sqrt();
        if se > 0.0 {
            Some(mean / se)
        } else {
            None
        }
    } else {
        None
    };

    clvs.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    let median = if clvs.len() % 2 == 1 {
        clvs[clvs.len() / 2]
    } else {
        (clvs[clvs.len() / 2 - 1] + clvs[clvs.len() / 2]) / 2.0
    };

    ClvSummary {
        trades: trades.len(),
        mean_clv: mean,
        median_clv: median,
        positive_rate: positive / n,
        mean_claimed_edge: mean_edge,
        clv_t_stat: t_stat,
    }
}

/// Full evaluation of a strategy or model run.
#[derive(Debug, Clone, Default, Serialize)]
pub struct SportsEvaluation {
    pub games: usize,
    pub games_traded: usize,
    pub total_pnl: f64,
    pub mean_pnl_per_game: f64,
    /// Max simultaneous capital deployed across overlapping games.
    pub max_concurrent_exposure: f64,
    /// Max number of games holding a position at the same instant. Distinct
    /// from `games_traded`, which counts the whole run — a resolved game frees
    /// its slot for another.
    pub max_concurrent_games_open: usize,
    pub clv: ClvSummary,
    /// Calibration of the model's probabilities against outcomes.
    pub model_brier: Option<f64>,
    pub model_log_loss: Option<f64>,
    pub model_ece: Option<f64>,
    /// Calibration of the market midpoint, as the benchmark to beat.
    pub market_brier: Option<f64>,
    /// Positive means the model beats the market's own prices.
    pub brier_skill_vs_market: Option<f64>,
    /// Sample-size warning. Emitted whenever the game count is too small for
    /// parameter search to mean anything.
    pub sample_size_warning: Option<String>,
}

/// Below this many games, a parameter sweep is fitting noise.
pub const MIN_GAMES_FOR_SWEEP: usize = 300;

pub fn sample_size_warning(games: usize, tunable_params: usize) -> Option<String> {
    if tunable_params == 0 {
        return None;
    }
    // Rough rule of thumb: a variant per ~50 observations is already generous.
    let variants = 3usize.saturating_pow(tunable_params as u32);
    if games < MIN_GAMES_FOR_SWEEP || variants * 50 > games {
        return Some(format!(
            "{games} games against {variants} parameter variants ({tunable_params} tunable \
             params): the best variant is overwhelmingly likely to be noise. Sports strategies \
             should carry at most 3 tunable params and be validated by season-split \
             walk-forward, not by picking the grid maximum."
        ));
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    fn p(prob: f64, occurred: bool) -> Prediction {
        Prediction {
            p: prob,
            occurred,
        }
    }

    #[test]
    fn brier_of_a_perfect_forecaster_is_zero() {
        let preds = vec![p(1.0, true), p(0.0, false), p(1.0, true)];
        assert_eq!(brier_score(&preds), Some(0.0));
    }

    #[test]
    fn brier_of_a_coin_flip_forecaster_is_a_quarter() {
        let preds = vec![p(0.5, true), p(0.5, false), p(0.5, true), p(0.5, false)];
        assert!((brier_score(&preds).unwrap() - 0.25).abs() < 1e-12);
    }

    #[test]
    fn brier_of_a_perfectly_wrong_forecaster_is_one() {
        let preds = vec![p(1.0, false), p(0.0, true)];
        assert!((brier_score(&preds).unwrap() - 1.0).abs() < 1e-12);
    }

    #[test]
    fn log_loss_does_not_blow_up_on_a_confident_miss() {
        // Without clamping this is infinite and would poison the mean.
        let preds = vec![p(1.0, false)];
        let ll = log_loss(&preds).unwrap();
        assert!(ll.is_finite(), "log loss must stay finite");
        assert!(ll > 10.0, "but it must still be heavily penalised: {ll}");
    }

    #[test]
    fn log_loss_of_a_perfect_forecaster_is_near_zero() {
        let preds = vec![p(1.0, true), p(0.0, false)];
        assert!(log_loss(&preds).unwrap() < 1e-5);
    }

    #[test]
    fn empty_inputs_yield_none_rather_than_zero() {
        assert_eq!(brier_score(&[]), None);
        assert_eq!(log_loss(&[]), None);
        assert_eq!(expected_calibration_error(&[], 10), None);
    }

    #[test]
    fn brier_skill_is_positive_when_the_model_beats_the_reference() {
        let model = vec![p(0.9, true), p(0.1, false)];
        let reference = vec![p(0.6, true), p(0.4, false)];
        let skill = brier_skill_score(&model, &reference).unwrap();
        assert!(skill > 0.0, "skill was {skill}");
    }

    #[test]
    fn brier_skill_is_negative_when_the_model_is_worse() {
        let model = vec![p(0.55, true), p(0.45, false)];
        let reference = vec![p(0.95, true), p(0.05, false)];
        assert!(brier_skill_score(&model, &reference).unwrap() < 0.0);
    }

    #[test]
    fn calibration_curve_buckets_correctly_including_the_endpoint() {
        let preds = vec![p(0.05, false), p(0.15, false), p(0.95, true), p(1.0, true)];
        let curve = calibration_curve(&preds, 10);
        assert_eq!(curve.len(), 10);
        assert_eq!(curve[0].count, 1);
        assert_eq!(curve[1].count, 1);
        // p=1.0 must land in the last bucket, not overflow.
        assert_eq!(curve[9].count, 2, "p=1.0 must fall in the final bucket");
        assert_eq!(curve[9].observed_rate, 1.0);
    }

    #[test]
    fn a_well_calibrated_forecaster_has_low_ece() {
        // 100 predictions at 0.7, 70 of which occur.
        let preds: Vec<Prediction> = (0..100).map(|i| p(0.7, i < 70)).collect();
        let ece = expected_calibration_error(&preds, 10).unwrap();
        assert!(ece < 0.01, "ece was {ece}");
    }

    #[test]
    fn a_miscalibrated_forecaster_has_high_ece() {
        // Claims 0.9 but only 30% occur.
        let preds: Vec<Prediction> = (0..100).map(|i| p(0.9, i < 30)).collect();
        let ece = expected_calibration_error(&preds, 10).unwrap();
        assert!(ece > 0.5, "ece was {ece}");
    }

    fn trade(entry: f64, closing: f64, model: f64, won: bool) -> TradeRecord {
        TradeRecord {
            game_key: "g".into(),
            leg: 0,
            leg_key: "home".into(),
            entry_price: entry,
            entry_midpoint: entry,
            closing_midpoint: closing,
            model_prob: model,
            won,
            tokens: 10.0,
            pnl: if won { 10.0 * (1.0 - entry) } else { -10.0 * entry },
            elapsed_seconds: 1000.0,
        }
    }

    #[test]
    fn clv_is_the_move_from_entry_to_close() {
        let t = trade(0.55, 0.70, 0.68, true);
        assert!((t.clv() - 0.15).abs() < 1e-9);
        assert!((t.claimed_edge() - 0.13).abs() < 1e-9);
    }

    #[test]
    fn clv_is_negative_when_the_market_moves_against_us() {
        let t = trade(0.55, 0.40, 0.68, false);
        assert!((t.clv() + 0.15).abs() < 1e-9);
    }

    #[test]
    fn clv_summary_is_independent_of_whether_trades_won() {
        // Both trades had identical CLV; one won and one lost. CLV must not
        // care, which is the entire point of using it at small sample sizes.
        let trades = vec![trade(0.50, 0.60, 0.62, true), trade(0.50, 0.60, 0.62, false)];
        let s = summarize_clv(&trades);
        assert_eq!(s.trades, 2);
        assert!((s.mean_clv - 0.10).abs() < 1e-9);
        assert!((s.positive_rate - 1.0).abs() < 1e-9);
    }

    #[test]
    fn clv_summary_reports_median_and_t_stat() {
        let trades = vec![
            trade(0.50, 0.55, 0.60, true),
            trade(0.50, 0.60, 0.60, true),
            trade(0.50, 0.65, 0.60, false),
        ];
        let s = summarize_clv(&trades);
        assert!((s.median_clv - 0.10).abs() < 1e-9);
        assert!(s.clv_t_stat.unwrap() > 0.0);
    }

    #[test]
    fn clv_summary_of_no_trades_is_harmless() {
        let s = summarize_clv(&[]);
        assert_eq!(s.trades, 0);
        assert_eq!(s.mean_clv, 0.0);
        assert_eq!(s.clv_t_stat, None);
    }

    #[test]
    fn sample_size_warning_fires_for_a_realistic_nfl_sweep() {
        // 272 games, 6 tunable params -> 729 variants. Hopeless.
        let w = sample_size_warning(272, 6);
        assert!(w.is_some());
        assert!(w.unwrap().contains("729"));
    }

    #[test]
    fn sample_size_warning_stays_quiet_with_enough_data_and_few_params() {
        // 2000 games, 2 params -> 9 variants.
        assert_eq!(sample_size_warning(2000, 2), None);
    }

    #[test]
    fn sample_size_warning_is_silent_when_nothing_is_tunable() {
        assert_eq!(sample_size_warning(10, 0), None);
    }
}
