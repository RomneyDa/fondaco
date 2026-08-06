//! Portfolio allocator for concurrent games.
//!
//! A genuinely new concern. In the crypto path exactly one position is open at
//! a time and `--session-cash-ratio` is sufficient. In sports, thirteen NFL
//! games kick off at once and every one of them wants the same bankroll. Worse,
//! they are **correlated**: a Sunday-afternoon slate resolving together is a
//! fat tail, not thirteen independent bets.
//!
//! The allocator is the single place that decides how much a game may deploy,
//! so no strategy can size its own position past the portfolio's limits.

use std::collections::HashMap;

use serde::Serialize;

/// Portfolio risk limits.
#[derive(Debug, Clone)]
pub struct AllocatorLimits {
    /// Fraction of bankroll deployable to any single game.
    pub max_per_game_fraction: f64,
    /// Absolute cap on a single game's stake.
    pub max_per_game_cash: f64,
    /// Fraction of bankroll deployable across all open positions.
    pub max_total_exposure_fraction: f64,
    /// Hard cap on simultaneously open games.
    pub max_concurrent_games: usize,
    /// Fraction deployable within one league, so a single slate cannot
    /// consume the whole book.
    pub max_per_league_fraction: f64,
    /// Stop opening new positions once the bankroll has fallen this far below
    /// its peak. A drawdown brake, not a stop-loss.
    pub max_drawdown_fraction: f64,
}

impl Default for AllocatorLimits {
    fn default() -> Self {
        Self {
            max_per_game_fraction: 0.05,
            max_per_game_cash: 50.0,
            max_total_exposure_fraction: 0.30,
            max_concurrent_games: 6,
            max_per_league_fraction: 0.20,
            max_drawdown_fraction: 0.25,
        }
    }
}

/// Why an allocation was refused or reduced.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum AllocationVerdict {
    Granted,
    /// The per-game cap bound.
    CappedPerGame,
    /// The portfolio-wide exposure cap bound.
    CappedTotalExposure,
    /// The per-league cap bound.
    CappedPerLeague,
    /// Too many games already open.
    ConcurrencyLimit,
    /// Drawdown brake engaged.
    DrawdownBrake,
    /// No cash left.
    NoCash,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Allocation {
    pub cash: f64,
    pub verdict: AllocationVerdict,
}

impl Allocation {
    pub fn refused(verdict: AllocationVerdict) -> Self {
        Self { cash: 0.0, verdict }
    }
}

/// One game's committed capital.
#[derive(Debug, Clone, Default)]
struct GameExposure {
    league: String,
    committed: f64,
    open: bool,
}

/// Tracks bankroll and per-game commitments.
pub struct Allocator {
    limits: AllocatorLimits,
    bankroll: f64,
    peak_bankroll: f64,
    exposures: HashMap<String, GameExposure>,
}

impl Allocator {
    pub fn new(bankroll: f64, limits: AllocatorLimits) -> Self {
        Self {
            limits,
            bankroll,
            peak_bankroll: bankroll,
            exposures: HashMap::new(),
        }
    }

    pub fn bankroll(&self) -> f64 {
        self.bankroll
    }

    pub fn peak_bankroll(&self) -> f64 {
        self.peak_bankroll
    }

    pub fn total_exposure(&self) -> f64 {
        self.exposures.values().map(|e| e.committed).sum()
    }

    pub fn league_exposure(&self, league: &str) -> f64 {
        self.exposures
            .values()
            .filter(|e| e.league == league)
            .map(|e| e.committed)
            .sum()
    }

    pub fn open_games(&self) -> usize {
        self.exposures.values().filter(|e| e.open).count()
    }

    pub fn current_drawdown(&self) -> f64 {
        if self.peak_bankroll <= 0.0 {
            return 0.0;
        }
        ((self.peak_bankroll - self.bankroll) / self.peak_bankroll).max(0.0)
    }

    /// How much this game may deploy right now.
    pub fn allocate(&self, game_key: &str, league: &str) -> Allocation {
        if self.bankroll <= 0.0 {
            return Allocation::refused(AllocationVerdict::NoCash);
        }

        // Drawdown brake. Existing positions are left alone — this only stops
        // opening new risk.
        let already_open = self
            .exposures
            .get(game_key)
            .map(|e| e.open)
            .unwrap_or(false);
        if !already_open && self.current_drawdown() >= self.limits.max_drawdown_fraction {
            return Allocation::refused(AllocationVerdict::DrawdownBrake);
        }

        if !already_open && self.open_games() >= self.limits.max_concurrent_games {
            return Allocation::refused(AllocationVerdict::ConcurrencyLimit);
        }

        let own = self
            .exposures
            .get(game_key)
            .map(|e| e.committed)
            .unwrap_or(0.0);

        let per_game = (self.bankroll * self.limits.max_per_game_fraction)
            .min(self.limits.max_per_game_cash)
            - own;
        let total_room = self.bankroll * self.limits.max_total_exposure_fraction
            - (self.total_exposure() - own)
            - own;
        let league_room = self.bankroll * self.limits.max_per_league_fraction
            - (self.league_exposure(league) - own)
            - own;

        let mut cash = per_game.min(total_room).min(league_room).min(self.bankroll);
        if cash <= 0.0 {
            // Report which constraint actually bound, so the decision log says
            // something useful rather than just "no".
            let verdict = if per_game <= 0.0 {
                AllocationVerdict::CappedPerGame
            } else if total_room <= 0.0 {
                AllocationVerdict::CappedTotalExposure
            } else if league_room <= 0.0 {
                AllocationVerdict::CappedPerLeague
            } else {
                AllocationVerdict::NoCash
            };
            return Allocation::refused(verdict);
        }

        let verdict = if cash >= per_game - 1e-9 {
            AllocationVerdict::CappedPerGame
        } else if cash >= total_room - 1e-9 {
            AllocationVerdict::CappedTotalExposure
        } else if cash >= league_room - 1e-9 {
            AllocationVerdict::CappedPerLeague
        } else {
            AllocationVerdict::Granted
        };

        cash = cash.max(0.0);
        Allocation { cash, verdict }
    }

    /// Record capital committed to a game.
    pub fn commit(&mut self, game_key: &str, league: &str, amount: f64) {
        if amount <= 0.0 {
            return;
        }
        let e = self
            .exposures
            .entry(game_key.to_string())
            .or_insert_with(|| GameExposure {
                league: league.to_string(),
                committed: 0.0,
                open: false,
            });
        e.committed += amount;
        e.open = true;
        self.bankroll -= amount;
    }

    /// Record capital returned (a sale or a resolution).
    pub fn release(&mut self, game_key: &str, proceeds: f64) {
        if let Some(e) = self.exposures.get_mut(game_key) {
            e.committed = 0.0;
            e.open = false;
        }
        self.bankroll += proceeds.max(0.0);
        self.peak_bankroll = self.peak_bankroll.max(self.bankroll);
    }

    /// Close a game out entirely, whatever it returned.
    pub fn close_game(&mut self, game_key: &str, proceeds: f64) {
        self.release(game_key, proceeds);
        self.exposures.remove(game_key);
    }

    pub fn snapshot(&self) -> AllocatorSnapshot {
        AllocatorSnapshot {
            bankroll: self.bankroll,
            peak_bankroll: self.peak_bankroll,
            total_exposure: self.total_exposure(),
            open_games: self.open_games(),
            drawdown: self.current_drawdown(),
        }
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct AllocatorSnapshot {
    pub bankroll: f64,
    pub peak_bankroll: f64,
    pub total_exposure: f64,
    pub open_games: usize,
    pub drawdown: f64,
}

#[cfg(test)]
mod tests {
    use super::*;

    fn limits() -> AllocatorLimits {
        AllocatorLimits {
            max_per_game_fraction: 0.10,
            max_per_game_cash: 1_000.0,
            max_total_exposure_fraction: 0.25,
            max_concurrent_games: 3,
            max_per_league_fraction: 0.20,
            max_drawdown_fraction: 0.25,
        }
    }

    #[test]
    fn a_single_game_gets_the_per_game_cap() {
        let a = Allocator::new(1000.0, limits());
        let alloc = a.allocate("g1", "nfl");
        assert!((alloc.cash - 100.0).abs() < 1e-9, "got {}", alloc.cash);
    }

    #[test]
    fn concurrent_games_share_the_portfolio_cap() {
        let mut a = Allocator::new(1000.0, limits());
        // Portfolio cap is 250; per-game is 100.
        a.commit("g1", "nfl", 100.0);
        a.commit("g2", "nfl", 100.0);

        // 200 already committed, 50 of portfolio room left.
        let alloc = a.allocate("g3", "nfl");
        assert!(
            alloc.cash <= 50.0 + 1e-9,
            "third game got {} but only 50 of portfolio room remained",
            alloc.cash
        );
    }

    #[test]
    fn the_concurrency_cap_refuses_a_fourth_game() {
        let mut a = Allocator::new(10_000.0, limits());
        a.commit("g1", "nfl", 10.0);
        a.commit("g2", "nfl", 10.0);
        a.commit("g3", "nfl", 10.0);

        let alloc = a.allocate("g4", "nfl");
        assert_eq!(alloc.verdict, AllocationVerdict::ConcurrencyLimit);
        assert_eq!(alloc.cash, 0.0);
    }

    #[test]
    fn an_already_open_game_is_not_blocked_by_the_concurrency_cap() {
        let mut a = Allocator::new(10_000.0, limits());
        a.commit("g1", "nfl", 10.0);
        a.commit("g2", "nfl", 10.0);
        a.commit("g3", "nfl", 10.0);

        // g2 is already open, so it may still add — otherwise a strategy could
        // never scale into or exit a position once the cap was reached.
        let alloc = a.allocate("g2", "nfl");
        assert_ne!(alloc.verdict, AllocationVerdict::ConcurrencyLimit);
        assert!(alloc.cash > 0.0);
    }

    #[test]
    fn the_per_league_cap_stops_one_slate_consuming_the_book() {
        let mut lim = limits();
        lim.max_per_league_fraction = 0.15;
        lim.max_total_exposure_fraction = 0.90;
        lim.max_concurrent_games = 10;
        let mut a = Allocator::new(1000.0, lim);

        a.commit("g1", "nfl", 100.0);
        // League cap is 150, so only 50 remains for NFL...
        let nfl = a.allocate("g2", "nfl");
        assert!(nfl.cash <= 50.0 + 1e-9, "got {}", nfl.cash);

        // ...but a different league has its own allowance.
        let epl = a.allocate("g3", "epl");
        assert!(epl.cash > 50.0, "other leagues must not be starved: {}", epl.cash);
    }

    #[test]
    fn correlated_same_league_games_cannot_each_take_the_full_per_game_cap() {
        // The central risk this exists to prevent: a Sunday NFL slate is not
        // thirteen independent bets.
        let mut lim = limits();
        lim.max_concurrent_games = 20;
        lim.max_per_league_fraction = 0.20;
        lim.max_total_exposure_fraction = 0.50;
        let mut a = Allocator::new(1000.0, lim);

        let mut total = 0.0;
        for i in 0..13 {
            let key = format!("g{i}");
            let alloc = a.allocate(&key, "nfl");
            if alloc.cash > 0.0 {
                a.commit(&key, "nfl", alloc.cash);
                total += alloc.cash;
            }
        }
        assert!(
            total <= 200.0 + 1e-6,
            "13 correlated games deployed ${total}, above the 20% league cap"
        );
    }

    #[test]
    fn the_drawdown_brake_stops_new_positions_but_not_existing_ones() {
        let mut a = Allocator::new(1000.0, limits());
        // Lose 30% of the bankroll.
        a.commit("g1", "nfl", 300.0);
        a.release("g1", 0.0);
        assert!(a.current_drawdown() >= 0.25);

        let fresh = a.allocate("g2", "nfl");
        assert_eq!(fresh.verdict, AllocationVerdict::DrawdownBrake);

        // A game that is already open may still act, so a position can be
        // exited rather than being stranded by the brake.
        a.commit("g3", "nfl", 10.0);
        let existing = a.allocate("g3", "nfl");
        assert_ne!(existing.verdict, AllocationVerdict::DrawdownBrake);
    }

    #[test]
    fn peak_bankroll_tracks_the_high_water_mark() {
        let mut a = Allocator::new(1000.0, limits());
        a.commit("g1", "nfl", 100.0);
        a.release("g1", 200.0); // profit
        assert!((a.bankroll() - 1100.0).abs() < 1e-9);
        assert!((a.peak_bankroll() - 1100.0).abs() < 1e-9);

        a.commit("g2", "nfl", 100.0);
        a.release("g2", 0.0); // loss
        assert!((a.bankroll() - 1000.0).abs() < 1e-9);
        assert!(
            (a.peak_bankroll() - 1100.0).abs() < 1e-9,
            "peak must not fall with the bankroll"
        );
        assert!((a.current_drawdown() - 100.0 / 1100.0).abs() < 1e-9);
    }

    #[test]
    fn a_zero_bankroll_refuses_everything() {
        let a = Allocator::new(0.0, limits());
        assert_eq!(a.allocate("g1", "nfl").verdict, AllocationVerdict::NoCash);
    }

    #[test]
    fn closing_a_game_frees_its_slot() {
        let mut a = Allocator::new(10_000.0, limits());
        for i in 0..3 {
            a.commit(&format!("g{i}"), "nfl", 10.0);
        }
        assert_eq!(a.allocate("new", "nfl").verdict, AllocationVerdict::ConcurrencyLimit);

        a.close_game("g0", 12.0);
        assert_eq!(a.open_games(), 2);
        assert_ne!(a.allocate("new", "nfl").verdict, AllocationVerdict::ConcurrencyLimit);
    }

    #[test]
    fn exposure_accounting_is_consistent() {
        let mut a = Allocator::new(1000.0, limits());
        a.commit("g1", "nfl", 40.0);
        a.commit("g2", "epl", 60.0);

        assert!((a.total_exposure() - 100.0).abs() < 1e-9);
        assert!((a.league_exposure("nfl") - 40.0).abs() < 1e-9);
        assert!((a.league_exposure("epl") - 60.0).abs() < 1e-9);
        assert!((a.bankroll() - 900.0).abs() < 1e-9);
    }
}
