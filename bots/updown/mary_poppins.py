"""MaryPoppins: mode-switching strategy with built-in sub-strategies.

Does NOT use RegimeGatedStrategy. It learns a rolling session context from
recent completed sessions and picks a mode at each new session:

- QUIET: low-vol/low-range environment -> fade extremes (mean-reversion)
- VOL_PERSIST: high-vol persistence -> breakout follow
- BIAS_UP: high crossing activity -> up-biased entries
- BIAS_DOWN: low crossing intensity -> down-biased entries
- NEUTRAL: conservative/no-trade unless clear setup
"""

from collections import deque
from typing import Dict, List, Optional

from core.defaults import BPS_DIVISOR
from core.strategies.base import Param, Strategy
from core.trader import HOLD, TradeAction, TickContext


class MaryPoppins(Strategy):
    name = "MaryPoppins"
    description = "Mode-switching strategy with internal breakout/fade/bias behaviors."
    version = "2"
    requires_full_session = True

    # --- Mode / rolling window params ---
    window_sessions = Param(default=5, min=3, max=6, tunable=False)

    # thresholds inspired by signal scan outputs
    quiet_vol_max = Param(default=1.83e-05, min=1.2e-05, max=2.4e-05, tunable=False)
    quiet_range_max = Param(default=0.00375, min=0.0025, max=0.0050, tunable=False)

    storm_vol_min = Param(default=2.12e-05, min=1.8e-05, max=2.8e-05, tunable=False)
    storm_range_mean_min = Param(default=0.0030, min=0.0020, max=0.0045, tunable=False)

    crosses_up_bias = Param(default=16, min=12, max=24, tunable=False)
    crosses_down_bias_max = Param(default=15, min=10, max=20, tunable=False)

    # --- Entry controls (focused tunable set) ---
    min_elapsed = Param(default=0.25, min=0.15, max=0.45)
    no_new_after = Param(default=0.90, tunable=False)

    breakout_bps = Param(default=9.0, min=5.0, max=14.0)

    fade_trigger = Param(default=0.62, min=0.58, max=0.72)
    max_price_fade = Param(default=0.78, min=0.70, max=0.88)
    max_price_breakout = Param(default=0.78, min=0.65, max=0.90)
    max_price_bias = Param(default=0.85, min=0.75, max=0.92, tunable=False)

    conf_bias = Param(default=0.56, min=0.52, max=0.65)

    stop_loss_quiet = Param(default=0.03, min=0.02, max=0.06, tunable=False)
    stop_loss_storm = Param(default=0.07, min=0.04, max=0.12)
    stop_loss_bias = Param(default=0.05, min=0.03, max=0.10, tunable=False)

    def __init__(self, **kwargs):
        defs = self.param_defs()
        for name, p in defs.items():
            setattr(self, name, kwargs.get(name, p.default))
        self._params = {name: getattr(self, name) for name in defs}

        # cross-session memory (in worker lifetime)
        self._recent = deque(maxlen=12)  # each item: dict(session metrics)
        self._mode = "NEUTRAL"

        self._reset_session_state()

    def _reset_session_state(self):
        self._btc = []  # type: List[float]
        self._up_mid = []  # type: List[float]
        self._entry_count = 0
        self._entry_midpoint = 0.0
        self._last_trade_tick = -999

    def _finalize_prev_session(self):
        if len(self._btc) < 10 or len(self._up_mid) < 10:
            return
        start = self._btc[0]
        if start <= 0:
            return
        ret = self._btc[-1] / start - 1.0
        rng = (max(self._btc) - min(self._btc)) / start

        rets = []
        for i in range(1, len(self._btc)):
            if self._btc[i - 1] > 0:
                rets.append(self._btc[i] / self._btc[i - 1] - 1.0)
        vol = (sum(r * r for r in rets) / len(rets)) ** 0.5 if rets else 0.0

        prev = None
        crosses = 0
        for u in self._up_mid:
            cur = 1 if u > 0.5 else (-1 if u < 0.5 else 0)
            if cur == 0:
                continue
            if prev is None:
                prev = cur
                continue
            if cur != prev:
                crosses += 1
                prev = cur

        above = sum(1 for u in self._up_mid if u > 0.5) / len(self._up_mid)

        self._recent.append({
            "ret": ret,
            "range": rng,
            "vol": vol,
            "crosses": crosses,
            "above": above,
        })

    def _choose_mode(self):
        w = min(int(self.window_sessions), len(self._recent))
        if w <= 0:
            self._mode = "NEUTRAL"
            return

        rs = list(self._recent)[-w:]
        vol_vals = [r["vol"] for r in rs]
        range_vals = [r["range"] for r in rs]
        crosses_vals = [r["crosses"] for r in rs]

        vol_mean = sum(vol_vals) / w
        vol_max = max(vol_vals)
        range_mean = sum(range_vals) / w
        range_max = max(range_vals)
        crosses_mean = sum(crosses_vals) / w
        crosses_max = max(crosses_vals)

        if vol_max <= self.quiet_vol_max and range_max <= self.quiet_range_max:
            self._mode = "QUIET"
        elif min(vol_vals) >= self.storm_vol_min or range_mean >= self.storm_range_mean_min:
            self._mode = "VOL_PERSIST"
        elif crosses_mean >= self.crosses_up_bias:
            self._mode = "BIAS_UP"
        elif crosses_max <= self.crosses_down_bias_max and w >= 4:
            self._mode = "BIAS_DOWN"
        else:
            self._mode = "NEUTRAL"

    def on_session_start(self, metadata: Dict):
        self._finalize_prev_session()
        self._choose_mode()
        self._reset_session_state()
        self.log("mode={} recent_sessions={}".format(self._mode, len(self._recent)))

    def _mode_stop_loss(self):
        if self._mode == "QUIET":
            return self.stop_loss_quiet
        if self._mode == "VOL_PERSIST":
            return self.stop_loss_storm
        if self._mode in ("BIAS_UP", "BIAS_DOWN"):
            return self.stop_loss_bias
        return None

    def on_tick(self, ctx: TickContext) -> TradeAction:
        btc = ctx.sample.get("btc_price_usd")
        up_mid = ctx.sample.get("up_token_midpoint")
        down_mid = ctx.sample.get("down_token_midpoint")

        if btc is not None and btc > 0:
            self._btc.append(float(btc))
        if up_mid is not None:
            self._up_mid.append(float(up_mid))

        if btc is None or up_mid is None or down_mid is None:
            return HOLD

        # manage position
        if self._held_side(ctx) is not None:
            exit_action = self._check_exit(ctx, up_mid, down_mid, self._mode_stop_loss())
            if exit_action is not None:
                return exit_action
            return HOLD

        self._check_expired_entry(ctx)
        if self._entry_count > 0:
            return HOLD

        if self._check_timing_guards(ctx, self.min_elapsed, self.no_new_after):
            return HOLD

        side = None
        price = None

        # --- Mode-specific entry behaviors ---
        if self._mode == "QUIET":
            # fade extremes
            if up_mid >= self.fade_trigger and up_mid <= self.max_price_fade:
                side, price = "buy_down", up_mid
            elif down_mid >= self.fade_trigger and down_mid <= self.max_price_fade:
                side, price = "buy_up", down_mid

        elif self._mode == "VOL_PERSIST":
            # breakout with BTC displacement
            if ctx.initial_btc_price > 0:
                disp_bps = (btc / ctx.initial_btc_price - 1.0) * BPS_DIVISOR
                if disp_bps >= self.breakout_bps and up_mid <= self.max_price_breakout:
                    side, price = "buy_up", up_mid
                elif disp_bps <= -self.breakout_bps and down_mid <= self.max_price_breakout:
                    side, price = "buy_down", down_mid

        elif self._mode == "BIAS_UP":
            if up_mid >= self.conf_bias and up_mid <= self.max_price_bias:
                side, price = "buy_up", up_mid

        elif self._mode == "BIAS_DOWN":
            if down_mid >= self.conf_bias and down_mid <= self.max_price_bias:
                side, price = "buy_down", down_mid

        else:  # NEUTRAL
            # conservative: only very clear confidence
            if up_mid >= 0.70 and up_mid <= self.max_price_bias:
                side, price = "buy_up", up_mid
            elif down_mid >= 0.70 and down_mid <= self.max_price_bias:
                side, price = "buy_down", down_mid

        if side is None:
            return HOLD

        self._entry_count += 1
        self._entry_midpoint = price
        self._last_trade_tick = ctx.tick_index

        self.log("ENTRY {} mode={} mid={:.3f} at {:.0f}%".format(
            side, self._mode, price, ctx.elapsed_fraction * 100,
        ))

        return TradeAction(
            side=side,
            amount=ctx.position.cash,
            price=price,
            cancel_after_s=10,
        )
