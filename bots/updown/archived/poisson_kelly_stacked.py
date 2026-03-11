"""Poisson jump model + Kelly criterion with incremental position stacking.

Extends PoissonKelly by allowing multiple buys on the same side within a
session. Instead of entering once and holding, the strategy continuously
re-evaluates the Kelly fraction and adds to the position when the model
says the optimal allocation is larger than what's currently deployed.

Key difference from other PoissonKelly variants:
- PoissonKelly: one entry, hold or stop-loss
- DynamicPoissonKelly: one entry, dynamic exit (no re-entry)
- PoissonKellyActive: sell and re-enter (always fully exits first)
- PoissonKellyStacked: scale into the position — buy more if edge grows

The bet_fraction param controls scaling pace: 1.0 = immediately fill to
target Kelly, 0.5 = fill half the gap each time, etc. Lower values give
more gradual scaling and reduce whipsaw risk from noisy Kelly estimates.
"""

import math
from typing import Any, Dict, List, Optional

from core.trader import HOLD, TradeAction, TickContext
from core.strategies.base import Strategy


def _norm_cdf(x):
    # type: (float) -> float
    """Standard normal CDF via math.erf."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


class PoissonKellyStacked(Strategy):
    """Poisson jump counting + Kelly sizing with incremental stacking.

    Params:
        jump_bps: minimum BTC move (in basis points) to count as a jump event
        min_jumps: minimum total jumps observed before allowing entry (warmup)
        min_elapsed: don't consider entry before this elapsed fraction
        no_new_after: stop new entries/additions after this elapsed fraction
        min_kelly: minimum Kelly fraction required to trigger initial entry
        max_kelly: cap Kelly fraction (avoid overbetting on noisy estimates)
        edge_min: minimum edge (p_model - market_price) required for initial entry
        stop_loss_pct: sell if midpoint drops this fraction below avg entry price
        bet_fraction: fraction of the Kelly gap to fill per add-on (0-1)
        add_edge_min: minimum additional edge required to add to position
        add_kelly_gap: minimum gap between target Kelly and deployed fraction to add
        min_add_ticks: minimum ticks between successive buys (anti-whipsaw)
    """

    name = "PoissonKellyStacked"
    description = (
        "Poisson jump model with Kelly sizing. Scales into positions "
        "incrementally when edge grows, instead of one-shot entry."
    )
    version = "1"
    requires_full_session = True

    def __init__(
        self,
        jump_bps=0.20,
        min_jumps=8,
        min_elapsed=0.20,
        no_new_after=0.55,
        min_kelly=0.03,
        max_kelly=0.45,
        edge_min=0.02,
        stop_loss_pct=0.09,
        bet_fraction=0.5,
        add_edge_min=0.03,
        add_kelly_gap=0.05,
        min_add_ticks=90,
    ):
        self.jump_bps = jump_bps
        self.min_jumps = min_jumps
        self.min_elapsed = min_elapsed
        self.no_new_after = no_new_after
        self.min_kelly = min_kelly
        self.max_kelly = max_kelly
        self.edge_min = edge_min
        self.stop_loss_pct = stop_loss_pct
        self.bet_fraction = bet_fraction
        self.add_edge_min = add_edge_min
        self.add_kelly_gap = add_kelly_gap
        self.min_add_ticks = min_add_ticks
        self._params = {
            "jump_bps": jump_bps,
            "min_jumps": min_jumps,
            "min_elapsed": min_elapsed,
            "no_new_after": no_new_after,
            "min_kelly": min_kelly,
            "max_kelly": max_kelly,
            "edge_min": edge_min,
            "stop_loss_pct": stop_loss_pct,
            "bet_fraction": bet_fraction,
            "add_edge_min": add_edge_min,
            "add_kelly_gap": add_kelly_gap,
            "min_add_ticks": min_add_ticks,
        }

        # Per-session state
        self._prev_btc = 0.0
        self._up_jumps = 0
        self._down_jumps = 0
        self._entry_side = None  # type: Optional[str]
        self._total_deployed = 0.0  # dollars deployed this session
        self._session_bankroll = 0.0  # bankroll at session start
        self._avg_entry_mid = 0.0  # weighted avg midpoint at entry
        self._last_buy_tick = -1
        self._exited = False
        self._exit_sell_attempted = False

    def on_session_start(self, metadata):
        # type: (Dict[str, Any]) -> None
        self._prev_btc = 0.0
        self._up_jumps = 0
        self._down_jumps = 0
        self._entry_side = None
        self._total_deployed = 0.0
        self._session_bankroll = 0.0
        self._avg_entry_mid = 0.0
        self._last_buy_tick = -1
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

    def _deployed_fraction(self):
        # type: () -> float
        """Fraction of session bankroll currently deployed."""
        if self._session_bankroll <= 0:
            return 0.0
        return self._total_deployed / self._session_bankroll

    def on_tick(self, ctx):
        # type: (TickContext) -> TradeAction
        btc = ctx.sample.get("btc_price_usd")
        up_mid = ctx.sample.get("up_token_midpoint")
        down_mid = ctx.sample.get("down_token_midpoint")

        if btc is None or up_mid is None or down_mid is None:
            return HOLD

        # Capture bankroll on first tick (before any trades)
        if self._session_bankroll <= 0:
            self._session_bankroll = ctx.max_buy_amount + self._total_deployed

        # --- Count jumps ---
        if self._prev_btc > 0:
            ret_bps = (btc / self._prev_btc - 1.0) * 10000.0
            if ret_bps >= self.jump_bps:
                self._up_jumps += 1
            elif ret_bps <= -self.jump_bps:
                self._down_jumps += 1
        self._prev_btc = btc

        # --- Manage existing position ---
        held = self._held_side(ctx)
        if held is not None:
            if self._exited:
                # Already tried to exit — one more sell attempt, then give up
                if not self._exit_sell_attempted:
                    self._exit_sell_attempted = True
                    current_mid = up_mid if held == "up" else down_mid
                    sell_side = "sell_up" if held == "up" else "sell_down"
                    tokens = ctx.position.up_tokens if held == "up" else ctx.position.down_tokens
                    if tokens > 0 and current_mid is not None and current_mid > 0:
                        return TradeAction(side=sell_side, amount=tokens, price=current_mid)
                return HOLD

            current_mid = up_mid if held == "up" else down_mid

            # Stop-loss based on weighted avg entry midpoint
            if self._avg_entry_mid > 0 and current_mid <= self._avg_entry_mid * (1.0 - self.stop_loss_pct):
                self.log("stop-loss: mid={:.3f} down {:.1f}% from avg_entry {:.3f}".format(
                    current_mid,
                    (1.0 - current_mid / self._avg_entry_mid) * 100,
                    self._avg_entry_mid,
                ))
                sell_side = "sell_up" if held == "up" else "sell_down"
                tokens = ctx.position.up_tokens if held == "up" else ctx.position.down_tokens
                self._exited = True
                return TradeAction(side=sell_side, amount=tokens, price=current_mid)

            # --- Check if we should add to position ---
            if ctx.elapsed_fraction < self.no_new_after:
                ticks_since_buy = ctx.tick_index - self._last_buy_tick
                if ticks_since_buy >= self.min_add_ticks:
                    p_up = self._compute_p_up(ctx)
                    if p_up is not None:
                        p_held = p_up if held == "up" else 1.0 - p_up
                        edge = p_held - current_mid
                        target_kelly = self._kelly_fraction(p_held, current_mid)
                        deployed_frac = self._deployed_fraction()
                        kelly_gap = target_kelly - deployed_frac

                        if edge >= self.add_edge_min and kelly_gap >= self.add_kelly_gap:
                            # Scale in: fill bet_fraction of the gap
                            add_frac = kelly_gap * self.bet_fraction
                            dollars = self._session_bankroll * add_frac

                            # Clamp to available cash
                            dollars = min(dollars, ctx.max_buy_amount)
                            if dollars > 0:
                                buy_side = "buy_up" if held == "up" else "buy_down"

                                # Update weighted avg entry midpoint
                                old_cost = self._total_deployed
                                new_cost = old_cost + dollars
                                if new_cost > 0:
                                    self._avg_entry_mid = (
                                        self._avg_entry_mid * old_cost + current_mid * dollars
                                    ) / new_cost

                                self._total_deployed = new_cost
                                self._last_buy_tick = ctx.tick_index

                                self.log("ADD {}: p={:.3f} mid={:.3f} edge={:.3f} "
                                         "target_k={:.3f} deployed={:.3f} gap={:.3f} "
                                         "+${:.2f} (total ${:.2f}) | jumps={}/{} at {:.0f}%".format(
                                             buy_side, p_held, current_mid, edge,
                                             target_kelly, deployed_frac, kelly_gap,
                                             dollars, self._total_deployed,
                                             self._up_jumps, self._down_jumps,
                                             ctx.elapsed_fraction * 100))

                                return TradeAction(side=buy_side, amount=dollars, price=current_mid)

            return HOLD

        # Already exited this session — done
        if self._exited:
            return HOLD

        # --- Initial entry logic ---
        if ctx.elapsed_fraction < self.min_elapsed:
            return HOLD
        if ctx.elapsed_fraction >= self.no_new_after:
            return HOLD

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
            self.log("HOLD: p_up={:.3f} up_mid={:.3f} k_up={:.3f} e_up={:.3f} | "
                     "p_dn={:.3f} dn_mid={:.3f} k_dn={:.3f} e_dn={:.3f} | "
                     "jumps={}/{}".format(
                         p_up, up_mid, kelly_up, edge_up,
                         p_down, down_mid, kelly_down, edge_down,
                         self._up_jumps, self._down_jumps))
            return HOLD

        self._entry_side = "up" if side == "buy_up" else "down"

        # Initial buy: use bet_fraction of the Kelly-suggested allocation
        initial_frac = kelly * self.bet_fraction
        dollars = self._session_bankroll * initial_frac
        dollars = min(dollars, ctx.max_buy_amount)

        self._avg_entry_mid = midpoint
        self._total_deployed = dollars
        self._last_buy_tick = ctx.tick_index

        self.log("ENTRY {}: p={:.3f} mid={:.3f} edge={:.3f} kelly={:.3f} "
                 "bet_frac={:.2f} ${:.2f} | jumps={}/{} at {:.0f}%".format(
                     side, p, midpoint, edge, kelly,
                     self.bet_fraction, dollars,
                     self._up_jumps, self._down_jumps,
                     ctx.elapsed_fraction * 100))

        return TradeAction(side=side, amount=dollars, price=midpoint)

    def param_grid(self):
        # type: () -> Dict[str, List[Any]]
        # 4 * 3 * 3 * 3 * 3 * 3 = 972 combos
        return {
            "jump_bps": [0.15, 0.18, 0.20, 0.25],
            "min_elapsed": [0.15, 0.20, 0.25],
            "no_new_after": [0.50, 0.55, 0.65],
            "max_kelly": [0.35, 0.45, 0.55],
            "bet_fraction": [0.3, 0.5, 0.7],
            "add_kelly_gap": [0.03, 0.05, 0.08],
        }
