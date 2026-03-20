//! DualHold — Buy both Up and Down tokens when their combined price plus
//! fees is below $1, guaranteeing profit at resolution.

use crate::defaults::*;
use crate::strategy::*;
use crate::trader::*;
use std::collections::HashMap;

pub struct DualHold {
    // Fixed
    pub no_new_after: f64,
    pub cancel_after_s: f64,
    // State
    up_order_pending: bool,
    down_order_pending: bool,
    up_limit_price: f64,     // price we placed the up limit order at
    initial_cash: f64,       // cash at session start
    tick_logs: Vec<String>,
}

impl Default for DualHold {
    fn default() -> Self {
        Self {
            no_new_after: 0.90,
            cancel_after_s: 10.0,
            up_order_pending: false,
            down_order_pending: false,
            up_limit_price: 0.0,
            initial_cash: 0.0,
            tick_logs: Vec::new(),
        }
    }
}

impl DualHold {
    fn reset_state(&mut self) {
        self.up_order_pending = false;
        self.down_order_pending = false;
        self.up_limit_price = 0.0;
        self.initial_cash = 0.0;
    }

    /// Per-token fee for buying at `price`.
    fn per_token_fee(price: f64) -> f64 {
        price * CRYPTO_FEE_RATE * (price * (1.0 - price)).powi(CRYPTO_FEE_EXPONENT)
    }

    /// Net profit per token pair after fees. Positive = guaranteed profit.
    fn edge(up_price: f64, down_price: f64) -> f64 {
        1.0 - up_price - down_price
            - Self::per_token_fee(up_price)
            - Self::per_token_fee(down_price)
    }

    fn has_pending_order(orders: &[OpenOrder], token: &str) -> bool {
        orders.iter().any(|o| o.token == token && o.side.starts_with("buy"))
    }
}

impl Strategy for DualHold {
    fn name(&self) -> &str {
        "DualHold"
    }
    fn version(&self) -> &str {
        "3"
    }
    fn requires_full_session(&self) -> bool {
        false
    }

    fn on_session_start(&mut self, _metadata: &SessionMetadata) {
        self.reset_state();
    }

    fn on_tick(&mut self, ctx: &TickContext) -> TradeAction {
        // Capture initial cash on first tick
        if self.initial_cash == 0.0 {
            self.initial_cash = ctx.position.cash;
        }

        let up_mid = match ctx.sample.up_token_midpoint {
            Some(v) if v > 0.0 => v,
            _ => return hold(),
        };
        let down_mid = match ctx.sample.down_token_midpoint {
            Some(v) if v > 0.0 => v,
            _ => return hold(),
        };

        // Track fill status from open orders and position
        let up_pending = Self::has_pending_order(&ctx.open_orders, "up");
        let down_pending = Self::has_pending_order(&ctx.open_orders, "down");
        let up_filled = ctx.position.up_tokens > 0.0;
        let down_filled = ctx.position.down_tokens > 0.0;

        // If up order was pending but is gone and no tokens — it expired, reset
        if self.up_order_pending && !up_pending && !up_filled {
            self.up_order_pending = false;
            self.up_limit_price = 0.0;
        }
        if self.down_order_pending && !down_pending && !down_filled {
            self.down_order_pending = false;
        }

        // Both sides filled — hold until resolution
        if up_filled && down_filled {
            return hold();
        }

        // Waiting for a pending order to fill
        if up_pending || down_pending {
            return hold();
        }

        // Don't enter new positions too late
        if ctx.elapsed_fraction >= self.no_new_after {
            return hold();
        }

        // --- First leg: buy Up ---
        if !up_filled && !down_filled && !self.up_order_pending {
            let edge = Self::edge(up_mid, down_mid);
            if edge <= 0.0 {
                return hold();
            }

            let total_price = up_mid + down_mid;
            let up_dollars = ctx.position.cash * (up_mid / total_price);

            self.up_order_pending = true;
            self.up_limit_price = up_mid;

            self.tick_logs.push(format!(
                "LEG1 buy_up @{:.3} down={:.3} edge={:.4}",
                up_mid, down_mid, edge
            ));

            return TradeAction::buy("buy_up", up_dollars, up_mid, Some(self.cancel_after_s));
        }

        // --- Second leg: buy Down ---
        if up_filled && !down_filled && !self.down_order_pending {
            // Use the actual price we paid for up (our limit price) to check
            // whether buying down at current price is still profitable.
            let up_cost = self.up_limit_price;
            let edge = Self::edge(up_cost, down_mid);
            if edge <= 0.0 {
                self.tick_logs.push(format!(
                    "LEG2 SKIP no edge: up_cost={:.3} down={:.3} edge={:.4}",
                    up_cost, down_mid, edge
                ));
                return hold();
            }

            let actual_up_tokens = ctx.position.up_tokens;
            let dollars = (actual_up_tokens * down_mid).min(ctx.position.cash);
            if dollars < CLOB_MIN_ORDER_TOKENS * down_mid {
                return hold();
            }

            self.down_order_pending = true;

            self.tick_logs.push(format!(
                "LEG2 buy_down @{:.3} tokens={:.1} edge={:.4}",
                down_mid, actual_up_tokens, edge
            ));

            return TradeAction::buy("buy_down", dollars, down_mid, Some(self.cancel_after_s));
        }

        hold()
    }

    fn drain_logs(&mut self) -> Vec<String> {
        std::mem::take(&mut self.tick_logs)
    }

    fn param_defs(&self) -> HashMap<String, Param> {
        let mut m = HashMap::new();
        m.insert("no_new_after".into(), Param::fixed_float(0.90));
        m.insert("cancel_after_s".into(), Param::fixed_float(10.0));
        m
    }

    fn params(&self) -> HashMap<String, ParamValue> {
        HashMap::new()
    }

    fn with_params(&self, params: &HashMap<String, ParamValue>) -> Box<dyn Strategy> {
        let mut s = DualHold::default();
        for (k, v) in params {
            match k.as_str() {
                "no_new_after" => {
                    if let Some(f) = v.as_f64() {
                        s.no_new_after = f;
                    }
                }
                "cancel_after_s" => {
                    if let Some(f) = v.as_f64() {
                        s.cancel_after_s = f;
                    }
                }
                _ => {}
            }
        }
        Box::new(s)
    }
}
