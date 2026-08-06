//! Sports markets: discovery, live feeds, fair value, collection and storage.
//!
//! Sports fits the existing framework by treating **a game as a session**. What
//! is genuinely new, and what these modules exist to handle:
//!
//! 1. **Concurrency** — up to ~15 games run at once and are not aligned to a
//!    clock grid, versus one crypto session at a time.
//! 2. **Schedule-driven discovery** — there is no deterministic
//!    `prefix + floor(now/300)*300` slug; games must be found from the Gamma
//!    schedule and joined to the live results feed.
//! 3. **Adaptive cadence** — a 3.5 hour game sampled at the crypto rate of
//!    18 Hz would be a quarter of a million samples of almost entirely nothing.
//!    Edge appears in bursts around scoring events, so sampling follows the
//!    game rather than the clock.

pub mod allocator;
pub mod backtest;
pub mod book;
pub mod confirmation;
pub mod cadence;
pub mod discovery;
pub mod espn;
pub mod feed;
pub mod fair_value;
pub mod latency;
pub mod model_client;
pub mod models;
pub mod schedule;
pub mod storage;
pub mod strategies;
pub mod strategy;

pub use models::{
    GameSample, GameSessionMeta, GameState, GameStatus, League, LegBook, MarketGroup, Sport,
    SportsLeg, SportsMarketKind,
};
