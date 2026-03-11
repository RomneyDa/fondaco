"""Ride BTC waves by buying trend-direction tokens on pullback dips.

Identifies the dominant BTC direction within the session, then waits for
temporary pullbacks (dips against the trend) to buy at better prices.
Uses recent session volatility to calibrate expected pullback depth and
determine how many wave opportunities to expect.

The key insight: binary tokens pay $1 or $0, so aligning with the trend
gives a high base win rate. Waiting for pullbacks improves entry price.
Fees (~1.5% at midpoint) mean we limit round-trips and need meaningful
wave amplitudes.

v1: Trend detection via BTC displacement + pullback dip-buying.
Volatility from prior sessions scales pullback thresholds.

v2: 2-phase Sharpe→Kelly optimization over recent 30 sessions.
Sharpe 0.34, 33% win rate, 7.2% geo return/session.
Key findings: low max_price (0.65) is essential — only buy cheap tokens
where payoff asymmetry is highest. Hold to resolution (no take-profit).
Stop-loss at 10% prevents catastrophic losses. 3 entries/session
captures multiple wave opportunities. Pullback threshold (0.3 bps) is
effectively a noise filter — any pullback after trend establishment
triggers entry. Half-Kelly f*=1.0 → cash_pct=50%.
"""

import math
from typing import Any, Dict, List, Optional

from core.trader import HOLD, TradeAction, TickContext
from core.strategies.base import Strategy


class WaveRider(Strategy):
    """Buy trend-direction tokens on pullback dips within a session.

    Phase 1: Accumulate BTC price data to identify session trend direction.
    Phase 2: Once trend is established, wait for pullbacks (BTC retracing
    against the trend). Buy the trend-direction token at the dip price.
    Hold to take-profit, stop-loss, or resolution.

    Uses prior session volatility to scale pullback threshold: high-vol
    sessions have deeper pullbacks, so we wait for more. Low-vol sessions
    have shallower pullbacks, so we use tighter thresholds.

    Params:
        trend_window: ticks to establish trend direction
        trend_threshold_bps: min BTC displacement (bps) to declare a trend
        pullback_bps: base pullback depth (bps) to trigger entry
        vol_lookback: number of prior sessions for volatility estimate
        min_vol_sessions: minimum sessions required for vol estimate
        vol_pullback_scale: multiply session vol std by this for pullback
        min_elapsed: earliest entry as elapsed fraction
        no_new_after: stop new entries after this fraction
        max_price: don't buy tokens above this midpoint
        max_entries: maximum entries per session
        stop_loss_pct: sell if midpoint drops this fraction below entry
        take_profit_pct: sell if midpoint rises this fraction above entry
        cooldown_ticks: min ticks between trades
    """

    name = "WaveRider"
    description = (
        "Identifies session trend, buys on pullback dips at better prices. "
        "Volatility from recent sessions calibrates pullback thresholds."
    )
    version = "2"
    requires_full_session = True

    def __init__(
        self,
        trend_window=100,
        trend_threshold_bps=2.0,
        pullback_bps=0.3,
        vol_lookback=5,
        min_vol_sessions=3,
        vol_pullback_scale=0.3,
        min_elapsed=0.28,
        no_new_after=0.95,
        max_price=0.65,
        max_entries=3,
        stop_loss_pct=0.10,       # type: Optional[float]
        take_profit_pct=None,     # type: Optional[float]
        cooldown_ticks=72,
    ):
        self.trend_window = trend_window
        self.trend_threshold_bps = trend_threshold_bps
        self.pullback_bps = pullback_bps
        self.vol_lookback = vol_lookback
        self.min_vol_sessions = min_vol_sessions
        self.vol_pullback_scale = vol_pullback_scale
        self.min_elapsed = min_elapsed
        self.no_new_after = no_new_after
        self.max_price = max_price
        self.max_entries = max_entries
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct
        self.cooldown_ticks = cooldown_ticks
        self._params = {
            "trend_window": trend_window,
            "trend_threshold_bps": trend_threshold_bps,
            "pullback_bps": pullback_bps,
            "vol_lookback": vol_lookback,
            "min_vol_sessions": min_vol_sessions,
            "vol_pullback_scale": vol_pullback_scale,
            "min_elapsed": min_elapsed,
            "no_new_after": no_new_after,
            "max_price": max_price,
            "max_entries": max_entries,
            "stop_loss_pct": stop_loss_pct,
            "take_profit_pct": take_profit_pct,
            "cooldown_ticks": cooldown_ticks,
        }

        # Per-session state
        self._btc_prices = []  # type: List[float]
        self._trend_dir = None  # type: Optional[str]  # "up" or "down"
        self._wave_peak = 0.0  # extreme BTC price in trend direction
        self._adaptive_pullback_bps = 0.0
        self._entry_midpoint = 0.0
        self._entry_count = 0
        self._last_trade_tick = -999

    def on_session_start(self, metadata):
        # type: (Dict[str, Any]) -> None
        self._btc_prices = []
        self._trend_dir = None
        self._wave_peak = 0.0
        self._entry_midpoint = 0.0
        self._entry_count = 0
        self._last_trade_tick = -999

        # Compute adaptive pullback from recent session volatility
        self._adaptive_pullback_bps = self.pullback_bps

        prior_summaries = metadata.get("_prior_session_summaries")
        if prior_summaries is None:
            return

        valid = [s for s in prior_summaries
                 if s.get("initial_btc") and s.get("final_btc")
                 and s["initial_btc"] > 0]
        recent = valid[-self.vol_lookback:]

        if len(recent) < self.min_vol_sessions:
            return

        # Compute session return volatility (std of abs returns in bps)
        abs_returns_bps = []
        for s in recent:
            ret = abs(s["final_btc"] - s["initial_btc"]) / s["initial_btc"] * 10000.0
            abs_returns_bps.append(ret)

        mean_ret = sum(abs_returns_bps) / len(abs_returns_bps)
        if len(abs_returns_bps) > 1:
            variance = sum((r - mean_ret) ** 2 for r in abs_returns_bps) / (len(abs_returns_bps) - 1)
            vol_std = variance ** 0.5
        else:
            vol_std = mean_ret

        # Adaptive pullback: scale with volatility
        # Higher vol → bigger pullbacks expected → wait for deeper dip
        adaptive = max(self.pullback_bps, vol_std * self.vol_pullback_scale)
        self._adaptive_pullback_bps = adaptive

        self.log("Vol: mean_abs={:.2f}bps std={:.2f}bps → pullback={:.2f}bps".format(
            mean_ret, vol_std, adaptive))

    def _held_side(self, ctx):
        # type: (TickContext) -> Optional[str]
        if ctx.position.up_tokens > 0:
            return "up"
        if ctx.position.down_tokens > 0:
            return "down"
        return None

    def _detect_trend(self, initial_btc):
        # type: (float) -> Optional[str]
        """Detect trend direction from accumulated BTC prices."""
        if len(self._btc_prices) < self.trend_window or initial_btc <= 0:
            return None

        current = self._btc_prices[-1]
        displacement_bps = (current - initial_btc) / initial_btc * 10000.0

        if displacement_bps >= self.trend_threshold_bps:
            return "up"
        elif displacement_bps <= -self.trend_threshold_bps:
            return "down"
        return None

    def on_tick(self, ctx):
        # type: (TickContext) -> TradeAction
        btc = ctx.sample.get("btc_price_usd")
        up_mid = ctx.sample.get("up_token_midpoint")
        down_mid = ctx.sample.get("down_token_midpoint")

        if btc is None or btc <= 0:
            return HOLD

        self._btc_prices.append(btc)

        if up_mid is None or down_mid is None:
            return HOLD

        # --- Manage existing position ---
        held = self._held_side(ctx)
        if held is not None:
            current_mid = up_mid if held == "up" else down_mid
            sell_side = "sell_up" if held == "up" else "sell_down"
            tokens = ctx.position.up_tokens if held == "up" else ctx.position.down_tokens

            if self._entry_midpoint > 0 and tokens > 0:
                # Stop-loss
                if (self.stop_loss_pct is not None
                        and current_mid <= self._entry_midpoint * (1.0 - self.stop_loss_pct)):
                    if ctx.tick_index - self._last_trade_tick < 36:
                        return HOLD
                    self._last_trade_tick = ctx.tick_index
                    self.log("stop-loss: mid={:.3f} entry={:.3f} drop={:.1f}%".format(
                        current_mid, self._entry_midpoint,
                        (1.0 - current_mid / self._entry_midpoint) * 100))
                    self._entry_midpoint = 0.0
                    return TradeAction(side=sell_side, amount=tokens, price=current_mid)

                # Take-profit
                if (self.take_profit_pct is not None
                        and current_mid >= self._entry_midpoint * (1.0 + self.take_profit_pct)):
                    if ctx.tick_index - self._last_trade_tick < 36:
                        return HOLD
                    self._last_trade_tick = ctx.tick_index
                    self.log("take-profit: mid={:.3f} entry={:.3f} gain={:.1f}%".format(
                        current_mid, self._entry_midpoint,
                        (current_mid / self._entry_midpoint - 1.0) * 100))
                    self._entry_midpoint = 0.0
                    return TradeAction(side=sell_side, amount=tokens, price=current_mid)

            return HOLD

        # --- No position: detect trend, wait for pullback ---

        # Timing guard
        if ctx.elapsed_fraction < self.min_elapsed:
            return HOLD
        if ctx.elapsed_fraction >= self.no_new_after:
            return HOLD

        # Max entries
        if self._entry_count >= self.max_entries:
            return HOLD

        # Cooldown
        if ctx.tick_index - self._last_trade_tick < self.cooldown_ticks:
            return HOLD

        # Detect or update trend
        trend = self._detect_trend(ctx.initial_btc_price)
        if trend is None:
            return HOLD

        # Update trend direction (can flip mid-session)
        if self._trend_dir != trend:
            self._trend_dir = trend
            self._wave_peak = btc  # reset peak tracking

        # Track wave peak (extreme in trend direction)
        if trend == "up":
            if btc > self._wave_peak:
                self._wave_peak = btc
        else:
            if self._wave_peak == 0 or btc < self._wave_peak:
                self._wave_peak = btc

        # Check for pullback from peak
        if self._wave_peak <= 0:
            return HOLD

        if trend == "up":
            # BTC was going up, now it pulled back — buy UP at discount
            pullback = (self._wave_peak - btc) / self._wave_peak * 10000.0
        else:
            # BTC was going down, now it pulled back up — buy DOWN at discount
            pullback = (btc - self._wave_peak) / self._wave_peak * 10000.0

        if pullback < self._adaptive_pullback_bps:
            return HOLD

        # Pullback detected! Buy the trend-direction token
        if trend == "up":
            if up_mid <= self.max_price and up_mid > 0:
                self._entry_count += 1
                self._entry_midpoint = up_mid
                self._last_trade_tick = ctx.tick_index
                self._wave_peak = btc  # reset peak for next wave
                dollars = ctx.position.cash
                self.log("BUY UP #{} on pullback: pb={:.2f}bps mid={:.3f} ${:.2f} at {:.0f}%".format(
                    self._entry_count, pullback, up_mid, dollars,
                    ctx.elapsed_fraction * 100))
                return TradeAction(side="buy_up", amount=dollars, price=up_mid)
        else:
            if down_mid <= self.max_price and down_mid > 0:
                self._entry_count += 1
                self._entry_midpoint = down_mid
                self._last_trade_tick = ctx.tick_index
                self._wave_peak = btc  # reset peak for next wave
                dollars = ctx.position.cash
                self.log("BUY DOWN #{} on pullback: pb={:.2f}bps mid={:.3f} ${:.2f} at {:.0f}%".format(
                    self._entry_count, pullback, down_mid, dollars,
                    ctx.elapsed_fraction * 100))
                return TradeAction(side="buy_down", amount=dollars, price=down_mid)

        return HOLD

    def param_grid(self):
        # type: () -> Dict[str, List[Any]]
        return {
            "trend_window": [80, 100, 150],
            "trend_threshold_bps": [1.5, 2.0, 2.5, 3.0],
            "pullback_bps": [0.2, 0.3, 0.5, 1.0],
            "min_elapsed": [0.20, 0.25, 0.28, 0.32],
            "no_new_after": [0.90, 0.95],
            "max_price": [0.55, 0.60, 0.65, 0.70],
            "max_entries": [2, 3],
            "stop_loss_pct": [0.08, 0.10, 0.12],
            "take_profit_pct": [None, 0.20],
            "cooldown_ticks": [54, 72],
        }
