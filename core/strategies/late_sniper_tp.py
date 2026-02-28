"""LateSniper variant with take-profit: sells early when ROI target is hit."""

from core.trader import HOLD, TradeAction
from core.strategies.late_sniper import LateSniper


class LateSniperTP(LateSniper):
    """LateSniper with an additional take-profit exit.

    Inherits all LateSniper behavior (late entry, stop-loss) and adds:
    if midpoint rises take_profit_pct above entry, sell to lock in gains.

    Params (in addition to LateSniper params):
        take_profit_pct: sell if midpoint rises this fraction above entry (e.g. 0.15)
    """

    name = "LateSniperTP"
    description = (
        "LateSniper variant that takes profit early. Waits until late in the "
        "window, buys the likely winner, then sells if ROI exceeds the "
        "take-profit threshold rather than holding to resolution."
    )

    def __init__(
        self,
        min_elapsed=0.92,
        confidence_threshold=0.60,
        max_price=0.90,
        stop_loss_pct=0.20,
        take_profit_pct=0.40,
        bet_fraction=1.0,
    ):
        super().__init__(
            min_elapsed=min_elapsed,
            confidence_threshold=confidence_threshold,
            max_price=max_price,
            stop_loss_pct=stop_loss_pct,
            bet_fraction=bet_fraction,
        )
        self.take_profit_pct = take_profit_pct
        self._params["take_profit_pct"] = take_profit_pct

    def on_tick(self, ctx):
        up_mid = ctx.sample.get("up_token_midpoint")
        down_mid = ctx.sample.get("down_token_midpoint")
        if up_mid is None or down_mid is None:
            return HOLD

        # Manage existing position — check stop-loss and take-profit
        held = self._held_side(ctx)
        if held is not None:
            current_mid = up_mid if held == "up" else down_mid
            if self._entry_midpoint > 0:
                sell_side = "sell_up" if held == "up" else "sell_down"
                tokens = ctx.position.up_tokens if held == "up" else ctx.position.down_tokens

                # Stop-loss
                if current_mid <= self._entry_midpoint * (1.0 - self.stop_loss_pct):
                    self.log("stop-loss: mid={:.2f} down {:.1f}% from entry {:.2f}".format(
                        current_mid,
                        (1.0 - current_mid / self._entry_midpoint) * 100,
                        self._entry_midpoint,
                    ))
                    self._exited = True
                    return TradeAction(side=sell_side, amount=tokens, price=current_mid)

                # Take-profit
                if current_mid >= self._entry_midpoint * (1.0 + self.take_profit_pct):
                    self.log("take-profit: mid={:.2f} up {:.1f}% from entry {:.2f}".format(
                        current_mid,
                        (current_mid / self._entry_midpoint - 1.0) * 100,
                        self._entry_midpoint,
                    ))
                    self._exited = True
                    return TradeAction(side=sell_side, amount=tokens, price=current_mid)

            return HOLD

        # Delegate entry logic to parent
        return super().on_tick(ctx)

    def param_grid(self):
        return {
            "min_elapsed": [0.88, 0.90, 0.92, 0.94],
            "confidence_threshold": [0.55, 0.58, 0.60, 0.62, 0.65],
            "max_price": [0.85, 0.88, 0.90, 0.93, 0.95],
            "stop_loss_pct": [0.15, 0.18, 0.20, 0.22],
            "take_profit_pct": [0.30, 0.35, 0.40, 0.50],
        }
