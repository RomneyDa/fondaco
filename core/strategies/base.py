"""Base class for all trading strategies."""

import math
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


from core.trader import HOLD, TradeAction, TickContext


class Param:
    """Declarative parameter descriptor for strategy tuning.

    Defines a tunable parameter with default, range, and whether None is valid.
    The optimizer uses these to generate search grids automatically.

    Args:
        default: Default value used when no override is provided.
        min: Minimum value for numeric params (inclusive).
        max: Maximum value for numeric params (inclusive).
        nullable: Whether None is a valid value (tested alongside numeric range).
        tunable: Whether the optimizer should vary this param. Fixed params
            (like lookback windows or internal thresholds) can set tunable=False.
    """

    def __init__(
        self,
        default=None,   # type: Any
        min=None,        # type: Optional[float]
        max=None,        # type: Optional[float]
        nullable=False,  # type: bool
        tunable=True,    # type: bool
    ):
        self.default = default
        self.min = min
        self.max = max
        self.nullable = nullable
        self.tunable = tunable

    def grid(self, n=3):
        # type: (int) -> List[Any]
        """Generate n evenly-spaced grid values between min and max.

        Always includes default if it falls within [min, max].
        If nullable, prepends None to the list.
        If min/max not set, returns [default].
        """
        values = []  # type: List[Any]
        if self.nullable:
            values.append(None)

        if self.min is not None and self.max is not None and self.min != self.max:
            is_int = isinstance(self.default, int) and isinstance(self.min, int) and isinstance(self.max, int)
            if is_int:
                all_ints = list(range(self.min, self.max + 1))
                if len(all_ints) <= n:
                    numeric = all_ints
                else:
                    indices = [round(i * (len(all_ints) - 1) / (n - 1)) for i in range(n)]
                    numeric = [all_ints[idx] for idx in dict.fromkeys(indices)]
            else:
                step = (self.max - self.min) / (n - 1) if n > 1 else 0
                numeric = [round(self.min + step * i, 6) for i in range(n)]

            # Ensure default is included (replace nearest point if needed)
            if self.default is not None and self.default not in numeric:
                if self.min <= self.default <= self.max:
                    # Find nearest and replace it
                    nearest_idx = min(range(len(numeric)),
                                      key=lambda i: abs(numeric[i] - self.default))
                    numeric[nearest_idx] = self.default
                    numeric.sort()

            values.extend(numeric)
        elif self.default is not None:
            values.append(self.default)

        return values

    def __repr__(self):
        # type: () -> str
        parts = ["default={}".format(self.default)]
        if self.min is not None:
            parts.append("min={}".format(self.min))
        if self.max is not None:
            parts.append("max={}".format(self.max))
        if self.nullable:
            parts.append("nullable=True")
        if not self.tunable:
            parts.append("tunable=False")
        return "Param({})".format(", ".join(parts))


class Strategy(ABC):
    """Base class for all trading strategies."""

    name = ""  # type: str
    description = ""  # type: str
    version = "1"  # type: str
    requires_full_session = False  # type: bool
    uses_history = False  # type: bool  # Set True if on_tick reads ctx.history
    min_elapsed_hint = 0.0  # type: float  # Simulator skips ticks before this (only when requires_full_session=False)

    @classmethod
    def param_defs(cls):
        # type: () -> Dict[str, Param]
        """Return declared Param descriptors from the class body.

        Strategies define params as class-level Param() attributes. This method
        collects them for grid generation, validation, and display.
        """
        defs = {}
        for klass in reversed(cls.__mro__):
            for key, val in vars(klass).items():
                if isinstance(val, Param):
                    defs[key] = val
        return defs

    @classmethod
    def defaults(cls):
        # type: () -> Dict[str, Any]
        """Return {param_name: default_value} for all declared Params."""
        return {k: p.default for k, p in cls.param_defs().items()}

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

    def param_grid(self, max_variants=5000):
        # type: (int) -> Dict[str, List[Any]]
        """Parameter grid for optimization.

        Auto-generated from Param descriptors if not overridden.
        Only includes tunable params with min/max ranges or nullable.
        Targets 3 points per param (min, mid, max), scales down if needed.
        """
        defs = self.param_defs()
        if not defs:
            return {}

        tunable = {name: p for name, p in defs.items()
                   if p.tunable and (p.min is not None and p.max is not None
                                     and p.min != p.max or p.nullable)}
        if not tunable:
            return {}

        # Target 3 points per numeric range. Scale down to 2 if too many.
        for n in (3, 2):
            total = 1
            for p in tunable.values():
                pts = len(p.grid(n=n))
                total *= pts
            if total <= max_variants:
                break

        grid = {}
        for name, p in tunable.items():
            values = p.grid(n=n)
            if len(values) > 1:
                grid[name] = values
        return grid
