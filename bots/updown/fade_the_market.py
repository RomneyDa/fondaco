"""Mean reversion: if midpoint swings to an extreme early, bet against it."""

from core.trader import HOLD, TradeAction
from core.strategies.base import Strategy


class FadeTheMarket(Strategy):
    """Params:
        overreaction_threshold: midpoint level considered overreaction (e.g. 0.75)
        persist_ticks: consecutive ticks above threshold before entering
        max_elapsed: only fade early moves (e.g. 0.3 = first 30%)
        stop_loss_mid: if the faded side's midpoint reaches this, cut losses
        bet_fraction: fraction of cash to bet
    """

    name = "FadeTheMarket"
    description = "Mean reversion contrarian play. If a token midpoint stays at an extreme for several ticks early in the window, bets against it. Exits on stop-loss if the trend continues."

    def __init__(
        self,
        overreaction_threshold=0.75,
        persist_ticks=5,
        max_elapsed=0.3,
        stop_loss_mid=0.85,
        bet_fraction=1.0,
    ):
        self.overreaction_threshold = overreaction_threshold
        self.persist_ticks = persist_ticks
        self.max_elapsed = max_elapsed
        self.stop_loss_mid = stop_loss_mid
        self.bet_fraction = bet_fraction
        self._faded_side = None     # type: str  # "up" or "down" — the side we bet against
        self._exited = False        # True after stop-loss sell — done for session
        self._streak_side = None    # type: str
        self._streak_count = 0
        self._params = {
            "overreaction_threshold": overreaction_threshold,
            "persist_ticks": persist_ticks,
            "max_elapsed": max_elapsed,
            "stop_loss_mid": stop_loss_mid,
            "bet_fraction": bet_fraction,
        }

    def on_session_start(self, metadata):
        self._faded_side = None
        self._exited = False
        self._streak_side = None
        self._streak_count = 0

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

        # Already entered — manage position or wait for fill
        if self._faded_side is not None:
            held = self._held_side(ctx)
            if held is not None:
                # Check stop-loss
                faded_mid = up_mid if self._faded_side == "up" else down_mid
                if faded_mid >= self.stop_loss_mid:
                    current_mid = up_mid if held == "up" else down_mid
                    self.log("stop-loss: faded {} mid={:.2f} >= {:.2f}".format(
                        self._faded_side, faded_mid, self.stop_loss_mid,
                    ))
                    sell_side = "sell_up" if held == "up" else "sell_down"
                    tokens = ctx.position.up_tokens if held == "up" else ctx.position.down_tokens
                    self._faded_side = None
                    self._exited = True
                    return TradeAction(side=sell_side, amount=tokens, price=current_mid)
            return HOLD  # waiting for fill or managing position

        # Already filled and exited (stop-loss) — done for this session
        if self._exited:
            return HOLD

        if ctx.elapsed_fraction > self.max_elapsed:
            return HOLD

        # Track persistence — consecutive ticks above threshold
        if up_mid >= self.overreaction_threshold:
            if self._streak_side == "up":
                self._streak_count += 1
            else:
                self._streak_side = "up"
                self._streak_count = 1
        elif down_mid >= self.overreaction_threshold:
            if self._streak_side == "down":
                self._streak_count += 1
            else:
                self._streak_side = "down"
                self._streak_count = 1
        else:
            self._streak_side = None
            self._streak_count = 0
            return HOLD

        if self._streak_count < self.persist_ticks:
            return HOLD

        # Persistent overreaction confirmed — fade it
        dollars = ctx.position.cash * self.bet_fraction
        if self._streak_side == "up":
            self._faded_side = "up"
            self.log("fading up mid={:.2f} ({} ticks), buying down mid={:.2f}".format(
                up_mid, self._streak_count, down_mid,
            ))
            return TradeAction(side="buy_down", amount=dollars, price=down_mid)
        elif self._streak_side == "down":
            self._faded_side = "down"
            self.log("fading down mid={:.2f} ({} ticks), buying up mid={:.2f}".format(
                down_mid, self._streak_count, up_mid,
            ))
            return TradeAction(side="buy_up", amount=dollars, price=up_mid)

        return HOLD

    def param_grid(self):
        # 4 * 3 * 3 * 3 * 3 = 324 combos
        return {
            "overreaction_threshold": [0.70, 0.75, 0.78, 0.80],
            "persist_ticks": [3, 5, 8],
            "max_elapsed": [0.2, 0.3, 0.4],
            "stop_loss_mid": [0.82, 0.85, 0.90],
            "bet_fraction": [0.25, 0.5, 1.0],
        }
