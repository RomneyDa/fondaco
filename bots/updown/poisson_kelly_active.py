"""PoissonKellyActive — actively managed Poisson/Kelly strategy.

Like PoissonKelly but trades multiple times per session. Continuously
re-evaluates the Poisson model and:
- Sells when the edge decays (Kelly fraction for held side drops)
- Sells when the model flips (other side becomes favored)
- Re-enters after selling if new edge appears
- Can flip sides within a session

The key insight: PoissonKelly enters once and either stop-losses or holds
to resolution. But the Poisson model's estimate evolves tick-by-tick as new
jumps are observed. If the model says "you're on the wrong side now", why
wait for stop-loss? Sell immediately and possibly re-enter the other side.
"""

import math
from typing import Any, Dict, List, Optional

from core.defaults import CLOB_MIN_ORDER_TOKENS
from core.trader import HOLD, TradeAction, TickContext
from core.strategies.base import Strategy


def _norm_cdf(x):
    # type: (float) -> float
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


class PoissonKellyActive(Strategy):
    """Actively managed Poisson jump model + Kelly criterion.

    Params:
        jump_bps: minimum BTC move (bps) to count as a jump
        min_jumps: minimum total jumps before allowing any trade
        min_elapsed: don't trade before this elapsed fraction
        no_new_after: stop new entries after this elapsed fraction
        min_kelly: minimum Kelly fraction to enter
        max_kelly: cap Kelly fraction
        edge_min: minimum edge (p_model - price) to enter
        exit_kelly: sell when Kelly for held side drops below this
        min_hold_ticks: minimum ticks to hold before allowing exit
        stop_loss_pct: hard stop-loss on midpoint drop
    """

    name = "PoissonKellyActive"
    description = (
        "Actively managed Poisson jump model. Enters, exits, and re-enters "
        "multiple times per session based on evolving probability estimates."
    )
    version = "1"
    requires_full_session = True

    def __init__(
        self,
        jump_bps=0.30,
        min_jumps=8,
        min_elapsed=0.20,
        no_new_after=0.80,
        min_kelly=0.25,
        max_kelly=1.0,
        edge_min=0.05,
        exit_kelly=0.0,
        min_hold_ticks=30,
        stop_loss_pct=0.035,
    ):
        self.jump_bps = jump_bps
        self.min_jumps = min_jumps
        self.min_elapsed = min_elapsed
        self.no_new_after = no_new_after
        self.min_kelly = min_kelly
        self.max_kelly = max_kelly
        self.edge_min = edge_min
        self.exit_kelly = exit_kelly
        self.min_hold_ticks = min_hold_ticks
        self.stop_loss_pct = stop_loss_pct
        self._params = {
            "jump_bps": jump_bps,
            "min_jumps": min_jumps,
            "min_elapsed": min_elapsed,
            "no_new_after": no_new_after,
            "min_kelly": min_kelly,
            "max_kelly": max_kelly,
            "edge_min": edge_min,
            "exit_kelly": exit_kelly,
            "min_hold_ticks": min_hold_ticks,
            "stop_loss_pct": stop_loss_pct,
        }

        # Per-session state
        self._prev_btc = 0.0
        self._up_jumps = 0
        self._down_jumps = 0
        self._entry_tick = -1
        self._entry_midpoint = 0.0
        self._sell_attempted = False

    def on_session_start(self, metadata):
        # type: (Dict[str, Any]) -> None
        self._prev_btc = 0.0
        self._up_jumps = 0
        self._down_jumps = 0
        self._entry_tick = -1
        self._entry_midpoint = 0.0
        self._sell_attempted = False

    def _held_side(self, ctx):
        # type: (TickContext) -> Optional[str]
        if ctx.position.up_tokens > 0:
            return "up"
        if ctx.position.down_tokens > 0:
            return "down"
        return None

    def _compute_p_up(self, ctx):
        # type: (TickContext) -> Optional[float]
        btc = ctx.sample.get("btc_price_usd")
        strike = ctx.initial_btc_price
        if btc is None or strike <= 0:
            return None

        elapsed = ctx.elapsed_fraction
        if elapsed <= 0.01:
            return None

        remaining = 1.0 - elapsed
        if remaining <= 0.01:
            return None

        total_jumps = self._up_jumps + self._down_jumps
        if total_jumps < self.min_jumps:
            return None

        lambda_up = self._up_jumps / elapsed
        lambda_down = self._down_jumps / elapsed

        mu_up = lambda_up * remaining
        mu_down = lambda_down * remaining

        displacement_bps = (btc / strike - 1.0) * 10000.0
        net_current = displacement_bps / max(self.jump_bps, 0.01)

        skellam_mean = mu_up - mu_down
        skellam_var = mu_up + mu_down

        if skellam_var < 1e-10:
            if net_current > 0:
                return 1.0
            elif net_current < 0:
                return 0.0
            return 0.5

        z = (net_current + skellam_mean) / math.sqrt(skellam_var)
        return _norm_cdf(z)

    def _kelly_fraction(self, p, market_price):
        # type: (float, float) -> float
        if market_price >= 0.99 or market_price <= 0.01:
            return 0.0
        f = (p - market_price) / (1.0 - market_price)
        return max(0.0, min(f, self.max_kelly))

    def on_tick(self, ctx):
        # type: (TickContext) -> TradeAction
        btc = ctx.sample.get("btc_price_usd")
        up_mid = ctx.sample.get("up_token_midpoint")
        down_mid = ctx.sample.get("down_token_midpoint")

        if btc is None or up_mid is None or down_mid is None:
            return HOLD

        # --- Count jumps ---
        if self._prev_btc > 0:
            ret_bps = (btc / self._prev_btc - 1.0) * 10000.0
            if ret_bps >= self.jump_bps:
                self._up_jumps += 1
            elif ret_bps <= -self.jump_bps:
                self._down_jumps += 1
        self._prev_btc = btc

        # --- Compute model probability ---
        p_up = self._compute_p_up(ctx)

        # --- Manage existing position ---
        held = self._held_side(ctx)
        if held is not None:
            current_mid = up_mid if held == "up" else down_mid

            # Hard stop-loss
            if self._entry_midpoint > 0 and current_mid <= self._entry_midpoint * (1.0 - self.stop_loss_pct):
                if not self._sell_attempted:
                    self._sell_attempted = True
                    self.log("STOP-LOSS: mid={:.3f} entry={:.3f}".format(
                        current_mid, self._entry_midpoint))
                    sell_side = "sell_up" if held == "up" else "sell_down"
                    tokens = ctx.position.up_tokens if held == "up" else ctx.position.down_tokens
                    return TradeAction(side=sell_side, amount=tokens, price=current_mid)
                return HOLD

            # Need model for active management
            if p_up is None:
                return HOLD

            p_held = p_up if held == "up" else 1.0 - p_up
            kelly_held = self._kelly_fraction(p_held, current_mid)

            # Respect min hold time to avoid whipsaw
            ticks_held = ctx.tick_index - self._entry_tick
            if ticks_held < self.min_hold_ticks:
                return HOLD

            # Active exit: Kelly for our side has decayed below threshold
            if kelly_held < self.exit_kelly:
                self._sell_attempted = False  # reset for potential re-entry
                self._entry_tick = -1
                self._entry_midpoint = 0.0
                self.log("EXIT edge-decay: {} kelly={:.3f} p={:.3f} mid={:.3f}".format(
                    held, kelly_held, p_held, current_mid))
                sell_side = "sell_up" if held == "up" else "sell_down"
                tokens = ctx.position.up_tokens if held == "up" else ctx.position.down_tokens
                return TradeAction(side=sell_side, amount=tokens, price=current_mid)

            return HOLD

        # Reset sell tracking when flat
        self._sell_attempted = False

        # --- Entry logic ---
        if ctx.elapsed_fraction < self.min_elapsed:
            return HOLD
        if ctx.elapsed_fraction >= self.no_new_after:
            return HOLD
        if p_up is None:
            return HOLD

        p_down = 1.0 - p_up

        kelly_up = self._kelly_fraction(p_up, up_mid)
        kelly_down = self._kelly_fraction(p_down, down_mid)
        edge_up = p_up - up_mid
        edge_down = p_down - down_mid

        if kelly_up >= kelly_down and kelly_up >= self.min_kelly and edge_up >= self.edge_min:
            side = "buy_up"
            kelly = kelly_up
            midpoint = up_mid
            edge = edge_up
            p = p_up
        elif kelly_down >= self.min_kelly and edge_down >= self.edge_min:
            side = "buy_down"
            kelly = kelly_down
            midpoint = down_mid
            edge = edge_down
            p = p_down
        else:
            return HOLD

        self._entry_tick = ctx.tick_index
        self._entry_midpoint = midpoint

        dollars = ctx.max_buy_amount * kelly
        # Ensure at least minimum order size (5 tokens * price), capped at available cash
        min_dollars = CLOB_MIN_ORDER_TOKENS * midpoint
        if dollars < min_dollars:
            dollars = min(min_dollars, ctx.max_buy_amount)

        self.log("ENTRY {}: p={:.3f} mid={:.3f} edge={:.3f} kelly={:.3f} ${:.2f} | "
                 "jumps={}/{} at {:.0f}%".format(
                     side, p, midpoint, edge, kelly, dollars,
                     self._up_jumps, self._down_jumps,
                     ctx.elapsed_fraction * 100))

        return TradeAction(side=side, amount=dollars, price=midpoint)

    def param_grid(self):
        # type: () -> Dict[str, List[Any]]
        # 4 * 3 * 3 * 3 * 3 * 3 = 972 combos
        return {
            "jump_bps": [0.10, 0.15, 0.20, 0.30],
            "min_elapsed": [0.15, 0.20, 0.30],
            "no_new_after": [0.65, 0.80, 0.90],
            "min_kelly": [0.10, 0.15, 0.25],
            "exit_kelly": [0.0, 0.02, 0.05],
            "min_hold_ticks": [30, 50, 100],
        }
