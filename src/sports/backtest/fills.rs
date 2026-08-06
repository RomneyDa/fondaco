//! Depth-aware fill simulation.
//!
//! `SimExecutor::check_fills` fills a limit order when the **midpoint** crosses
//! the order price by a fixed margin. That is a defensible approximation for a
//! crypto up/down book where `best_bid = 0.01` and `best_ask = 0.99` — there is
//! no real depth to model, and CLAUDE.md's "edge comes from picking direction,
//! not better fills" holds.
//!
//! It does not transfer to sports. Sports books have genuine depth and a real
//! spread, so a midpoint-crossing model both invents fills that would never
//! happen (crossing the mid says nothing about resting size) and misses fills
//! that would (a marketable order takes the touch immediately). Either error
//! silently biases every backtest.
//!
//! This module walks the recorded book instead:
//!   - a **marketable** order consumes levels from the opposite side until
//!     filled or exhausted, producing a size-weighted average price
//!   - a **resting** order fills only when the opposite side trades through its
//!     price, and is capped by the size available there
//!
//! It is only as good as the recorded depth, which is why `LegBook` captures
//! levels rather than just the touch — that cannot be backfilled later.

use crate::sports::models::LegBook;

/// Outcome of attempting to fill an order against a book.
#[derive(Debug, Clone, PartialEq)]
pub struct FillResult {
    /// Tokens actually filled.
    pub tokens: f64,
    /// Size-weighted average price paid or received.
    pub avg_price: f64,
    /// Whether the whole requested size was filled.
    pub complete: bool,
    /// Price impact: how far the average execution is from the touch.
    pub slippage: f64,
}

impl FillResult {
    pub fn none() -> Self {
        Self {
            tokens: 0.0,
            avg_price: 0.0,
            complete: false,
            slippage: 0.0,
        }
    }

    pub fn is_empty(&self) -> bool {
        self.tokens <= 0.0
    }
}

/// How much of the visible book we are willing to assume is ours.
///
/// Taking 100% of every level assumes no competition and no latency, which
/// flatters a backtest. This caps participation at a fraction of displayed
/// size per level.
pub const DEFAULT_PARTICIPATION: f64 = 0.5;

#[derive(Debug, Clone)]
pub struct FillConfig {
    /// Fraction of each displayed level we may consume.
    pub participation: f64,
    /// Whether a resting order at exactly the touch is assumed to fill.
    ///
    /// Defaults to false: joining the touch puts us at the back of the queue,
    /// and assuming a fill there is one of the easiest ways to fabricate edge.
    pub fill_at_touch: bool,
}

impl Default for FillConfig {
    fn default() -> Self {
        Self {
            participation: DEFAULT_PARTICIPATION,
            fill_at_touch: false,
        }
    }
}

/// Simulate a **marketable buy**: cross the spread and consume asks.
pub fn fill_market_buy(book: &LegBook, size: f64, cfg: &FillConfig) -> FillResult {
    if size <= 0.0 || book.ask_levels.is_empty() {
        // With no recorded depth we cannot honestly claim a fill. Returning
        // nothing is the conservative choice — inventing one at the touch would
        // silently assume unlimited liquidity.
        return FillResult::none();
    }
    walk(&book.ask_levels, size, cfg, true)
}

/// Simulate a **marketable sell**: cross the spread and consume bids.
pub fn fill_market_sell(book: &LegBook, size: f64, cfg: &FillConfig) -> FillResult {
    if size <= 0.0 || book.bid_levels.is_empty() {
        return FillResult::none();
    }
    walk(&book.bid_levels, size, cfg, false)
}

fn walk(levels: &[(f64, f64)], size: f64, cfg: &FillConfig, is_buy: bool) -> FillResult {
    let touch = levels[0].0;
    let mut remaining = size;
    let mut cost = 0.0;
    let mut filled = 0.0;

    for (price, available) in levels {
        if remaining <= 0.0 {
            break;
        }
        let usable = available * cfg.participation.clamp(0.0, 1.0);
        let take = remaining.min(usable);
        if take <= 0.0 {
            continue;
        }
        cost += take * price;
        filled += take;
        remaining -= take;
    }

    if filled <= 0.0 {
        return FillResult::none();
    }

    let avg = cost / filled;
    FillResult {
        tokens: filled,
        avg_price: avg,
        complete: remaining <= 1e-9,
        slippage: if is_buy { avg - touch } else { touch - avg },
    }
}

/// Simulate a **resting limit buy** at `limit_price` against a later book.
///
/// A resting bid fills when the market trades down to it — approximated by the
/// best ask reaching or crossing our price. Size is capped by what is displayed
/// there, so a large resting order does not fill instantly off a thin book.
pub fn fill_resting_buy(
    book: &LegBook,
    limit_price: f64,
    size: f64,
    cfg: &FillConfig,
) -> FillResult {
    let best_ask = match book.best_ask {
        Some(a) => a,
        None => return FillResult::none(),
    };

    let crossed = if cfg.fill_at_touch {
        best_ask <= limit_price
    } else {
        best_ask < limit_price
    };
    if !crossed {
        return FillResult::none();
    }

    // Available size at or below our limit.
    let available: f64 = book
        .ask_levels
        .iter()
        .filter(|(p, _)| *p <= limit_price)
        .map(|(_, s)| s * cfg.participation.clamp(0.0, 1.0))
        .sum();

    // With no recorded depth, fall back to the requested size — the price
    // condition has been met and refusing entirely would be its own bias.
    let capacity = if book.ask_levels.is_empty() {
        size
    } else {
        available
    };
    let filled = size.min(capacity);
    if filled <= 0.0 {
        return FillResult::none();
    }

    // A resting order fills at its own price, not the market's.
    FillResult {
        tokens: filled,
        avg_price: limit_price,
        complete: (filled - size).abs() <= 1e-9,
        slippage: 0.0,
    }
}

/// Simulate a **resting limit sell** at `limit_price`.
pub fn fill_resting_sell(
    book: &LegBook,
    limit_price: f64,
    size: f64,
    cfg: &FillConfig,
) -> FillResult {
    let best_bid = match book.best_bid {
        Some(b) => b,
        None => return FillResult::none(),
    };

    let crossed = if cfg.fill_at_touch {
        best_bid >= limit_price
    } else {
        best_bid > limit_price
    };
    if !crossed {
        return FillResult::none();
    }

    let available: f64 = book
        .bid_levels
        .iter()
        .filter(|(p, _)| *p >= limit_price)
        .map(|(_, s)| s * cfg.participation.clamp(0.0, 1.0))
        .sum();

    let capacity = if book.bid_levels.is_empty() {
        size
    } else {
        available
    };
    let filled = size.min(capacity);
    if filled <= 0.0 {
        return FillResult::none();
    }

    FillResult {
        tokens: filled,
        avg_price: limit_price,
        complete: (filled - size).abs() <= 1e-9,
        slippage: 0.0,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn book() -> LegBook {
        LegBook {
            midpoint: Some(0.595),
            best_bid: Some(0.58),
            best_ask: Some(0.61),
            bid_levels: vec![(0.58, 100.0), (0.57, 200.0), (0.55, 500.0)],
            ask_levels: vec![(0.61, 100.0), (0.62, 200.0), (0.64, 500.0)],
        }
    }

    fn full_participation() -> FillConfig {
        FillConfig {
            participation: 1.0,
            fill_at_touch: false,
        }
    }

    #[test]
    fn a_small_marketable_buy_fills_at_the_touch() {
        let r = fill_market_buy(&book(), 50.0, &full_participation());
        assert!(r.complete);
        assert_eq!(r.tokens, 50.0);
        assert!((r.avg_price - 0.61).abs() < 1e-9);
        assert!(r.slippage.abs() < 1e-9);
    }

    #[test]
    fn a_large_marketable_buy_walks_the_book_and_pays_up() {
        // 250 tokens: 100 @ 0.61, 150 @ 0.62.
        let r = fill_market_buy(&book(), 250.0, &full_participation());
        assert!(r.complete);
        assert_eq!(r.tokens, 250.0);
        let expected = (100.0 * 0.61 + 150.0 * 0.62) / 250.0;
        assert!((r.avg_price - expected).abs() < 1e-9, "got {}", r.avg_price);
        assert!(
            r.slippage > 0.0,
            "walking the book must cost more than the touch"
        );
    }

    #[test]
    fn an_oversized_order_fills_partially_rather_than_wholly() {
        // Book holds 800 total; ask for 2000.
        let r = fill_market_buy(&book(), 2000.0, &full_participation());
        assert!(!r.complete, "must not invent liquidity beyond the book");
        assert!((r.tokens - 800.0).abs() < 1e-9);
    }

    #[test]
    fn participation_caps_how_much_of_each_level_we_take() {
        let cfg = FillConfig {
            participation: 0.5,
            fill_at_touch: false,
        };
        // Only half of each level is available: 50 + 100 + 250 = 400.
        let r = fill_market_buy(&book(), 2000.0, &cfg);
        assert!((r.tokens - 400.0).abs() < 1e-9);
    }

    #[test]
    fn a_marketable_sell_consumes_bids_downward() {
        // 250 tokens: 100 @ 0.58, 150 @ 0.57.
        let r = fill_market_sell(&book(), 250.0, &full_participation());
        let expected = (100.0 * 0.58 + 150.0 * 0.57) / 250.0;
        assert!((r.avg_price - expected).abs() < 1e-9);
        assert!(
            r.slippage > 0.0,
            "selling into depth must realise less than the touch"
        );
    }

    #[test]
    fn no_recorded_depth_yields_no_marketable_fill() {
        // This is the crucial difference from the crypto model: without depth
        // we refuse rather than assuming unlimited size at the touch.
        let thin = LegBook {
            midpoint: Some(0.5),
            best_bid: Some(0.49),
            best_ask: Some(0.51),
            bid_levels: vec![],
            ask_levels: vec![],
        };
        assert!(fill_market_buy(&thin, 10.0, &full_participation()).is_empty());
        assert!(fill_market_sell(&thin, 10.0, &full_participation()).is_empty());
    }

    #[test]
    fn a_resting_buy_does_not_fill_while_the_market_is_above_it() {
        let r = fill_resting_buy(&book(), 0.55, 50.0, &full_participation());
        assert!(r.is_empty(), "ask is 0.61, our bid is 0.55");
    }

    #[test]
    fn a_resting_buy_fills_at_its_own_price_when_crossed() {
        let mut b = book();
        b.best_ask = Some(0.59);
        b.ask_levels = vec![(0.59, 300.0)];

        let r = fill_resting_buy(&b, 0.62, 100.0, &full_participation());
        assert!(r.complete);
        assert_eq!(r.tokens, 100.0);
        assert!(
            (r.avg_price - 0.62).abs() < 1e-9,
            "a resting order fills at its own limit price"
        );
    }

    #[test]
    fn a_resting_buy_is_capped_by_displayed_size() {
        let mut b = book();
        b.best_ask = Some(0.59);
        b.ask_levels = vec![(0.59, 40.0)];

        let r = fill_resting_buy(&b, 0.62, 500.0, &full_participation());
        assert!(!r.complete);
        assert!((r.tokens - 40.0).abs() < 1e-9, "got {}", r.tokens);
    }

    #[test]
    fn joining_the_touch_does_not_fill_by_default() {
        let mut b = book();
        b.best_ask = Some(0.60);
        b.ask_levels = vec![(0.60, 100.0)];

        // Our bid exactly equals the ask: queue position means this is not a
        // free fill, and assuming otherwise fabricates edge.
        let r = fill_resting_buy(&b, 0.60, 50.0, &FillConfig::default());
        assert!(r.is_empty());

        let optimistic = FillConfig {
            participation: 1.0,
            fill_at_touch: true,
        };
        assert!(!fill_resting_buy(&b, 0.60, 50.0, &optimistic).is_empty());
    }

    #[test]
    fn a_resting_sell_mirrors_the_buy_logic() {
        let mut b = book();
        b.best_bid = Some(0.65);
        b.bid_levels = vec![(0.65, 80.0)];

        assert!(fill_resting_sell(&b, 0.70, 50.0, &full_participation()).is_empty());

        let r = fill_resting_sell(&b, 0.60, 200.0, &full_participation());
        assert!((r.tokens - 80.0).abs() < 1e-9);
        assert!((r.avg_price - 0.60).abs() < 1e-9);
    }

    #[test]
    fn a_book_with_no_touch_cannot_fill_a_resting_order() {
        let empty = LegBook::default();
        assert!(fill_resting_buy(&empty, 0.5, 10.0, &full_participation()).is_empty());
        assert!(fill_resting_sell(&empty, 0.5, 10.0, &full_participation()).is_empty());
    }

    #[test]
    fn zero_or_negative_size_never_fills() {
        assert!(fill_market_buy(&book(), 0.0, &full_participation()).is_empty());
        assert!(fill_market_buy(&book(), -5.0, &full_participation()).is_empty());
    }
}
