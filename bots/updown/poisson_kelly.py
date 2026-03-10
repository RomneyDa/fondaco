"""Poisson jump model + Kelly criterion for BTC up/down markets.

Models BTC tick-by-tick price changes as a compound Poisson process. Counts
significant up-jumps and down-jumps, estimates arrival rates, then uses the
Skellam distribution (normal approximation for the difference of two Poissons)
to compute the probability of BTC finishing above or below the strike.

Sizes bets using the Kelly criterion: f* = (p - price) / (1 - price), which
is the optimal fraction for a binary bet paying $1 on win and $0 on loss.

Key differences from DigitalOption (Black-Scholes):
- Discrete jump dynamics instead of continuous diffusion
- Kelly criterion for theoretically optimal bet sizing
- Jump threshold adapts to market microstructure noise floor
"""

import math
from typing import Any, Dict, List, Optional

from core.trader import HOLD, TradeAction, TickContext
from core.strategies.base import Strategy


def _norm_cdf(x):
    # type: (float) -> float
    """Standard normal CDF via math.erf."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


class PoissonKelly(Strategy):
    """Poisson jump counting + Kelly sizing for up/down binary markets.

    Params:
        jump_bps: minimum BTC move (in basis points) to count as a jump event
        min_jumps: minimum total jumps observed before allowing entry (warmup)
        min_elapsed: don't consider entry before this elapsed fraction
        no_new_after: stop new entries after this elapsed fraction
        min_kelly: minimum Kelly fraction required to trigger entry
        max_kelly: cap Kelly fraction (avoid overbetting on noisy estimates)
        edge_min: minimum edge (p_model - market_price) required for entry
        stop_loss_pct: sell if midpoint drops this fraction below entry price
    """

    name = "PoissonKelly"
    description = (
        "Models BTC price as a Poisson jump process, estimates win probability "
        "via Skellam distribution, and sizes bets using the Kelly criterion."
    )

    def __init__(
        self,
        jump_bps=0.38,
        min_jumps=8,
        min_elapsed=0.22,
        no_new_after=0.60,
        min_kelly=0.10,
        max_kelly=1.0,
        edge_min=0.15,
        stop_loss_pct=0.035,
    ):
        self.jump_bps = jump_bps
        self.min_jumps = min_jumps
        self.min_elapsed = min_elapsed
        self.no_new_after = no_new_after
        self.min_kelly = min_kelly
        self.max_kelly = max_kelly
        self.edge_min = edge_min
        self.stop_loss_pct = stop_loss_pct
        self._params = {
            "jump_bps": jump_bps,
            "min_jumps": min_jumps,
            "min_elapsed": min_elapsed,
            "no_new_after": no_new_after,
            "min_kelly": min_kelly,
            "max_kelly": max_kelly,
            "edge_min": edge_min,
            "stop_loss_pct": stop_loss_pct,
        }

        # Per-session state
        self._prev_btc = 0.0
        self._up_jumps = 0
        self._down_jumps = 0
        self._entry_side = None  # type: Optional[str]
        self._entry_midpoint = 0.0
        self._exited = False
        self._exit_sell_attempted = False

    def on_session_start(self, metadata):
        # type: (Dict[str, Any]) -> None
        self._prev_btc = 0.0
        self._up_jumps = 0
        self._down_jumps = 0
        self._entry_side = None
        self._entry_midpoint = 0.0
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
        """Compute P(BTC finishes above strike) using Poisson jump model.

        Estimates up/down jump arrival rates from observed counts, then uses
        the Skellam distribution (normal approximation) to compute the
        probability that the net jump count plus current BTC displacement
        remains positive through end of session.
        """
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

        # Estimate arrival rates (jumps per unit elapsed fraction)
        lambda_up = self._up_jumps / elapsed
        lambda_down = self._down_jumps / elapsed

        # Expected jumps in remaining time
        mu_up = lambda_up * remaining
        mu_down = lambda_down * remaining

        # Current BTC displacement expressed as equivalent net jumps
        displacement_bps = (btc / strike - 1.0) * 10000.0
        net_current = displacement_bps / max(self.jump_bps, 0.01)

        # Skellam distribution: X = Poisson(mu_up) - Poisson(mu_down)
        # E[X] = mu_up - mu_down, Var[X] = mu_up + mu_down
        # P(BTC up at end) ≈ P(net_current + X > 0) = Phi(z)
        skellam_mean = mu_up - mu_down
        skellam_var = mu_up + mu_down

        if skellam_var < 1e-10:
            # No expected future jumps — use current displacement sign
            if net_current > 0:
                return 1.0
            elif net_current < 0:
                return 0.0
            return 0.5

        z = (net_current + skellam_mean) / math.sqrt(skellam_var)
        return _norm_cdf(z)

    def _kelly_fraction(self, p, market_price):
        # type: (float, float) -> float
        """Kelly criterion for a binary bet.

        Buy token at market_price, pays $1 on win, $0 on loss.
        f* = (p - market_price) / (1 - market_price)
        """
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

        # --- Manage existing position: stop-loss ---
        held = self._held_side(ctx)
        if held is not None:
            if self._exited:
                # Already tried to exit — try sell once more, then give up
                if not self._exit_sell_attempted:
                    self._exit_sell_attempted = True
                    current_mid = up_mid if held == "up" else down_mid
                    sell_side = "sell_up" if held == "up" else "sell_down"
                    tokens = ctx.position.up_tokens if held == "up" else ctx.position.down_tokens
                    if tokens > 0 and current_mid is not None and current_mid > 0:
                        return TradeAction(side=sell_side, amount=tokens, price=current_mid)
                return HOLD
            current_mid = up_mid if held == "up" else down_mid
            if self._entry_midpoint > 0 and current_mid <= self._entry_midpoint * (1.0 - self.stop_loss_pct):
                self.log("stop-loss: mid={:.3f} down {:.1f}% from entry {:.3f}".format(
                    current_mid,
                    (1.0 - current_mid / self._entry_midpoint) * 100,
                    self._entry_midpoint,
                ))
                sell_side = "sell_up" if held == "up" else "sell_down"
                tokens = ctx.position.up_tokens if held == "up" else ctx.position.down_tokens
                self._exited = True
                return TradeAction(side=sell_side, amount=tokens, price=current_mid)
            return HOLD

        # Already entered or exited — done for this session
        if self._exited or self._entry_side is not None:
            return HOLD

        # Timing guards
        if ctx.elapsed_fraction < self.min_elapsed:
            return HOLD
        if ctx.elapsed_fraction >= self.no_new_after:
            return HOLD

        # --- Compute model probability ---
        p_up = self._compute_p_up(ctx)
        if p_up is None:
            return HOLD

        p_down = 1.0 - p_up

        # Kelly fraction and edge for each side
        kelly_up = self._kelly_fraction(p_up, up_mid)
        kelly_down = self._kelly_fraction(p_down, down_mid)
        edge_up = p_up - up_mid
        edge_down = p_down - down_mid

        # Pick the better side (must clear both Kelly and edge thresholds)
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
            self.log("HOLD: p_up={:.3f} up_mid={:.3f} k_up={:.3f} e_up={:.3f} | "
                     "p_dn={:.3f} dn_mid={:.3f} k_dn={:.3f} e_dn={:.3f} | "
                     "jumps={}/{}".format(
                         p_up, up_mid, kelly_up, edge_up,
                         p_down, down_mid, kelly_down, edge_down,
                         self._up_jumps, self._down_jumps))
            return HOLD

        self._entry_side = "up" if side == "buy_up" else "down"
        self._entry_midpoint = midpoint

        # Kelly-sized bet (respects guardrails via max_buy_amount)
        dollars = ctx.max_buy_amount * kelly

        self.log("ENTRY {}: p={:.3f} mid={:.3f} edge={:.3f} kelly={:.3f} ${:.2f} | "
                 "jumps={}/{} at {:.0f}%".format(
                     side, p, midpoint, edge, kelly, dollars,
                     self._up_jumps, self._down_jumps,
                     ctx.elapsed_fraction * 100))

        return TradeAction(side=side, amount=dollars, price=midpoint)

    def param_grid(self):
        # type: () -> Dict[str, List[Any]]
        # 4 * 3 * 4 * 4 * 3 = 576 combos
        return {
            "jump_bps": [0.10, 0.15, 0.20, 0.30],
            "min_elapsed": [0.15, 0.20, 0.30],
            "no_new_after": [0.65, 0.70, 0.75, 0.80],
            "min_kelly": [0.10, 0.15, 0.20, 0.30],
            "edge_min": [0.05, 0.08, 0.12],
        }
