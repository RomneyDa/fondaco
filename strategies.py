"""Trading strategy definitions for Polymarket up/down backtesting."""

from abc import ABC, abstractmethod
from typing import Any, Dict, List

from simulator import HOLD, TradeAction, TickContext


class Strategy(ABC):
    """Base class for all trading strategies."""

    name = ""  # type: str

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

    def param_grid(self):
        # type: () -> Dict[str, List[Any]]
        """Parameter grid for optimization. Override to enable grid search."""
        return {}


# ---------------------------------------------------------------------------
# Baseline strategies
# ---------------------------------------------------------------------------


class AlwaysBuyUp(Strategy):
    """Baseline: buy Up tokens at first tick, hold to resolution."""

    name = "AlwaysBuyUp"

    def __init__(self, bet_fraction=1.0):
        self.bet_fraction = bet_fraction
        self._bought = False
        self._params = {"bet_fraction": bet_fraction}

    def on_session_start(self, metadata):
        self._bought = False

    def on_tick(self, ctx):
        if not self._bought:
            self._bought = True
            return TradeAction(side="buy_up", amount=ctx.position.cash * self.bet_fraction)
        return HOLD

    def param_grid(self):
        return {"bet_fraction": [0.25, 0.5, 0.75, 1.0]}


class AlwaysBuyDown(Strategy):
    """Baseline: buy Down tokens at first tick, hold to resolution."""

    name = "AlwaysBuyDown"

    def __init__(self, bet_fraction=1.0):
        self.bet_fraction = bet_fraction
        self._bought = False
        self._params = {"bet_fraction": bet_fraction}

    def on_session_start(self, metadata):
        self._bought = False

    def on_tick(self, ctx):
        if not self._bought:
            self._bought = True
            return TradeAction(side="buy_down", amount=ctx.position.cash * self.bet_fraction)
        return HOLD

    def param_grid(self):
        return {"bet_fraction": [0.25, 0.5, 0.75, 1.0]}


# ---------------------------------------------------------------------------
# Signal-based strategies
# ---------------------------------------------------------------------------


class MomentumEntry(Strategy):
    """Buy in the direction BTC is moving if it exceeds a threshold early in the window.

    Params:
        entry_window: max elapsed fraction to enter (e.g. 0.3 = first 30%)
        price_threshold_bps: BTC move in basis points to trigger entry
        bet_fraction: fraction of cash to bet
    """

    name = "MomentumEntry"

    def __init__(self, entry_window=0.3, price_threshold_bps=5.0, bet_fraction=0.5):
        self.entry_window = entry_window
        self.price_threshold_bps = price_threshold_bps
        self.bet_fraction = bet_fraction
        self._entered = False
        self._params = {
            "entry_window": entry_window,
            "price_threshold_bps": price_threshold_bps,
            "bet_fraction": bet_fraction,
        }

    def on_session_start(self, metadata):
        self._entered = False

    def on_tick(self, ctx):
        if self._entered:
            return HOLD
        if ctx.elapsed_fraction > self.entry_window:
            return HOLD

        btc_now = ctx.sample.get("btc_price_usd")
        if btc_now is None or ctx.initial_btc_price <= 0:
            return HOLD

        move_bps = ((btc_now - ctx.initial_btc_price) / ctx.initial_btc_price) * 10000.0

        if move_bps > self.price_threshold_bps:
            self._entered = True
            return TradeAction(side="buy_up", amount=ctx.position.cash * self.bet_fraction)
        elif move_bps < -self.price_threshold_bps:
            self._entered = True
            return TradeAction(side="buy_down", amount=ctx.position.cash * self.bet_fraction)

        return HOLD

    def param_grid(self):
        return {
            "entry_window": [0.2, 0.3, 0.5],
            "price_threshold_bps": [2.0, 5.0, 10.0, 20.0],
            "bet_fraction": [0.25, 0.5, 1.0],
        }


class MidpointMomentum(Strategy):
    """Follow token midpoint trend over recent ticks.

    If the Up midpoint is rising above a threshold with momentum, buy Up (and vice versa).

    Params:
        lookback_ticks: number of ticks to measure trend over
        midpoint_threshold: minimum midpoint to consider (e.g. 0.55)
        momentum_threshold: minimum midpoint change over lookback
        entry_deadline: max elapsed fraction to enter
        bet_fraction: fraction of cash to bet
    """

    name = "MidpointMomentum"

    def __init__(
        self,
        lookback_ticks=5,
        midpoint_threshold=0.55,
        momentum_threshold=0.03,
        entry_deadline=0.6,
        bet_fraction=0.5,
    ):
        self.lookback_ticks = lookback_ticks
        self.midpoint_threshold = midpoint_threshold
        self.momentum_threshold = momentum_threshold
        self.entry_deadline = entry_deadline
        self.bet_fraction = bet_fraction
        self._entered = False
        self._params = {
            "lookback_ticks": lookback_ticks,
            "midpoint_threshold": midpoint_threshold,
            "momentum_threshold": momentum_threshold,
            "entry_deadline": entry_deadline,
            "bet_fraction": bet_fraction,
        }

    def on_session_start(self, metadata):
        self._entered = False

    def on_tick(self, ctx):
        if self._entered:
            return HOLD
        if ctx.elapsed_fraction > self.entry_deadline:
            return HOLD
        if len(ctx.history) < self.lookback_ticks:
            return HOLD

        # Current midpoints
        up_now = ctx.sample.get("up_token_midpoint")
        down_now = ctx.sample.get("down_token_midpoint")
        if up_now is None or down_now is None:
            return HOLD

        # Midpoint from lookback_ticks ago
        past = ctx.history[-self.lookback_ticks]
        up_past = past.get("up_token_midpoint")
        down_past = past.get("down_token_midpoint")
        if up_past is None or down_past is None:
            return HOLD

        up_momentum = up_now - up_past
        down_momentum = down_now - down_past

        if up_now >= self.midpoint_threshold and up_momentum >= self.momentum_threshold:
            self._entered = True
            return TradeAction(side="buy_up", amount=ctx.position.cash * self.bet_fraction)
        elif down_now >= self.midpoint_threshold and down_momentum >= self.momentum_threshold:
            self._entered = True
            return TradeAction(side="buy_down", amount=ctx.position.cash * self.bet_fraction)

        return HOLD

    def param_grid(self):
        return {
            "lookback_ticks": [3, 5, 10],
            "midpoint_threshold": [0.52, 0.55, 0.60],
            "momentum_threshold": [0.02, 0.03, 0.05],
            "entry_deadline": [0.4, 0.6, 0.8],
            "bet_fraction": [0.25, 0.5, 1.0],
        }


class LateSniper(Strategy):
    """Wait until late in the window when outcome is more certain, then buy the likely winner.

    Params:
        min_elapsed: minimum elapsed fraction before considering entry (e.g. 0.7)
        confidence_threshold: minimum midpoint to treat as "confident" (e.g. 0.7)
        max_price: don't buy above this price (limits downside)
        bet_fraction: fraction of cash to bet
    """

    name = "LateSniper"

    def __init__(
        self,
        min_elapsed=0.7,
        confidence_threshold=0.70,
        max_price=0.85,
        bet_fraction=0.5,
    ):
        self.min_elapsed = min_elapsed
        self.confidence_threshold = confidence_threshold
        self.max_price = max_price
        self.bet_fraction = bet_fraction
        self._entered = False
        self._params = {
            "min_elapsed": min_elapsed,
            "confidence_threshold": confidence_threshold,
            "max_price": max_price,
            "bet_fraction": bet_fraction,
        }

    def on_session_start(self, metadata):
        self._entered = False

    def on_tick(self, ctx):
        if self._entered:
            return HOLD
        if ctx.elapsed_fraction < self.min_elapsed:
            return HOLD

        up_mid = ctx.sample.get("up_token_midpoint")
        down_mid = ctx.sample.get("down_token_midpoint")
        if up_mid is None or down_mid is None:
            return HOLD

        if up_mid >= self.confidence_threshold and up_mid <= self.max_price:
            self._entered = True
            return TradeAction(side="buy_up", amount=ctx.position.cash * self.bet_fraction)
        elif down_mid >= self.confidence_threshold and down_mid <= self.max_price:
            self._entered = True
            return TradeAction(side="buy_down", amount=ctx.position.cash * self.bet_fraction)

        return HOLD

    def param_grid(self):
        return {
            "min_elapsed": [0.5, 0.6, 0.7, 0.8],
            "confidence_threshold": [0.60, 0.65, 0.70, 0.80],
            "max_price": [0.80, 0.85, 0.90, 0.95],
            "bet_fraction": [0.25, 0.5, 1.0],
        }


class FadeTheMarket(Strategy):
    """Mean reversion: if midpoint swings to an extreme early, bet against it.

    Params:
        overreaction_threshold: midpoint level considered overreaction (e.g. 0.70)
        max_elapsed: only fade early moves (e.g. 0.3 = first 30%)
        bet_fraction: fraction of cash to bet
    """

    name = "FadeTheMarket"

    def __init__(
        self,
        overreaction_threshold=0.70,
        max_elapsed=0.3,
        bet_fraction=0.5,
    ):
        self.overreaction_threshold = overreaction_threshold
        self.max_elapsed = max_elapsed
        self.bet_fraction = bet_fraction
        self._entered = False
        self._params = {
            "overreaction_threshold": overreaction_threshold,
            "max_elapsed": max_elapsed,
            "bet_fraction": bet_fraction,
        }

    def on_session_start(self, metadata):
        self._entered = False

    def on_tick(self, ctx):
        if self._entered:
            return HOLD
        if ctx.elapsed_fraction > self.max_elapsed:
            return HOLD

        up_mid = ctx.sample.get("up_token_midpoint")
        down_mid = ctx.sample.get("down_token_midpoint")
        if up_mid is None or down_mid is None:
            return HOLD

        # If Up is overreacting high, fade it by buying Down
        if up_mid >= self.overreaction_threshold:
            self._entered = True
            return TradeAction(side="buy_down", amount=ctx.position.cash * self.bet_fraction)
        # If Down is overreacting high, fade it by buying Up
        elif down_mid >= self.overreaction_threshold:
            self._entered = True
            return TradeAction(side="buy_up", amount=ctx.position.cash * self.bet_fraction)

        return HOLD

    def param_grid(self):
        return {
            "overreaction_threshold": [0.65, 0.70, 0.75, 0.80],
            "max_elapsed": [0.2, 0.3, 0.4],
            "bet_fraction": [0.25, 0.5, 1.0],
        }


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

ALL_STRATEGIES = {
    "AlwaysBuyUp": AlwaysBuyUp,
    "AlwaysBuyDown": AlwaysBuyDown,
    "MomentumEntry": MomentumEntry,
    "MidpointMomentum": MidpointMomentum,
    "LateSniper": LateSniper,
    "FadeTheMarket": FadeTheMarket,
}
