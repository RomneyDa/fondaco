//! Layer 0: book staleness after a scoring event.
//!
//! The sports analog of `trader::FlatlineDetector`, and probably the single
//! highest-value cheap signal in the whole system: *the score changed eight
//! seconds ago and the book has not moved.* It requires no model, no team
//! strengths, and no external reference — only the two things we already have.
//!
//! Note the difference from the crypto flatline detector, which **suppresses**
//! trading because a frozen book means the API is broken. Here a frozen book is
//! the opportunity, so the two must not be confused: `FlatlineDetector` asks
//! "is our data broken?", this asks "has the market noticed yet?".
//!
//! Guarding against the obvious failure mode — a book that is frozen because
//! the feed is broken rather than because the market is slow — is the caller's
//! job via `is_book_alive`.

use std::time::Instant;

/// Configuration for staleness detection.
#[derive(Debug, Clone)]
pub struct StalenessConfig {
    /// Minimum midpoint move counted as "the market reacted".
    pub move_threshold: f64,
    /// How long after an event the book is still considered stale-able. Beyond
    /// this the market has had ample time and the absence of a move is
    /// information, not lag.
    pub max_reaction_window_s: f64,
    /// Minimum time since the event before a signal is emitted, so we do not
    /// fire on the same tick the feed reported.
    pub min_reaction_delay_s: f64,
}

impl Default for StalenessConfig {
    fn default() -> Self {
        Self {
            move_threshold: 0.02,
            max_reaction_window_s: 30.0,
            min_reaction_delay_s: 1.0,
        }
    }
}

/// A detected stale-book opportunity.
#[derive(Debug, Clone, PartialEq)]
pub struct StaleSignal {
    /// Seconds since the event was reported.
    pub seconds_since_event: f64,
    /// How far the book has moved since the event (max across legs).
    pub observed_move: f64,
    /// The leg whose price should move most, given who scored.
    pub favoured_leg: usize,
}

/// Tracks the book's response to the most recent material event.
pub struct StalenessDetector {
    cfg: StalenessConfig,
    event_at: Option<Instant>,
    /// Midpoints captured at the moment of the event.
    baseline: Vec<Option<f64>>,
    favoured_leg: Option<usize>,
    /// Set once the book has reacted, so we do not signal repeatedly.
    consumed: bool,
}

impl StalenessDetector {
    pub fn new(cfg: StalenessConfig) -> Self {
        Self {
            cfg,
            event_at: None,
            baseline: Vec::new(),
            favoured_leg: None,
            consumed: false,
        }
    }

    /// Register that a material event occurred, capturing the pre-event book.
    ///
    /// `favoured_leg` is the leg the event should push *up* — normally the leg
    /// of the team that just scored.
    pub fn on_event(&mut self, now: Instant, midpoints: &[Option<f64>], favoured_leg: usize) {
        self.event_at = Some(now);
        self.baseline = midpoints.to_vec();
        self.favoured_leg = Some(favoured_leg);
        self.consumed = false;
    }

    /// Evaluate the current book against the pre-event baseline.
    ///
    /// Returns a signal only while the book has *not* yet reacted, within the
    /// reaction window.
    pub fn evaluate(&mut self, now: Instant, midpoints: &[Option<f64>]) -> Option<StaleSignal> {
        if self.consumed {
            return None;
        }
        let event_at = self.event_at?;
        let favoured_leg = self.favoured_leg?;

        let elapsed = now.duration_since(event_at).as_secs_f64();
        if elapsed < self.cfg.min_reaction_delay_s {
            return None;
        }
        if elapsed > self.cfg.max_reaction_window_s {
            // The market has had long enough; absence of a move is now
            // information rather than lag.
            self.consumed = true;
            return None;
        }

        let moved = max_abs_move(&self.baseline, midpoints);
        if moved >= self.cfg.move_threshold {
            // The market has repriced — the opportunity is gone.
            self.consumed = true;
            return None;
        }

        Some(StaleSignal {
            seconds_since_event: elapsed,
            observed_move: moved,
            favoured_leg,
        })
    }

    /// Mark the current signal as acted upon so it does not fire again.
    pub fn consume(&mut self) {
        self.consumed = true;
    }

    pub fn has_pending_event(&self) -> bool {
        self.event_at.is_some() && !self.consumed
    }
}

/// Largest absolute midpoint move across legs.
fn max_abs_move(baseline: &[Option<f64>], current: &[Option<f64>]) -> f64 {
    let mut max = 0.0_f64;
    for (b, c) in baseline.iter().zip(current.iter()) {
        if let (Some(b), Some(c)) = (b, c) {
            max = max.max((c - b).abs());
        }
    }
    max
}

/// Whether the book is actually live, as opposed to frozen because the data
/// path is broken.
///
/// A stale-book signal derived from a dead feed is not an opportunity, it is a
/// bug — so a signal must always be gated on this.
pub fn is_book_alive(last_book_update: Option<Instant>, max_age_s: f64) -> bool {
    match last_book_update {
        Some(t) => t.elapsed().as_secs_f64() <= max_age_s,
        None => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    fn det() -> StalenessDetector {
        StalenessDetector::new(StalenessConfig::default())
    }

    #[test]
    fn no_signal_without_an_event() {
        let mut d = det();
        assert_eq!(d.evaluate(Instant::now(), &[Some(0.5), Some(0.5)]), None);
    }

    #[test]
    fn signals_while_the_book_has_not_moved() {
        let mut d = det();
        let t0 = Instant::now();
        d.on_event(t0, &[Some(0.50), Some(0.50)], 0);

        let s = d
            .evaluate(t0 + Duration::from_secs_f64(8.0), &[Some(0.50), Some(0.50)])
            .expect("stale book should signal");
        assert!((s.seconds_since_event - 8.0).abs() < 0.1);
        assert_eq!(s.favoured_leg, 0);
        assert!(s.observed_move < 0.001);
    }

    #[test]
    fn does_not_signal_on_the_same_instant_as_the_event() {
        let mut d = det();
        let t0 = Instant::now();
        d.on_event(t0, &[Some(0.50), Some(0.50)], 0);
        assert_eq!(
            d.evaluate(t0, &[Some(0.50), Some(0.50)]),
            None,
            "must allow a beat for the book to react before calling it stale"
        );
    }

    #[test]
    fn stops_signalling_once_the_market_reprices() {
        let mut d = det();
        let t0 = Instant::now();
        d.on_event(t0, &[Some(0.50), Some(0.50)], 0);

        assert!(d
            .evaluate(t0 + Duration::from_secs_f64(3.0), &[Some(0.50), Some(0.50)])
            .is_some());

        // Book moves past the threshold — opportunity gone.
        assert!(d
            .evaluate(t0 + Duration::from_secs_f64(4.0), &[Some(0.65), Some(0.35)])
            .is_none());

        // And it must not come back even if the book drifts back.
        assert!(d
            .evaluate(t0 + Duration::from_secs_f64(5.0), &[Some(0.50), Some(0.50)])
            .is_none());
    }

    #[test]
    fn stops_signalling_after_the_reaction_window() {
        let mut d = det();
        let t0 = Instant::now();
        d.on_event(t0, &[Some(0.50), Some(0.50)], 0);

        assert!(d
            .evaluate(t0 + Duration::from_secs_f64(31.0), &[Some(0.50), Some(0.50)])
            .is_none());
        assert!(!d.has_pending_event());
    }

    #[test]
    fn a_sub_threshold_move_still_counts_as_stale() {
        let mut d = det();
        let t0 = Instant::now();
        d.on_event(t0, &[Some(0.50), Some(0.50)], 0);

        let s = d
            .evaluate(t0 + Duration::from_secs_f64(5.0), &[Some(0.505), Some(0.495)])
            .expect("a 0.005 twitch is not a reprice");
        assert!((s.observed_move - 0.005).abs() < 1e-9);
    }

    #[test]
    fn consuming_prevents_repeat_signals() {
        let mut d = det();
        let t0 = Instant::now();
        d.on_event(t0, &[Some(0.50)], 0);
        assert!(d.evaluate(t0 + Duration::from_secs_f64(3.0), &[Some(0.50)]).is_some());

        d.consume();
        assert!(d.evaluate(t0 + Duration::from_secs_f64(4.0), &[Some(0.50)]).is_none());
    }

    #[test]
    fn a_new_event_rearms_the_detector() {
        let mut d = det();
        let t0 = Instant::now();
        d.on_event(t0, &[Some(0.50)], 0);
        d.consume();
        assert!(d.evaluate(t0 + Duration::from_secs_f64(2.0), &[Some(0.50)]).is_none());

        let t1 = t0 + Duration::from_secs_f64(600.0);
        d.on_event(t1, &[Some(0.70)], 1);
        let s = d
            .evaluate(t1 + Duration::from_secs_f64(3.0), &[Some(0.70)])
            .expect("second event should arm again");
        assert_eq!(s.favoured_leg, 1);
    }

    #[test]
    fn missing_midpoints_do_not_read_as_a_move() {
        let mut d = det();
        let t0 = Instant::now();
        d.on_event(t0, &[Some(0.50), None], 0);
        let s = d.evaluate(t0 + Duration::from_secs_f64(4.0), &[Some(0.50), None]);
        assert!(s.is_some(), "a missing leg must not fabricate a reprice");
    }

    #[test]
    fn a_dead_book_is_not_a_live_opportunity() {
        assert!(!is_book_alive(None, 20.0));
        assert!(is_book_alive(Some(Instant::now()), 20.0));
        assert!(!is_book_alive(
            Some(Instant::now() - Duration::from_secs(60)),
            20.0
        ));
    }
}
