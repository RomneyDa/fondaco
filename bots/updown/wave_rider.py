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

from typing import Any, Dict, List, Optional

from core.defaults import BPS_DIVISOR
from core.trader import HOLD, TradeAction, TickContext
from core.strategies.base import Param, Strategy
from core.volatility import compute_session_volatility


class WaveRider(Strategy):
    """Buy trend-direction tokens on pullback dips within a session."""

    name = "WaveRider"
    description = (
        "Identifies session trend, buys on pullback dips at better prices. "
        "Volatility from recent sessions calibrates pullback thresholds."
    )
    version = "6"
    requires_full_session = True

    # --- Tunable params ---
    trend_threshold_bps = Param(default=2.0, min=1.0, max=4.0)
    pullback_bps = Param(default=0.3, min=0.1, max=1.5)
    min_elapsed = Param(default=0.28, min=0.15, max=0.40)
    max_price = Param(default=0.70, min=0.50, max=0.80)
    max_entries = Param(default=1, min=1, max=4)
    stop_loss_pct = Param(default=0.30, min=0.05, max=0.35, nullable=True)
    take_profit_pct = Param(default=None, min=0.10, max=0.40, nullable=True)

    # --- Fixed params (not tuned by optimizer) ---
    trend_window = Param(default=100, min=60, max=200, tunable=False)
    no_new_after = Param(default=0.95, tunable=False)
    cooldown_ticks = Param(default=72, tunable=False)
    vol_lookback = Param(default=5, tunable=False)
    min_vol_sessions = Param(default=3, tunable=False)
    vol_pullback_scale = Param(default=0.3, tunable=False)

    def __init__(self, **kwargs):
        defs = self.param_defs()
        for name, p in defs.items():
            setattr(self, name, kwargs.get(name, p.default))
        self._params = {name: getattr(self, name) for name in defs}

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
        vol = compute_session_volatility(
            prior_summaries, self.vol_lookback, self.min_vol_sessions)
        if vol is None:
            if prior_summaries is not None:
                self.log("Insufficient lookback for volatility")
            return

        mean_ret, vol_std = vol
        adaptive = max(self.pullback_bps, vol_std * self.vol_pullback_scale)
        self._adaptive_pullback_bps = adaptive
        self.log("Vol: mean_abs={:.2f}bps std={:.2f}bps → pullback={:.2f}bps".format(
            mean_ret, vol_std, adaptive))

    def _detect_trend(self, initial_btc):
        # type: (float) -> Optional[str]
        """Detect trend direction from accumulated BTC prices."""
        if len(self._btc_prices) < self.trend_window or initial_btc <= 0:
            return None

        current = self._btc_prices[-1]
        displacement_bps = (current - initial_btc) / initial_btc * BPS_DIVISOR

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
        if self._held_side(ctx) is not None:
            exit_action = self._check_exit(ctx, up_mid, down_mid,
                                            self.stop_loss_pct, self.take_profit_pct)
            return exit_action if exit_action is not None else HOLD

        # --- No position: detect trend, wait for pullback ---

        # Buy order expired without filling — reset entry state
        self._check_expired_entry(ctx)

        # Timing guard
        if self._check_timing_guards(ctx, self.min_elapsed, self.no_new_after):
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
            pullback = (self._wave_peak - btc) / self._wave_peak * BPS_DIVISOR
        else:
            # BTC was going down, now it pulled back up — buy DOWN at discount
            pullback = (btc - self._wave_peak) / self._wave_peak * BPS_DIVISOR

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

