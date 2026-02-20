"""Core simulation engine for backtesting Polymarket up/down strategies."""

import glob
import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class TradeAction:
    """Output from a strategy's on_tick method."""
    side: str  # buy_up, buy_down, sell_up, sell_down, hold
    amount: float = 0.0  # dollars for buys, token count for sells


HOLD = TradeAction(side="hold", amount=0.0)


@dataclass
class Position:
    """Current holdings."""
    cash: float
    up_tokens: float = 0.0
    down_tokens: float = 0.0


@dataclass
class TickContext:
    """Everything a strategy sees at each tick."""
    tick_index: int
    sample: Dict[str, Any]
    history: List[Dict[str, Any]]
    position: Position
    metadata: Dict[str, Any]
    initial_btc_price: float
    elapsed_fraction: float  # 0.0 to 1.0


@dataclass
class Trade:
    """Record of an executed trade."""
    tick_index: int
    side: str
    price: float
    amount: float  # dollars spent or tokens sold
    tokens: float  # tokens received or sold


@dataclass
class SessionResult:
    """Result for one session."""
    session_file: str
    outcome: str
    pnl: float
    pnl_pct: float
    trades: List[Trade]
    final_position: Position
    num_samples: int


@dataclass
class BacktestResult:
    """Aggregate results across sessions."""
    strategy_name: str
    params: Dict[str, Any]
    sessions: List[SessionResult]
    total_pnl: float
    mean_pnl: float
    win_rate: float
    sharpe: float
    num_sessions: int


def load_sessions(data_dir):
    # type: (str) -> List[Dict[str, Any]]
    """Load all resolved session JSON files from a directory tree."""
    pattern = os.path.join(data_dir, "**", "*.json")
    files = glob.glob(pattern, recursive=True)
    if not files:
        # Also try non-recursive (flat directory)
        pattern = os.path.join(data_dir, "*.json")
        files = glob.glob(pattern)
    sessions = []
    for f in sorted(files):
        with open(f, "r") as fh:
            data = json.load(fh)
        # Skip unresolved sessions
        if data.get("outcome") is None:
            continue
        data["_source_file"] = f
        sessions.append(data)
    return sessions


class Simulator:
    def __init__(
        self,
        starting_cash=100.0,
        execution_mode="midpoint_slippage",
        slippage=0.02,
    ):
        self.starting_cash = starting_cash
        self.execution_mode = execution_mode
        self.slippage = slippage

    def run_session(self, strategy, session_dict):
        # type: (Any, Dict[str, Any]) -> SessionResult
        """Replay one session through a strategy."""
        samples = session_dict["samples"]
        metadata = session_dict["metadata"]
        outcome = session_dict["outcome"]
        initial_btc = session_dict.get("initial_btc_price_usd") or 0.0
        source_file = session_dict.get("_source_file", "")

        # Compute window duration from samples
        if len(samples) >= 2:
            window_duration = samples[-1]["elapsed_seconds"]
        else:
            window_duration = 300.0

        position = Position(cash=self.starting_cash)
        strategy.on_session_start(metadata)
        trades = []  # type: List[Trade]
        history = []  # type: List[Dict[str, Any]]

        for i, sample in enumerate(samples):
            elapsed_frac = sample["elapsed_seconds"] / window_duration if window_duration > 0 else 0.0
            elapsed_frac = min(elapsed_frac, 1.0)

            ctx = TickContext(
                tick_index=i,
                sample=sample,
                history=list(history),
                position=Position(
                    cash=position.cash,
                    up_tokens=position.up_tokens,
                    down_tokens=position.down_tokens,
                ),
                metadata=metadata,
                initial_btc_price=initial_btc,
                elapsed_fraction=elapsed_frac,
            )

            action = strategy.on_tick(ctx)
            trade = self._execute(action, sample, position)
            if trade is not None:
                trades.append(trade)

            history.append(sample)

        # Resolution: winning tokens pay $1, losing tokens pay $0
        if outcome == "Up":
            position.cash += position.up_tokens * 1.0
            position.up_tokens = 0.0
            position.down_tokens = 0.0
        elif outcome == "Down":
            position.cash += position.down_tokens * 1.0
            position.down_tokens = 0.0
            position.up_tokens = 0.0

        pnl = position.cash - self.starting_cash
        pnl_pct = (pnl / self.starting_cash) * 100.0 if self.starting_cash > 0 else 0.0

        return SessionResult(
            session_file=source_file,
            outcome=outcome,
            pnl=pnl,
            pnl_pct=pnl_pct,
            trades=trades,
            final_position=position,
            num_samples=len(samples),
        )

    def run_backtest(self, strategy, sessions):
        # type: (Any, List[Dict[str, Any]]) -> BacktestResult
        """Run a strategy across all sessions and aggregate stats."""
        results = []
        for s in sessions:
            results.append(self.run_session(strategy, s))

        total_pnl = sum(r.pnl for r in results)
        n = len(results)
        mean_pnl = total_pnl / n if n > 0 else 0.0
        wins = sum(1 for r in results if r.pnl > 0)
        win_rate = wins / n if n > 0 else 0.0

        # Sharpe: mean / std of per-session PnL
        if n > 1:
            pnls = [r.pnl for r in results]
            mean = mean_pnl
            variance = sum((p - mean) ** 2 for p in pnls) / (n - 1)
            std = variance ** 0.5
            sharpe = mean / std if std > 0 else 0.0
        else:
            sharpe = 0.0

        return BacktestResult(
            strategy_name=strategy.name,
            params=dict(getattr(strategy, "_params", {})),
            sessions=results,
            total_pnl=total_pnl,
            mean_pnl=mean_pnl,
            win_rate=win_rate,
            sharpe=sharpe,
            num_sessions=n,
        )

    def _execute(self, action, sample, position):
        # type: (TradeAction, Dict[str, Any], Position) -> Optional[Trade]
        """Execute a trade action, mutating position in place."""
        if action.side == "hold" or action.amount <= 0:
            return None

        price = self._get_execution_price(action.side, sample)
        if price is None or price <= 0 or price >= 1.0:
            return None

        if action.side == "buy_up":
            dollars = min(action.amount, position.cash)
            if dollars <= 0:
                return None
            tokens = dollars / price
            position.cash -= dollars
            position.up_tokens += tokens
            return Trade(tick_index=0, side="buy_up", price=price, amount=dollars, tokens=tokens)

        elif action.side == "buy_down":
            dollars = min(action.amount, position.cash)
            if dollars <= 0:
                return None
            tokens = dollars / price
            position.cash -= dollars
            position.down_tokens += tokens
            return Trade(tick_index=0, side="buy_down", price=price, amount=dollars, tokens=tokens)

        elif action.side == "sell_up":
            tokens = min(action.amount, position.up_tokens)
            if tokens <= 0:
                return None
            proceeds = tokens * price
            position.up_tokens -= tokens
            position.cash += proceeds
            return Trade(tick_index=0, side="sell_up", price=price, amount=proceeds, tokens=tokens)

        elif action.side == "sell_down":
            tokens = min(action.amount, position.down_tokens)
            if tokens <= 0:
                return None
            proceeds = tokens * price
            position.down_tokens -= tokens
            position.cash += proceeds
            return Trade(tick_index=0, side="sell_down", price=price, amount=proceeds, tokens=tokens)

        return None

    def _get_execution_price(self, side, sample):
        # type: (str, Dict[str, Any]) -> Optional[float]
        """Look up execution price based on mode and action side."""
        if self.execution_mode == "midpoint_slippage":
            table = {
                "buy_up": sample.get("up_token_midpoint"),
                "sell_up": sample.get("up_token_midpoint"),
                "buy_down": sample.get("down_token_midpoint"),
                "sell_down": sample.get("down_token_midpoint"),
            }
            base = table.get(side)
            if base is None:
                return None
            if side.startswith("buy"):
                return base + self.slippage
            else:
                return base - self.slippage

        elif self.execution_mode == "midpoint":
            table = {
                "buy_up": sample.get("up_token_midpoint"),
                "sell_up": sample.get("up_token_midpoint"),
                "buy_down": sample.get("down_token_midpoint"),
                "sell_down": sample.get("down_token_midpoint"),
            }
            return table.get(side)

        elif self.execution_mode == "book":
            table = {
                "buy_up": sample.get("up_token_best_ask"),
                "sell_up": sample.get("up_token_best_bid"),
                "buy_down": sample.get("down_token_best_ask"),
                "sell_down": sample.get("down_token_best_bid"),
            }
            return table.get(side)

        return None
