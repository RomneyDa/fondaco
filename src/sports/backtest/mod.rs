//! Sports backtesting.
//!
//! Three things differ fundamentally from the crypto backtester and each has
//! its own module:
//!
//! - `fills` — sports books have real depth, so the crypto
//!   "filled when the midpoint crosses my price" model does not transfer.
//! - `simulator` — games overlap, so a sequential per-session walk would let
//!   the same dollar fund thirteen simultaneous Sunday-afternoon bets.
//! - `metrics` — 272 NFL games a year means PnL Sharpe has almost no
//!   statistical power; calibration and closing line value do.

pub mod fills;
pub mod metrics;
pub mod simulator;

pub use metrics::{SportsEvaluation, TradeRecord};
pub use simulator::{AllocatorConfig, SimConfig, SimResult};
