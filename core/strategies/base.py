"""Base class for all trading strategies."""

from abc import ABC, abstractmethod
from typing import Any, Dict, List

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
