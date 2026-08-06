//! Per-market economics: fees, tick size, order-size limits.
//!
//! These were global constants in `defaults.rs`, which is correct only for the
//! crypto up/down markets. Sports markets have a different fee schedule and
//! their books carry a per-market `tick_size` / `min_order_size` in the CLOB
//! `/book` response. Rounding a sports price to 2dp because the crypto market
//! uses 2dp throws away real edge, and applying the crypto fee curve to a
//! sports fill silently corrupts every backtest PnL.
//!
//! `MarketEconomics::crypto()` reproduces the previous global constants exactly,
//! so existing crypto behaviour is unchanged.

use crate::defaults::{
    CLOB_MIN_ORDER_TOKENS, CRYPTO_FEE_EXPONENT, CRYPTO_FEE_RATE, MAX_TOKEN_PRICE, PRICE_DECIMALS,
    TOKEN_DECIMALS,
};

#[derive(Debug, Clone, PartialEq)]
pub struct MarketEconomics {
    /// Taker fee rate coefficient.
    pub fee_rate: f64,
    /// Exponent applied to `price * (1 - price)` in the fee curve.
    pub fee_exponent: i32,
    /// Minimum price increment (e.g. 0.01 or 0.001).
    pub tick_size: f64,
    /// Minimum order size in tokens.
    pub min_order_tokens: f64,
    /// Highest price we are willing to pay / that the venue accepts.
    pub max_token_price: f64,
    /// Decimal places for token quantities.
    pub token_decimals: u32,
}

impl MarketEconomics {
    /// The crypto up/down schedule — identical to the former global constants.
    pub fn crypto() -> Self {
        Self {
            fee_rate: CRYPTO_FEE_RATE,
            fee_exponent: CRYPTO_FEE_EXPONENT,
            tick_size: 10f64.powi(-(PRICE_DECIMALS as i32)),
            min_order_tokens: CLOB_MIN_ORDER_TOKENS,
            max_token_price: MAX_TOKEN_PRICE,
            token_decimals: TOKEN_DECIMALS,
        }
    }

    /// Sports markets.
    ///
    /// Polymarket has historically run sports markets without the crypto
    /// up/down taker fee. `fee_rate` is therefore 0 by default — but this is
    /// exactly the kind of assumption that silently corrupts PnL, so it must be
    /// confirmed against a real fill before going live, and overridden per
    /// league in `markets.rs` if the venue changes it.
    pub fn sports() -> Self {
        Self {
            fee_rate: 0.0,
            fee_exponent: CRYPTO_FEE_EXPONENT,
            tick_size: 0.01,
            min_order_tokens: CLOB_MIN_ORDER_TOKENS,
            max_token_price: MAX_TOKEN_PRICE,
            token_decimals: TOKEN_DECIMALS,
        }
    }

    /// Number of decimal places implied by `tick_size` (e.g. 0.01 -> 2).
    pub fn price_decimals(&self) -> u32 {
        if self.tick_size <= 0.0 {
            return PRICE_DECIMALS;
        }
        let d = -(self.tick_size.log10());
        let rounded = d.round();
        if (d - rounded).abs() < 1e-9 && rounded >= 0.0 {
            rounded as u32
        } else {
            PRICE_DECIMALS
        }
    }

    /// Round a price to the market's tick grid.
    pub fn round_price(&self, price: f64) -> f64 {
        if self.tick_size <= 0.0 {
            return price;
        }
        (price / self.tick_size).round() * self.tick_size
    }

    /// Taker fee for a fill. Mirrors `defaults::compute_taker_fee` but with
    /// per-market coefficients.
    pub fn taker_fee(&self, price: f64, tokens: f64, enabled: bool) -> f64 {
        if !enabled
            || self.fee_rate == 0.0
            || price <= 0.0
            || price >= 1.0
            || tokens <= 0.0
        {
            return 0.0;
        }
        tokens * price * self.fee_rate * (price * (1.0 - price)).powi(self.fee_exponent)
    }

    /// Apply venue limits reported by the CLOB `/book` response.
    pub fn with_book_limits(mut self, tick_size: Option<f64>, min_order_size: Option<f64>) -> Self {
        if let Some(t) = tick_size {
            if t > 0.0 {
                self.tick_size = t;
            }
        }
        if let Some(m) = min_order_size {
            if m > 0.0 {
                self.min_order_tokens = m;
            }
        }
        self
    }
}

impl Default for MarketEconomics {
    fn default() -> Self {
        Self::crypto()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::defaults::compute_taker_fee;

    #[test]
    fn crypto_economics_match_the_former_globals() {
        let e = MarketEconomics::crypto();
        assert_eq!(e.price_decimals(), PRICE_DECIMALS);
        assert_eq!(e.min_order_tokens, CLOB_MIN_ORDER_TOKENS);
        assert_eq!(e.max_token_price, MAX_TOKEN_PRICE);

        // The fee curve must be bit-for-bit what defaults::compute_taker_fee
        // produced, otherwise every historical backtest silently shifts.
        for &(price, tokens) in &[
            (0.5, 100.0),
            (0.9, 100.0),
            (0.05, 33.0),
            (0.99, 7.0),
            (0.01, 1000.0),
        ] {
            assert_eq!(
                e.taker_fee(price, tokens, true),
                compute_taker_fee(price, tokens, true),
                "fee mismatch at price={price} tokens={tokens}",
            );
        }
        assert_eq!(e.taker_fee(0.5, 100.0, false), 0.0);
    }

    #[test]
    fn sports_economics_charge_no_taker_fee_by_default() {
        let e = MarketEconomics::sports();
        assert_eq!(e.taker_fee(0.5, 100.0, true), 0.0);
    }

    #[test]
    fn price_decimals_derived_from_tick_size() {
        let mut e = MarketEconomics::crypto();
        assert_eq!(e.price_decimals(), 2);
        e.tick_size = 0.001;
        assert_eq!(e.price_decimals(), 3);
        e.tick_size = 0.1;
        assert_eq!(e.price_decimals(), 1);
    }

    #[test]
    fn round_price_snaps_to_tick_grid() {
        let mut e = MarketEconomics::crypto();
        assert!((e.round_price(0.5149) - 0.51).abs() < 1e-9);
        e.tick_size = 0.001;
        assert!((e.round_price(0.51449) - 0.514).abs() < 1e-9);
    }

    #[test]
    fn book_limits_override_defaults_but_ignore_garbage() {
        let e = MarketEconomics::sports().with_book_limits(Some(0.001), Some(15.0));
        assert_eq!(e.tick_size, 0.001);
        assert_eq!(e.min_order_tokens, 15.0);

        let e2 = MarketEconomics::sports().with_book_limits(Some(0.0), None);
        assert_eq!(e2.tick_size, 0.01, "zero tick size must not be adopted");
    }
}
