//! Order construction for the CLOB bridge.
//!
//! This is the last hop before real money moves, and it used to live inline in
//! `bot_worker.rs` where it could not be tested: the surrounding function does
//! HTTP, so nothing exercised the routing or the wire encoding. The golden
//! backtest guards `SimExecutor`, which is a *different* code path entirely.
//!
//! The pure part — routing a side to a token, sizing it, and encoding the
//! request body — is extracted here so it can be snapshot-tested. The impure
//! parts (on-chain balance reconciliation, the POST itself) stay in the
//! executor.
//!
//! Both the crypto and sports workers build orders through this one function,
//! so there is a single definition of what gets sent to the venue.

use serde::Serialize;

use crate::economics::MarketEconomics;
use crate::trader::{Position, Side, TradeAction};

/// Why an order could not be built. Every variant is a refusal to trade, never
/// a guess.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum OrderError {
    /// Hold, or a non-positive amount.
    NothingToDo,
    /// The side string did not parse to a leg. Deliberately fatal rather than
    /// defaulting to leg 0 — mis-routing sends real money to the wrong token.
    UnroutableSide,
    /// No token id for the routed leg.
    MissingTokenId,
    /// No limit price. FAK is not supported: CLAUDE.md requires limit orders
    /// with an expiration.
    NoPrice,
    /// Price outside `(0, 1)` after rounding to the market tick.
    InvalidPrice,
    /// Below the venue's minimum order size, or no cash / no tokens.
    BelowMinimumSize,
}

impl OrderError {
    pub fn as_str(&self) -> &'static str {
        match self {
            OrderError::NothingToDo => "nothing to do",
            OrderError::UnroutableSide => "unroutable side",
            OrderError::MissingTokenId => "missing token_id",
            OrderError::NoPrice => "FAK not supported without book data",
            OrderError::InvalidPrice => "invalid price",
            OrderError::BelowMinimumSize => "insufficient size",
        }
    }
}

/// A fully-formed CLOB bridge request, plus what it means for the position.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct BuiltOrder {
    pub token_id: String,
    /// `"BUY"` or `"SELL"`.
    pub side: &'static str,
    /// Price, formatted to the market's tick precision.
    pub price: String,
    /// Size, formatted to the market's token precision.
    pub size: String,
    pub order_type: &'static str,
    pub expiration_seconds: u64,

    /// Numeric values, for position bookkeeping after a successful post.
    #[serde(skip)]
    pub tokens: f64,
    #[serde(skip)]
    pub price_value: f64,
    #[serde(skip)]
    pub leg: usize,
    #[serde(skip)]
    pub is_sell: bool,
}

impl BuiltOrder {
    /// The JSON body posted to `/create_and_post_order`.
    pub fn body(&self) -> serde_json::Value {
        serde_json::json!({
            "token_id": self.token_id,
            "side": self.side,
            "price": self.price,
            "size": self.size,
            "order_type": self.order_type,
            "expiration_seconds": self.expiration_seconds,
        })
    }
}

/// Build the order for a trade action.
///
/// `token_for_leg` resolves a leg index to its token id, which keeps this
/// function independent of whether the caller holds a crypto `SessionMetadata`
/// or a sports `MarketGroup`.
pub fn build_order(
    action: &TradeAction,
    position: &Position,
    econ: &MarketEconomics,
    default_expiration: f64,
    token_for_leg: impl Fn(usize) -> Option<String>,
) -> Result<BuiltOrder, OrderError> {
    if action.is_hold() || action.amount <= 0.0 {
        return Err(OrderError::NothingToDo);
    }

    let parsed = action.parsed_side();
    let leg = match parsed {
        Side::Buy(l) | Side::Sell(l) => l as usize,
        _ => return Err(OrderError::UnroutableSide),
    };

    let token_id = match token_for_leg(leg) {
        Some(t) if !t.is_empty() => t,
        _ => return Err(OrderError::MissingTokenId),
    };

    let price = action.price.ok_or(OrderError::NoPrice)?;
    if !(price > 0.0 && price < 1.0) {
        return Err(OrderError::InvalidPrice);
    }

    let (tokens, _cost) =
        position.compute_trade_size(&action.side, Some(price), action.amount, econ);
    if tokens <= 0.0 || tokens < econ.min_order_tokens {
        return Err(OrderError::BelowMinimumSize);
    }

    let expiration = action.expiration_seconds.unwrap_or(default_expiration);

    Ok(BuiltOrder {
        token_id,
        side: if parsed.is_buy() { "BUY" } else { "SELL" },
        // Precision follows the market's tick size, not a hardcoded 2dp —
        // sending 2dp into a market that ticks at 0.001 rounds away real edge.
        price: format!("{:.*}", econ.price_decimals() as usize, price),
        size: format!("{:.*}", econ.token_decimals as usize, tokens),
        order_type: "GTD",
        expiration_seconds: expiration as u64,
        tokens,
        price_value: price,
        leg,
        is_sell: parsed.is_sell(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::defaults::DEFAULT_EXPIRATION_SECONDS;
    use crate::trader::LegSpec;

    fn crypto_legs() -> Vec<LegSpec> {
        LegSpec::crypto_pair("tok-up", "tok-down")
    }

    fn resolver(legs: Vec<LegSpec>) -> impl Fn(usize) -> Option<String> {
        move |i| legs.get(i).map(|l| l.token_id.clone())
    }

    fn build(
        action: &TradeAction,
        position: &Position,
        econ: &MarketEconomics,
    ) -> Result<BuiltOrder, OrderError> {
        build_order(
            action,
            position,
            econ,
            DEFAULT_EXPIRATION_SECONDS,
            resolver(crypto_legs()),
        )
    }

    // -----------------------------------------------------------------
    // Wire format — these are the bytes that reach the venue.
    // -----------------------------------------------------------------

    #[test]
    fn crypto_buy_up_encodes_exactly_as_before() {
        let p = Position::new(100.0);
        let a = TradeAction::buy("buy_up", 100.0, 0.57, None);
        let o = build(&a, &p, &MarketEconomics::crypto()).unwrap();

        assert_eq!(o.token_id, "tok-up");
        assert_eq!(o.side, "BUY");
        assert_eq!(o.price, "0.57", "crypto ticks at 0.01 -> 2dp");
        assert_eq!(o.size, "175.4386", "token precision is 4dp");
        assert_eq!(o.order_type, "GTD");
        assert_eq!(o.expiration_seconds, 120);

        assert_eq!(
            o.body(),
            serde_json::json!({
                "token_id": "tok-up",
                "side": "BUY",
                "price": "0.57",
                "size": "175.4386",
                "order_type": "GTD",
                "expiration_seconds": 120,
            })
        );
    }

    #[test]
    fn crypto_sell_down_routes_to_the_down_token() {
        let mut p = Position::new(0.0);
        p.set_leg(1, 40.0);
        let a = TradeAction::sell("sell_down", 40.0, 0.80);
        let o = build(&a, &p, &MarketEconomics::crypto()).unwrap();

        assert_eq!(o.token_id, "tok-down");
        assert_eq!(o.side, "SELL");
        assert_eq!(o.price, "0.80");
        assert_eq!(o.size, "40.0000");
        assert!(o.is_sell);
        assert_eq!(o.leg, 1);
    }

    #[test]
    fn every_crypto_side_routes_to_the_token_the_old_substring_test_chose() {
        // The pre-refactor code used `side.contains("up")`. These four cases
        // are the entire crypto surface; each must land on the same token.
        let cases = [
            ("buy_up", "tok-up", "BUY"),
            ("buy_down", "tok-down", "BUY"),
            ("sell_up", "tok-up", "SELL"),
            ("sell_down", "tok-down", "SELL"),
        ];
        for (side, want_token, want_side) in cases {
            let mut p = Position::new(100.0);
            p.set_leg(0, 50.0);
            p.set_leg(1, 50.0);
            let a = TradeAction {
                side: side.to_string(),
                amount: 30.0,
                price: Some(0.5),
                expiration_seconds: Some(DEFAULT_EXPIRATION_SECONDS),
                cancel_after_s: None,
            };
            let o = build(&a, &p, &MarketEconomics::crypto())
                .unwrap_or_else(|e| panic!("{side} failed: {e:?}"));
            assert_eq!(o.token_id, want_token, "{side}");
            assert_eq!(o.side, want_side, "{side}");
        }
    }

    #[test]
    fn sports_sides_route_through_the_same_builder() {
        let legs = vec![
            LegSpec::new("home", "Chiefs", "tok-home", "c"),
            LegSpec::new("away", "Eagles", "tok-away", "c"),
            LegSpec::new("draw", "Draw", "tok-draw", "c"),
        ];
        for (side, want) in [
            ("buy_home", "tok-home"),
            ("buy_away", "tok-away"),
            ("buy_draw", "tok-draw"),
        ] {
            let p = Position::new(100.0);
            let a = TradeAction::buy(side, 50.0, 0.5, None);
            let o = build_order(
                &a,
                &p,
                &MarketEconomics::sports(),
                DEFAULT_EXPIRATION_SECONDS,
                resolver(legs.clone()),
            )
            .unwrap_or_else(|e| panic!("{side} failed: {e:?}"));
            assert_eq!(o.token_id, want, "{side}");
        }
    }

    #[test]
    fn a_finer_tick_market_gets_more_price_precision() {
        // The bug this guards: a hardcoded 2dp silently rounded away edge on
        // any market that ticks finer than 0.01.
        let mut econ = MarketEconomics::sports();
        econ.tick_size = 0.001;
        let p = Position::new(100.0);
        let a = TradeAction::buy("buy_home", 100.0, 0.5145, None);
        let o = build_order(
            &a,
            &p,
            &econ,
            DEFAULT_EXPIRATION_SECONDS,
            resolver(vec![LegSpec::new("home", "H", "tok-home", "c")]),
        )
        .unwrap();
        assert_eq!(o.price, "0.514", "0.001 tick -> 3dp, not 0.51");
    }

    // -----------------------------------------------------------------
    // Refusals — every one must decline rather than guess.
    // -----------------------------------------------------------------

    #[test]
    fn an_unparseable_side_is_refused_not_routed_to_leg_zero() {
        // Under the old `contains("up")` test this would have bought the up
        // token with real money.
        let p = Position::new(100.0);
        for bad in ["buy_upside", "buy_uptown", "nonsense", "trade_up"] {
            let a = TradeAction::buy(bad, 100.0, 0.5, None);
            assert_eq!(
                build(&a, &p, &MarketEconomics::crypto()),
                Err(OrderError::UnroutableSide),
                "{bad} must not route"
            );
        }
    }

    #[test]
    fn a_hold_or_zero_amount_builds_nothing() {
        let p = Position::new(100.0);
        assert_eq!(
            build(&crate::trader::hold(), &p, &MarketEconomics::crypto()),
            Err(OrderError::NothingToDo)
        );
        let a = TradeAction::buy("buy_up", 0.0, 0.5, None);
        assert_eq!(
            build(&a, &p, &MarketEconomics::crypto()),
            Err(OrderError::NothingToDo)
        );
    }

    #[test]
    fn a_missing_price_is_refused_because_fak_is_not_supported() {
        let p = Position::new(100.0);
        let a = TradeAction {
            side: "buy_up".into(),
            amount: 100.0,
            price: None,
            expiration_seconds: None,
            cancel_after_s: None,
        };
        assert_eq!(
            build(&a, &p, &MarketEconomics::crypto()),
            Err(OrderError::NoPrice)
        );
    }

    #[test]
    fn prices_outside_the_open_interval_are_refused() {
        let p = Position::new(100.0);
        for bad in [0.0, 1.0, -0.5, 1.5] {
            let a = TradeAction::buy("buy_up", 100.0, bad, None);
            assert_eq!(
                build(&a, &p, &MarketEconomics::crypto()),
                Err(OrderError::InvalidPrice),
                "price {bad} must be refused"
            );
        }
    }

    #[test]
    fn a_leg_with_no_token_id_is_refused() {
        let p = Position::new(100.0);
        let a = TradeAction::buy("buy_draw", 100.0, 0.5, None);
        // Crypto has only two legs, so leg 2 has no token.
        assert_eq!(
            build(&a, &p, &MarketEconomics::crypto()),
            Err(OrderError::MissingTokenId)
        );
    }

    #[test]
    fn an_empty_token_id_is_refused_rather_than_posted() {
        let p = Position::new(100.0);
        let a = TradeAction::buy("buy_up", 100.0, 0.5, None);
        assert_eq!(
            build_order(
                &a,
                &p,
                &MarketEconomics::crypto(),
                DEFAULT_EXPIRATION_SECONDS,
                |_| Some(String::new()),
            ),
            Err(OrderError::MissingTokenId)
        );
    }

    #[test]
    fn a_size_below_the_venue_minimum_is_refused() {
        // $1 at 0.50 is 2 tokens, under the 5-token CLOB minimum.
        let p = Position::new(1.0);
        let a = TradeAction::buy("buy_up", 1.0, 0.50, None);
        assert_eq!(
            build(&a, &p, &MarketEconomics::crypto()),
            Err(OrderError::BelowMinimumSize)
        );
    }

    #[test]
    fn selling_more_than_is_held_is_capped_not_refused() {
        let mut p = Position::new(0.0);
        p.set_leg(0, 12.0);
        let a = TradeAction::sell("sell_up", 500.0, 0.60);
        let o = build(&a, &p, &MarketEconomics::crypto()).unwrap();
        assert_eq!(o.size, "12.0000", "capped at the holding");
    }

    #[test]
    fn selling_a_leg_that_is_not_held_is_refused() {
        let p = Position::new(100.0);
        let a = TradeAction::sell("sell_up", 10.0, 0.60);
        assert_eq!(
            build(&a, &p, &MarketEconomics::crypto()),
            Err(OrderError::BelowMinimumSize)
        );
    }

    #[test]
    fn a_buy_is_capped_by_available_cash() {
        let p = Position::new(10.0);
        let a = TradeAction::buy("buy_up", 1000.0, 0.50, None);
        let o = build(&a, &p, &MarketEconomics::crypto()).unwrap();
        // 10 / 0.50 = 20 tokens, not 2000.
        assert_eq!(o.size, "20.0000");
    }

    #[test]
    fn a_custom_expiration_is_honoured() {
        let p = Position::new(100.0);
        let a = TradeAction {
            side: "buy_up".into(),
            amount: 100.0,
            price: Some(0.5),
            expiration_seconds: Some(45.0),
            cancel_after_s: None,
        };
        let o = build(&a, &p, &MarketEconomics::crypto()).unwrap();
        assert_eq!(o.expiration_seconds, 45);
    }
}
