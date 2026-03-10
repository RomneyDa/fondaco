"""Poisson jump model + Kelly criterion with dynamic position re-evaluation.

Extends PoissonKelly by continuously re-evaluating whether a held position
still has edge. Instead of a fixed stop-loss, the model re-computes P(win)
and the Kelly fraction every tick while holding. If the Kelly fraction on the
held side drops below exit_kelly, the position is closed — whether that's a
profit (market caught up to model) or a loss (model changed its mind).

This is the theoretically correct approach: use the same model for both
entry and exit decisions. The sell trigger is "the model no longer likes
this position" rather than an arbitrary price threshold.
"""

import math
from typing import Any, Dict, List, Optional

from core.defaults import CLOB_MIN_ORDER_TOKENS
from core.trader import HOLD, TradeAction, TickContext
from core.strategies.base import Strategy


def _norm_cdf(x):
    # type: (float) -> float
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


class DynamicPoissonKelly(Strategy):
    """Poisson jump model with dynamic Kelly re-evaluation for exits.

    Params:
        jump_bps: minimum BTC move (in basis points) to count as a jump event
        min_jumps: minimum total jumps before allowing entry (warmup)
        min_elapsed: don't enter before this elapsed fraction
        no_new_after: stop new entries after this elapsed fraction
        min_kelly: minimum Kelly fraction required for entry
        max_kelly: cap Kelly fraction (avoid overbetting)
        edge_min: minimum edge (p_model - market_price) required for entry
        exit_kelly: sell when Kelly fraction on held side drops below this
        hold_ticks: minimum ticks to hold before allowing dynamic exit
        stop_loss_pct: hard floor — sell if midpoint drops this fraction below entry
    """

    name = "DynamicPoissonKelly"
    description = (
        "Poisson jump model with Kelly sizing. Continuously re-evaluates "
        "held positions and exits when the model no longer sees edge."
    )
    version = "1"
    requires_full_session = True

    def __init__(
        self,
        jump_bps=0.35,
        min_jumps=8,
        min_elapsed=0.25,
        no_new_after=0.50,
        min_kelly=0.10,
        max_kelly=0.45,
        edge_min=0.12,
        exit_kelly=0.02,
        hold_ticks=90,
        stop_loss_pct=0.05,
    ):
        self.jump_bps = jump_bps
        self.min_jumps = min_jumps
        self.min_elapsed = min_elapsed
        self.no_new_after = no_new_after
        self.min_kelly = min_kelly
        self.max_kelly = max_kelly
        self.edge_min = edge_min
        self.exit_kelly = exit_kelly
        self.hold_ticks = hold_ticks
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
            "hold_ticks": hold_ticks,
            "stop_loss_pct": stop_loss_pct,
        }

        # Per-session state
        self._prev_btc = 0.0
        self._up_jumps = 0
        self._down_jumps = 0
        self._entry_side = None  # type: Optional[str]
        self._entry_midpoint = 0.0
        self._ticks_held = 0
        self._exited = False
        self._exit_sell_attempted = False

    def on_session_start(self, metadata):
        # type: (Dict[str, Any]) -> None
        self._prev_btc = 0.0
        self._up_jumps = 0
        self._down_jumps = 0
        self._entry_side = None
        self._entry_midpoint = 0.0
        self._ticks_held = 0
        self._exited = False
        self._exit_sell_attempted = False

    def _held_side(self, ctx):
        # type: (TickContext) -> Optional[str]
        if ctx.position.up_tokens > 0:
            return "up"
        if ctx.position.down_tokens > 0:
            return "down"
        return None

    def _compute_p_up(self, ctx):
        # type: (TickContext) -> Optional[float]
        """Compute P(BTC finishes above strike) using Poisson jump model."""
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
        """Kelly criterion for a binary bet: f* = (p - price) / (1 - price)."""
        if market_price >= 0.99 or market_price <= 0.01:
            return 0.0
        f = (p - market_price) / (1.0 - market_price)
        return max(0.0, min(f, self.max_kelly))

    def _try_exit(self, ctx, held, reason):
        # type: (TickContext, str, str) -> TradeAction
        """Build a sell action and mark as exited."""
        up_mid = ctx.sample.get("up_token_midpoint")
        down_mid = ctx.sample.get("down_token_midpoint")
        current_mid = up_mid if held == "up" else down_mid
        sell_side = "sell_up" if held == "up" else "sell_down"
        tokens = ctx.position.up_tokens if held == "up" else ctx.position.down_tokens

        pnl_pct = 0.0
        if self._entry_midpoint > 0 and current_mid is not None:
            pnl_pct = (current_mid / self._entry_midpoint - 1.0) * 100

        self.log("{}: mid={:.3f} entry={:.3f} pnl={:+.1f}% held={}t".format(
            reason, current_mid or 0, self._entry_midpoint,
            pnl_pct, self._ticks_held))

        self._exited = True
        if tokens > 0 and current_mid is not None and current_mid > 0:
            return TradeAction(side=sell_side, amount=tokens, price=current_mid)
        return HOLD

    def on_tick(self, ctx):
        # type: (TickContext) -> TradeAction
        btc = ctx.sample.get("btc_price_usd")
        up_mid = ctx.sample.get("up_token_midpoint")
        down_mid = ctx.sample.get("down_token_midpoint")

        if btc is None or up_mid is None or down_mid is None:
            return HOLD

        # --- Count jumps (always, even while holding) ---
        if self._prev_btc > 0:
            ret_bps = (btc / self._prev_btc - 1.0) * 10000.0
            if ret_bps >= self.jump_bps:
                self._up_jumps += 1
            elif ret_bps <= -self.jump_bps:
                self._down_jumps += 1
        self._prev_btc = btc

        # --- Manage existing position: dynamic re-evaluation ---
        held = self._held_side(ctx)
        if held is not None:
            if self._exited:
                if not self._exit_sell_attempted:
                    self._exit_sell_attempted = True
                    current_mid = up_mid if held == "up" else down_mid
                    sell_side = "sell_up" if held == "up" else "sell_down"
                    tokens = ctx.position.up_tokens if held == "up" else ctx.position.down_tokens
                    if tokens > 0 and current_mid is not None and current_mid > 0:
                        return TradeAction(side=sell_side, amount=tokens, price=current_mid)
                return HOLD

            self._ticks_held += 1

            # Hard stop-loss on midpoint
            current_mid = up_mid if held == "up" else down_mid
            if (self.stop_loss_pct > 0 and self._entry_midpoint > 0
                    and current_mid <= self._entry_midpoint * (1.0 - self.stop_loss_pct)):
                return self._try_exit(ctx, held,
                    "stop-loss: mid={:.3f} down {:.1f}% from {:.3f}".format(
                        current_mid,
                        (1.0 - current_mid / self._entry_midpoint) * 100,
                        self._entry_midpoint))

            # Dynamic re-evaluation using the model
            p_up = self._compute_p_up(ctx)
            if p_up is not None and self._ticks_held >= self.hold_ticks:
                if held == "up":
                    kelly_held = self._kelly_fraction(p_up, up_mid)
                else:
                    kelly_held = self._kelly_fraction(1.0 - p_up, down_mid)

                if kelly_held < self.exit_kelly:
                    return self._try_exit(ctx, held,
                        "edge-gone: kelly={:.3f}<{:.3f}".format(
                            kelly_held, self.exit_kelly))

            return HOLD

        # Already exited this session — done
        if self._exited or self._entry_side is not None:
            return HOLD

        # Timing guards
        if ctx.elapsed_fraction < self.min_elapsed:
            return HOLD
        if ctx.elapsed_fraction >= self.no_new_after:
            return HOLD

        # --- Compute model probability for entry ---
        p_up = self._compute_p_up(ctx)
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

        self._entry_side = "up" if side == "buy_up" else "down"
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
        # 3 * 3 * 3 * 3 * 3 * 3 = 729
        return {
            "jump_bps": [0.30, 0.35, 0.40],
            "min_elapsed": [0.22, 0.25, 0.28],
            "edge_min": [0.08, 0.10, 0.12],
            "max_kelly": [0.35, 0.45, 0.55],
            "exit_kelly": [0.01, 0.02, 0.03],
            "stop_loss_pct": [0.04, 0.05, 0.06],
        }
