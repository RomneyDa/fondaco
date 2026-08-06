//! Event confirmation — protection against retracted scores.
//!
//! **This is the trap that will cost money if it is not handled.** A soccer
//! goal is disallowed by VAR. An NFL touchdown is reversed on review or wiped
//! by a penalty. A naive bot sees the initial report, buys the top, and then
//! eats the full reversal — often at a worse price than it entered, because the
//! book snaps back faster than it moved out.
//!
//! Three independent defences, any of which can be enabled:
//!
//! 1. **Confirmation delay** — do not act on a score change for N seconds.
//! 2. **Two-feed agreement** — require the Polymarket sports feed and ESPN to
//!    report the same scoreline before treating it as real.
//! 3. **Review suspension** — refuse to trade at all while the feed flags a
//!    play or goal as under official review.
//!
//! This is the sports analog of `FlatlineDetector` and deserves the same
//! first-class treatment: it does not improve returns, it removes a way to lose
//! badly.

use std::collections::HashMap;
use std::time::{Duration, Instant};

use super::models::GameState;

#[derive(Debug, Clone)]
pub struct ConfirmationConfig {
    /// Wait this long after a score change before acting on it.
    ///
    /// Soccer needs more than American football: a VAR review can take well
    /// over a minute, but the goal is usually *reported* immediately and
    /// retracted after. Set per sport by the caller.
    pub delay: Duration,
    /// Require a secondary feed to agree on the scoreline.
    pub require_second_feed: bool,
    /// How long to wait for the secondary feed before giving up on
    /// confirmation and treating the event as unconfirmed.
    pub second_feed_timeout: Duration,
    /// Suspend trading entirely while the state is flagged under review.
    pub suspend_under_review: bool,
}

impl ConfirmationConfig {
    pub fn soccer() -> Self {
        Self {
            // VAR checks routinely run 30s+; an immediate entry on a reported
            // goal is exactly the exposure this exists to prevent.
            delay: Duration::from_secs(25),
            require_second_feed: true,
            second_feed_timeout: Duration::from_secs(45),
            suspend_under_review: true,
        }
    }

    pub fn american_football() -> Self {
        Self {
            delay: Duration::from_secs(12),
            require_second_feed: true,
            second_feed_timeout: Duration::from_secs(30),
            suspend_under_review: true,
        }
    }
}

impl Default for ConfirmationConfig {
    fn default() -> Self {
        Self::american_football()
    }
}

/// Why an event is not yet actionable.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ConfirmationStatus {
    /// No pending event; normal trading.
    Clear,
    /// Within the confirmation delay.
    AwaitingDelay,
    /// Delay elapsed but the secondary feed has not agreed.
    AwaitingSecondFeed,
    /// The secondary feed reports a *different* score — one of them is wrong.
    FeedsDisagree,
    /// Officials are reviewing; state may be retracted.
    UnderReview,
    /// Confirmed and actionable.
    Confirmed,
}

impl ConfirmationStatus {
    /// Whether trading is permitted.
    pub fn is_tradeable(&self) -> bool {
        matches!(self, ConfirmationStatus::Clear | ConfirmationStatus::Confirmed)
    }

    pub fn as_str(&self) -> &'static str {
        match self {
            ConfirmationStatus::Clear => "clear",
            ConfirmationStatus::AwaitingDelay => "awaiting_delay",
            ConfirmationStatus::AwaitingSecondFeed => "awaiting_second_feed",
            ConfirmationStatus::FeedsDisagree => "feeds_disagree",
            ConfirmationStatus::UnderReview => "under_review",
            ConfirmationStatus::Confirmed => "confirmed",
        }
    }
}

struct PendingEvent {
    home_score: i32,
    away_score: i32,
    observed_at: Instant,
    confirmed_by_second: bool,
}

/// Per-game confirmation state.
pub struct ConfirmationGate {
    cfg: ConfirmationConfig,
    pending: HashMap<i64, PendingEvent>,
    last_state: HashMap<i64, (i32, i32)>,
    /// Latest scoreline reported by the secondary feed.
    second_feed: HashMap<i64, (i32, i32)>,
}

impl ConfirmationGate {
    pub fn new(cfg: ConfirmationConfig) -> Self {
        Self {
            cfg,
            pending: HashMap::new(),
            last_state: HashMap::new(),
            second_feed: HashMap::new(),
        }
    }

    /// Record a primary-feed observation.
    pub fn observe(&mut self, now: Instant, game_id: i64, state: &GameState) {
        let score = (state.home_score, state.away_score);
        let changed = self
            .last_state
            .get(&game_id)
            .map(|prev| *prev != score)
            .unwrap_or(false);
        self.last_state.insert(game_id, score);

        if changed {
            self.pending.insert(
                game_id,
                PendingEvent {
                    home_score: score.0,
                    away_score: score.1,
                    observed_at: now,
                    confirmed_by_second: self
                        .second_feed
                        .get(&game_id)
                        .map(|s| *s == score)
                        .unwrap_or(false),
                },
            );
        }
    }

    /// Record a secondary-feed (ESPN) observation.
    pub fn observe_secondary(&mut self, game_id: i64, home: i32, away: i32) {
        self.second_feed.insert(game_id, (home, away));
        if let Some(p) = self.pending.get_mut(&game_id) {
            if p.home_score == home && p.away_score == away {
                p.confirmed_by_second = true;
            }
        }
    }

    /// Current status for a game.
    pub fn status(&self, now: Instant, game_id: i64, state: &GameState) -> ConfirmationStatus {
        if self.cfg.suspend_under_review && state.under_review {
            return ConfirmationStatus::UnderReview;
        }

        let pending = match self.pending.get(&game_id) {
            Some(p) => p,
            None => return ConfirmationStatus::Clear,
        };

        let elapsed = now.duration_since(pending.observed_at);
        if elapsed < self.cfg.delay {
            return ConfirmationStatus::AwaitingDelay;
        }

        if self.cfg.require_second_feed && !pending.confirmed_by_second {
            // If the secondary feed reports a *different* score, that is a
            // disagreement rather than mere lag.
            if let Some(second) = self.second_feed.get(&game_id) {
                if *second != (pending.home_score, pending.away_score) {
                    return ConfirmationStatus::FeedsDisagree;
                }
            }
            if elapsed < self.cfg.second_feed_timeout {
                return ConfirmationStatus::AwaitingSecondFeed;
            }
            // Timed out waiting. The secondary feed may simply be down, so this
            // does not become a hard block forever — but it is not a
            // confirmation either.
            return ConfirmationStatus::AwaitingSecondFeed;
        }

        ConfirmationStatus::Confirmed
    }

    /// Drop the pending event once acted upon or expired.
    pub fn clear(&mut self, game_id: i64) {
        self.pending.remove(&game_id);
    }

    /// Expire pending events older than the second-feed timeout so the map does
    /// not grow across a long slate.
    pub fn expire(&mut self, now: Instant) {
        let timeout = self.cfg.second_feed_timeout.max(self.cfg.delay) * 4;
        self.pending
            .retain(|_, p| now.duration_since(p.observed_at) < timeout);
    }

    pub fn pending_count(&self) -> usize {
        self.pending.len()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sports::models::GameStatus;

    fn state(home: i32, away: i32, under_review: bool) -> GameState {
        GameState {
            home_score: home,
            away_score: away,
            period: "2H".into(),
            clock: "60:00".into(),
            status: GameStatus::InProgress,
            elapsed_minutes: Some(60.0),
            home_red_cards: 0,
            away_red_cards: 0,
            under_review,
        }
    }

    fn gate() -> ConfirmationGate {
        ConfirmationGate::new(ConfirmationConfig::soccer())
    }

    #[test]
    fn a_quiet_game_is_clear() {
        let mut g = gate();
        let now = Instant::now();
        g.observe(now, 1, &state(0, 0, false));
        assert_eq!(g.status(now, 1, &state(0, 0, false)), ConfirmationStatus::Clear);
        assert!(g.status(now, 1, &state(0, 0, false)).is_tradeable());
    }

    #[test]
    fn a_fresh_goal_is_not_immediately_actionable() {
        let mut g = gate();
        let t0 = Instant::now();
        g.observe(t0, 1, &state(0, 0, false));
        g.observe(t0, 1, &state(1, 0, false));

        let s = g.status(t0, 1, &state(1, 0, false));
        assert_eq!(s, ConfirmationStatus::AwaitingDelay);
        assert!(
            !s.is_tradeable(),
            "buying the top of an unconfirmed goal is exactly the exposure to avoid"
        );
    }

    #[test]
    fn a_goal_becomes_actionable_after_the_delay_and_second_feed_agreement() {
        let mut g = gate();
        let t0 = Instant::now();
        g.observe(t0, 1, &state(0, 0, false));
        g.observe(t0, 1, &state(1, 0, false));
        g.observe_secondary(1, 1, 0);

        let later = t0 + Duration::from_secs(30);
        let s = g.status(later, 1, &state(1, 0, false));
        assert_eq!(s, ConfirmationStatus::Confirmed);
        assert!(s.is_tradeable());
    }

    #[test]
    fn without_second_feed_agreement_it_stays_blocked() {
        let mut g = gate();
        let t0 = Instant::now();
        g.observe(t0, 1, &state(0, 0, false));
        g.observe(t0, 1, &state(1, 0, false));
        // ESPN never confirms.

        let later = t0 + Duration::from_secs(30);
        assert_eq!(
            g.status(later, 1, &state(1, 0, false)),
            ConfirmationStatus::AwaitingSecondFeed
        );
    }

    #[test]
    fn a_disagreeing_second_feed_is_reported_as_a_conflict_not_as_lag() {
        let mut g = gate();
        let t0 = Instant::now();
        g.observe(t0, 1, &state(0, 0, false));
        g.observe(t0, 1, &state(1, 0, false));
        // ESPN still shows 0-0 — one of the feeds is wrong.
        g.observe_secondary(1, 0, 0);

        let later = t0 + Duration::from_secs(30);
        let s = g.status(later, 1, &state(1, 0, false));
        assert_eq!(s, ConfirmationStatus::FeedsDisagree);
        assert!(!s.is_tradeable());
    }

    #[test]
    fn a_review_flag_blocks_trading_outright() {
        let mut g = gate();
        let now = Instant::now();
        g.observe(now, 1, &state(0, 0, false));

        let s = g.status(now, 1, &state(0, 0, true));
        assert_eq!(s, ConfirmationStatus::UnderReview);
        assert!(!s.is_tradeable());
    }

    #[test]
    fn a_review_flag_outranks_an_otherwise_confirmed_event() {
        let mut g = gate();
        let t0 = Instant::now();
        g.observe(t0, 1, &state(0, 0, false));
        g.observe(t0, 1, &state(1, 0, false));
        g.observe_secondary(1, 1, 0);

        let later = t0 + Duration::from_secs(30);
        assert_eq!(
            g.status(later, 1, &state(1, 0, true)),
            ConfirmationStatus::UnderReview,
            "a confirmed goal still under review must not be traded"
        );
    }

    #[test]
    fn a_retracted_goal_supersedes_the_pending_event() {
        let mut g = gate();
        let t0 = Instant::now();
        g.observe(t0, 1, &state(0, 0, false));
        g.observe(t0, 1, &state(1, 0, false));

        // VAR disallows it; the feed reverts to 0-0. That is itself a change,
        // so a new pending event replaces the stale one rather than the old
        // goal quietly becoming actionable.
        let t1 = t0 + Duration::from_secs(20);
        g.observe(t1, 1, &state(0, 0, false));

        let later = t1 + Duration::from_secs(30);
        g.observe_secondary(1, 0, 0);
        let s = g.status(later, 1, &state(0, 0, false));
        assert_eq!(s, ConfirmationStatus::Confirmed);
        // And crucially the confirmed scoreline is the reverted one.
        assert_eq!(g.last_state.get(&1), Some(&(0, 0)));
    }

    #[test]
    fn american_football_uses_a_shorter_delay_than_soccer() {
        assert!(
            ConfirmationConfig::american_football().delay < ConfirmationConfig::soccer().delay,
            "VAR reviews run longer than NFL replay reviews"
        );
    }

    #[test]
    fn config_can_disable_the_second_feed_requirement() {
        let cfg = ConfirmationConfig {
            delay: Duration::from_secs(5),
            require_second_feed: false,
            second_feed_timeout: Duration::from_secs(30),
            suspend_under_review: true,
        };
        let mut g = ConfirmationGate::new(cfg);
        let t0 = Instant::now();
        g.observe(t0, 1, &state(0, 0, false));
        g.observe(t0, 1, &state(1, 0, false));

        let later = t0 + Duration::from_secs(10);
        assert_eq!(
            g.status(later, 1, &state(1, 0, false)),
            ConfirmationStatus::Confirmed
        );
    }

    #[test]
    fn clearing_removes_the_pending_event() {
        let mut g = gate();
        let t0 = Instant::now();
        g.observe(t0, 1, &state(0, 0, false));
        g.observe(t0, 1, &state(1, 0, false));
        assert_eq!(g.pending_count(), 1);

        g.clear(1);
        assert_eq!(g.pending_count(), 0);
        assert_eq!(g.status(t0, 1, &state(1, 0, false)), ConfirmationStatus::Clear);
    }

    #[test]
    fn expiry_bounds_the_pending_map_over_a_long_slate() {
        let mut g = gate();
        let t0 = Instant::now();
        for id in 0..10 {
            g.observe(t0, id, &state(0, 0, false));
            g.observe(t0, id, &state(1, 0, false));
        }
        assert_eq!(g.pending_count(), 10);

        g.expire(t0 + Duration::from_secs(600));
        assert_eq!(g.pending_count(), 0);
    }

    #[test]
    fn games_are_tracked_independently() {
        let mut g = gate();
        let t0 = Instant::now();
        g.observe(t0, 1, &state(0, 0, false));
        g.observe(t0, 2, &state(0, 0, false));
        g.observe(t0, 1, &state(1, 0, false));

        assert_eq!(g.status(t0, 1, &state(1, 0, false)), ConfirmationStatus::AwaitingDelay);
        assert_eq!(
            g.status(t0, 2, &state(0, 0, false)),
            ConfirmationStatus::Clear,
            "one game's event must not gate another"
        );
    }
}
