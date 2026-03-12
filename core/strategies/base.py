"""Base class for all trading strategies."""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from core.trader import HOLD, TradeAction, TickContext


class Strategy(ABC):
    """Base class for all trading strategies."""

    name = ""  # type: str
    description = ""  # type: str
    version = "1"  # type: str
    requires_full_session = False  # type: bool
    uses_history = False  # type: bool  # Set True if on_tick reads ctx.history
    min_elapsed_hint = 0.0  # type: float  # Simulator skips ticks before this (only when requires_full_session=False)

    @abstractmethod
    def on_session_start(self, metadata):
        # type: (Dict[str, Any]) -> None
        """Reset per-session state. Called before first tick."""
        pass

    @abstractmethod
    def on_tick(self, ctx):
        # type: (TickContext) -> TradeAction
        """Core logic. Called on every price sample."""
        pass

    def _held_side(self, ctx):
        # type: (TickContext) -> Optional[str]
        """Return 'up', 'down', or None based on current token position."""
        if ctx.position.up_tokens > 0:
            return "up"
        if ctx.position.down_tokens > 0:
            return "down"
        return None

    def _has_position(self, ctx):
        # type: (TickContext) -> bool
        """Check if any tokens are held."""
        return ctx.position.up_tokens > 0 or ctx.position.down_tokens > 0

    def _check_exit(self, ctx, up_mid, down_mid, stop_loss_pct, take_profit_pct=None,
                     cooldown=36):
        # type: (TickContext, float, float, Optional[float], Optional[float], int) -> Optional[TradeAction]
        """Check stop-loss and take-profit for an existing position.

        Returns a sell TradeAction if triggered, HOLD if in cooldown, or None
        if no exit condition met (caller should return HOLD for held positions).

        Expects self._entry_midpoint and self._last_sell_tick / self._last_trade_tick
        to be set by the strategy.
        """
        held = self._held_side(ctx)
        if held is None:
            return None

        current_mid = up_mid if held == "up" else down_mid
        entry_mid = getattr(self, "_entry_midpoint", 0.0)
        if entry_mid <= 0:
            return None

        sell_side = "sell_up" if held == "up" else "sell_down"
        tokens = ctx.position.up_tokens if held == "up" else ctx.position.down_tokens

        # Find the last trade/sell tick attribute (strategies use different names)
        last_tick = getattr(self, "_last_sell_tick",
                            getattr(self, "_last_trade_tick", -999))

        # Stop-loss
        if stop_loss_pct is not None and current_mid <= entry_mid * (1.0 - stop_loss_pct):
            if ctx.tick_index - last_tick < cooldown:
                return HOLD
            self._update_last_trade_tick(ctx.tick_index)
            self.log("stop-loss: mid={:.3f} down {:.1f}% from entry {:.3f}".format(
                current_mid,
                (1.0 - current_mid / entry_mid) * 100,
                entry_mid,
            ))
            self._entry_midpoint = 0.0
            return TradeAction(side=sell_side, amount=tokens, price=current_mid,
                               expiration_seconds=10)

        # Take-profit
        if take_profit_pct is not None and current_mid >= entry_mid * (1.0 + take_profit_pct):
            if ctx.tick_index - last_tick < cooldown:
                return HOLD
            self._update_last_trade_tick(ctx.tick_index)
            self.log("take-profit: mid={:.3f} up {:.1f}% from entry {:.3f}".format(
                current_mid,
                (current_mid / entry_mid - 1.0) * 100,
                entry_mid,
            ))
            self._entry_midpoint = 0.0
            return TradeAction(side=sell_side, amount=tokens, price=current_mid,
                               expiration_seconds=10)

        return None

    def _update_last_trade_tick(self, tick_index):
        # type: (int) -> None
        """Update whichever last-trade-tick attribute the strategy uses."""
        if hasattr(self, "_last_sell_tick"):
            self._last_sell_tick = tick_index
        if hasattr(self, "_last_trade_tick"):
            self._last_trade_tick = tick_index

    def _check_timing_guards(self, ctx, min_elapsed, no_new_after):
        # type: (TickContext, float, float) -> bool
        """Return True if entry should be blocked by timing guards."""
        if ctx.elapsed_fraction < min_elapsed:
            return True
        if ctx.elapsed_fraction >= no_new_after:
            return True
        return False

    def _check_expired_entry(self, ctx):
        # type: (TickContext) -> None
        """Reset entry state if a buy order expired without filling."""
        if getattr(self, "_entry_midpoint", 0.0) > 0 and self._held_side(ctx) is None:
            self._entry_midpoint = 0.0

    def log(self, msg):
        # type: (str) -> None
        """Append a decision log message for this tick.

        Strategies call this during on_tick() to explain decisions.
        The Trader drains the buffer after each tick.
        """
        if not hasattr(self, "_tick_logs"):
            self._tick_logs = []  # type: List[str]
        self._tick_logs.append(msg)

    def param_grid(self):
        # type: () -> Dict[str, List[Any]]
        """Parameter grid for optimization. Override to enable grid search."""
        return {}
