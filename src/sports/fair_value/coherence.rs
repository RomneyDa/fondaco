//! Layer 0: cross-leg coherence.
//!
//! On a coherent moneyline the legs' midpoints sum to ~1.0 (two-way) or ~1.0
//! across home/draw/away (three-way). Deviations mean one of three things:
//!
//! - **Overround** (sum > 1): the normal state of any book with a spread. Small
//!   and expected; not a signal.
//! - **A stale leg** (sum far from 1): one side has repriced and the other has
//!   not. This is tradeable and needs no model.
//! - **A genuine underround** (sum < 1): buying every leg locks in a profit.
//!   Rare, usually an artefact of a one-sided book, and must be checked against
//!   *executable* prices rather than midpoints before being believed.
//!
//! The critical distinction this module enforces: a midpoint-based underround
//! is **not** an arbitrage. Midpoints are not executable. Only the sum of the
//! best *asks* tells you whether buying every leg actually pays.

/// Sum of midpoints beyond which the book is considered incoherent rather than
/// merely carrying an overround.
pub const INCOHERENCE_THRESHOLD: f64 = 0.06;

#[derive(Debug, Clone, PartialEq)]
pub struct CoherenceReport {
    /// Sum of moneyline midpoints.
    pub midpoint_sum: f64,
    /// Sum of best asks — the cost of actually buying every leg.
    pub ask_sum: Option<f64>,
    /// Legs with no midpoint at all.
    pub missing_legs: usize,
    /// Deviation of the midpoint sum from 1.0.
    pub deviation: f64,
}

impl CoherenceReport {
    /// Whether the book looks internally inconsistent enough to act on.
    pub fn is_incoherent(&self) -> bool {
        self.missing_legs == 0 && self.deviation.abs() >= INCOHERENCE_THRESHOLD
    }

    /// A true arbitrage: buying every leg at the ask costs less than the $1 the
    /// winning leg is guaranteed to pay.
    ///
    /// Deliberately requires asks. A midpoint sum below 1.0 is not an
    /// arbitrage, because midpoints cannot be traded.
    pub fn is_executable_arbitrage(&self) -> bool {
        match self.ask_sum {
            Some(s) => self.missing_legs == 0 && s < 1.0,
            None => false,
        }
    }

    /// Profit per $1 of payout if the arbitrage is real.
    pub fn arbitrage_edge(&self) -> Option<f64> {
        let s = self.ask_sum?;
        if self.missing_legs > 0 || s >= 1.0 {
            return None;
        }
        Some(1.0 - s)
    }
}

/// Assess coherence across the moneyline legs.
///
/// `midpoints` and `asks` must be parallel and contain only moneyline legs.
pub fn assess(midpoints: &[Option<f64>], asks: &[Option<f64>]) -> CoherenceReport {
    let missing_legs = midpoints.iter().filter(|m| m.is_none()).count();
    let midpoint_sum: f64 = midpoints.iter().flatten().sum();

    let ask_sum = if asks.len() == midpoints.len() && asks.iter().all(|a| a.is_some()) {
        Some(asks.iter().flatten().sum())
    } else {
        None
    };

    CoherenceReport {
        midpoint_sum,
        ask_sum,
        missing_legs,
        deviation: midpoint_sum - 1.0,
    }
}

/// Which leg looks mispriced, given a model's view.
///
/// Returns the leg with the largest positive gap between the model probability
/// and the market midpoint — the leg we would be buying too cheaply.
pub fn most_underpriced_leg(
    model_probs: &[f64],
    midpoints: &[Option<f64>],
) -> Option<(usize, f64)> {
    let mut best: Option<(usize, f64)> = None;
    for (i, (p, m)) in model_probs.iter().zip(midpoints.iter()).enumerate() {
        if let Some(mid) = m {
            let edge = p - mid;
            if best.map(|(_, e)| edge > e).unwrap_or(true) {
                best = Some((i, edge));
            }
        }
    }
    best.filter(|(_, e)| *e > 0.0)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_normal_two_way_book_is_coherent() {
        let r = assess(&[Some(0.62), Some(0.39)], &[Some(0.63), Some(0.40)]);
        assert!((r.midpoint_sum - 1.01).abs() < 1e-9);
        assert!(!r.is_incoherent(), "a 1% overround is normal");
    }

    #[test]
    fn a_stale_leg_shows_up_as_incoherence() {
        // Home repriced to 0.75 after a goal, away still at 0.45.
        let r = assess(&[Some(0.75), Some(0.45)], &[None, None]);
        assert!((r.deviation - 0.20).abs() < 1e-9);
        assert!(r.is_incoherent());
    }

    #[test]
    fn a_three_way_soccer_book_sums_across_all_legs() {
        let r = assess(
            &[Some(0.45), Some(0.30), Some(0.27)],
            &[Some(0.46), Some(0.31), Some(0.28)],
        );
        assert!((r.midpoint_sum - 1.02).abs() < 1e-9);
        assert!(!r.is_incoherent());
    }

    #[test]
    fn a_missing_leg_suppresses_the_incoherence_verdict() {
        // Without every leg the sum is meaningless — must not read as a signal.
        let r = assess(&[Some(0.45), None, Some(0.27)], &[None, None, None]);
        assert_eq!(r.missing_legs, 1);
        assert!(
            !r.is_incoherent(),
            "an incomplete book must not be reported as incoherent"
        );
    }

    #[test]
    fn a_midpoint_underround_is_not_an_arbitrage() {
        // Midpoints sum below 1 but asks sum above it: buying every leg loses.
        let r = assess(&[Some(0.46), Some(0.46)], &[Some(0.52), Some(0.53)]);
        assert!(r.midpoint_sum < 1.0);
        assert!(
            !r.is_executable_arbitrage(),
            "midpoints are not executable — this must not read as arbitrage"
        );
        assert_eq!(r.arbitrage_edge(), None);
    }

    #[test]
    fn an_executable_arbitrage_is_recognised() {
        let r = assess(&[Some(0.45), Some(0.45)], &[Some(0.47), Some(0.48)]);
        assert!(r.is_executable_arbitrage());
        let edge = r.arbitrage_edge().unwrap();
        assert!((edge - 0.05).abs() < 1e-9);
    }

    #[test]
    fn arbitrage_requires_asks_on_every_leg() {
        let r = assess(&[Some(0.45), Some(0.45)], &[Some(0.47), None]);
        assert_eq!(r.ask_sum, None);
        assert!(!r.is_executable_arbitrage());
    }

    #[test]
    fn finds_the_leg_the_model_thinks_is_cheap() {
        // Model says home 0.70, market 0.60 -> edge 0.10 on leg 0.
        let found = most_underpriced_leg(&[0.70, 0.30], &[Some(0.60), Some(0.42)]);
        let (leg, edge) = found.expect("leg 0 is underpriced");
        assert_eq!(leg, 0);
        assert!((edge - 0.10).abs() < 1e-9);
    }

    #[test]
    fn no_underpriced_leg_when_the_market_is_richer_everywhere() {
        assert_eq!(
            most_underpriced_leg(&[0.50, 0.50], &[Some(0.55), Some(0.56)]),
            None
        );
    }

    #[test]
    fn legs_without_a_midpoint_are_skipped() {
        let (leg, _) = most_underpriced_leg(&[0.70, 0.80], &[None, Some(0.40)]).unwrap();
        assert_eq!(leg, 1, "must not pick a leg with no price");
    }
}
