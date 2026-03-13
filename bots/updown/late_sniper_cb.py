"""Late-session sniper with circuit breaker.

Less conservative than ConfirmedLate — lower confidence threshold, shorter
confirmation window, earlier entry. Trades more often but uses a circuit
breaker to pause after bad streaks rather than relying on ultra-selectivity
to avoid losses.

v1: Initial implementation. Lower confidence (0.85), earlier entry (0.70),
    shorter confirm (500ms), circuit breaker at 2 losses in 6 trades.
"""

from collections import deque
from typing import Any, Dict, Optional

from core.strategies.base import Param
from core.strategies.regime_gated import RegimeGatedStrategy
from core.trader import HOLD, TradeAction, TickContext


class LateSniperCB(RegimeGatedStrategy):
    """Late-session entry with circuit breaker protection."""

    name = "LateSniperCB"
    description = (
        "Late-session confidence entry, less selective than ConfirmedLate. "
        "Circuit breaker pauses trading after bad streaks."
    )
    version = "1"
    requires_full_session = False
    min_elapsed_hint = 0.65

    # --- Regime gate (NOT tunable) ---
    required_vol = {"medium", "high"}
    required_trend = None
    skip_stale_data = True

    # --- Tunable params ---
    min_elapsed = Param(default=0.60, min=0.50, max=0.75)
    confidence_threshold = Param(default=0.80, min=0.75, max=0.88)
    max_price = Param(default=0.80, min=0.75, max=0.85)
    confirm_ms = Param(default=300, min=200, max=800)

    # --- Fixed params ---
    no_new_after = Param(default=0.90, tunable=False)
    cb_lookback = Param(default=6, tunable=False)
    cb_max_losses = Param(default=2, tunable=False)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Circuit breaker state — persists across sessions
        self._cb_results = deque(maxlen=self.cb_lookback)  # type: deque
        self._cb_dry_run = False
        self._last_bought_side = None  # type: Optional[str]
        self._dry_run_side = None  # type: Optional[str]

    def _on_session_start_inner(self, metadata):
        # type: (dict) -> None
        self._score_last_trade(metadata)
        self._update_circuit_breaker()

        self._entry_count = 0
        self._entry_midpoint = 0.0
        self._confirm_side = None  # type: Optional[str]
        self._confirm_start_s = -999.0
        self._dry_run_side = None

    def _score_last_trade(self, metadata):
        # type: (dict) -> None
        """Check if last session's trade (live or shadow) was correct."""
        side = self._last_bought_side or self._dry_run_side
        if side is None:
            return

        prior = metadata.get("_prior_session_summaries")
        if not prior:
            self._last_bought_side = None
            return

        last_outcome = prior[-1].get("outcome")
        if last_outcome is None:
            self._last_bought_side = None
            return

        won = (side == "up" and last_outcome == "Up") or \
              (side == "down" and last_outcome == "Down")

        if self._cb_dry_run and self._dry_run_side is not None:
            if won:
                self._cb_dry_run = False
                self._cb_results.clear()
                self.log("CIRCUIT BREAKER OFF: shadow pick correct ({} = {})".format(
                    side, last_outcome))
            else:
                self.log("CIRCUIT BREAKER: shadow pick wrong ({} != {})".format(
                    side, last_outcome))
        elif self._last_bought_side is not None:
            self._cb_results.append(won)

        self._last_bought_side = None

    def _update_circuit_breaker(self):
        # type: () -> None
        if self._cb_dry_run:
            return
        if len(self._cb_results) < self.cb_lookback:
            return
        losses = sum(1 for r in self._cb_results if not r)
        if losses >= self.cb_max_losses:
            self._cb_dry_run = True
            self.log("CIRCUIT BREAKER ON: {} losses in last {} trades".format(
                losses, len(self._cb_results)))

    def on_tick_active(self, ctx):
        # type: (TickContext) -> TradeAction
        up_mid = ctx.sample.get("up_token_midpoint")
        down_mid = ctx.sample.get("down_token_midpoint")

        if up_mid is None or down_mid is None:
            return HOLD

        # Already holding
        if self._held_side(ctx) is not None:
            return HOLD

        self._check_expired_entry(ctx)
        if self._entry_count > 0:
            return HOLD

        if self._check_timing_guards(ctx, self.min_elapsed, self.no_new_after):
            return HOLD

        # Determine confident side
        if up_mid >= self.confidence_threshold:
            side = "up"
            signal_mid = up_mid
        elif down_mid >= self.confidence_threshold:
            side = "down"
            signal_mid = down_mid
        else:
            self._confirm_side = None
            self._confirm_start_s = -999.0
            return HOLD

        if signal_mid > self.max_price:
            return HOLD

        # Confirmation timer
        if self._confirm_side != side:
            self._confirm_side = side
            self._confirm_start_s = ctx.elapsed_seconds
            return HOLD

        confirm_s = self.confirm_ms / 1000.0
        if ctx.elapsed_seconds - self._confirm_start_s < confirm_s:
            return HOLD

        # All gates passed
        buy_side = "buy_up" if side == "up" else "buy_down"

        # Circuit breaker: shadow pick only
        if self._cb_dry_run:
            if self._dry_run_side is None:
                self._dry_run_side = side
                self._entry_count += 1
                self.log("SHADOW {}: mid={:.3f} at {:.0f}% (circuit breaker active)".format(
                    buy_side, signal_mid, ctx.elapsed_fraction * 100))
            return HOLD

        self._entry_count += 1
        self._entry_midpoint = signal_mid
        self._last_bought_side = side

        self.log("ENTRY {}: mid={:.3f} confirmed for {:.0f}ms at {:.0f}%".format(
            buy_side, signal_mid, (ctx.elapsed_seconds - self._confirm_start_s) * 1000,
            ctx.elapsed_fraction * 100))

        return TradeAction(
            side=buy_side,
            amount=ctx.position.cash,
            price=signal_mid,
            cancel_after_s=5,
        )
