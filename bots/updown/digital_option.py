"""Black-Scholes digital option model for 5-minute BTC up/down markets.

Computes a model-derived fair probability using the digital option formula,
then trades when the market midpoint diverges from fair value. Adapts across
the session via regime detection (near-strike caution, trend-following, late lockout).
"""

import math

from core.trader import HOLD, TradeAction
from core.strategies.base import Strategy


def _norm_cdf(x):
    """Standard normal CDF via math.erf."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


class DigitalOption(Strategy):
    """Params:
        sigma_span: EWMA span for per-tick volatility (ticks)
        momentum_span: EWMA span for drift estimate (ticks)
        momentum_weight: multiplier on momentum tilt in z-score
        base_edge: minimum edge (p_fair - midpoint) for entry
        no_new_after: elapsed fraction cutoff for new entries
        stop_loss_pct: sell if midpoint drops this fraction below entry
        bet_fraction: fraction of cash to bet
    """

    name = "DigitalOption"
    description = (
        "Models the market as a digital option using Black-Scholes fair probability. "
        "Enters when market midpoint diverges from model price, with regime-adaptive thresholds."
    )
    version = "1"
    requires_full_session = True

    def __init__(
        self,
        sigma_span=15,
        momentum_span=32,
        momentum_weight=0.05,
        base_edge=0.25,
        no_new_after=0.70,
        stop_loss_pct=0.06,
        bet_fraction=1.0,
    ):
        self.sigma_span = sigma_span
        self.momentum_span = momentum_span
        self.momentum_weight = momentum_weight
        self.base_edge = base_edge
        self.no_new_after = no_new_after
        self.stop_loss_pct = stop_loss_pct
        self.bet_fraction = bet_fraction
        self._params = {
            "sigma_span": sigma_span,
            "momentum_span": momentum_span,
            "momentum_weight": momentum_weight,
            "base_edge": base_edge,
            "no_new_after": no_new_after,
            "stop_loss_pct": stop_loss_pct,
            "bet_fraction": bet_fraction,
        }

        # Per-session state (reset in on_session_start)
        self._prev_btc = 0.0
        self._sigma_sq = 0.0
        self._momentum_ewma = 0.0
        self._warmup_returns = []  # type: list
        self._warmup_done = False
        self._entry_midpoint = 0.0
        self._entry_side = None  # type: str | None
        self._exited = False

    def on_session_start(self, metadata):
        self._prev_btc = 0.0
        self._sigma_sq = 0.0
        self._momentum_ewma = 0.0
        self._warmup_returns = []
        self._warmup_done = False
        self._entry_midpoint = 0.0
        self._entry_side = None
        self._exited = False

    def _has_position(self, ctx):
        return ctx.position.up_tokens > 0 or ctx.position.down_tokens > 0

    def _held_side(self, ctx):
        if ctx.position.up_tokens > 0:
            return "up"
        if ctx.position.down_tokens > 0:
            return "down"
        return None

    def on_tick(self, ctx):
        btc = ctx.sample.get("btc_price_usd")
        up_mid = ctx.sample.get("up_token_midpoint")
        down_mid = ctx.sample.get("down_token_midpoint")

        if btc is None or up_mid is None or down_mid is None:
            return HOLD

        # -- Update EWMA statistics --
        if self._prev_btc > 0:
            log_ret = math.log(btc / self._prev_btc)
            warmup_len = max(self.sigma_span // 5, 10)

            if not self._warmup_done:
                self._warmup_returns.append(log_ret)
                if len(self._warmup_returns) >= warmup_len:
                    # Seed sigma_sq with sample variance
                    mean_r = sum(self._warmup_returns) / len(self._warmup_returns)
                    self._sigma_sq = sum((r - mean_r) ** 2 for r in self._warmup_returns) / len(self._warmup_returns)
                    # Seed momentum with mean return
                    self._momentum_ewma = mean_r
                    self._warmup_done = True
                    self._warmup_returns = []  # free memory
            else:
                # EWMA updates
                alpha_s = 2.0 / (self.sigma_span + 1)
                self._sigma_sq = alpha_s * (log_ret ** 2) + (1.0 - alpha_s) * self._sigma_sq

                alpha_m = 2.0 / (self.momentum_span + 1)
                self._momentum_ewma = alpha_m * log_ret + (1.0 - alpha_m) * self._momentum_ewma

        self._prev_btc = btc

        # Need warmup before computing model
        if not self._warmup_done:
            return HOLD

        # -- Manage existing position: stop-loss --
        held = self._held_side(ctx)
        if held is not None:
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

        # Late cutoff — no new entries
        if ctx.elapsed_fraction >= self.no_new_after:
            return HOLD

        # -- Compute fair probability --
        strike = ctx.initial_btc_price
        if strike <= 0:
            return HOLD

        d = math.log(btc / strike)
        sigma_tick = math.sqrt(max(self._sigma_sq, 1e-20))

        # Estimate ticks remaining
        elapsed_secs = ctx.elapsed_fraction * 300.0
        if elapsed_secs < 1.0:
            return HOLD  # too early to estimate tick rate
        tick_rate = (ctx.tick_index + 1) / elapsed_secs
        secs_remaining = (1.0 - ctx.elapsed_fraction) * 300.0
        n = max(tick_rate * secs_remaining, 1.0)

        denom = sigma_tick * math.sqrt(n)
        if denom < 1e-12:
            return HOLD

        # z without momentum (for regime detection)
        z_raw = d / denom

        # z with momentum tilt
        mu_adj = self.momentum_weight * self._momentum_ewma * n
        z = (d + mu_adj) / denom

        p_fair = _norm_cdf(z)

        # -- Regime detection --
        abs_z_raw = abs(z_raw)
        if abs_z_raw < 0.3:
            regime = "near_strike"
            threshold = self.base_edge * 1.5
        elif abs_z_raw < 1.0:
            regime = "drifting"
            threshold = self.base_edge
        else:
            regime = "decided"
            threshold = self.base_edge * 0.7

        # -- Entry logic --
        # Edge = difference between model fair value and market midpoint
        # If p_fair > up_mid + threshold → Up is underpriced, buy Up
        # If (1-p_fair) > down_mid + threshold → Down is underpriced, buy Down
        up_edge = p_fair - up_mid
        down_edge = (1.0 - p_fair) - down_mid

        if up_edge > threshold and up_edge >= down_edge:
            side = "buy_up"
            midpoint = up_mid
            edge = up_edge
        elif down_edge > threshold:
            side = "buy_down"
            midpoint = down_mid
            edge = down_edge
        else:
            self.log("HOLD {}: z={:.2f} z_raw={:.2f} p_fair={:.3f} up_mid={:.3f} down_mid={:.3f} "
                     "up_edge={:.3f} down_edge={:.3f} thresh={:.3f}".format(
                         regime, z, z_raw, p_fair, up_mid, down_mid, up_edge, down_edge, threshold))
            return HOLD

        self._entry_side = "up" if side == "buy_up" else "down"
        self._entry_midpoint = midpoint
        self.log("ENTRY {}: {} edge={:.3f} p_fair={:.3f} mid={:.3f} z={:.2f} z_raw={:.2f} "
                 "sigma={:.6f} at {:.0f}%".format(
                     regime, side, edge, p_fair, midpoint, z, z_raw,
                     sigma_tick, ctx.elapsed_fraction * 100))

        return TradeAction(
            side=side,
            amount=ctx.position.cash * self.bet_fraction,
            price=midpoint,
        )

    def param_grid(self):
        return {
            "sigma_span": [8, 12, 15, 20, 30],
            "momentum_span": [20, 28, 32, 40, 50],
            "momentum_weight": [0.0, 0.03, 0.05, 0.08, 0.15],
            "base_edge": [0.18, 0.22, 0.25, 0.30, 0.38],
            "no_new_after": [0.65, 0.70, 0.80],
            "stop_loss_pct": [0.04, 0.06, 0.10],
        }
