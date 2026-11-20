//! fondaco: Polymarket up/down trading system.
//!
//! Core library for backtesting and live trading of crypto prediction markets.

pub mod defaults;
pub mod economics;
pub mod markets;
pub mod math_utils;
pub mod models;
pub mod volatility;
pub mod trader;
pub mod strategy;
pub mod strategies;
pub mod session_cache;
pub mod engine;
pub mod rtds;
pub mod clob_order;
pub mod control_guard;
pub mod clob_ws;
pub mod price_fetcher;
pub mod market_discovery;
pub mod session_io;
pub mod tick_broadcast;
pub mod notifications;
pub mod sports;
pub mod window_ops;
