"""Wait until late in the window when outcome is more certain, then buy the likely winner."""

from core.trader import HOLD, TradeAction
from core.strategies.base import Strategy


class LateSniper(Strategy):
    """Params:
        min_elapsed: minimum elapsed fraction before considering entry (e.g. 0.7)
        confidence_threshold: minimum midpoint to treat as "confident" (e.g. 0.70)
        max_price: don't buy above this price (limits downside)
        stop_loss_pct: sell if midpoint drops this fraction below entry (e.g. 0.15)
        bet_fraction: fraction of cash to bet
    """

    name = "LateSniper"
    description = "Waits until late in the window when the outcome is more predictable, then buys the likely winner. Exits on stop-loss if the market reverses."

    def __init__(
        self,
        min_elapsed=0.7,
        confidence_threshold=0.70,
        max_price=0.85,
        stop_loss_pct=0.15,
        bet_fraction=1.0,
    ):
        self.min_elapsed = min_elapsed
        self.confidence_threshold = confidence_threshold
        self.max_price = max_price
        self.stop_loss_pct = stop_loss_pct
        self.bet_fraction = bet_fraction
        self._entry_midpoint = 0.0
        self._exited = False  # True after stop-loss sell — done for session
        self._params = {
            "min_elapsed": min_elapsed,
            "confidence_threshold": confidence_threshold,
            "max_price": max_price,
            "stop_loss_pct": stop_loss_pct,
            "bet_fraction": bet_fraction,
        }

    def on_session_start(self, metadata):
        self._entry_midpoint = 0.0
        self._exited = False

    def _has_position(self, ctx):
        """Check if we actually hold tokens (fill succeeded)."""
        return ctx.position.up_tokens > 0 or ctx.position.down_tokens > 0

    def _held_side(self, ctx):
        """Return which side we hold, or None."""
        if ctx.position.up_tokens > 0:
            return "up"
        if ctx.position.down_tokens > 0:
            return "down"
        return None

    def on_tick(self, ctx):
        up_mid = ctx.sample.get("up_token_midpoint")
        down_mid = ctx.sample.get("down_token_midpoint")
        if up_mid is None or down_mid is None:
            return HOLD

        # Manage existing position — check stop-loss
        held = self._held_side(ctx)
        if held is not None:
            current_mid = up_mid if held == "up" else down_mid
            if self._entry_midpoint > 0 and current_mid <= self._entry_midpoint * (1.0 - self.stop_loss_pct):
                self.log("stop-loss: mid={:.2f} down {:.1f}% from entry {:.2f}".format(
                    current_mid,
                    (1.0 - current_mid / self._entry_midpoint) * 100,
                    self._entry_midpoint,
                ))
                sell_side = "sell_up" if held == "up" else "sell_down"
                tokens = ctx.position.up_tokens if held == "up" else ctx.position.down_tokens
                self._exited = True
                return TradeAction(side=sell_side, amount=tokens)
            return HOLD

        # Already filled and exited (stop-loss) — done for this session
        if self._exited:
            return HOLD

        # Wait for late window
        if ctx.elapsed_fraction < self.min_elapsed:
            return HOLD

        up_ask = ctx.sample.get("up_token_best_ask")
        down_ask = ctx.sample.get("down_token_best_ask")

        if up_mid >= self.confidence_threshold and up_ask is not None and up_ask <= self.max_price:
            self._entry_midpoint = up_mid
            self.log("BUY UP: mid={:.2f} ask={:.2f} at {:.0f}% elapsed".format(
                up_mid, up_ask, ctx.elapsed_fraction * 100,
            ))
            return TradeAction(side="buy_up", amount=ctx.position.cash * self.bet_fraction)
        elif down_mid >= self.confidence_threshold and down_ask is not None and down_ask <= self.max_price:
            self._entry_midpoint = down_mid
            self.log("BUY DOWN: mid={:.2f} ask={:.2f} at {:.0f}% elapsed".format(
                down_mid, down_ask, ctx.elapsed_fraction * 100,
            ))
            return TradeAction(side="buy_down", amount=ctx.position.cash * self.bet_fraction)

        # Log why we held
        best_mid = max(up_mid, down_mid)
        best_side = "up" if up_mid >= down_mid else "down"
        best_ask = up_ask if best_side == "up" else down_ask
        reasons = []
        if best_mid < self.confidence_threshold:
            reasons.append("low confidence {:.2f}<{:.2f}".format(best_mid, self.confidence_threshold))
        if best_ask is None:
            reasons.append("no ask")
        elif best_ask > self.max_price:
            reasons.append("ask too high {:.2f}>{:.2f}".format(best_ask, self.max_price))
        self.log("HOLD at {:.0f}%: best={} mid={:.2f} ask={} | {}".format(
            ctx.elapsed_fraction * 100,
            best_side,
            best_mid,
            "{:.2f}".format(best_ask) if best_ask is not None else "None",
            ", ".join(reasons) if reasons else "confident but no ask",
        ))

        return HOLD

    def param_grid(self):
        return {
            "min_elapsed": [0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85],
            "confidence_threshold": [0.60, 0.65, 0.68, 0.70, 0.73, 0.75],
            "max_price": [0.80, 0.83, 0.85, 0.87, 0.90],
            "stop_loss_pct": [0.10, 0.15, 0.20],
            "bet_fraction": [0.3, 0.5, 0.7],
        }
